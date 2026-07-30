"""Atomic end-to-end local raster terrain builds."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import trimesh
import yaml
from PIL import Image

from topoforge import __version__
from topoforge.config import dump_resolved_config
from topoforge.exceptions import ConfigurationError, MeshValidationError
from topoforge.exporters import export_glb, export_stl
from topoforge.exporters.three_mf import ThreeMFInspection, export_3mf, inspect_3mf
from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.models import BuildConfig
from topoforge.provenance import write_json, write_validation_html
from topoforge.raster import process_local_raster
from topoforge.rendering import render_elevation_preview
from topoforge.scaling import apply_vertical_scale, resolve_scaling
from topoforge.util import sha256_file
from topoforge.validation import validate_mesh


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
    try:
        processed = process_local_raster(stage_config)
        scaling = resolve_scaling(processed.elevations_m, processed.report, resolved_config)
        top_z_mm = apply_vertical_scale(processed.elevations_m, scaling)
        mesh = build_rectangular_terrain_mesh(
            top_z_mm,
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
        validation["triangle_budget"] = config.max_grid_cells * 4 + 16
        validation["triangle_budget_passed"] = int(validation["triangle_count"]) <= int(
            validation["triangle_budget"]
        )
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
            }
            validation["three_mf_dimensions_match"] = bool(
                np.allclose(three_mf.dimensions_mm, expected_dimensions, atol=0.05, rtol=0.0)
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
        generated_at = datetime.now(UTC).isoformat()
        provenance: dict[str, Any] = {
            "topoforge_version": __version__,
            "generated_at": generated_at,
            "provider_selection": {
                "selected": "local",
                "reason": [
                    "user supplied a local elevation raster",
                    "local inputs have priority over network providers",
                    "no provider fallback was required",
                ],
                "attempted_providers": [{"provider": "local", "status": "selected"}],
            },
            "dataset": processed.report.metadata.model_dump(mode="json"),
            "source_file_checksums": processed.report.metadata.checksums,
            "processing": {
                "pipeline": [
                    "read metadata",
                    "normalize to a north-up metric CRS when required",
                    "enforce cell budget using average resampling",
                    "preserve original NoData mask",
                    "interpolate bounded interior holes from nearest valid cells",
                    "write processed DEM",
                ],
                "processed_crs": processed.report.crs,
                "automatic_projection_choice": processed.report.crs,
                "array_shape": processed.report.array_shape,
                "horizontal_resolution_m": processed.report.metadata.horizontal_resolution_m,
                "nodata_percentage": processed.report.original_nodata_fraction * 100.0,
                "interpolated_percentage": processed.report.interpolated_fraction * 100.0,
                "original_nodata_mask": "original_nodata_mask.tif",
                "elevation_min_m": processed.report.elevation_min_m,
                "elevation_max_m": processed.report.elevation_max_m,
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
        write_json(artifacts["validation_json"], validation)
        write_validation_html(artifacts["validation_html"], validation)
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
        verify_artifact_bundle(stage, required_formats=config.output_formats)
        stage.replace(output)
        final_artifacts = {key: output / path.name for key, path in artifacts.items()}
        return BuildResult(output, final_artifacts, validation, provenance)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_artifact_bundle(
    output_dir: Path, required_formats: list[str] | None = None
) -> dict[str, Any]:
    """Reopen every required output role and return literal verification measurements."""
    formats = required_formats or ["stl", "3mf", "glb"]
    required = [
        "processed_dem.tif",
        "original_nodata_mask.tif",
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
    missing = [
        name
        for name in required
        if not (output_dir / name).is_file() or (output_dir / name).stat().st_size == 0
    ]
    if missing:
        raise MeshValidationError(
            f"Artifact bundle is missing non-empty files: {', '.join(missing)}"
        )

    with rasterio.open(output_dir / "processed_dem.tif") as dataset:
        raster_shape = (dataset.height, dataset.width)
        raster_crs = str(dataset.crs)
        if dataset.count != 1 or dataset.crs is None or not np.all(np.isfinite(dataset.read(1))):
            raise MeshValidationError(
                "Reopened processed_dem.tif failed finite single-band CRS checks"
            )
    with rasterio.open(output_dir / "original_nodata_mask.tif") as dataset:
        mask_values = np.unique(dataset.read(1))
        if not set(int(value) for value in mask_values).issubset({0, 1}):
            raise MeshValidationError("Reopened original_nodata_mask.tif is not binary")
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    validation = json.loads((output_dir / "validation.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "build_manifest.json").read_text(encoding="utf-8"))
    manifest_artifacts = manifest.get("artifacts", {})
    manifest_checksums = manifest.get("sha256", {})
    if not isinstance(manifest_artifacts, dict) or not isinstance(manifest_checksums, dict):
        raise MeshValidationError("build_manifest.json artifact/checksum maps are invalid")
    for role, expected_sha256 in manifest_checksums.items():
        artifact_name = manifest_artifacts.get(role)
        if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
            raise MeshValidationError(f"Manifest role {role!r} has an unsafe artifact path")
        artifact_path = output_dir / artifact_name
        if not artifact_path.is_file():
            raise MeshValidationError(f"Manifest role {role!r} points to a missing artifact")
        actual_sha256 = sha256_file(artifact_path)
        if actual_sha256 != expected_sha256:
            raise MeshValidationError(
                f"Manifest checksum mismatch for {role}: expected {expected_sha256}, "
                f"found {actual_sha256}"
            )
    resolved = yaml.safe_load(
        (output_dir / "build_config.resolved.yaml").read_text(encoding="utf-8")
    )
    with Image.open(output_dir / "preview.png") as preview:
        preview.verify()
    if "stl" in formats:
        reopened_stl = _load_stl(output_dir / "model.stl")
        if not reopened_stl.is_watertight:
            raise MeshValidationError("Reopened model.stl is not watertight")
    if "3mf" in formats:
        inspect_3mf(output_dir / "model.3mf")
    if "glb" in formats:
        glb = trimesh.load(output_dir / "preview.glb", force="scene")
        if not isinstance(glb, trimesh.Scene) or len(glb.geometry) == 0:
            raise MeshValidationError("Reopened preview.glb has no geometry")
    return {
        "raster_shape": raster_shape,
        "raster_crs": raster_crs,
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
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slicer_path = build_dir / "slicer_validation.json"
    write_json(slicer_path, result)
    validation["slicer_result"] = result
    provenance["slicer_validation"] = {
        "slicer": result.get("slicer"),
        "profile": result.get("profile"),
        "success": result.get("status") == "succeeded",
        "gcode_generated": result.get("gcode_generated"),
        "gcode_size_bytes": result.get("gcode_size_bytes"),
        "metrics": result.get("metrics"),
        "report": slicer_path.name,
    }
    write_json(validation_path, validation)
    write_validation_html(build_dir / "validation.html", validation)
    write_json(provenance_path, provenance)
    manifest.setdefault("artifacts", {})["slicer_validation"] = slicer_path.name
    manifest.setdefault("sha256", {})["slicer_validation"] = sha256_file(slicer_path)
    manifest["sha256"]["validation_json"] = sha256_file(validation_path)
    manifest["sha256"]["validation_html"] = sha256_file(build_dir / "validation.html")
    manifest["sha256"]["provenance"] = sha256_file(provenance_path)
    write_json(manifest_path, manifest)
    verify_artifact_bundle(build_dir)
    return slicer_path
