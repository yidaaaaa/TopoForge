"""Deterministic per-tile meshes and shared-frame assembly verification."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Self

import numpy as np
import rasterio
import trimesh
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError, MeshValidationError, RasterProcessingError
from topoforge.exporters import export_glb, export_stl
from topoforge.exporters.three_mf import ThreeMFInspection, export_3mf, inspect_3mf
from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.models import ScalingResult
from topoforge.scaling import apply_vertical_scale
from topoforge.tiling.extract import AssemblyManifest, verify_tile_set
from topoforge.tiling.layout import (
    TerrainTile,
    TileLayout,
    canonical_tile_layout_bytes,
    read_tile_layout,
)
from topoforge.validation import ValidationReport, validate_mesh

_TILE_MESH_SCHEMA_VERSION = "topoforge-tile-mesh-artifact-v1"
_TILE_MESH_ASSEMBLY_SCHEMA_VERSION = "topoforge-tile-mesh-assembly-v1"
_TILE_MESH_ASSEMBLY_VALIDATION_SCHEMA_VERSION = "topoforge-tile-mesh-assembly-validation-v1"
_BOUNDS_TOLERANCE_MM = 0.001
_SEAM_TOLERANCE_MM = 0.001
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundsMm = tuple[float, float, float, float, float, float]


class TileMeshValidation(BaseModel):
    """Strictly reopened geometry and format checks for one global-frame tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_MESH_SCHEMA_VERSION
    tile_id: str
    layout_id: str
    core_sample_shape: tuple[int, int]
    physical_bounds_mm: dict[str, float]
    expected_global_bounds_mm: BoundsMm
    format_bounds_mm: dict[str, BoundsMm]
    expected_peak_coordinate_mm: tuple[float, float, float]
    format_peak_coordinates_mm: dict[str, tuple[float, float, float]]
    expected_triangle_count: int = Field(gt=0)
    format_triangle_counts: dict[str, int]
    geometry: ValidationReport
    strict_3mf_warning_count: int = Field(ge=0)
    bounds_tolerance_mm: float = Field(ge=0)
    bounds_match: bool
    format_triangle_counts_match: bool
    peak_coordinates_match: bool
    orientation_consistent: bool
    required_checks_passed: bool


class TileMeshArtifactManifest(BaseModel):
    """Checksummed mesh roles derived from one exact raster tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_MESH_SCHEMA_VERSION
    tile_id: str
    tile_key: str
    layout_id: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    source_tile_manifest_sha256: Sha256Hex
    source_processed_dem_sha256: Sha256Hex
    physical_bounds_mm: dict[str, float]
    files: dict[str, str]
    sha256: dict[str, Sha256Hex]
    validation: TileMeshValidation

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        required = {"model_stl", "model_3mf", "preview_glb", "validation"}
        if set(self.files) != required or set(self.sha256) != required:
            raise ValueError("tile mesh manifest must contain the complete v1 role set")
        if any(Path(value).name != value for value in self.files.values()):
            raise ValueError("tile mesh artifact paths must be local filenames")
        return self


class TileMeshAssemblyTileRecord(BaseModel):
    """Root assembly record for one global-frame tile mesh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_id: str
    tile_key: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    directory: str
    tile_mesh_manifest: str
    tile_mesh_manifest_sha256: Sha256Hex
    source_tile_manifest_sha256: Sha256Hex
    files: dict[str, str]
    sha256: dict[str, Sha256Hex]
    physical_bounds_mm: dict[str, float]
    global_bounds_mm: BoundsMm
    triangle_count: int = Field(gt=0)
    volume_mm3: float = Field(gt=0)


class TileMeshSeamComparison(BaseModel):
    """Reopened STL boundary continuity for one adjacent tile pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seam_id: str
    direction: str
    first_tile_id: str
    second_tile_id: str
    expected_sample_count: int = Field(gt=0)
    first_sample_count: int = Field(ge=0)
    second_sample_count: int = Field(ge=0)
    maximum_planar_alignment_error_mm: float = Field(ge=0)
    maximum_z_difference_mm: float = Field(ge=0)
    z_mismatch_count: int = Field(ge=0)
    required_checks_passed: bool


class TileMeshAssemblyValidation(BaseModel):
    """Measured global footprint, volume, and mesh-boundary assembly evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_MESH_ASSEMBLY_VALIDATION_SCHEMA_VERSION
    layout_id: str
    tile_count: int = Field(gt=0)
    seam_count: int = Field(ge=0)
    source_model_bounds_mm: BoundsMm
    assembled_bounds_mm: BoundsMm
    source_model_volume_mm3: float = Field(gt=0)
    tile_volume_sum_mm3: float = Field(gt=0)
    volume_difference_mm3: float = Field(ge=0)
    volume_tolerance_mm3: float = Field(gt=0)
    expected_footprint_area_mm2: float = Field(gt=0)
    tile_footprint_area_sum_mm2: float = Field(gt=0)
    footprint_overlap_area_mm2: float = Field(ge=0)
    maximum_planar_alignment_error_mm: float = Field(ge=0)
    maximum_top_seam_gap_mm: float = Field(ge=0)
    total_top_seam_mismatch_count: int = Field(ge=0)
    bounds_tolerance_mm: float = Field(ge=0)
    seam_tolerance_mm: float = Field(ge=0)
    coverage_image_size_px: tuple[int, int]
    all_tile_checks_passed: bool
    global_bounds_match: bool
    footprint_partition_passed: bool
    volume_match: bool
    mesh_seam_status: str
    required_checks_passed: bool
    seams: list[TileMeshSeamComparison]


