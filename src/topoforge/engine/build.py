"""Atomic end-to-end local raster terrain builds."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import trimesh
import yaml
from affine import Affine
from PIL import Image
from rasterio.crs import CRS as RasterioCRS

from topoforge import __version__
from topoforge.config import dump_resolved_config
from topoforge.engine.preflight import (
    ManufacturingPreflightReport,
    evaluate_manufacturing_preflight,
)
from topoforge.exceptions import ConfigurationError, MeshValidationError
from topoforge.exporters import export_glb, export_stl
from topoforge.exporters.three_mf import ThreeMFInspection, export_3mf, inspect_3mf
from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.models import BuildConfig, RasterResult, ScalingResult
from topoforge.provenance import write_json, write_validation_html
from topoforge.raster import process_local_raster
from topoforge.rendering import render_elevation_preview
from topoforge.scaling import apply_vertical_scale, resolve_scaling
from topoforge.util import sha256_file
from topoforge.validation import evaluate_bambu_p2s_release_gate, validate_mesh

_GEOMETRY_SERIALIZATION_TOLERANCE_MM = 5e-5
_ARTIFACT_BINDING_SCHEMA = "topoforge-artifact-bindings-v1"
_MAX_CONTROL_FILE_BYTES = 16 * 1024 * 1024
_MAX_PREVIEW_FILE_BYTES = 128 * 1024 * 1024
_MAX_VERIFICATION_GRID_CELLS = 1_500_000
_MAX_MODEL_FILE_BYTES = 2_147_483_648
_GLB_MAGIC = 0x46546C67
_GLB_JSON_CHUNK = 0x4E4F534A
_GLB_BIN_CHUNK = 0x004E4942

_MUTABLE_ARTIFACT_ROLES = frozenset(
    {
        "provenance",
        "validation_json",
        "validation_html",
        "slicer_validation",
        "bambu_studio_validation",
    }
)


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Completed build paths and measured reports."""

    output_dir: Path
    artifacts: dict[str, Path]
    validation: dict[str, Any]
    provenance: dict[str, Any]


def _require_empty_destination(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ConfigurationError(f"Output path exists and is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ConfigurationError(
                f"Output directory is not empty: {output_dir}. "
                "Choose a new directory to preserve prior evidence."
            )
        output_dir.rmdir()
    output_dir.parent.mkdir(parents=True, exist_ok=True)


def _load_stl(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="stl", force="mesh", process=True)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"Reopened STL is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _load_glb(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="glb", force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"Reopened GLB is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _load_3mf_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="3mf", force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"Reopened 3MF is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _bounded_text(path: Path, *, label: str, max_bytes: int = _MAX_CONTROL_FILE_BYTES) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        raise MeshValidationError(
            f"{label} is {size} bytes, above the safe {max_bytes}-byte limit; "
            "restore or rebuild the artifact bundle"
        )
    return path.read_text(encoding="utf-8")


def _require_self_contained_gtiff(dataset: Any, path: Path, *, label: str) -> None:
    if dataset.driver != "GTiff":
        raise MeshValidationError(f"{label} must be a self-contained GeoTIFF")
    expected = path.resolve()
    try:
        referenced = {Path(item).resolve(strict=True) for item in dataset.files}
    except (OSError, RuntimeError) as exc:
        raise MeshValidationError(
            f"{label} has an unreadable external raster dependency; rebuild the bundle"
        ) from exc
    if referenced != {expected}:
        raise MeshValidationError(
            f"{label} references files outside its canonical bundle artifact; rebuild the bundle"
        )


def _model_file_limit(*, vertex_count: int, triangle_count: int) -> int:
    estimated = 16 * 1024 * 1024 + vertex_count * 192 + triangle_count * 96
    return min(_MAX_MODEL_FILE_BYTES, estimated)


def _three_mf_uncompressed_limit(*, vertex_count: int, triangle_count: int) -> int:
    estimated = 16 * 1024 * 1024 + vertex_count * 160 + triangle_count * 112
    return min(1_610_612_736, estimated)


def _verify_self_contained_glb(
    path: Path,
    *,
    vertex_count: int,
    triangle_count: int,
) -> None:
    file_size = path.stat().st_size
    max_bytes = _model_file_limit(
        vertex_count=vertex_count,
        triangle_count=triangle_count,
    )
    if file_size > max_bytes:
        raise MeshValidationError(
            f"preview.glb is {file_size} bytes, above its bounded {max_bytes}-byte limit"
        )
    json_payload: bytes | None = None
    binary_length: int | None = None
    with path.open("rb") as source:
        header = source.read(12)
        if len(header) != 12:
            raise MeshValidationError("preview.glb has a truncated binary header")
        magic, version, declared_length = struct.unpack("<III", header)
        if magic != _GLB_MAGIC or version != 2 or declared_length != file_size:
            raise MeshValidationError("preview.glb has a non-canonical magic/version/length header")
        chunk_types: list[int] = []
        while source.tell() < declared_length:
            chunk_header = source.read(8)
            if len(chunk_header) != 8:
                raise MeshValidationError("preview.glb has a truncated chunk header")
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            if chunk_length > max_bytes or source.tell() + chunk_length > declared_length:
                raise MeshValidationError("preview.glb declares an unsafe chunk length")
            chunk_types.append(chunk_type)
            if chunk_type == _GLB_JSON_CHUNK:
                if json_payload is not None or chunk_length > _MAX_CONTROL_FILE_BYTES:
                    raise MeshValidationError("preview.glb must contain one bounded JSON chunk")
                json_payload = source.read(chunk_length)
            elif chunk_type == _GLB_BIN_CHUNK:
                if binary_length is not None:
                    raise MeshValidationError("preview.glb contains duplicate BIN chunks")
                binary_length = chunk_length
                source.seek(chunk_length, 1)
            else:
                raise MeshValidationError("preview.glb contains an unsupported chunk type")
        if source.tell() != declared_length:
            raise MeshValidationError("preview.glb chunk lengths do not match the file length")
    if chunk_types != [_GLB_JSON_CHUNK, _GLB_BIN_CHUNK]:
        raise MeshValidationError(
            "preview.glb must contain exactly one embedded JSON chunk and one BIN chunk"
        )
    try:
        header_payload = json.loads((json_payload or b"").rstrip(b" \t\r\n\x00"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MeshValidationError("preview.glb contains invalid JSON") from exc
    if not isinstance(header_payload, dict):
        raise MeshValidationError("preview.glb JSON root must be an object")
    buffers = header_payload.get("buffers")
    if not isinstance(buffers, list) or len(buffers) != 1 or not isinstance(buffers[0], dict):
        raise MeshValidationError("preview.glb must declare one embedded buffer")
    if "uri" in buffers[0]:
        raise MeshValidationError("preview.glb must not reference an external buffer URI")
    declared_buffer_length = buffers[0].get("byteLength")
    if (
        isinstance(declared_buffer_length, bool)
        or not isinstance(declared_buffer_length, int)
        or declared_buffer_length < 0
        or binary_length is None
        or declared_buffer_length > binary_length
        or binary_length - declared_buffer_length > 3
    ):
        raise MeshValidationError("preview.glb embedded buffer length is invalid")
    images = header_payload.get("images", [])
    if not isinstance(images, list) or any(
        not isinstance(image, dict) or "uri" in image for image in images
    ):
        raise MeshValidationError("preview.glb must not reference external image URIs")
    buffer_views = header_payload.get("bufferViews", [])
    if not isinstance(buffer_views, list):
        raise MeshValidationError("preview.glb bufferViews must be a list")
    for view in buffer_views:
        if not isinstance(view, dict):
            raise MeshValidationError("preview.glb bufferView must be an object")
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if (
            view.get("buffer") != 0
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 0
            or offset + length > declared_buffer_length
        ):
            raise MeshValidationError("preview.glb has an out-of-range bufferView")
    max_accessor_count = max(vertex_count, triangle_count * 3)
    accessors = header_payload.get("accessors", [])
    if not isinstance(accessors, list):
        raise MeshValidationError("preview.glb accessors must be a list")
    for accessor in accessors:
        if not isinstance(accessor, dict):
            raise MeshValidationError("preview.glb accessor must be an object")
        count = accessor.get("count")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count > max_accessor_count
            or "bufferView" not in accessor
            or "sparse" in accessor
        ):
            raise MeshValidationError("preview.glb accessor exceeds the bounded terrain topology")


def _semantic_array_digest(
    values: np.ndarray[Any, Any],
    *,
    kind: str,
    crs: RasterioCRS,
    transform: Affine,
) -> str:
    normalized_crs = RasterioCRS.from_user_input(crs)
    authority = normalized_crs.to_authority()
    crs_identity: dict[str, Any] = (
        {"authority": list(authority)}
        if authority is not None
        else {"proj": normalized_crs.to_dict()}
    )
    digest = hashlib.sha256()
    digest.update(b"topoforge-raster-semantic-v1\x00")
    digest.update(kind.encode("ascii"))
    digest.update(b"\x00")
    digest.update(
        json.dumps(
            {
                "shape": list(values.shape),
                "crs": crs_identity,
                "transform": [float(value) for value in tuple(transform)[:6]],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if kind == "processed-dem":
        normalized = np.asarray(values, dtype="<f4", order="C")
    else:
        normalized = np.asarray(values, dtype=np.uint8, order="C")
    digest.update(normalized.tobytes(order="C"))
    return digest.hexdigest()


def _canonical_mesh_arrays(
    mesh: trimesh.Trimesh,
    *,
    serialize_float32: bool,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    vertex_dtype = "<f4" if serialize_float32 else "<f8"
    vertices = np.asarray(mesh.vertices, dtype=vertex_dtype).copy()
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if not np.all(np.isfinite(vertices)):
        raise MeshValidationError("Canonical geometry requires finite vertices")
    vertices[vertices == 0.0] = 0.0
    canonical_vertices, inverse = np.unique(vertices, axis=0, return_inverse=True)
    if len(canonical_vertices) != len(vertices):
        raise MeshValidationError("Terrain contains duplicate vertices")
    remapped = inverse[faces]
    rotations = (
        remapped,
        remapped[:, [1, 2, 0]],
        remapped[:, [2, 0, 1]],
    )
    canonical_faces = rotations[0].copy()
    for candidate in rotations[1:]:
        lower = (candidate[:, 0] < canonical_faces[:, 0]) | (
            (candidate[:, 0] == canonical_faces[:, 0])
            & (
                (candidate[:, 1] < canonical_faces[:, 1])
                | (
                    (candidate[:, 1] == canonical_faces[:, 1])
                    & (candidate[:, 2] < canonical_faces[:, 2])
                )
            )
        )
        canonical_faces[lower] = candidate[lower]
    face_order = np.lexsort((canonical_faces[:, 2], canonical_faces[:, 1], canonical_faces[:, 0]))
    canonical_faces = canonical_faces[face_order]
    return canonical_vertices, canonical_faces


def _canonical_mesh_digest(mesh: trimesh.Trimesh) -> str:
    canonical_vertices, canonical_faces = _canonical_mesh_arrays(
        mesh,
        serialize_float32=True,
    )
    digest = hashlib.sha256()
    digest.update(b"topoforge-float32-triangle-geometry-v1\x00")
    digest.update(np.asarray(canonical_vertices.shape, dtype="<u8").tobytes())
    digest.update(np.asarray(canonical_vertices, dtype="<f4").tobytes(order="C"))
    digest.update(np.asarray(canonical_faces.shape, dtype="<u8").tobytes())
    digest.update(np.asarray(canonical_faces, dtype="<u8").tobytes(order="C"))
    return digest.hexdigest()


def _meshes_are_serialization_equivalent(
    expected: trimesh.Trimesh,
    actual: trimesh.Trimesh,
) -> bool:
    expected_vertices, expected_faces = _canonical_mesh_arrays(
        expected,
        serialize_float32=False,
    )
    actual_vertices, actual_faces = _canonical_mesh_arrays(
        actual,
        serialize_float32=False,
    )
    return (
        expected_vertices.shape == actual_vertices.shape
        and expected_faces.shape == actual_faces.shape
        and np.array_equal(expected_faces, actual_faces)
        and np.allclose(
            expected_vertices,
            actual_vertices,
            atol=_GEOMETRY_SERIALIZATION_TOLERANCE_MM,
            rtol=0.0,
        )
    )


def _immutable_artifact_role_hashes(artifacts: dict[str, Path]) -> dict[str, str]:
    return {
        role: sha256_file(path)
        for role, path in sorted(artifacts.items())
        if role != "manifest" and role not in _MUTABLE_ARTIFACT_ROLES and path.is_file()
    }


def _peak_coordinate_mm(
    mesh: trimesh.Trimesh, *, expected_xy_mm: tuple[float, float]
) -> tuple[float, float, float]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    maximum_z = float(np.max(vertices[:, 2]))
    candidate_indices = np.flatnonzero(np.isclose(vertices[:, 2], maximum_z, atol=1e-6, rtol=0.0))
    candidates = vertices[candidate_indices]
    distances = np.square(candidates[:, 0] - expected_xy_mm[0]) + np.square(
        candidates[:, 1] - expected_xy_mm[1]
    )
    peak = candidates[int(np.argmin(distances))]
    return float(peak[0]), float(peak[1]), float(peak[2])


def _closest_peak_coordinate_mm(
    candidates: tuple[tuple[float, float, float], ...],
    *,
    expected_xy_mm: tuple[float, float],
) -> tuple[float, float, float]:
    return min(
        candidates,
        key=lambda value: (value[0] - expected_xy_mm[0]) ** 2 + (value[1] - expected_xy_mm[1]) ** 2,
    )


def _expected_model_peak_mm(
    processed_elevations_m: np.ndarray,
    processed_peak_coordinate: dict[str, object],
    top_z_mm: np.ndarray,
    *,
    width_mm: float,
    depth_mm: float,
) -> tuple[float, float, float]:
    rows, columns = processed_elevations_m.shape
    row_value = processed_peak_coordinate.get("row")
    column_value = processed_peak_coordinate.get("column")
    if not isinstance(row_value, int) or not isinstance(column_value, int):
        raise MeshValidationError("Processed peak raster indices are not integers")
    row = row_value
    column = column_value
    x_mm = width_mm * column / max(columns - 1, 1)
    y_mm = depth_mm * (rows - 1 - row) / max(rows - 1, 1)
    return x_mm, y_mm, float(top_z_mm[row, column])


def _required_validation_passed(report: dict[str, Any]) -> bool:
    true_checks = (
        "finite_vertices",
        "finite_face_normals",
        "watertight",
        "winding_consistent",
        "manifold",
        "positive_volume",
        "flat_bottom",
        "dimensions_within_tolerance",
        "height_limit_passed",
        "minimum_base_thickness_passed",
        "triangle_budget_passed",
        "terrain_fidelity_passed",
        "orientation_consistent",
    )
    if not all(report.get(key) is True for key in true_checks):
        return False
    return (
        report.get("connected_components") == 1
        and report.get("degenerate_faces") == 0
        and report.get("duplicate_faces") == 0
        and float(report.get("bottom_planarity_error_mm") or 0.0) <= 0.01
    )


def _artifact_map(stage: Path, formats: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {
        "processed_dem": stage / "processed_dem.tif",
        "original_nodata_mask": stage / "original_nodata_mask.tif",
        "manufacturing_preflight": stage / "manufacturing_preflight.json",
        "provenance": stage / "provenance.json",
        "validation_json": stage / "validation.json",
        "validation_html": stage / "validation.html",
        "resolved_config": stage / "build_config.resolved.yaml",
        "preview_png": stage / "preview.png",
        "manifest": stage / "build_manifest.json",
    }
    if "stl" in formats:
        artifacts["model_stl"] = stage / "model.stl"
    if "3mf" in formats:
        artifacts["model_3mf"] = stage / "model.3mf"
    if "glb" in formats:
        artifacts["preview_glb"] = stage / "preview.glb"
    return artifacts


def build_local_terrain(config: BuildConfig) -> BuildResult:
    """Run one local GeoTIFF build atomically and independently reopen every artifact."""
    source = config.dem_path.expanduser().resolve()
    output = config.output_dir.expanduser().resolve()
    _require_empty_destination(output)
    stage = output.parent / f".{output.name}.topoforge-stage-{uuid.uuid4().hex}"
    stage.mkdir(parents=False)
    stage_config = config.model_copy(update={"dem_path": source, "output_dir": stage})
    resolved_config = config.model_copy(update={"dem_path": source, "output_dir": output})
    artifacts = _artifact_map(stage, config.output_formats)
    source_acquisition: dict[str, Any] | None = None
    source_quality_inputs: dict[str, tuple[Path, str]] = {}
    if resolved_config.source_acquisition_manifest is not None:
        source_acquisition_path = resolved_config.source_acquisition_manifest.expanduser().resolve()
        if not source_acquisition_path.is_file():
            raise ConfigurationError(
                f"Source acquisition manifest does not exist: {source_acquisition_path}"
            )
        try:
            loaded_acquisition = json.loads(source_acquisition_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Source acquisition manifest is not valid JSON: {source_acquisition_path}"
            ) from exc
        if not isinstance(loaded_acquisition, dict):
            raise ConfigurationError("Source acquisition manifest root must be a JSON object")
        source_acquisition = loaded_acquisition
        artifacts["source_acquisition"] = stage / "source_acquisition.json"
        quality_masks = source_acquisition.get("quality_masks", [])
        if not isinstance(quality_masks, list):
            raise ConfigurationError("Source acquisition quality_masks must be a list")
        for record in quality_masks:
            if not isinstance(record, dict) or record.get("availability") != "present":
                continue
            role = record.get("role")
            output_record = record.get("output")
            if role not in {"edm", "flm", "hem", "wbm"} or not isinstance(output_record, dict):
                raise ConfigurationError("Present source quality mask has invalid metadata")
            raw_path = output_record.get("path")
            expected_sha256 = output_record.get("sha256")
            if not isinstance(raw_path, str) or not isinstance(expected_sha256, str):
                raise ConfigurationError(
                    f"Present source quality mask {role} is missing path/SHA-256"
                )
            source_quality_path = Path(raw_path).expanduser().resolve()
            if not source_quality_path.is_file():
                raise ConfigurationError(
                    f"Source quality mask does not exist: {source_quality_path}"
                )
            if sha256_file(source_quality_path) != expected_sha256:
                raise ConfigurationError(
                    f"Source quality mask checksum changed before build: {source_quality_path}"
                )
            artifact_role = f"source_quality_{role}"
            artifact_name = f"source_quality_{role}.tif"
            artifacts[artifact_role] = stage / artifact_name
            source_quality_inputs[artifact_role] = (source_quality_path, expected_sha256)
            output_record["bundled_artifact"] = artifact_name
    try:
        for artifact_role, (source_quality_path, expected_sha256) in source_quality_inputs.items():
            shutil.copyfile(source_quality_path, artifacts[artifact_role])
            if sha256_file(artifacts[artifact_role]) != expected_sha256:
                raise ConfigurationError(
                    f"Bundled source quality mask checksum changed: {artifact_role}"
                )
            with rasterio.open(artifacts[artifact_role]) as quality_dataset:
                if quality_dataset.count != 1 or quality_dataset.crs is None:
                    raise ConfigurationError(
                        f"Bundled source quality mask failed raster reopen: {artifact_role}"
                    )
        if source_acquisition is not None:
            write_json(artifacts["source_acquisition"], source_acquisition)
        processed = process_local_raster(stage_config)
        scaling = resolve_scaling(processed.elevations_m, processed.report, resolved_config)
        manufacturing_preflight = evaluate_manufacturing_preflight(
            processed.report, scaling, resolved_config
        )
        write_json(
            artifacts["manufacturing_preflight"],
            manufacturing_preflight.model_dump(mode="json"),
        )
        top_z_mm = apply_vertical_scale(processed.elevations_m, scaling)
        expected_peak_mm = _expected_model_peak_mm(
            processed.elevations_m,
            processed.report.processed_peak_coordinate,
            top_z_mm,
            width_mm=scaling.model_width_mm,
            depth_mm=scaling.model_depth_mm,
        )
        # Raster row 0 is north.  Flip rows so mesh row 0 is south/y=0 and +Y is North.
        oriented_top_z_mm = np.flipud(top_z_mm)
        mesh = build_rectangular_terrain_mesh(
            oriented_top_z_mm,
            width_mm=scaling.model_width_mm,
            depth_mm=scaling.model_depth_mm,
            base_thickness_mm=scaling.base_thickness_mm,
        )
        if "stl" in config.output_formats:
            export_stl(mesh, artifacts["model_stl"])
        if "glb" in config.output_formats:
            export_glb(mesh, artifacts["preview_glb"])
        three_mf: ThreeMFInspection | None = None
        if "3mf" in config.output_formats:
            export_3mf(
                mesh,
                artifacts["model_3mf"],
                metadata={
                    "dataset_name": processed.report.metadata.dataset_name,
                    "dataset_type": processed.report.metadata.dataset_type.value,
                    "horizontal_crs": processed.report.metadata.horizontal_crs,
                    "vertical_datum": processed.report.metadata.vertical_datum,
                    "vertical_exaggeration": format(scaling.vertical_exaggeration, ".9g"),
                    "east_axis": "+X = East",
                    "north_axis": "+Y = North",
                    "north_edge": "y=model_depth_mm",
                    "source_bounds": json.dumps(
                        processed.report.source_bounds,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "orientation_transform": (
                        "mesh_heightfield=flipud(processed_dem); "
                        "source row 0 north maps to y=model_depth_mm"
                    ),
                },
            )
            three_mf = inspect_3mf(artifacts["model_3mf"])

        validation_mesh = (
            _load_stl(artifacts["model_stl"]) if "stl" in config.output_formats else mesh
        )
        mesh_extents = np.asarray(mesh.extents, dtype=np.float64)
        expected_dimensions = (
            float(mesh_extents[0]),
            float(mesh_extents[1]),
            float(mesh_extents[2]),
        )
        geometry_report = validate_mesh(
            validation_mesh,
            expected_dimensions_mm=expected_dimensions,
            dimension_tolerance_mm=0.05,
            flat_bottom_tolerance_mm=0.01,
        )
        validation = geometry_report.model_dump(mode="json")
        validation["validation_source"] = (
            "reopened model.stl" if "stl" in config.output_formats else "in-memory mesh"
        )
        validation["mesh_units"] = "millimetres by TopoForge coordinate contract"
        validation["estimated_triangle_count"] = processed.report.estimated_triangle_count
        validation["triangle_budget"] = config.max_estimated_triangles or (
            config.max_grid_cells * 4 - 4
        )
        validation["triangle_budget_passed"] = int(validation["triangle_count"]) <= int(
            validation["triangle_budget"]
        )
        validation["manufacturing_preflight"] = manufacturing_preflight.model_dump(mode="json")
        validation["height_limit_mm"] = scaling.height_limit_mm
        validation["height_limit_passed"] = (
            float(validation["dimensions_mm"][2]) <= scaling.height_limit_mm + 0.05
        )
        validation["minimum_base_thickness_required_mm"] = (
            config.printer_profile.minimum_base_thickness_mm
        )
        measured_base = validation.get("minimum_base_thickness_mm")
        validation["minimum_base_thickness_passed"] = (
            measured_base is not None
            and float(measured_base) >= config.printer_profile.minimum_base_thickness_mm - 0.01
        )
        if three_mf is not None:
            validation["three_mf_round_trip"] = {
                "unit": three_mf.unit,
                "object_count": three_mf.object_count,
                "build_item_count": three_mf.build_item_count,
                "vertex_count": three_mf.vertex_count,
                "triangle_count": three_mf.triangle_count,
                "dimensions_mm": three_mf.dimensions_mm,
                "object_names": three_mf.object_names,
                "strict_warning_count": three_mf.strict_warning_count,
                "lib3mf_version": three_mf.lib3mf_version,
                "peak_coordinate_mm": three_mf.peak_coordinate_mm,
                "peak_coordinates_mm": three_mf.peak_coordinates_mm,
                "metadata": three_mf.metadata,
            }
            validation["three_mf_dimensions_match"] = bool(
                np.allclose(three_mf.dimensions_mm, expected_dimensions, atol=0.05, rtol=0.0)
            )

        expected_xy_mm = (expected_peak_mm[0], expected_peak_mm[1])
        format_peaks: dict[str, tuple[float, float, float]] = {
            "in_memory": _peak_coordinate_mm(mesh, expected_xy_mm=expected_xy_mm)
        }
        if "stl" in config.output_formats:
            format_peaks["stl"] = _peak_coordinate_mm(
                validation_mesh, expected_xy_mm=expected_xy_mm
            )
        if "glb" in config.output_formats:
            format_peaks["glb"] = _peak_coordinate_mm(
                _load_glb(artifacts["preview_glb"]), expected_xy_mm=expected_xy_mm
            )
        if three_mf is not None:
            format_peaks["3mf"] = _closest_peak_coordinate_mm(
                three_mf.peak_coordinates_mm,
                expected_xy_mm=expected_xy_mm,
            )
        orientation_consistent = all(
            np.allclose(value, expected_peak_mm, atol=0.01, rtol=0.0)
            for value in format_peaks.values()
        )
        reference_peak_difference_m = (
            None
            if config.reference_peak_elevation_m is None
            else config.reference_peak_elevation_m - processed.report.raw_elevation_max_m
        )
        validation.update(
            {
                "reference_peak_elevation_m": config.reference_peak_elevation_m,
                "reference_peak_difference_m": reference_peak_difference_m,
                "reference_peak_adjustment_applied": False,
                "source_horizontal_resolution_m": processed.report.source_horizontal_resolution_m,
                "processed_horizontal_resolution_m": (
                    processed.report.processed_horizontal_resolution_m
                ),
                "source_grid_shape": processed.report.source_grid_shape,
                "processed_grid_shape": processed.report.processed_grid_shape,
                "downsampling_factor": processed.report.downsampling_factor,
                "physical_sample_spacing_mm": processed.report.physical_sample_spacing_mm,
                "estimated_memory_mb": processed.report.estimated_memory_mb,
                "raw_elevation_min_m": processed.report.raw_elevation_min_m,
                "raw_elevation_max_m": processed.report.raw_elevation_max_m,
                "processed_elevation_min_m": processed.report.processed_elevation_min_m,
                "processed_elevation_max_m": processed.report.processed_elevation_max_m,
                "peak_elevation_loss_m": processed.report.peak_elevation_loss_m,
                "raw_peak_coordinate": processed.report.raw_peak_coordinate,
                "processed_peak_coordinate": processed.report.processed_peak_coordinate,
                "peak_horizontal_shift_m": processed.report.peak_horizontal_shift_m,
                "sampling_decision_reasons": processed.report.sampling_decision_reasons,
                "sampling_warnings": processed.report.sampling_warnings,
                "terrain_fidelity_status": processed.report.terrain_fidelity_status,
                "terrain_fidelity_passed": processed.report.terrain_fidelity_passed,
                "peak_elevation_loss_threshold_m": (
                    processed.report.peak_elevation_loss_threshold_m
                ),
                "peak_horizontal_shift_threshold_m": (
                    processed.report.peak_horizontal_shift_threshold_m
                ),
                "orientation_consistent": orientation_consistent,
                "orientation": {
                    "east_axis": "+X = East",
                    "north_axis": "+Y = North",
                    "up_axis": "+Z = Up",
                    "north_edge": "y=model_depth_mm",
                    "orientation_transform": (
                        "mesh_heightfield=flipud(processed_dem); "
                        "source row 0 north maps to y=model_depth_mm"
                    ),
                    "expected_processed_peak_mm": expected_peak_mm,
                    "reopened_format_peak_coordinates_mm": format_peaks,
                    "coordinate_correction_only": True,
                },
            }
        )
        validation["required_checks_passed"] = _required_validation_passed(validation)
        if not validation["required_checks_passed"]:
            raise MeshValidationError(
                "Generated STL failed a required geometry invariant; "
                "inspect the retained command log"
            )
        if three_mf is not None and not validation.get("three_mf_dimensions_match"):
            raise MeshValidationError("Reopened 3MF dimensions differ from the manufacturing mesh")

        render_elevation_preview(
            processed.elevations_m,
            artifacts["preview_png"],
            title=processed.report.metadata.dataset_name,
        )
        dump_resolved_config(resolved_config, artifacts["resolved_config"])
        artifact_bindings = {
            "schema": _ARTIFACT_BINDING_SCHEMA,
            "role_sha256": _immutable_artifact_role_hashes(artifacts),
            "processed_dem_semantic_sha256": _semantic_array_digest(
                processed.elevations_m,
                kind="processed-dem",
                crs=processed.crs,
                transform=processed.transform,
            ),
            "original_nodata_mask_semantic_sha256": _semantic_array_digest(
                processed.original_nodata_mask,
                kind="original-nodata-mask",
                crs=processed.crs,
                transform=processed.transform,
            ),
            "terrain_geometry_semantic_sha256": _canonical_mesh_digest(mesh),
        }
        validation["artifact_bindings"] = artifact_bindings
        generated_at = datetime.now(UTC).isoformat()
        recorded_provider_selection = (
            (source_acquisition or {}).get("provider_selection")
            if source_acquisition is not None
            else None
        )
        if not isinstance(recorded_provider_selection, dict):
            recorded_provider_selection = {
                "selected": processed.report.metadata.provider,
                "reason": (
                    [
                        "user supplied a local elevation raster",
                        "local inputs have priority over network providers",
                        "no provider fallback was required",
                    ]
                    if processed.report.metadata.provider == "local"
                    else list(
                        (source_acquisition or {})
                        .get("plan", {})
                        .get(
                            "decisions",
                            ["provider acquisition manifest selected this cached raster"],
                        )
                    )
                ),
                "attempted_providers": [
                    {
                        "provider": processed.report.metadata.provider,
                        "status": "selected",
                        "dataset": processed.report.metadata.dataset_name,
                    }
                ],
                "source_acquisition_artifact": (
                    "source_acquisition.json" if source_acquisition is not None else None
                ),
            }
        provenance: dict[str, Any] = {
            "topoforge_version": __version__,
            "generated_at": generated_at,
            "provider_selection": recorded_provider_selection,
            "source_acquisition": source_acquisition,
            "dataset": processed.report.metadata.model_dump(mode="json"),
            "source_file_checksums": processed.report.metadata.checksums,
            "manufacturing_preflight": manufacturing_preflight.model_dump(mode="json"),
            "artifact_bindings": artifact_bindings,
            "elevation_reference_comparison": {
                "raw_dem_peak_elevation_m": processed.report.raw_elevation_max_m,
                "published_reference_peak_elevation_m": config.reference_peak_elevation_m,
                "reference_minus_raw_dem_m": reference_peak_difference_m,
                "note": config.reference_peak_elevation_note,
                "dataset_semantics": processed.report.metadata.dataset_type.value,
                "vertical_datum": processed.report.metadata.vertical_datum,
                "terrain_adjustment_applied": False,
                "interpretation": (
                    "The published nominal elevation is contextual provenance only; "
                    "the model retains measured source raster elevations without "
                    "an artificial peak."
                ),
            },
            "processing": {
                "pipeline": [
                    "read metadata",
                    "normalize to a north-up metric CRS when required",
                    "resolve print-aware/source-preserving/custom sampling",
                    (
                        "enforce deterministic cell, triangle, and memory budgets "
                        "using average resampling"
                    ),
                    "preserve original NoData mask",
                    "interpolate bounded interior holes from nearest valid cells",
                    "write processed DEM",
                ],
                "processed_crs": processed.report.crs,
                "automatic_projection_choice": processed.report.crs,
                "array_shape": processed.report.array_shape,
                "source_grid_shape": processed.report.source_grid_shape,
                "processed_grid_shape": processed.report.processed_grid_shape,
                "source_horizontal_resolution_m": (processed.report.source_horizontal_resolution_m),
                "processed_horizontal_resolution_m": (
                    processed.report.processed_horizontal_resolution_m
                ),
                "downsampling_factor": processed.report.downsampling_factor,
                "physical_sample_spacing_mm": processed.report.physical_sample_spacing_mm,
                "physical_sample_spacing_xy_mm": (processed.report.physical_sample_spacing_xy_mm),
                "estimated_triangle_count": processed.report.estimated_triangle_count,
                "estimated_memory_mb": processed.report.estimated_memory_mb,
                "sampling_mode": config.sampling_mode.value,
                "mesh_sampling_mm": config.mesh_sampling_mm,
                "max_grid_cells": config.max_grid_cells,
                "max_estimated_triangles": config.max_estimated_triangles,
                "max_estimated_memory_mb": config.max_estimated_memory_mb,
                "resource_budget_mode": config.resource_budget_mode.value,
                "sampling_decision_reasons": processed.report.sampling_decision_reasons,
                "sampling_warnings": processed.report.sampling_warnings,
                "horizontal_resolution_m": (processed.report.processed_horizontal_resolution_m),
                "nodata_percentage": processed.report.original_nodata_fraction * 100.0,
                "interpolated_percentage": processed.report.interpolated_fraction * 100.0,
                "original_nodata_mask": "original_nodata_mask.tif",
                "elevation_min_m": processed.report.elevation_min_m,
                "elevation_max_m": processed.report.elevation_max_m,
                "raw_elevation_min_m": processed.report.raw_elevation_min_m,
                "raw_elevation_max_m": processed.report.raw_elevation_max_m,
                "processed_elevation_min_m": processed.report.processed_elevation_min_m,
                "processed_elevation_max_m": processed.report.processed_elevation_max_m,
                "peak_elevation_loss_m": processed.report.peak_elevation_loss_m,
                "raw_peak_coordinate": processed.report.raw_peak_coordinate,
                "processed_peak_coordinate": processed.report.processed_peak_coordinate,
                "peak_horizontal_shift_m": processed.report.peak_horizontal_shift_m,
                "peak_elevation_loss_threshold_m": (
                    processed.report.peak_elevation_loss_threshold_m
                ),
                "peak_horizontal_shift_threshold_m": (
                    processed.report.peak_horizontal_shift_threshold_m
                ),
                "terrain_fidelity_status": processed.report.terrain_fidelity_status,
                "terrain_fidelity_passed": processed.report.terrain_fidelity_passed,
                "source_bounds": processed.report.source_bounds,
                "aoi": processed.report.aoi,
            },
            "orientation": {
                "east_axis": "+X = East",
                "north_axis": "+Y = North",
                "up_axis": "+Z = Up",
                "north_edge": "y=model_depth_mm",
                "source_bounds": processed.report.source_bounds,
                "orientation_transform": (
                    "mesh_heightfield=flipud(processed_dem); "
                    "source row 0 north maps to y=model_depth_mm"
                ),
                "coordinate_correction_only": True,
            },
            "scaling": scaling.model_dump(mode="json"),
            "geometry": {
                "construction": "explicit regular-grid top, four boundary walls, and flat bottom",
                "manufacturing_units": "millimetres",
                "self_intersection_status": validation["self_intersection_status"],
            },
            "formats": {
                "stl": {"units_contract": "millimetres"},
                "3mf": (
                    {
                        "writer": "lib3mf",
                        "lib3mf_version": three_mf.lib3mf_version,
                        "strict_warning_count": three_mf.strict_warning_count,
                    }
                    if three_mf is not None
                    else None
                ),
                "glb": {"role": "preview only"},
            },
            "license_note": (
                "Dataset terms are separate from the Apache-2.0 TopoForge code license."
            ),
        }
        provenance.update(
            {
                "source_horizontal_resolution_m": (processed.report.source_horizontal_resolution_m),
                "processed_horizontal_resolution_m": (
                    processed.report.processed_horizontal_resolution_m
                ),
                "source_grid_shape": processed.report.source_grid_shape,
                "processed_grid_shape": processed.report.processed_grid_shape,
                "downsampling_factor": processed.report.downsampling_factor,
                "physical_sample_spacing_mm": processed.report.physical_sample_spacing_mm,
                "estimated_triangle_count": processed.report.estimated_triangle_count,
                "raw_elevation_min_m": processed.report.raw_elevation_min_m,
                "raw_elevation_max_m": processed.report.raw_elevation_max_m,
                "processed_elevation_min_m": processed.report.processed_elevation_min_m,
                "processed_elevation_max_m": processed.report.processed_elevation_max_m,
                "peak_elevation_loss_m": processed.report.peak_elevation_loss_m,
                "raw_peak_coordinate": processed.report.raw_peak_coordinate,
                "processed_peak_coordinate": processed.report.processed_peak_coordinate,
                "peak_horizontal_shift_m": processed.report.peak_horizontal_shift_m,
                "sampling_decision_reasons": processed.report.sampling_decision_reasons,
                "terrain_fidelity_status": processed.report.terrain_fidelity_status,
            }
        )
        write_json(artifacts["validation_json"], validation)
        persisted_validation = json.loads(artifacts["validation_json"].read_text(encoding="utf-8"))
        write_validation_html(artifacts["validation_html"], persisted_validation)
        write_json(artifacts["provenance"], provenance)

        checksums = {
            key: sha256_file(path)
            for key, path in sorted(artifacts.items())
            if key != "manifest" and path.is_file()
        }
        write_json(
            artifacts["manifest"],
            {
                "topoforge_version": __version__,
                "generated_at": generated_at,
                "source_sha256": sha256_file(source),
                "resolved_config_sha256": sha256_file(artifacts["resolved_config"]),
                "artifacts": {key: path.name for key, path in sorted(artifacts.items())},
                "sha256": checksums,
            },
        )
        del mesh, validation_mesh, top_z_mm, oriented_top_z_mm, processed
        verify_artifact_bundle(stage, required_formats=config.output_formats)
        stage.replace(output)
        final_artifacts = {key: output / path.name for key, path in artifacts.items()}
        return BuildResult(output, final_artifacts, validation, provenance)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _object_record(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MeshValidationError(f"{label} must be a JSON object; rebuild the artifact bundle")
    return value


def _finite_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MeshValidationError(f"{label} must be numeric; rebuild the artifact bundle")
    converted = float(value)
    if not np.isfinite(converted):
        raise MeshValidationError(f"{label} must be finite; rebuild the artifact bundle")
    return converted


def _nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MeshValidationError(
            f"{label} must be a non-negative integer; rebuild the artifact bundle"
        )
    return value


def _recorded_shape(value: Any, *, label: str) -> tuple[int, int]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise MeshValidationError(f"{label} must contain two dimensions; rebuild the bundle")
    rows = _nonnegative_integer(value[0], label=f"{label}[0]")
    columns = _nonnegative_integer(value[1], label=f"{label}[1]")
    if rows < 2 or columns < 2:
        raise MeshValidationError(f"{label} must be at least 2 x 2; rebuild the bundle")
    return rows, columns


def _coordinate_mm(value: Any, *, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise MeshValidationError(f"{label} must contain three coordinates; rebuild the bundle")
    return (
        _finite_float(value[0], label=f"{label}[0]"),
        _finite_float(value[1], label=f"{label}[1]"),
        _finite_float(value[2], label=f"{label}[2]"),
    )


def _require_close(
    *,
    label: str,
    actual: float,
    recorded: Any,
    atol: float,
    rtol: float = 0.0,
) -> None:
    expected = _finite_float(recorded, label=label)
    if not np.isclose(actual, expected, atol=atol, rtol=rtol):
        raise MeshValidationError(
            f"{label} does not match the reopened artifact: recorded {expected}, "
            f"measured {actual}; rebuild the artifact bundle"
        )


def _verify_raster_evidence(
    *,
    elevations: np.ndarray[Any, Any],
    mask: np.ndarray[Any, Any],
    raster_crs: RasterioCRS,
    raster_transform: Affine,
    processed_tags: dict[str, str],
    mask_tags: dict[str, str],
    validation: dict[str, Any],
    provenance: dict[str, Any],
    preflight: ManufacturingPreflightReport,
) -> dict[str, Any]:
    processing = _object_record(
        provenance.get("processing"),
        label="provenance.processing",
    )
    raster_shape = (int(elevations.shape[0]), int(elevations.shape[1]))
    shape_records = (
        ("validation.processed_grid_shape", validation.get("processed_grid_shape")),
        ("provenance.processed_grid_shape", provenance.get("processed_grid_shape")),
        ("provenance.processing.array_shape", processing.get("array_shape")),
        (
            "provenance.processing.processed_grid_shape",
            processing.get("processed_grid_shape"),
        ),
        ("manufacturing_preflight.processed_grid_shape", preflight.processed_grid_shape),
    )
    for label, value in shape_records:
        if _recorded_shape(value, label=label) != raster_shape:
            raise MeshValidationError(
                f"{label} does not match reopened processed_dem.tif shape {raster_shape}; "
                "rebuild the artifact bundle"
            )

    for label in ("processed_crs", "automatic_projection_choice"):
        recorded_crs = processing.get(label)
        try:
            parsed_crs = RasterioCRS.from_user_input(recorded_crs)
        except (TypeError, ValueError) as exc:
            raise MeshValidationError(
                f"provenance.processing.{label} is not a valid CRS; rebuild the bundle"
            ) from exc
        if parsed_crs != raster_crs:
            raise MeshValidationError(
                f"provenance.processing.{label} does not match processed_dem.tif CRS; "
                "rebuild the artifact bundle"
            )

    if not (
        raster_transform.a > 0.0
        and raster_transform.e < 0.0
        and abs(raster_transform.b) < 1e-12
        and abs(raster_transform.d) < 1e-12
    ):
        raise MeshValidationError(
            "processed_dem.tif is not a north-up, east-right metric raster; rebuild the bundle"
        )
    pixel_x_m = float(np.hypot(raster_transform.a, raster_transform.d))
    pixel_y_m = float(np.hypot(raster_transform.b, raster_transform.e))
    measured_resolution_m = (pixel_x_m + pixel_y_m) / 2.0
    resolution_records = (
        (
            "validation.processed_horizontal_resolution_m",
            validation.get("processed_horizontal_resolution_m"),
        ),
        (
            "provenance.processed_horizontal_resolution_m",
            provenance.get("processed_horizontal_resolution_m"),
        ),
        (
            "provenance.processing.processed_horizontal_resolution_m",
            processing.get("processed_horizontal_resolution_m"),
        ),
        (
            "provenance.processing.horizontal_resolution_m",
            processing.get("horizontal_resolution_m"),
        ),
    )
    resolution_tolerance = max(1e-9, measured_resolution_m * 1e-9)
    for label, value in resolution_records:
        _require_close(
            label=label,
            actual=measured_resolution_m,
            recorded=value,
            atol=resolution_tolerance,
            rtol=1e-12,
        )

    measured_min_m = float(np.min(elevations))
    measured_max_m = float(np.max(elevations))
    elevation_records = (
        (
            "validation.processed_elevation_min_m",
            validation.get("processed_elevation_min_m"),
            measured_min_m,
        ),
        (
            "validation.processed_elevation_max_m",
            validation.get("processed_elevation_max_m"),
            measured_max_m,
        ),
        (
            "provenance.processed_elevation_min_m",
            provenance.get("processed_elevation_min_m"),
            measured_min_m,
        ),
        (
            "provenance.processed_elevation_max_m",
            provenance.get("processed_elevation_max_m"),
            measured_max_m,
        ),
        (
            "provenance.processing.elevation_min_m",
            processing.get("elevation_min_m"),
            measured_min_m,
        ),
        (
            "provenance.processing.elevation_max_m",
            processing.get("elevation_max_m"),
            measured_max_m,
        ),
        (
            "provenance.processing.processed_elevation_min_m",
            processing.get("processed_elevation_min_m"),
            measured_min_m,
        ),
        (
            "provenance.processing.processed_elevation_max_m",
            processing.get("processed_elevation_max_m"),
            measured_max_m,
        ),
    )
    for label, value, actual in elevation_records:
        _require_close(label=label, actual=actual, recorded=value, atol=1e-6, rtol=1e-9)

    peak_records = (
        ("validation.processed_peak_coordinate", validation.get("processed_peak_coordinate")),
        ("provenance.processed_peak_coordinate", provenance.get("processed_peak_coordinate")),
        (
            "provenance.processing.processed_peak_coordinate",
            processing.get("processed_peak_coordinate"),
        ),
    )
    canonical_peak = _object_record(peak_records[0][1], label=peak_records[0][0])
    for label, value in peak_records[1:]:
        if _object_record(value, label=label) != canonical_peak:
            raise MeshValidationError(
                f"{label} disagrees with validation.processed_peak_coordinate; rebuild the bundle"
            )
    measured_row, measured_column = np.unravel_index(int(np.argmax(elevations)), raster_shape)
    if (
        _nonnegative_integer(canonical_peak.get("row"), label="processed peak row") != measured_row
        or _nonnegative_integer(canonical_peak.get("column"), label="processed peak column")
        != measured_column
    ):
        raise MeshValidationError(
            "Recorded processed peak indices do not match processed_dem.tif; rebuild the bundle"
        )
    measured_x = (
        raster_transform.a * (measured_column + 0.5)
        + raster_transform.b * (measured_row + 0.5)
        + raster_transform.c
    )
    measured_y = (
        raster_transform.d * (measured_column + 0.5)
        + raster_transform.e * (measured_row + 0.5)
        + raster_transform.f
    )
    _require_close(
        label="processed peak x",
        actual=float(measured_x),
        recorded=canonical_peak.get("x"),
        atol=1e-6,
    )
    _require_close(
        label="processed peak y",
        actual=float(measured_y),
        recorded=canonical_peak.get("y"),
        atol=1e-6,
    )
    try:
        peak_crs = RasterioCRS.from_user_input(canonical_peak.get("crs"))
    except (TypeError, ValueError) as exc:
        raise MeshValidationError(
            "Recorded processed peak CRS is invalid; rebuild the bundle"
        ) from exc
    if peak_crs != raster_crs:
        raise MeshValidationError(
            "Recorded processed peak CRS does not match processed_dem.tif; rebuild the bundle"
        )

    nodata_percentage = _finite_float(
        processing.get("nodata_percentage"),
        label="provenance.processing.nodata_percentage",
    )
    interpolated_percentage = _finite_float(
        processing.get("interpolated_percentage"),
        label="provenance.processing.interpolated_percentage",
    )
    if not 0.0 <= nodata_percentage <= 100.0 or not np.isclose(
        nodata_percentage,
        interpolated_percentage,
        atol=1e-12,
        rtol=0.0,
    ):
        raise MeshValidationError(
            "Recorded NoData/interpolation fractions are inconsistent; rebuild the bundle"
        )
    downsampling = _finite_float(
        validation.get("downsampling_factor"),
        label="validation.downsampling_factor",
    )
    for label, value in (
        ("provenance.downsampling_factor", provenance.get("downsampling_factor")),
        ("provenance.processing.downsampling_factor", processing.get("downsampling_factor")),
    ):
        _require_close(label=label, actual=downsampling, recorded=value, atol=1e-12)
    recorded_fraction = nodata_percentage / 100.0
    measured_mask_fraction = float(np.count_nonzero(mask) / mask.size)
    pixel_fraction = 1.0 / mask.size
    if recorded_fraction == 0.0:
        if measured_mask_fraction != 0.0:
            raise MeshValidationError(
                "NoData-free provenance is paired with a non-empty original mask; "
                "rebuild the bundle"
            )
    elif downsampling <= 1.0 + 1e-9:
        if not np.isclose(measured_mask_fraction, recorded_fraction, atol=1e-12, rtol=0.0):
            raise MeshValidationError(
                "original_nodata_mask.tif fraction does not match provenance; rebuild the bundle"
            )
    else:
        conservative_upper = min(
            1.0,
            recorded_fraction * float((int(np.ceil(downsampling)) + 1) ** 2),
        )
        if (
            measured_mask_fraction + pixel_fraction < recorded_fraction
            or measured_mask_fraction > conservative_upper + pixel_fraction
        ):
            raise MeshValidationError(
                "Conservatively sampled NoData mask fraction is outside provenance bounds; "
                "rebuild the artifact bundle"
            )
    if (
        processing.get("original_nodata_mask") != "original_nodata_mask.tif"
        or processed_tags.get("ORIGINAL_NODATA_MASK") != "original_nodata_mask.tif"
        or processed_tags.get("ORIENTATION") != "row 0 is north; model export maps north to +Y"
        or mask_tags.get("MASK_MEANING")
        != "1=source/reprojection NoData before interpolation, conservatively sampled"
    ):
        raise MeshValidationError(
            "Processed DEM/mask semantic tags do not match provenance; rebuild the bundle"
        )
    return {
        "shape": raster_shape,
        "crs": str(raster_crs),
        "resolution_m": measured_resolution_m,
        "elevation_min_m": measured_min_m,
        "elevation_max_m": measured_max_m,
        "original_nodata_mask_fraction": measured_mask_fraction,
    }


def _require_coordinate_close(
    *,
    label: str,
    actual: tuple[float, float, float],
    recorded: Any,
    atol: float,
) -> None:
    expected = _coordinate_mm(recorded, label=label)
    if not np.allclose(actual, expected, atol=atol, rtol=0.0):
        raise MeshValidationError(
            f"{label} does not match the reopened artifact: recorded {expected}, "
            f"measured {actual}; rebuild the artifact bundle"
        )


def _verify_orientation_evidence(
    validation: dict[str, Any],
    provenance: dict[str, Any],
) -> tuple[tuple[float, float, float], dict[str, Any]]:
    validation_orientation = _object_record(
        validation.get("orientation"),
        label="validation.orientation",
    )
    provenance_orientation = _object_record(
        provenance.get("orientation"),
        label="provenance.orientation",
    )
    expected_contract: dict[str, Any] = {
        "east_axis": "+X = East",
        "north_axis": "+Y = North",
        "up_axis": "+Z = Up",
        "north_edge": "y=model_depth_mm",
        "orientation_transform": (
            "mesh_heightfield=flipud(processed_dem); source row 0 north maps to y=model_depth_mm"
        ),
        "coordinate_correction_only": True,
    }
    for field, expected in expected_contract.items():
        if (
            validation_orientation.get(field) != expected
            or provenance_orientation.get(field) != expected
        ):
            raise MeshValidationError(
                f"Recorded orientation field {field!r} is inconsistent; rebuild the bundle"
            )
    if validation.get("orientation_consistent") is not True:
        raise MeshValidationError(
            "validation.orientation_consistent is not true; rebuild the artifact bundle"
        )
    expected_peak_mm = _coordinate_mm(
        validation_orientation.get("expected_processed_peak_mm"),
        label="validation.orientation.expected_processed_peak_mm",
    )
    format_peaks = _object_record(
        validation_orientation.get("reopened_format_peak_coordinates_mm"),
        label="validation.orientation.reopened_format_peak_coordinates_mm",
    )
    _require_coordinate_close(
        label="validation.orientation.reopened_format_peak_coordinates_mm.in_memory",
        actual=expected_peak_mm,
        recorded=format_peaks.get("in_memory"),
        atol=0.01,
    )
    return expected_peak_mm, format_peaks


def _verify_recorded_model_dimensions(
    *,
    validation: dict[str, Any],
    provenance: dict[str, Any],
    preflight: ManufacturingPreflightReport,
) -> tuple[float, float, float]:
    expected_dimensions = _coordinate_mm(
        validation.get("dimensions_mm"),
        label="validation.dimensions_mm",
    )
    dimension_records = (
        ("validation.expected_dimensions_mm", validation.get("expected_dimensions_mm")),
        (
            "manufacturing_preflight.resolved_model_dimensions_mm",
            preflight.resolved_model_dimensions_mm,
        ),
    )
    for label, value in dimension_records:
        _require_coordinate_close(
            label=label,
            actual=expected_dimensions,
            recorded=value,
            atol=0.05,
        )
    scaling = _object_record(provenance.get("scaling"), label="provenance.scaling")
    scaling_dimensions = (
        _finite_float(scaling.get("model_width_mm"), label="provenance.scaling.model_width_mm"),
        _finite_float(scaling.get("model_depth_mm"), label="provenance.scaling.model_depth_mm"),
        _finite_float(
            scaling.get("predicted_max_z_mm"),
            label="provenance.scaling.predicted_max_z_mm",
        ),
    )
    if not np.allclose(expected_dimensions, scaling_dimensions, atol=0.05, rtol=0.0):
        raise MeshValidationError(
            "validation dimensions do not match provenance scaling; rebuild the artifact bundle"
        )
    return expected_dimensions


def _verify_mesh_evidence(
    *,
    output_format: str,
    mesh: trimesh.Trimesh,
    validation: dict[str, Any],
    raster_shape: tuple[int, int],
    expected_dimensions: tuple[float, float, float],
    expected_peak_mm: tuple[float, float, float],
    expected_geometry: trimesh.Trimesh,
    format_peaks: dict[str, Any],
) -> dict[str, Any]:
    report = validate_mesh(
        mesh,
        expected_dimensions_mm=expected_dimensions,
        dimension_tolerance_mm=0.05,
        flat_bottom_tolerance_mm=0.01,
    )
    measured = report.model_dump(mode="json")
    geometry_sha256 = _canonical_mesh_digest(mesh)
    if not _meshes_are_serialization_equivalent(expected_geometry, mesh):
        raise MeshValidationError(
            f"Reopened {output_format} does not match the processed DEM terrain geometry; "
            "rebuild the artifact bundle"
        )
    true_fields = (
        "finite_vertices",
        "finite_face_normals",
        "watertight",
        "winding_consistent",
        "manifold",
        "positive_volume",
        "flat_bottom",
        "dimensions_within_tolerance",
    )
    for field in true_fields:
        if validation.get(field) is not True or measured.get(field) is not True:
            raise MeshValidationError(
                f"Reopened {output_format} failed measured geometry field {field}; "
                "rebuild the artifact bundle"
            )

    rows, columns = raster_shape
    expected_vertex_count = 2 * rows * columns
    expected_triangle_count = 4 * rows * columns - 4
    validation_triangle_count = _nonnegative_integer(
        validation.get("triangle_count"),
        label="validation.triangle_count",
    )
    estimated_triangle_count = _nonnegative_integer(
        validation.get("estimated_triangle_count"),
        label="validation.estimated_triangle_count",
    )
    if (
        validation_triangle_count != expected_triangle_count
        or estimated_triangle_count != expected_triangle_count
        or len(mesh.faces) != expected_triangle_count
        or len(mesh.vertices) != expected_vertex_count
    ):
        raise MeshValidationError(
            f"Reopened {output_format} topology counts do not match the processed grid; "
            "rebuild the artifact bundle"
        )
    for field in ("connected_components", "degenerate_faces", "duplicate_faces"):
        recorded = _nonnegative_integer(validation.get(field), label=f"validation.{field}")
        measured_value = _nonnegative_integer(measured.get(field), label=f"{output_format}.{field}")
        if measured_value != recorded:
            raise MeshValidationError(
                f"Reopened {output_format} {field} does not match validation.json; "
                "rebuild the artifact bundle"
            )

    recorded_volume = _finite_float(
        validation.get("volume_mm3"),
        label="validation.volume_mm3",
    )
    measured_volume = _finite_float(measured.get("volume_mm3"), label=f"{output_format}.volume_mm3")
    if not np.isclose(measured_volume, recorded_volume, atol=0.05, rtol=1e-5):
        raise MeshValidationError(
            f"Reopened {output_format} volume does not match validation.json; rebuild the bundle"
        )
    for field, tolerance in (
        ("bottom_planarity_error_mm", 0.01),
        ("minimum_base_thickness_mm", 0.01),
    ):
        actual_value = _finite_float(measured.get(field), label=f"{output_format}.{field}")
        _require_close(
            label=f"validation.{field}",
            actual=actual_value,
            recorded=validation.get(field),
            atol=tolerance,
        )

    measured_peak = _peak_coordinate_mm(
        mesh,
        expected_xy_mm=(expected_peak_mm[0], expected_peak_mm[1]),
    )
    _require_coordinate_close(
        label=f"validation.orientation.reopened_format_peak_coordinates_mm.{output_format}",
        actual=measured_peak,
        recorded=format_peaks.get(output_format),
        atol=0.01,
    )
    if not np.allclose(measured_peak, expected_peak_mm, atol=0.01, rtol=0.0):
        raise MeshValidationError(
            f"Reopened {output_format} peak/orientation differs from validation.json; "
            "rebuild the artifact bundle"
        )
    return {
        "dimensions_mm": measured["dimensions_mm"],
        "vertex_count": len(mesh.vertices),
        "triangle_count": len(mesh.faces),
        "volume_mm3": measured_volume,
        "watertight": measured["watertight"],
        "winding_consistent": measured["winding_consistent"],
        "manifold": measured["manifold"],
        "connected_components": measured["connected_components"],
        "degenerate_faces": measured["degenerate_faces"],
        "duplicate_faces": measured["duplicate_faces"],
        "positive_volume": measured["positive_volume"],
        "peak_coordinate_mm": measured_peak,
        "geometry_semantic_sha256": geometry_sha256,
    }


def _coordinate_sequence(
    value: Any,
    *,
    label: str,
) -> tuple[tuple[float, float, float], ...]:
    if not isinstance(value, list | tuple) or not value:
        raise MeshValidationError(f"{label} must contain coordinates; rebuild the artifact bundle")
    return tuple(
        _coordinate_mm(item, label=f"{label}[{index}]") for index, item in enumerate(value)
    )


def _verify_three_mf_evidence(
    *,
    inspection: ThreeMFInspection,
    validation: dict[str, Any],
    raster_shape: tuple[int, int],
    expected_dimensions: tuple[float, float, float],
) -> dict[str, Any]:
    round_trip = _object_record(
        validation.get("three_mf_round_trip"),
        label="validation.three_mf_round_trip",
    )
    if round_trip.get("unit") != inspection.unit or inspection.unit != "millimeter":
        raise MeshValidationError(
            "Reopened 3MF unit does not match validation.json; rebuild bundle"
        )
    recorded_lib3mf_version = round_trip.get("lib3mf_version")
    if (
        not isinstance(recorded_lib3mf_version, list | tuple)
        or tuple(recorded_lib3mf_version) != inspection.lib3mf_version
    ):
        raise MeshValidationError(
            "Reopened 3MF lib3mf version does not match validation.json; rebuild the bundle"
        )
    if validation.get("three_mf_dimensions_match") is not True:
        raise MeshValidationError("validation.three_mf_dimensions_match is not true")
    count_fields = {
        "object_count": inspection.object_count,
        "build_item_count": inspection.build_item_count,
        "vertex_count": inspection.vertex_count,
        "triangle_count": inspection.triangle_count,
        "strict_warning_count": inspection.strict_warning_count,
    }
    for field, measured_value in count_fields.items():
        recorded = _nonnegative_integer(
            round_trip.get(field),
            label=f"validation.three_mf_round_trip.{field}",
        )
        if recorded != measured_value:
            raise MeshValidationError(
                f"Reopened 3MF {field} does not match validation.json; rebuild the bundle"
            )
    rows, columns = raster_shape
    if (
        inspection.object_count != 1
        or inspection.build_item_count != 1
        or inspection.vertex_count != 2 * rows * columns
        or inspection.triangle_count != 4 * rows * columns - 4
        or inspection.strict_warning_count != 0
        or inspection.components_object_count != 0
        or inspection.component_count != 0
        or inspection.base_material_group_count != 0
        or inspection.material_assigned_object_count != 0
    ):
        raise MeshValidationError(
            "Reopened 3MF resource/build topology is not the canonical terrain shape; "
            "rebuild the artifact bundle"
        )
    _require_coordinate_close(
        label="validation.three_mf_round_trip.dimensions_mm",
        actual=inspection.dimensions_mm,
        recorded=round_trip.get("dimensions_mm"),
        atol=0.05,
    )
    if not np.allclose(inspection.dimensions_mm, expected_dimensions, atol=0.05, rtol=0.0):
        raise MeshValidationError(
            "Reopened 3MF dimensions do not match validation.json; rebuild the bundle"
        )
    recorded_names = round_trip.get("object_names")
    if (
        not isinstance(recorded_names, list)
        or not all(isinstance(name, str) for name in recorded_names)
        or tuple(recorded_names) != inspection.object_names
        or inspection.object_names != ("TopoForge terrain",)
    ):
        raise MeshValidationError(
            "Reopened 3MF object names do not match validation.json; rebuild the bundle"
        )
    recorded_metadata = _object_record(
        round_trip.get("metadata"),
        label="validation.three_mf_round_trip.metadata",
    )
    if recorded_metadata != inspection.metadata:
        raise MeshValidationError(
            "Reopened 3MF metadata does not match validation.json; rebuild the artifact bundle"
        )
    _require_coordinate_close(
        label="validation.three_mf_round_trip.peak_coordinate_mm",
        actual=inspection.peak_coordinate_mm,
        recorded=round_trip.get("peak_coordinate_mm"),
        atol=1e-6,
    )
    recorded_peaks = _coordinate_sequence(
        round_trip.get("peak_coordinates_mm"),
        label="validation.three_mf_round_trip.peak_coordinates_mm",
    )
    if len(recorded_peaks) != len(inspection.peak_coordinates_mm) or not np.allclose(
        recorded_peaks,
        inspection.peak_coordinates_mm,
        atol=1e-6,
        rtol=0.0,
    ):
        raise MeshValidationError(
            "Reopened 3MF peak coordinate inventory does not match validation.json; "
            "rebuild the artifact bundle"
        )
    return {
        **count_fields,
        "dimensions_mm": inspection.dimensions_mm,
        "peak_coordinate_mm": inspection.peak_coordinate_mm,
    }


def _sha256_text(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MeshValidationError(
            f"{label} is not a lowercase SHA-256 digest; rebuild the artifact bundle"
        )
    return value


def _verify_artifact_bindings(
    *,
    validation: dict[str, Any],
    provenance: dict[str, Any],
    manifest_checksums: dict[str, Any],
    elevations: np.ndarray[Any, Any],
    mask: np.ndarray[Any, Any],
    raster_crs: RasterioCRS,
    raster_transform: Affine,
) -> str:
    validation_bindings = _object_record(
        validation.get("artifact_bindings"),
        label="validation.artifact_bindings",
    )
    provenance_bindings = _object_record(
        provenance.get("artifact_bindings"),
        label="provenance.artifact_bindings",
    )
    if validation_bindings != provenance_bindings:
        raise MeshValidationError(
            "Validation and provenance artifact bindings disagree; rebuild the artifact bundle"
        )
    if validation_bindings.get("schema") != _ARTIFACT_BINDING_SCHEMA:
        raise MeshValidationError("Artifact binding schema is unsupported; rebuild the bundle")
    role_hashes = _object_record(
        validation_bindings.get("role_sha256"),
        label="validation.artifact_bindings.role_sha256",
    )
    expected_roles = set(manifest_checksums) - _MUTABLE_ARTIFACT_ROLES
    if set(role_hashes) != expected_roles:
        raise MeshValidationError(
            "Artifact binding role inventory does not match the immutable manifest roles; "
            "rebuild the artifact bundle"
        )
    for role in sorted(expected_roles):
        recorded = _sha256_text(
            role_hashes.get(role),
            label=f"validation.artifact_bindings.role_sha256.{role}",
        )
        manifest_sha256 = _sha256_text(
            manifest_checksums.get(role),
            label=f"build_manifest.sha256.{role}",
        )
        if recorded != manifest_sha256:
            raise MeshValidationError(
                f"Immutable artifact role {role} changed after validation; rebuild the bundle"
            )

    semantic_digests = {
        "processed_dem_semantic_sha256": _semantic_array_digest(
            elevations,
            kind="processed-dem",
            crs=raster_crs,
            transform=raster_transform,
        ),
        "original_nodata_mask_semantic_sha256": _semantic_array_digest(
            mask,
            kind="original-nodata-mask",
            crs=raster_crs,
            transform=raster_transform,
        ),
    }
    for field, measured in semantic_digests.items():
        recorded = _sha256_text(
            validation_bindings.get(field),
            label=f"validation.artifact_bindings.{field}",
        )
        if recorded != measured:
            raise MeshValidationError(
                f"Reopened bundle {field} does not match its semantic binding; rebuild the bundle"
            )
    return _sha256_text(
        validation_bindings.get("terrain_geometry_semantic_sha256"),
        label="validation.artifact_bindings.terrain_geometry_semantic_sha256",
    )


def _rebuild_expected_geometry_digest(
    *,
    elevations: np.ndarray[Any, Any],
    raster_transform: Affine,
    resolved_config: BuildConfig,
    provenance: dict[str, Any],
) -> tuple[str, trimesh.Trimesh]:
    try:
        recorded_scaling = ScalingResult.model_validate(provenance.get("scaling"))
        rows, columns = elevations.shape
        raster_stub = RasterResult.model_construct(
            ground_width_m=float(np.hypot(raster_transform.a, raster_transform.d)) * columns,
            ground_depth_m=float(np.hypot(raster_transform.b, raster_transform.e)) * rows,
        )
        expected_scaling = resolve_scaling(elevations, raster_stub, resolved_config)
    except (ConfigurationError, TypeError, ValueError) as exc:
        raise MeshValidationError(
            "Provenance scaling cannot be reproduced from the resolved config and processed DEM"
        ) from exc

    exact_fields = ("height_limit_applied", "scale_mode")
    for field in exact_fields:
        if getattr(recorded_scaling, field) != getattr(expected_scaling, field):
            raise MeshValidationError(
                f"provenance.scaling.{field} is not reproducible; rebuild the artifact bundle"
            )
    numeric_fields = (
        "horizontal_scale_mm_per_m",
        "model_width_mm",
        "model_depth_mm",
        "base_thickness_mm",
        "baseline_elevation_m",
        "robust_low_elevation_m",
        "robust_high_elevation_m",
        "policy_vertical_exaggeration",
        "vertical_exaggeration",
        "height_limit_mm",
        "predicted_min_z_mm",
        "predicted_max_z_mm",
    )
    for field in numeric_fields:
        recorded = float(getattr(recorded_scaling, field))
        expected = float(getattr(expected_scaling, field))
        tolerance = max(1e-9, abs(expected) * 1e-12)
        if not np.isclose(recorded, expected, atol=tolerance, rtol=1e-12):
            raise MeshValidationError(
                f"provenance.scaling.{field} is not reproducible; rebuild the artifact bundle"
            )

    expected_top_z_mm = np.flipud(apply_vertical_scale(elevations, expected_scaling))
    expected_mesh = build_rectangular_terrain_mesh(
        expected_top_z_mm,
        width_mm=expected_scaling.model_width_mm,
        depth_mm=expected_scaling.model_depth_mm,
        base_thickness_mm=expected_scaling.base_thickness_mm,
    )
    return _canonical_mesh_digest(expected_mesh), expected_mesh


def verify_artifact_bundle(
    output_dir: Path, required_formats: list[str] | None = None
) -> dict[str, Any]:
    """Reopen every required output role and return literal verification measurements."""
    bootstrap_names = (
        "build_config.resolved.yaml",
        "build_manifest.json",
        "manufacturing_preflight.json",
        "provenance.json",
        "validation.json",
    )
    bootstrap_missing = [
        name
        for name in bootstrap_names
        if (output_dir / name).is_symlink()
        or not (output_dir / name).is_file()
        or (output_dir / name).stat().st_size == 0
    ]
    if bootstrap_missing:
        raise MeshValidationError(
            f"Artifact bundle is missing non-empty files: {', '.join(bootstrap_missing)}"
        )
    try:
        resolved = yaml.safe_load(
            _bounded_text(
                output_dir / "build_config.resolved.yaml",
                label="build_config.resolved.yaml",
            )
        )
        if not isinstance(resolved, dict):
            raise ValueError("resolved build configuration root is not an object")
        resolved_config = BuildConfig.model_validate(resolved)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise MeshValidationError(
            "build_config.resolved.yaml is not a valid typed BuildConfig; rebuild the bundle"
        ) from exc
    configured_formats = list(dict.fromkeys(resolved_config.output_formats))
    formats = configured_formats if required_formats is None else list(required_formats)
    if not formats:
        raise MeshValidationError(
            "At least one manufacturing output format is required: choose STL, 3MF, or GLB"
        )
    unknown_formats = set(formats) - {"stl", "3mf", "glb"}
    if unknown_formats:
        raise MeshValidationError(
            f"Unsupported required artifact formats: {', '.join(sorted(unknown_formats))}"
        )
    formats = list(dict.fromkeys(formats))
    if set(formats) != set(configured_formats):
        raise MeshValidationError(
            "Required formats do not match build_config.resolved.yaml output_formats; "
            "verify the complete configured manufacturing bundle"
        )
    required = [
        "processed_dem.tif",
        "original_nodata_mask.tif",
        "manufacturing_preflight.json",
        "provenance.json",
        "validation.json",
        "validation.html",
        "build_config.resolved.yaml",
        "preview.png",
        "build_manifest.json",
    ]
    required.extend(
        {"stl": "model.stl", "3mf": "model.3mf", "glb": "preview.glb"}[item] for item in formats
    )
    required_paths = {name: output_dir / name for name in required}
    missing = [
        name
        for name, path in required_paths.items()
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0
    ]
    if missing:
        raise MeshValidationError(
            f"Artifact bundle is missing non-empty files: {', '.join(missing)}"
        )

    processed_dem_path = output_dir / "processed_dem.tif"
    with rasterio.open(processed_dem_path) as dataset:
        _require_self_contained_gtiff(dataset, processed_dem_path, label="processed_dem.tif")
        raster_cells = int(dataset.height) * int(dataset.width)
        safe_cell_limit = min(resolved_config.max_grid_cells, _MAX_VERIFICATION_GRID_CELLS)
        if raster_cells > safe_cell_limit:
            raise MeshValidationError(
                f"processed_dem.tif declares {raster_cells} cells, above the bounded "
                f"verification limit {safe_cell_limit}; rebuild with a smaller grid"
            )
        raster_file_limit = 16 * 1024 * 1024 + raster_cells * 16
        if processed_dem_path.stat().st_size > raster_file_limit:
            raise MeshValidationError("processed_dem.tif exceeds its bounded encoded-size limit")
        raster_elevations = dataset.read(1)
        if (
            dataset.count != 1
            or dataset.crs is None
            or dataset.dtypes != ("float32",)
            or dataset.nodata is not None
            or not np.all(np.isfinite(raster_elevations))
        ):
            raise MeshValidationError(
                "Reopened processed_dem.tif failed finite single-band CRS checks"
            )
        raster_shape = (dataset.height, dataset.width)
        raster_crs_value = dataset.crs
        raster_crs = str(raster_crs_value)
        raster_transform = dataset.transform
        processed_tags = dataset.tags()
    mask_path = output_dir / "original_nodata_mask.tif"
    with rasterio.open(mask_path) as dataset:
        _require_self_contained_gtiff(
            dataset,
            mask_path,
            label="original_nodata_mask.tif",
        )
        mask_file_limit = 16 * 1024 * 1024 + raster_cells * 4
        if mask_path.stat().st_size > mask_file_limit:
            raise MeshValidationError(
                "original_nodata_mask.tif exceeds its bounded encoded-size limit"
            )
        mask = dataset.read(1)
        mask_tags = dataset.tags()
        if (
            dataset.count != 1
            or dataset.shape != raster_shape
            or dataset.crs != raster_crs_value
            or not dataset.transform.almost_equals(raster_transform)
            or dataset.nodata is not None
        ):
            raise MeshValidationError(
                "Reopened original_nodata_mask.tif is not aligned with processed_dem.tif"
            )
        mask_values = np.unique(mask)
        if (
            dataset.dtypes != ("uint8",)
            or not np.all(np.isfinite(mask_values))
            or not np.all(np.isin(mask_values, (0, 1)))
        ):
            raise MeshValidationError("Reopened original_nodata_mask.tif is not binary")
    provenance = json.loads(_bounded_text(output_dir / "provenance.json", label="provenance.json"))
    validation = json.loads(_bounded_text(output_dir / "validation.json", label="validation.json"))
    if not isinstance(provenance, dict) or not isinstance(validation, dict):
        raise MeshValidationError("provenance.json and validation.json must be objects")
    if validation.get("required_checks_passed") is not True:
        raise MeshValidationError(
            "validation.json does not pass required checks; rebuild the artifact bundle"
        )
    preflight = ManufacturingPreflightReport.model_validate_json(
        _bounded_text(
            output_dir / "manufacturing_preflight.json",
            label="manufacturing_preflight.json",
        )
    )
    if preflight.status not in {"passed", "passed-with-warnings"}:
        raise MeshValidationError("manufacturing_preflight.json did not pass")
    preflight_payload = preflight.model_dump(mode="json")
    if validation.get("manufacturing_preflight") != preflight_payload:
        raise MeshValidationError("validation.json manufacturing preflight does not match artifact")
    if provenance.get("manufacturing_preflight") != preflight_payload:
        raise MeshValidationError("provenance.json manufacturing preflight does not match artifact")
    manifest = json.loads(
        _bounded_text(output_dir / "build_manifest.json", label="build_manifest.json")
    )
    if not isinstance(manifest, dict):
        raise MeshValidationError("build_manifest.json must contain an object")
    manifest_artifacts = manifest.get("artifacts", {})
    manifest_checksums = manifest.get("sha256", {})
    if not isinstance(manifest_artifacts, dict) or not isinstance(manifest_checksums, dict):
        raise MeshValidationError("build_manifest.json artifact/checksum maps are invalid")
    if manifest_artifacts.get("manifest") != "build_manifest.json":
        raise MeshValidationError("build_manifest.json does not bind its canonical manifest role")
    artifact_roles = set(manifest_artifacts) - {"manifest"}
    if set(manifest_checksums) != artifact_roles:
        raise MeshValidationError(
            "build_manifest.json artifact and checksum role inventories differ"
        )
    canonical_roles = {
        "processed_dem": "processed_dem.tif",
        "original_nodata_mask": "original_nodata_mask.tif",
        "manufacturing_preflight": "manufacturing_preflight.json",
        "provenance": "provenance.json",
        "validation_json": "validation.json",
        "validation_html": "validation.html",
        "resolved_config": "build_config.resolved.yaml",
        "preview_png": "preview.png",
    }
    format_roles = {
        "stl": ("model_stl", "model.stl"),
        "3mf": ("model_3mf", "model.3mf"),
        "glb": ("preview_glb", "preview.glb"),
    }
    for output_format in formats:
        role, filename = format_roles[output_format]
        canonical_roles[role] = filename
    allowed_roles = {
        *canonical_roles,
        "manifest",
        "source_acquisition",
        "source_quality_edm",
        "source_quality_flm",
        "source_quality_hem",
        "source_quality_wbm",
        "slicer_validation",
        "bambu_studio_validation",
    }
    unexpected_roles = set(manifest_artifacts) - allowed_roles
    if unexpected_roles:
        raise MeshValidationError(
            f"build_manifest.json contains unexpected artifact roles: "
            f"{', '.join(sorted(unexpected_roles))}"
        )
    for role, filename in canonical_roles.items():
        if manifest_artifacts.get(role) != filename or role not in manifest_checksums:
            raise MeshValidationError(
                f"build_manifest.json does not bind canonical artifact role {role} to {filename}"
            )
    manifest_names = list(manifest_artifacts.values())
    if not all(isinstance(name, str) for name in manifest_names) or len(set(manifest_names)) != len(
        manifest_names
    ):
        raise MeshValidationError("build_manifest.json artifact filenames must be unique strings")
    resolved_config_sha256 = sha256_file(output_dir / "build_config.resolved.yaml")
    if (
        _sha256_text(
            manifest.get("resolved_config_sha256"),
            label="build_manifest.resolved_config_sha256",
        )
        != resolved_config_sha256
        or manifest_checksums.get("resolved_config") != resolved_config_sha256
    ):
        raise MeshValidationError(
            "build_manifest.json does not bind the resolved configuration digest"
        )
    _sha256_text(manifest.get("source_sha256"), label="build_manifest.source_sha256")
    if manifest.get("topoforge_version") != provenance.get("topoforge_version") or manifest.get(
        "generated_at"
    ) != provenance.get("generated_at"):
        raise MeshValidationError(
            "build_manifest.json version/time identity does not match provenance.json"
        )
    for role, expected_sha256 in manifest_checksums.items():
        expected_sha256 = _sha256_text(
            expected_sha256,
            label=f"build_manifest.sha256.{role}",
        )
        artifact_name = manifest_artifacts.get(role)
        if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
            raise MeshValidationError(f"Manifest role {role!r} has an unsafe artifact path")
        artifact_path = output_dir / artifact_name
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise MeshValidationError(f"Manifest role {role!r} points to a missing artifact")
        actual_sha256 = sha256_file(artifact_path)
        if actual_sha256 != expected_sha256:
            raise MeshValidationError(
                f"Manifest checksum mismatch for {role}: expected {expected_sha256}, "
                f"found {actual_sha256}"
            )
    recorded_geometry_sha256 = _verify_artifact_bindings(
        validation=validation,
        provenance=provenance,
        manifest_checksums=manifest_checksums,
        elevations=raster_elevations,
        mask=mask,
        raster_crs=raster_crs_value,
        raster_transform=raster_transform,
    )

    source_acquisition_artifact = output_dir / "source_acquisition.json"
    source_acquisition_claim = provenance.get("source_acquisition")
    source_quality_roles = {
        role for role in manifest_artifacts if role.startswith("source_quality_")
    }
    source_quality_files = {
        path.name
        for path in output_dir.glob("source_quality_*.tif")
        if path.is_file() or path.is_symlink()
    }
    source_artifacts_present = (
        "source_acquisition" in manifest_artifacts
        or "source_acquisition" in manifest_checksums
        or source_acquisition_artifact.is_file()
        or source_acquisition_artifact.is_symlink()
        or bool(source_quality_roles)
        or bool(source_quality_files)
    )
    if source_acquisition_claim is None:
        if source_artifacts_present:
            raise MeshValidationError(
                "Source acquisition artifacts exist without provenance.source_acquisition"
            )
    else:
        if not isinstance(source_acquisition_claim, dict):
            raise MeshValidationError("provenance.source_acquisition must be an object or null")
        if (
            manifest_artifacts.get("source_acquisition") != "source_acquisition.json"
            or "source_acquisition" not in manifest_checksums
            or source_acquisition_artifact.is_symlink()
            or not source_acquisition_artifact.is_file()
        ):
            raise MeshValidationError(
                "Source acquisition provenance requires canonical source_acquisition.json evidence"
            )
        source_acquisition = json.loads(
            _bounded_text(
                source_acquisition_artifact,
                label="source_acquisition.json",
            )
        )
        if source_acquisition != source_acquisition_claim:
            raise MeshValidationError(
                "source_acquisition.json does not match provenance.source_acquisition"
            )
        quality_masks = source_acquisition.get("quality_masks", [])
        if not isinstance(quality_masks, list):
            raise MeshValidationError("source_acquisition quality_masks is not a list")
        expected_quality_roles: set[str] = set()
        expected_quality_files: set[str] = set()
        for record in quality_masks:
            if not isinstance(record, dict) or record.get("availability") != "present":
                continue
            role = record.get("role")
            output_record = record.get("output")
            if role not in {"edm", "flm", "hem", "wbm"} or not isinstance(output_record, dict):
                raise MeshValidationError("Present source quality mask metadata is invalid")
            artifact_role = f"source_quality_{role}"
            if artifact_role in expected_quality_roles:
                raise MeshValidationError(f"Duplicate source quality mask role: {role}")
            expected_quality_roles.add(artifact_role)
            expected_artifact = f"{artifact_role}.tif"
            expected_quality_files.add(expected_artifact)
            bundled_artifact = output_record.get("bundled_artifact")
            if (
                bundled_artifact != expected_artifact
                or manifest_artifacts.get(artifact_role) != expected_artifact
                or artifact_role not in manifest_checksums
            ):
                raise MeshValidationError(
                    f"Source quality mask role {role} is not bound to its canonical bundle artifact"
                )
            quality_path = output_dir / expected_artifact
            if quality_path.is_symlink() or not quality_path.is_file():
                raise MeshValidationError(f"Source quality mask role {role} is not a regular file")
            recorded_quality_sha256 = _sha256_text(
                output_record.get("sha256"),
                label=f"source_acquisition.quality_masks.{role}.output.sha256",
            )
            if recorded_quality_sha256 != manifest_checksums[artifact_role]:
                raise MeshValidationError(
                    f"Source quality mask role {role} checksum does not match acquisition evidence"
                )
            if quality_path.stat().st_size > _MAX_PREVIEW_FILE_BYTES:
                raise MeshValidationError(
                    f"Source quality mask role {role} exceeds the bounded encoded-size limit"
                )
            with rasterio.open(quality_path) as quality_dataset:
                _require_self_contained_gtiff(
                    quality_dataset,
                    quality_path,
                    label=f"source quality mask {role}",
                )
                expected_shape = tuple(output_record.get("grid_shape", []))
                expected_transform = tuple(output_record.get("transform", []))
                expected_crs = output_record.get("crs")
                try:
                    aligned = (
                        quality_dataset.count == 1
                        and quality_dataset.shape == expected_shape
                        and RasterioCRS.from_user_input(expected_crs) == quality_dataset.crs
                        and len(expected_transform) == 6
                        and np.allclose(
                            tuple(quality_dataset.transform)[:6],
                            expected_transform,
                            atol=1e-12,
                            rtol=0.0,
                        )
                    )
                except (TypeError, ValueError):
                    aligned = False
                if not aligned:
                    raise MeshValidationError(
                        f"Bundled source quality mask is not aligned as recorded: {role}"
                    )
        if (
            source_quality_roles != expected_quality_roles
            or source_quality_files != expected_quality_files
        ):
            raise MeshValidationError(
                "Source quality mask role/file inventory does not match acquisition provenance"
            )

    slicer_paths = {
        "slicer_validation": output_dir / "slicer_validation.json",
        "bambu_studio_validation": output_dir / "bambu_studio_validation.json",
    }
    slicer_signal = (
        "slicer_result" in validation
        or "manufacturing_validation" in validation
        or "slicer_validation" in provenance
        or any(role in manifest_artifacts for role in slicer_paths)
        or any(path.is_file() or path.is_symlink() for path in slicer_paths.values())
    )
    if slicer_signal:
        if (
            "slicer_result" not in validation
            or "manufacturing_validation" not in validation
            or not isinstance(provenance.get("slicer_validation"), dict)
        ):
            raise MeshValidationError(
                "Slicer evidence is incomplete across validation and provenance"
            )
        for role, path in slicer_paths.items():
            if (
                manifest_artifacts.get(role) != path.name
                or role not in manifest_checksums
                or path.is_symlink()
                or not path.is_file()
            ):
                raise MeshValidationError(
                    f"Slicer claims require canonical manifest role and file: {role}"
                )
        slicer_payload = json.loads(
            _bounded_text(
                slicer_paths["slicer_validation"],
                label="slicer_validation.json",
            )
        )
        bambu_payload = json.loads(
            _bounded_text(
                slicer_paths["bambu_studio_validation"],
                label="bambu_studio_validation.json",
            )
        )
        if not isinstance(slicer_payload, dict) or validation["slicer_result"] != slicer_payload:
            raise MeshValidationError(
                "slicer_validation.json does not match validation.slicer_result"
            )
        expected_bambu = {
            "manufacturing_validation": validation["manufacturing_validation"],
            "slicer_result": slicer_payload,
        }
        if bambu_payload != expected_bambu:
            raise MeshValidationError(
                "bambu_studio_validation.json does not match validation evidence"
            )
        slicer_record = slicer_payload.get("slicer")
        slicer_name = slicer_record.get("name") if isinstance(slicer_record, dict) else None
        expected_slicer_provenance = {
            "slicer": slicer_record,
            "release_role": (
                "official-p2s-release" if slicer_name == "BambuStudio" else "diagnostic"
            ),
            "profile": slicer_payload.get("profile"),
            "success": slicer_payload.get("status") == "succeeded",
            "gcode_generated": slicer_payload.get("gcode_generated"),
            "gcode_size_bytes": slicer_payload.get("gcode_size_bytes"),
            "metrics": slicer_payload.get("metrics"),
            "manufacturing_validation": validation["manufacturing_validation"],
            "report": "slicer_validation.json",
            "bambu_report": "bambu_studio_validation.json",
        }
        if provenance["slicer_validation"] != expected_slicer_provenance:
            raise MeshValidationError(
                "provenance.slicer_validation does not match canonical slicer evidence"
            )

    if (resolved.get("source_acquisition_manifest") is None) != (source_acquisition_claim is None):
        raise MeshValidationError(
            "Resolved config and provenance disagree about source acquisition evidence"
        )
    raster_evidence = _verify_raster_evidence(
        elevations=raster_elevations,
        mask=mask,
        raster_crs=raster_crs_value,
        raster_transform=raster_transform,
        processed_tags=processed_tags,
        mask_tags=mask_tags,
        validation=validation,
        provenance=provenance,
        preflight=preflight,
    )
    expected_peak_mm, format_peaks = _verify_orientation_evidence(validation, provenance)
    expected_dimensions = _verify_recorded_model_dimensions(
        validation=validation,
        provenance=provenance,
        preflight=preflight,
    )
    expected_geometry_sha256, expected_geometry = _rebuild_expected_geometry_digest(
        elevations=raster_elevations,
        raster_transform=raster_transform,
        resolved_config=resolved_config,
        provenance=provenance,
    )
    if recorded_geometry_sha256 != expected_geometry_sha256:
        raise MeshValidationError(
            "Recorded terrain geometry is not reproducible from processed_dem.tif and "
            "build_config.resolved.yaml; rebuild the artifact bundle"
        )

    preview_path = output_dir / "preview.png"
    if preview_path.stat().st_size > _MAX_PREVIEW_FILE_BYTES:
        raise MeshValidationError("preview.png exceeds the bounded encoded-size limit")
    with Image.open(preview_path) as preview:
        if preview.format != "PNG" or preview.width * preview.height > 16_000_000:
            raise MeshValidationError("preview.png is not a bounded canonical PNG preview")
        preview.verify()
    validation_html_path = output_dir / "validation.html"
    _bounded_text(validation_html_path, label="validation.html")
    with tempfile.TemporaryDirectory(prefix="topoforge-validation-verify-") as temporary_dir:
        regenerated_html = Path(temporary_dir) / "validation.html"
        write_validation_html(regenerated_html, validation)
        if regenerated_html.read_bytes() != validation_html_path.read_bytes():
            raise MeshValidationError(
                "validation.html is not the deterministic rendering of validation.json"
            )

    rows, columns = raster_shape
    expected_vertex_count = 2 * rows * columns
    expected_triangle_count = 4 * rows * columns - 4
    model_file_limit = _model_file_limit(
        vertex_count=expected_vertex_count,
        triangle_count=expected_triangle_count,
    )
    format_measurements: dict[str, Any] = {}
    if "stl" in formats:
        stl_path = output_dir / "model.stl"
        if stl_path.stat().st_size > model_file_limit:
            raise MeshValidationError("model.stl exceeds its bounded topology-derived size limit")
        reopened_stl = _load_stl(stl_path)
        format_measurements["stl"] = _verify_mesh_evidence(
            output_format="stl",
            mesh=reopened_stl,
            validation=validation,
            raster_shape=raster_shape,
            expected_dimensions=expected_dimensions,
            expected_peak_mm=expected_peak_mm,
            expected_geometry=expected_geometry,
            format_peaks=format_peaks,
        )
    if "3mf" in formats:
        three_mf_path = output_dir / "model.3mf"
        if three_mf_path.stat().st_size > model_file_limit:
            raise MeshValidationError("model.3mf exceeds its bounded topology-derived size limit")
        try:
            three_mf = inspect_3mf(
                three_mf_path,
                max_uncompressed_bytes=_three_mf_uncompressed_limit(
                    vertex_count=expected_vertex_count,
                    triangle_count=expected_triangle_count,
                ),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise MeshValidationError(
                "model.3mf failed bounded strict package inspection; rebuild the artifact bundle"
            ) from exc
        three_mf_evidence = _verify_three_mf_evidence(
            inspection=three_mf,
            validation=validation,
            raster_shape=raster_shape,
            expected_dimensions=expected_dimensions,
        )
        metadata_names = set(three_mf.metadata)
        for required_suffix in (
            "east_axis",
            "north_axis",
            "north_edge",
            "source_bounds",
            "orientation_transform",
        ):
            if not any(name.endswith(required_suffix) for name in metadata_names):
                raise MeshValidationError(
                    f"Reopened model.3mf is missing orientation metadata {required_suffix}"
                )
        three_mf_mesh = _load_3mf_mesh(three_mf_path)
        format_measurements["3mf"] = {
            **_verify_mesh_evidence(
                output_format="3mf",
                mesh=three_mf_mesh,
                validation=validation,
                raster_shape=raster_shape,
                expected_dimensions=expected_dimensions,
                expected_peak_mm=expected_peak_mm,
                expected_geometry=expected_geometry,
                format_peaks=format_peaks,
            ),
            "strict_round_trip": three_mf_evidence,
        }
    if "glb" in formats:
        glb_path = output_dir / "preview.glb"
        _verify_self_contained_glb(
            glb_path,
            vertex_count=expected_vertex_count,
            triangle_count=expected_triangle_count,
        )
        glb = trimesh.load(glb_path, force="scene")
        if not isinstance(glb, trimesh.Scene) or len(glb.geometry) != 1:
            raise MeshValidationError(
                "Reopened preview.glb does not contain exactly one terrain geometry; "
                "rebuild the artifact bundle"
            )
        format_measurements["glb"] = {
            **_verify_mesh_evidence(
                output_format="glb",
                mesh=_load_glb(glb_path),
                validation=validation,
                raster_shape=raster_shape,
                expected_dimensions=expected_dimensions,
                expected_peak_mm=expected_peak_mm,
                expected_geometry=expected_geometry,
                format_peaks=format_peaks,
            ),
            "geometry_count": len(glb.geometry),
        }
    return {
        "raster_shape": raster_shape,
        "raster_crs": raster_crs,
        "raster_measurements": raster_evidence,
        "format_measurements": format_measurements,
        "required_checks_passed": validation.get("required_checks_passed"),
        "dataset_name": provenance.get("dataset", {}).get("dataset_name"),
        "resolved_output_dir": resolved.get("output_dir"),
        "manifest_artifact_count": len(manifest.get("artifacts", {})),
    }


def record_slice_validation(build_dir: Path, result: dict[str, Any]) -> Path:
    """Attach one actually executed slicer result to an existing verified build."""
    build_dir = build_dir.expanduser().resolve()
    validation_path = build_dir / "validation.json"
    provenance_path = build_dir / "provenance.json"
    manifest_path = build_dir / "build_manifest.json"
    if not all(path.is_file() for path in (validation_path, provenance_path, manifest_path)):
        raise MeshValidationError(f"{build_dir} is not a completed TopoForge artifact bundle")
    verify_artifact_bundle(build_dir)
    validation = json.loads(_bounded_text(validation_path, label="validation.json"))
    provenance = json.loads(_bounded_text(provenance_path, label="provenance.json"))
    manifest = json.loads(_bounded_text(manifest_path, label="build_manifest.json"))
    try:
        resolved_payload = yaml.safe_load(
            _bounded_text(
                build_dir / "build_config.resolved.yaml",
                label="build_config.resolved.yaml",
            )
        )
        resolved_config = BuildConfig.model_validate(resolved_payload)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise MeshValidationError(
            "build_config.resolved.yaml is not a valid typed BuildConfig"
        ) from exc
    printer_profile_id = resolved_config.printer_profile.profile_id
    manufacturing_validation = evaluate_bambu_p2s_release_gate(
        result, printer_profile_id=printer_profile_id
    )
    slicer_path = build_dir / "slicer_validation.json"
    write_json(slicer_path, result)
    bambu_path = build_dir / "bambu_studio_validation.json"
    write_json(
        bambu_path,
        {
            "manufacturing_validation": manufacturing_validation,
            "slicer_result": result,
        },
    )
    validation["slicer_result"] = result
    validation["manufacturing_validation"] = manufacturing_validation
    slicer_record = result.get("slicer")
    slicer_name = slicer_record.get("name") if isinstance(slicer_record, dict) else None
    release_role = "official-p2s-release" if slicer_name == "BambuStudio" else "diagnostic"
    provenance["slicer_validation"] = {
        "slicer": slicer_record,
        "release_role": release_role,
        "profile": result.get("profile"),
        "success": result.get("status") == "succeeded",
        "gcode_generated": result.get("gcode_generated"),
        "gcode_size_bytes": result.get("gcode_size_bytes"),
        "metrics": result.get("metrics"),
        "manufacturing_validation": manufacturing_validation,
        "report": slicer_path.name,
        "bambu_report": bambu_path.name,
    }
    write_json(validation_path, validation)
    persisted_validation = json.loads(validation_path.read_text(encoding="utf-8"))
    write_validation_html(build_dir / "validation.html", persisted_validation)
    write_json(provenance_path, provenance)
    manifest.setdefault("artifacts", {})["slicer_validation"] = slicer_path.name
    manifest.setdefault("artifacts", {})["bambu_studio_validation"] = bambu_path.name
    manifest.setdefault("sha256", {})["slicer_validation"] = sha256_file(slicer_path)
    manifest.setdefault("sha256", {})["bambu_studio_validation"] = sha256_file(bambu_path)
    manifest["sha256"]["validation_json"] = sha256_file(validation_path)
    manifest["sha256"]["validation_html"] = sha256_file(build_dir / "validation.html")
    manifest["sha256"]["provenance"] = sha256_file(provenance_path)
    write_json(manifest_path, manifest)
    verify_artifact_bundle(build_dir)
    if (
        manufacturing_validation["required"]
        and slicer_name == "BambuStudio"
        and not manufacturing_validation["release_gate_passed"]
    ):
        raise MeshValidationError(
            "Bambu Lab P2S release gate failed; inspect bambu_studio_validation.json"
        )
    return slicer_path