class TileMeshAssemblyManifest(BaseModel):
    """Canonical root binding source tile evidence to all derived mesh roles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_MESH_ASSEMBLY_SCHEMA_VERSION
    layout_id: str
    layout_path: str = "tile-layout.json"
    layout_sha256: Sha256Hex
    source_tile_set_assembly_sha256: Sha256Hex
    source_tile_set_seam_report_sha256: Sha256Hex
    source_bundle_manifest_sha256: Sha256Hex
    source_model_stl_sha256: Sha256Hex
    scaling: ScalingResult
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    row_origin: str = "north"
    column_origin: str = "west"
    east_axis: str = "+X = East"
    north_axis: str = "+Y = North"
    up_axis: str = "+Z = Up"
    global_origin_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    coverage_image_path: str = "tile-coverage.png"
    coverage_image_sha256: Sha256Hex
    assembly_validation_path: str = "tile-mesh-assembly-validation.json"
    assembly_validation_sha256: Sha256Hex
    tiles: list[TileMeshAssemblyTileRecord]

    @model_validator(mode="after")
    def validate_tiles(self) -> Self:
        rows, columns = self.tile_grid_shape
        expected = [(row, column) for row in range(rows) for column in range(columns)]
        actual = [(tile.row, tile.column) for tile in self.tiles]
        if self.tile_count != rows * columns or len(self.tiles) != self.tile_count:
            raise ValueError("tile mesh assembly count does not match tile grid")
        if actual != expected or len({tile.tile_id for tile in self.tiles}) != self.tile_count:
            raise ValueError("tile mesh records must be unique and in row-major order")
        return self


class TileMeshAssemblyResult(BaseModel):
    """Published paths for one deterministic global-frame tile mesh set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    assembly_manifest_path: Path
    assembly_validation_path: Path
    coverage_image_path: Path
    tile_mesh_manifest_paths: list[Path]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_bytes(value: BaseModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_canonical_json(path: Path, value: BaseModel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical_json_bytes(value))
    temporary.replace(path)
    return path


def _read_canonical_json(path: Path, model: type[BaseModel]) -> BaseModel:
    value = model.model_validate_json(path.read_text(encoding="utf-8"))
    if path.read_bytes() != _canonical_json_bytes(value):
        raise ConfigurationError(f"JSON is not canonical: {path}")
    return value


def _resolve_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ConfigurationError(f"mesh assembly path escapes output directory: {relative}")
    return candidate


def _bounds(mesh: trimesh.Trimesh) -> BoundsMm:
    values = np.asarray(mesh.bounds, dtype=np.float64)
    return (
        float(values[0, 0]),
        float(values[0, 1]),
        float(values[0, 2]),
        float(values[1, 0]),
        float(values[1, 1]),
        float(values[1, 2]),
    )


def _inspection_bounds(inspection: ThreeMFInspection) -> BoundsMm:
    minimum, maximum = inspection.bounds_mm
    return (*minimum, *maximum)


def _load_stl(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="stl", force="mesh", process=True)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"reopened tile STL is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _load_glb(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="glb", force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"reopened tile GLB is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _physical_bounds(tile: TerrainTile) -> dict[str, float]:
    return tile.physical_bounds_mm.model_dump(mode="json")


def _core_values(tile: TerrainTile, dem_path: Path) -> np.ndarray[Any, Any]:
    with rasterio.open(dem_path) as dataset:
        expected_shape = (
            tile.sampling_window.row_stop - tile.sampling_window.row_start,
            tile.sampling_window.column_stop - tile.sampling_window.column_start,
        )
        if dataset.count != 1 or dataset.crs is None or dataset.shape != expected_shape:
            raise RasterProcessingError(f"tile DEM is invalid for mesh generation: {tile.tile_id}")
        values = dataset.read(1)
    row_start = tile.core_sample_window.row_start - tile.sampling_window.row_start
    row_stop = tile.core_sample_window.row_stop - tile.sampling_window.row_start
    column_start = tile.core_sample_window.column_start - tile.sampling_window.column_start
    column_stop = tile.core_sample_window.column_stop - tile.sampling_window.column_start
    core = values[row_start:row_stop, column_start:column_stop]
    if core.shape != (
        tile.core_sample_window.row_stop - tile.core_sample_window.row_start,
        tile.core_sample_window.column_stop - tile.core_sample_window.column_start,
    ) or not np.all(np.isfinite(core)):
        raise RasterProcessingError(f"tile core DEM is invalid for mesh generation: {tile.tile_id}")
    return core


def _expected_peak(
    tile: TerrainTile,
    core_elevations_m: np.ndarray[Any, Any],
    top_z_mm: np.ndarray[Any, Any],
) -> tuple[float, float, float]:
    rows, columns = core_elevations_m.shape
    peak_index = int(np.argmax(core_elevations_m))
    row, column = np.unravel_index(peak_index, core_elevations_m.shape)
    bounds = tile.physical_bounds_mm
    x = bounds.x_min + (bounds.x_max - bounds.x_min) * column / max(columns - 1, 1)
    y = bounds.y_max - (bounds.y_max - bounds.y_min) * row / max(rows - 1, 1)
    return float(x), float(y), float(top_z_mm[row, column])


def _closest_peak(
    mesh: trimesh.Trimesh, expected: tuple[float, float, float]
) -> tuple[float, float, float]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    maximum_z = float(np.max(vertices[:, 2]))
    candidates = vertices[np.isclose(vertices[:, 2], maximum_z, atol=1e-6, rtol=0.0)]
    distances = np.square(candidates[:, 0] - expected[0]) + np.square(
        candidates[:, 1] - expected[1]
    )
    peak = candidates[int(np.argmin(distances))]
    return float(peak[0]), float(peak[1]), float(peak[2])


def _closest_3mf_peak(
    inspection: ThreeMFInspection, expected: tuple[float, float, float]
) -> tuple[float, float, float]:
    return min(
        inspection.peak_coordinates_mm,
        key=lambda value: (value[0] - expected[0]) ** 2 + (value[1] - expected[1]) ** 2,
    )


def _required_geometry(report: ValidationReport) -> bool:
    return bool(
        report.finite_vertices
        and report.finite_face_normals
        and report.watertight
        and report.winding_consistent
        and report.manifold
        and report.positive_volume
        and report.flat_bottom
        and report.dimensions_within_tolerance
        and report.connected_components == 1
        and report.degenerate_faces == 0
        and report.duplicate_faces == 0
        and (report.bottom_planarity_error_mm or 0.0) <= 0.01
    )


def _measure_tile_validation(
    tile: TerrainTile,
    *,
    core_elevations_m: np.ndarray[Any, Any],
    scaling: ScalingResult,
    stl_path: Path,
    three_mf_path: Path,
    glb_path: Path,
) -> TileMeshValidation:
    top_z_mm = apply_vertical_scale(core_elevations_m.astype(np.float32), scaling)
    bounds = tile.physical_bounds_mm
    expected_bounds: BoundsMm = (
        bounds.x_min,
        bounds.y_min,
        0.0,
        bounds.x_max,
        bounds.y_max,
        float(np.max(top_z_mm)),
    )
    expected_dimensions = (
        bounds.x_max - bounds.x_min,
        bounds.y_max - bounds.y_min,
        expected_bounds[5],
    )
    stl = _load_stl(stl_path)
    glb = _load_glb(glb_path)
    three_mf = inspect_3mf(three_mf_path)
    geometry = validate_mesh(
        stl,
        expected_dimensions_mm=expected_dimensions,
        dimension_tolerance_mm=_BOUNDS_TOLERANCE_MM,
        flat_bottom_tolerance_mm=0.01,
    )
    expected_triangle_count = int(
        4 * (core_elevations_m.shape[0] - 1) * (core_elevations_m.shape[1] - 1)
        + 4 * (core_elevations_m.shape[0] + core_elevations_m.shape[1] - 2)
    )
    format_bounds = {
        "stl": _bounds(stl),
        "3mf": _inspection_bounds(three_mf),
        "glb": _bounds(glb),
    }
    expected_peak = _expected_peak(tile, core_elevations_m, top_z_mm)
    format_peaks = {
        "stl": _closest_peak(stl, expected_peak),
        "3mf": _closest_3mf_peak(three_mf, expected_peak),
        "glb": _closest_peak(glb, expected_peak),
    }
    triangle_counts = {
        "stl": len(stl.faces),
        "3mf": int(three_mf.triangle_count),
        "glb": len(glb.faces),
    }
    bounds_match = all(
        np.allclose(value, expected_bounds, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
        for value in format_bounds.values()
    )
    triangle_counts_match = all(
        value == expected_triangle_count for value in triangle_counts.values()
    )
    peaks_match = all(
        np.allclose(value, expected_peak, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
        for value in format_peaks.values()
    )
    metadata = three_mf.metadata
    orientation_consistent = (
        metadata.get("customXMLNS0:tile_id") == tile.tile_id
        and metadata.get("customXMLNS0:east_axis") == "+X = East"
        and metadata.get("customXMLNS0:north_axis") == "+Y = North"
        and metadata.get("customXMLNS0:up_axis") == "+Z = Up"
    )
    required = bool(
        _required_geometry(geometry)
        and bounds_match
        and triangle_counts_match
        and peaks_match
        and orientation_consistent
        and three_mf.strict_warning_count == 0
    )
    return TileMeshValidation(
        tile_id=tile.tile_id,
        layout_id=tile.tile_key.split("/", maxsplit=1)[0],
        core_sample_shape=core_elevations_m.shape,
        physical_bounds_mm=_physical_bounds(tile),
        expected_global_bounds_mm=expected_bounds,
        format_bounds_mm=format_bounds,
        expected_peak_coordinate_mm=expected_peak,
        format_peak_coordinates_mm=format_peaks,
        expected_triangle_count=expected_triangle_count,
        format_triangle_counts=triangle_counts,
        geometry=geometry,
        strict_3mf_warning_count=three_mf.strict_warning_count,
        bounds_tolerance_mm=_BOUNDS_TOLERANCE_MM,
        bounds_match=bounds_match,
        format_triangle_counts_match=triangle_counts_match,
        peak_coordinates_match=peaks_match,
        orientation_consistent=orientation_consistent,
        required_checks_passed=required,
    )


def _render_coverage(layout: TileLayout, path: Path, *, width_px: int = 1200) -> tuple[int, int]:
    header = 86
    margin = 64
    plot_width = width_px - 2 * margin
    model_width, model_depth = layout.model_size_mm
    plot_height = max(320, round(plot_width * model_depth / model_width))
    height_px = header + plot_height + 2 * margin
    image = Image.new("RGB", (width_px, height_px), (244, 246, 247))
    draw = ImageDraw.Draw(image)
    draw.text((margin, 22), f"TopoForge tile coverage: {layout.layout_id}", fill=(24, 31, 36))
    draw.text(
        (margin, 50),
        (
            f"{layout.tile_grid_shape[0]} rows x {layout.tile_grid_shape[1]} columns "
            "| +X East | +Y North"
        ),
        fill=(68, 78, 85),
    )
    palette = ((47, 111, 78), (43, 106, 155), (190, 125, 36), (154, 70, 82))
    left = margin
    top = header + margin
    for tile in layout.tiles:
        bounds = tile.physical_bounds_mm
        x0 = left + round(plot_width * bounds.x_min / model_width)
        x1 = left + round(plot_width * bounds.x_max / model_width)
        y0 = top + round(plot_height * (1.0 - bounds.y_max / model_depth))
        y1 = top + round(plot_height * (1.0 - bounds.y_min / model_depth))
        color = palette[(tile.row + tile.column) % len(palette)]
        draw.rectangle((x0, y0, x1, y1), fill=color, outline=(24, 31, 36), width=3)
        label_box = draw.textbbox((0, 0), tile.tile_id)
        label_width = label_box[2] - label_box[0]
        label_height = label_box[3] - label_box[1]
        if x1 - x0 >= label_width + 12 and y1 - y0 >= label_height + 12:
            draw.text(
                ((x0 + x1 - label_width) / 2, (y0 + y1 - label_height) / 2),
                tile.tile_id,
                fill=(255, 255, 255),
            )
    north_x = width_px - margin
    north_tip = top
    north_tail = top + 56
    draw.line((north_x, north_tail, north_x, north_tip), fill=(24, 31, 36), width=4)
    draw.polygon(
        ((north_x, north_tip), (north_x - 9, north_tip + 16), (north_x + 9, north_tip + 16)),
        fill=(24, 31, 36),
    )
    draw.text((north_x - 5, north_tail + 4), "N", fill=(24, 31, 36))
    draw.line(
        (left, top + plot_height + 24, left + 70, top + plot_height + 24),
        fill=(24, 31, 36),
        width=4,
    )
    draw.polygon(
        (
            (left + 70, top + plot_height + 24),
            (left + 54, top + plot_height + 15),
            (left + 54, top + plot_height + 33),
        ),
        fill=(24, 31, 36),
    )
    draw.text((left + 78, top + plot_height + 18), "East", fill=(24, 31, 36))
    temporary = path.with_name(f".{path.name}.tmp")
    image.save(temporary, format="PNG", optimize=True)
    temporary.replace(path)
    with Image.open(path) as reopened:
        reopened.verify()
    return width_px, height_px


def _boundary_samples(
    mesh: trimesh.Trimesh,
    *,
    fixed_axis: int,
    fixed_value: float,
    along_axis: int,
) -> np.ndarray[Any, Any]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    selected = vertices[
        np.isclose(vertices[:, fixed_axis], fixed_value, atol=_SEAM_TOLERANCE_MM, rtol=0.0)
        & (vertices[:, 2] > _SEAM_TOLERANCE_MM)
    ][:, (along_axis, 2)]
    if len(selected) == 0:
        return selected
    selected = np.unique(selected, axis=0)
    return selected[np.argsort(selected[:, 0], kind="stable")]


def _mesh_seam(
    first: TerrainTile,
    second: TerrainTile,
    *,
    direction: str,
    first_mesh: trimesh.Trimesh,
    second_mesh: trimesh.Trimesh,
) -> TileMeshSeamComparison:
    if direction == "east-west":
        first_samples = _boundary_samples(
            first_mesh,
            fixed_axis=0,
            fixed_value=first.physical_bounds_mm.x_max,
            along_axis=1,
        )
        second_samples = _boundary_samples(
            second_mesh,
            fixed_axis=0,
            fixed_value=second.physical_bounds_mm.x_min,
            along_axis=1,
        )
        expected = first.core_sample_window.row_stop - first.core_sample_window.row_start
        fixed_error = abs(first.physical_bounds_mm.x_max - second.physical_bounds_mm.x_min)
    else:
        first_samples = _boundary_samples(
            first_mesh,
            fixed_axis=1,
            fixed_value=first.physical_bounds_mm.y_min,
            along_axis=0,
        )
        second_samples = _boundary_samples(
            second_mesh,
            fixed_axis=1,
            fixed_value=second.physical_bounds_mm.y_max,
            along_axis=0,
        )
        expected = first.core_sample_window.column_stop - first.core_sample_window.column_start
        fixed_error = abs(first.physical_bounds_mm.y_min - second.physical_bounds_mm.y_max)
    comparable = len(first_samples) == len(second_samples) and len(first_samples) > 0
    if comparable:
        planar_error = max(
            fixed_error, float(np.max(np.abs(first_samples[:, 0] - second_samples[:, 0])))
        )
        differences = np.abs(first_samples[:, 1] - second_samples[:, 1])
        z_error = float(np.max(differences))
        mismatches = int(np.count_nonzero(differences > _SEAM_TOLERANCE_MM))
    else:
        planar_error = fixed_error
        z_error = 0.0
        mismatches = max(len(first_samples), len(second_samples), expected)
    passed = bool(
        comparable
        and len(first_samples) == expected
        and planar_error <= _SEAM_TOLERANCE_MM
        and mismatches == 0
    )
    return TileMeshSeamComparison(
        seam_id=f"mesh-seam-{first.tile_id}-{direction}-{second.tile_id}",
        direction=direction,
        first_tile_id=first.tile_id,
        second_tile_id=second.tile_id,
        expected_sample_count=expected,
        first_sample_count=len(first_samples),
        second_sample_count=len(second_samples),
        maximum_planar_alignment_error_mm=planar_error,
        maximum_z_difference_mm=z_error,
        z_mismatch_count=mismatches,
        required_checks_passed=passed,
    )


def _overlap_area(first: TerrainTile, second: TerrainTile) -> float:
    x = max(
        0.0,
        min(first.physical_bounds_mm.x_max, second.physical_bounds_mm.x_max)
        - max(first.physical_bounds_mm.x_min, second.physical_bounds_mm.x_min),
    )
    y = max(
        0.0,
        min(first.physical_bounds_mm.y_max, second.physical_bounds_mm.y_max)
        - max(first.physical_bounds_mm.y_min, second.physical_bounds_mm.y_min),
    )
    return x * y


def _measure_assembly(
    layout: TileLayout,
    *,
    tile_meshes: dict[str, trimesh.Trimesh],
    tile_validations: dict[str, TileMeshValidation],
    source_model: trimesh.Trimesh,
    coverage_image_size_px: tuple[int, int],
) -> TileMeshAssemblyValidation:
    tile_by_id = {tile.tile_id: tile for tile in layout.tiles}
    seams: list[TileMeshSeamComparison] = []
    for tile in layout.tiles:
        for neighbor_id, direction in (
            (tile.east_neighbor, "east-west"),
            (tile.south_neighbor, "north-south"),
        ):
            if neighbor_id is not None:
                seams.append(
                    _mesh_seam(
                        tile,
                        tile_by_id[neighbor_id],
                        direction=direction,
                        first_mesh=tile_meshes[tile.tile_id],
                        second_mesh=tile_meshes[neighbor_id],
                    )
                )
    all_bounds = np.asarray([_bounds(mesh) for mesh in tile_meshes.values()], dtype=np.float64)
    assembled_bounds: BoundsMm = (
        float(np.min(all_bounds[:, 0])),
        float(np.min(all_bounds[:, 1])),
        float(np.min(all_bounds[:, 2])),
        float(np.max(all_bounds[:, 3])),
        float(np.max(all_bounds[:, 4])),
        float(np.max(all_bounds[:, 5])),
    )
    source_bounds = _bounds(source_model)
    source_volume = float(source_model.volume)
    tile_volume_sum = float(sum(float(mesh.volume) for mesh in tile_meshes.values()))
    volume_difference = abs(tile_volume_sum - source_volume)
    volume_tolerance = max(0.05, source_volume * 1e-5)
    expected_area = layout.model_size_mm[0] * layout.model_size_mm[1]
    tile_area_sum = sum(
        (tile.physical_bounds_mm.x_max - tile.physical_bounds_mm.x_min)
        * (tile.physical_bounds_mm.y_max - tile.physical_bounds_mm.y_min)
        for tile in layout.tiles
    )
    footprint_overlap = sum(
        _overlap_area(first, second)
        for index, first in enumerate(layout.tiles)
        for second in layout.tiles[index + 1 :]
    )
    global_bounds_match = bool(
        np.allclose(assembled_bounds, source_bounds, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
    )
    footprint_passed = bool(
        abs(tile_area_sum - expected_area) <= _BOUNDS_TOLERANCE_MM
        and footprint_overlap <= _BOUNDS_TOLERANCE_MM
    )
    volume_match = volume_difference <= volume_tolerance
    all_tiles = all(item.required_checks_passed for item in tile_validations.values())
    seam_passed = all(item.required_checks_passed for item in seams)
    required = bool(
        all_tiles and seam_passed and global_bounds_match and footprint_passed and volume_match
    )
    return TileMeshAssemblyValidation(
        layout_id=layout.layout_id,
        tile_count=layout.tile_count,
        seam_count=len(seams),
        source_model_bounds_mm=source_bounds,
        assembled_bounds_mm=assembled_bounds,
        source_model_volume_mm3=source_volume,
        tile_volume_sum_mm3=tile_volume_sum,
        volume_difference_mm3=volume_difference,
        volume_tolerance_mm3=volume_tolerance,
        expected_footprint_area_mm2=expected_area,
        tile_footprint_area_sum_mm2=tile_area_sum,
        footprint_overlap_area_mm2=footprint_overlap,
        maximum_planar_alignment_error_mm=max(
            (item.maximum_planar_alignment_error_mm for item in seams), default=0.0
        ),
        maximum_top_seam_gap_mm=max((item.maximum_z_difference_mm for item in seams), default=0.0),
        total_top_seam_mismatch_count=sum(item.z_mismatch_count for item in seams),
        bounds_tolerance_mm=_BOUNDS_TOLERANCE_MM,
        seam_tolerance_mm=_SEAM_TOLERANCE_MM,
        coverage_image_size_px=coverage_image_size_px,
        all_tile_checks_passed=all_tiles,
        global_bounds_match=global_bounds_match,
        footprint_partition_passed=footprint_passed,
        volume_match=volume_match,
        mesh_seam_status="passed" if seam_passed else "failed",
        required_checks_passed=required,
        seams=seams,
    )


def _source_records(
    tile_set: Path, bundle: Path
) -> tuple[TileLayout, AssemblyManifest, ScalingResult, dict[str, Any], dict[str, Any]]:
    evidence = verify_tile_set(tile_set, bundle)
    if not evidence.get("seam_report_present") or evidence.get("terrain_seam_status") != "passed":
        raise ConfigurationError(
            "tile mesh generation requires a checksummed passing seam_report.json"
        )
    layout = read_tile_layout(tile_set / "tile-layout.json")
    assembly = AssemblyManifest.model_validate_json(
        (tile_set / "assembly_manifest.json").read_text(encoding="utf-8")
    )
    provenance = json.loads((bundle / "provenance.json").read_text(encoding="utf-8"))
    scaling_value = provenance.get("scaling")
    if not isinstance(scaling_value, dict):
        raise ConfigurationError("source provenance.json does not contain scaling evidence")
    scaling = ScalingResult.model_validate(scaling_value)
    build_manifest = json.loads((bundle / "build_manifest.json").read_text(encoding="utf-8"))
    return layout, assembly, scaling, provenance, build_manifest


def _tile_mesh_record(
    tile: TerrainTile,
    *,
    layout: TileLayout,
    source_record: Any,
    tile_set: Path,
    scaling: ScalingResult,
    output_tile_dir: Path,
) -> tuple[TileMeshArtifactManifest, TileMeshAssemblyTileRecord, trimesh.Trimesh]:
    dem_path = _resolve_relative(tile_set, source_record.files["processed_dem"])
    core = _core_values(tile, dem_path)
    top_z_mm = apply_vertical_scale(core.astype(np.float32), scaling)
    bounds = tile.physical_bounds_mm
    mesh = build_rectangular_terrain_mesh(
        np.flipud(top_z_mm),
        width_mm=bounds.x_max - bounds.x_min,
        depth_mm=bounds.y_max - bounds.y_min,
        base_thickness_mm=scaling.base_thickness_mm,
    )
    mesh.apply_translation((bounds.x_min, bounds.y_min, 0.0))
    output_tile_dir.mkdir(parents=True)
    stl_path = export_stl(mesh, output_tile_dir / "model.global.stl")
    three_mf_path = export_3mf(
        mesh,
        output_tile_dir / "model.global.3mf",
        object_name=f"TopoForge {tile.tile_id}",
        metadata={
            "layout_id": layout.layout_id,
            "tile_id": tile.tile_id,
            "tile_key": tile.tile_key,
            "east_axis": "+X = East",
            "north_axis": "+Y = North",
            "up_axis": "+Z = Up",
            "global_origin_mm": "[0,0,0]",
            "physical_bounds_mm": json.dumps(
                _physical_bounds(tile), sort_keys=True, separators=(",", ":")
            ),
            "core_sample_window": tile.core_sample_window.model_dump_json(),
            "orientation_transform": (
                "tile_heightfield=flipud(core_processed_dem); translated to global physical bounds"
            ),
        },
    )
    glb_path = export_glb(mesh, output_tile_dir / "preview.global.glb")
    validation = _measure_tile_validation(
        tile,
        core_elevations_m=core,
        scaling=scaling,
        stl_path=stl_path,
        three_mf_path=three_mf_path,
        glb_path=glb_path,
    )
    if not validation.required_checks_passed:
        raise MeshValidationError(f"tile mesh validation failed: {tile.tile_id}")
    validation_path = _write_canonical_json(
        output_tile_dir / "tile_mesh_validation.json", validation
    )
    files = {
        "model_stl": stl_path.name,
        "model_3mf": three_mf_path.name,
        "preview_glb": glb_path.name,
        "validation": validation_path.name,
    }
    checksums = {role: _sha256(output_tile_dir / name) for role, name in files.items()}
    manifest = TileMeshArtifactManifest(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        layout_id=layout.layout_id,
        row=tile.row,
        column=tile.column,
        source_tile_manifest_sha256=source_record.tile_manifest_sha256,
        source_processed_dem_sha256=source_record.sha256["processed_dem"],
        physical_bounds_mm=_physical_bounds(tile),
        files=files,
        sha256=checksums,
        validation=validation,
    )
    manifest_path = _write_canonical_json(output_tile_dir / "tile_mesh_manifest.json", manifest)
    relative_directory = f"tiles/{tile.tile_id}"
    record = TileMeshAssemblyTileRecord(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        row=tile.row,
        column=tile.column,
        directory=relative_directory,
        tile_mesh_manifest=f"{relative_directory}/tile_mesh_manifest.json",
        tile_mesh_manifest_sha256=_sha256(manifest_path),
        source_tile_manifest_sha256=source_record.tile_manifest_sha256,
        files={role: f"{relative_directory}/{name}" for role, name in files.items()},
        sha256=checksums,
        physical_bounds_mm=_physical_bounds(tile),
        global_bounds_mm=validation.expected_global_bounds_mm,
        triangle_count=validation.expected_triangle_count,
        volume_mm3=validation.geometry.volume_mm3,
    )
    return manifest, record, _load_stl(stl_path)


def generate_tile_mesh_set(
    tile_set_dir: Path,
    source_bundle_dir: Path,
    output_dir: Path,
) -> TileMeshAssemblyResult:
    """Generate global-frame mesh roles from a verified raster tile set."""
    tile_set = tile_set_dir.expanduser().resolve()
    bundle = source_bundle_dir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise ConfigurationError(f"tile mesh destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    layout, source_assembly, scaling, _, build_manifest = _source_records(tile_set, bundle)
    source_model_name = build_manifest.get("artifacts", {}).get("model_stl")
    source_model_hash = build_manifest.get("sha256", {}).get("model_stl")
    if not isinstance(source_model_name, str) or not isinstance(source_model_hash, str):
        raise ConfigurationError("source bundle requires a checksummed model_stl role")
    source_model_path = bundle / source_model_name
    if _sha256(source_model_path) != source_model_hash:
        raise ConfigurationError("source model STL checksum does not match build manifest")
    if source_assembly.seam_report_path is None or source_assembly.seam_report_sha256 is None:
        raise ConfigurationError("source tile assembly does not bind seam_report.json")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.topoforge-stage-", dir=output.parent))
    try:
        (staging / "tile-layout.json").write_bytes(canonical_tile_layout_bytes(layout))
        coverage_size = _render_coverage(layout, staging / "tile-coverage.png")
        records: list[TileMeshAssemblyTileRecord] = []
        validations: dict[str, TileMeshValidation] = {}
        tile_meshes: dict[str, trimesh.Trimesh] = {}
        source_record_by_id = {record.tile_id: record for record in source_assembly.tiles}
        for tile in layout.tiles:
            _, record, reopened_mesh = _tile_mesh_record(
                tile,
                layout=layout,
                source_record=source_record_by_id[tile.tile_id],
                tile_set=tile_set,
                scaling=scaling,
                output_tile_dir=staging / "tiles" / tile.tile_id,
            )
            records.append(record)
            validations[tile.tile_id] = TileMeshValidation.model_validate_json(
                (staging / record.files["validation"]).read_text(encoding="utf-8")
            )
            tile_meshes[tile.tile_id] = reopened_mesh
        assembly_validation = _measure_assembly(
            layout,
            tile_meshes=tile_meshes,
            tile_validations=validations,
            source_model=_load_stl(source_model_path),
            coverage_image_size_px=coverage_size,
        )
        if not assembly_validation.required_checks_passed:
            raise MeshValidationError("multi-tile mesh assembly validation failed")
        validation_path = _write_canonical_json(
            staging / "tile-mesh-assembly-validation.json", assembly_validation
        )
        manifest = TileMeshAssemblyManifest(
            layout_id=layout.layout_id,
            layout_sha256=_sha256(staging / "tile-layout.json"),
            source_tile_set_assembly_sha256=_sha256(tile_set / "assembly_manifest.json"),
            source_tile_set_seam_report_sha256=source_assembly.seam_report_sha256,
            source_bundle_manifest_sha256=source_assembly.source_bundle_manifest_sha256,
            source_model_stl_sha256=source_model_hash,
            scaling=scaling,
            tile_grid_shape=layout.tile_grid_shape,
            tile_count=layout.tile_count,
            coverage_image_sha256=_sha256(staging / "tile-coverage.png"),
            assembly_validation_sha256=_sha256(validation_path),
            tiles=records,
        )
        _write_canonical_json(staging / "tile-mesh-assembly-manifest.json", manifest)
        verify_tile_mesh_set(staging, tile_set, bundle)
        staging.replace(output)
        return TileMeshAssemblyResult(
            output_dir=output,
            assembly_manifest_path=output / "tile-mesh-assembly-manifest.json",
            assembly_validation_path=output / "tile-mesh-assembly-validation.json",
            coverage_image_path=output / "tile-coverage.png",
            tile_mesh_manifest_paths=[output / record.tile_mesh_manifest for record in records],
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_tile_mesh_set(
    mesh_set_dir: Path,
    source_tile_set_dir: Path,
    source_bundle_dir: Path,
) -> dict[str, Any]:
    """Strictly reopen all tile mesh roles and remeasure global assembly evidence."""
    root = mesh_set_dir.expanduser().resolve()
    tile_set = source_tile_set_dir.expanduser().resolve()
    bundle = source_bundle_dir.expanduser().resolve()
    required = (
        root / "tile-layout.json",
        root / "tile-coverage.png",
        root / "tile-mesh-assembly-validation.json",
        root / "tile-mesh-assembly-manifest.json",
    )
    if not all(path.is_file() and path.stat().st_size > 0 for path in required):
        raise ConfigurationError("tile mesh set is missing a required root artifact")
    layout = read_tile_layout(root / "tile-layout.json")
    manifest = _read_canonical_json(
        root / "tile-mesh-assembly-manifest.json", TileMeshAssemblyManifest
    )
    if not isinstance(manifest, TileMeshAssemblyManifest):
        raise AssertionError("unexpected mesh manifest model")
    reported_validation = _read_canonical_json(
        root / "tile-mesh-assembly-validation.json", TileMeshAssemblyValidation
    )
    if not isinstance(reported_validation, TileMeshAssemblyValidation):
        raise AssertionError("unexpected assembly validation model")
    source_layout, source_assembly, scaling, _, build_manifest = _source_records(tile_set, bundle)
    if layout != source_layout:
        raise ConfigurationError("tile mesh layout does not match source tile layout")
    source_model_name = build_manifest.get("artifacts", {}).get("model_stl")
    source_model_hash = build_manifest.get("sha256", {}).get("model_stl")
    if not isinstance(source_model_name, str) or not isinstance(source_model_hash, str):
        raise ConfigurationError("source bundle requires a checksummed model_stl role")
    if (
        manifest.layout_id != layout.layout_id
        or manifest.layout_sha256 != _sha256(root / "tile-layout.json")
        or manifest.source_tile_set_assembly_sha256 != _sha256(tile_set / "assembly_manifest.json")
        or manifest.source_tile_set_seam_report_sha256 != source_assembly.seam_report_sha256
        or manifest.source_bundle_manifest_sha256 != source_assembly.source_bundle_manifest_sha256
        or manifest.source_model_stl_sha256 != source_model_hash
        or manifest.scaling != scaling
        or manifest.tile_grid_shape != layout.tile_grid_shape
        or manifest.tile_count != layout.tile_count
    ):
        raise ConfigurationError("tile mesh assembly manifest does not match source identities")
    if (
        manifest.coverage_image_path != "tile-coverage.png"
        or manifest.coverage_image_sha256 != _sha256(root / "tile-coverage.png")
        or manifest.assembly_validation_path != "tile-mesh-assembly-validation.json"
        or manifest.assembly_validation_sha256
        != _sha256(root / "tile-mesh-assembly-validation.json")
    ):
        raise ConfigurationError("tile mesh root artifact checksum mismatch")
    with Image.open(root / manifest.coverage_image_path) as image:
        coverage_size = image.size
        image.verify()
    source_record_by_id = {record.tile_id: record for record in source_assembly.tiles}
    tile_meshes: dict[str, trimesh.Trimesh] = {}
    validations: dict[str, TileMeshValidation] = {}
    for tile, record in zip(layout.tiles, manifest.tiles, strict=True):
        expected_directory = f"tiles/{tile.tile_id}"
        expected_manifest = f"{expected_directory}/tile_mesh_manifest.json"
        if (
            record.tile_id != tile.tile_id
            or record.tile_key != tile.tile_key
            or (record.row, record.column) != (tile.row, tile.column)
            or record.directory != expected_directory
            or record.tile_mesh_manifest != expected_manifest
            or record.physical_bounds_mm != _physical_bounds(tile)
        ):
            raise ConfigurationError(f"tile mesh root record mismatch: {tile.tile_id}")
        tile_dir = _resolve_relative(root, record.directory)
        manifest_path = _resolve_relative(root, record.tile_mesh_manifest)
        if (
            manifest_path.parent != tile_dir
            or _sha256(manifest_path) != record.tile_mesh_manifest_sha256
        ):
            raise ConfigurationError(f"tile mesh manifest checksum mismatch: {tile.tile_id}")
        artifact = _read_canonical_json(manifest_path, TileMeshArtifactManifest)
        if not isinstance(artifact, TileMeshArtifactManifest):
            raise AssertionError("unexpected tile mesh artifact model")
        source_record = source_record_by_id[tile.tile_id]
        if (
            artifact.tile_id != tile.tile_id
            or artifact.tile_key != tile.tile_key
            or artifact.layout_id != layout.layout_id
            or (artifact.row, artifact.column) != (tile.row, tile.column)
            or artifact.source_tile_manifest_sha256 != source_record.tile_manifest_sha256
            or artifact.source_processed_dem_sha256 != source_record.sha256["processed_dem"]
            or artifact.physical_bounds_mm != _physical_bounds(tile)
            or artifact.sha256 != record.sha256
            or record.source_tile_manifest_sha256 != source_record.tile_manifest_sha256
            or record.files
            != {role: f"{expected_directory}/{name}" for role, name in artifact.files.items()}
        ):
            raise ConfigurationError(f"tile mesh artifact identity mismatch: {tile.tile_id}")
        for role, name in artifact.files.items():
            local_path = tile_dir / name
            assembly_path = _resolve_relative(root, record.files[role])
            if local_path != assembly_path or _sha256(local_path) != artifact.sha256[role]:
                raise ConfigurationError(
                    f"tile mesh artifact checksum mismatch: {tile.tile_id}/{role}"
                )
        core = _core_values(
            tile,
            _resolve_relative(tile_set, source_record.files["processed_dem"]),
        )
        measured = _measure_tile_validation(
            tile,
            core_elevations_m=core,
            scaling=scaling,
            stl_path=tile_dir / artifact.files["model_stl"],
            three_mf_path=tile_dir / artifact.files["model_3mf"],
            glb_path=tile_dir / artifact.files["preview_glb"],
        )
        if measured != artifact.validation or not measured.required_checks_passed:
            raise MeshValidationError(f"tile mesh validation changed on reopen: {tile.tile_id}")
        if (
            record.global_bounds_mm != measured.expected_global_bounds_mm
            or record.triangle_count != measured.expected_triangle_count
            or abs(record.volume_mm3 - measured.geometry.volume_mm3) > 1e-9
        ):
            raise ConfigurationError(f"tile mesh summary mismatch: {tile.tile_id}")
        validations[tile.tile_id] = measured
        tile_meshes[tile.tile_id] = _load_stl(tile_dir / artifact.files["model_stl"])
    source_model_path = bundle / source_model_name
    if _sha256(source_model_path) != source_model_hash:
        raise ConfigurationError("source model STL checksum changed")
    measured_assembly = _measure_assembly(
        layout,
        tile_meshes=tile_meshes,
        tile_validations=validations,
        source_model=_load_stl(source_model_path),
        coverage_image_size_px=coverage_size,
    )
    if measured_assembly != reported_validation or not measured_assembly.required_checks_passed:
        raise MeshValidationError("tile mesh assembly validation changed on reopen")
    return {
        "status": "verified",
        "output_dir": str(root),
        "layout_id": layout.layout_id,
        "tile_grid_shape": layout.tile_grid_shape,
        "tile_count": layout.tile_count,
        "mesh_seam_count": measured_assembly.seam_count,
        "mesh_seam_status": measured_assembly.mesh_seam_status,
        "maximum_top_seam_gap_mm": measured_assembly.maximum_top_seam_gap_mm,
        "volume_difference_mm3": measured_assembly.volume_difference_mm3,
        "coverage_image": str(root / manifest.coverage_image_path),
        "required_checks_passed": True,
    }
