"""Deterministic connector geometry and print-local tile placement."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Self, cast

import numpy as np
import trimesh
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.spatial import KDTree
from shapely.geometry import Polygon

from topoforge.config import load_build_config
from topoforge.exceptions import ConfigurationError, MeshValidationError
from topoforge.exporters import export_glb, export_stl
from topoforge.exporters.three_mf import ThreeMFInspection, export_3mf, inspect_3mf
from topoforge.models import PrinterProfile
from topoforge.tiling.layout import TerrainTile, TileLayout, read_tile_layout
from topoforge.tiling.meshes import (
    TileMeshAssemblyManifest,
    verify_tile_mesh_set,
)
from topoforge.validation import ValidationReport, validate_mesh

_CONNECTOR_PLAN_SCHEMA_VERSION = "topoforge-connector-plan-v1"
_PRINT_TILE_SCHEMA_VERSION = "topoforge-print-tile-artifact-v1"
_PRINT_ASSEMBLY_SCHEMA_VERSION = "topoforge-print-tile-assembly-v1"
_PRINT_VALIDATION_SCHEMA_VERSION = "topoforge-print-tile-assembly-validation-v1"
_BOUNDS_TOLERANCE_MM = 0.001
_GEOMETRY_TOLERANCE_MM = 0.001
_VOLUME_RELATIVE_TOLERANCE = 1e-4
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundsMm = tuple[float, float, float, float, float, float]
Point2D = tuple[float, float]


class ConnectorPolicy(BaseModel):
    """Printer-derived dimensions and fit thresholds for bottom dovetails."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _CONNECTOR_PLAN_SCHEMA_VERSION
    connector_type: str = "bottom-open vertical-assembly dovetail"
    boolean_engine: str = "manifold"
    printer_profile: PrinterProfile
    base_thickness_mm: float = Field(gt=0)
    tolerance_definition: str = (
        "connector_tolerance_mm is total lateral clearance; half is applied per side"
    )
    total_lateral_clearance_mm: float = Field(gt=0)
    per_side_lateral_clearance_mm: float = Field(gt=0)
    vertical_clearance_mm: float = Field(gt=0)
    minimum_verified_clearance_mm: float = Field(gt=0)
    maximum_verified_clearance_mm: float = Field(gt=0)
    fit_classification: str
    minimum_wall_thickness_mm: float = Field(gt=0)
    male_height_mm: float = Field(gt=0)
    female_cavity_height_mm: float = Field(gt=0)
    remaining_roof_thickness_mm: float = Field(gt=0)
    anchor_depth_mm: float = Field(gt=0)
    insertion_depth_mm: float = Field(gt=0)
    male_neck_width_mm: float = Field(gt=0)
    male_head_width_mm: float = Field(gt=0)
    female_neck_width_mm: float = Field(gt=0)
    female_head_width_mm: float = Field(gt=0)
    edge_margin_mm: float = Field(gt=0)
    maximum_connector_spacing_mm: float = Field(gt=0)
    boolean_opening_padding_mm: float = Field(gt=0)
    decision_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        if self.male_head_width_mm <= self.male_neck_width_mm:
            raise ValueError("dovetail head width must exceed neck width")
        if self.female_head_width_mm <= self.male_head_width_mm:
            raise ValueError("female head width must include lateral clearance")
        if self.female_neck_width_mm <= self.male_neck_width_mm:
            raise ValueError("female neck width must include lateral clearance")
        if self.female_cavity_height_mm <= self.male_height_mm:
            raise ValueError("female cavity height must include vertical clearance")
        if self.remaining_roof_thickness_mm < self.minimum_wall_thickness_mm:
            raise ValueError("connector cavity leaves less than the required roof thickness")
        return self


class ConnectorPlacement(BaseModel):
    """One stable male/female connector pair on an internal tile seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_id: str
    seam_id: str
    direction: str
    index: int = Field(ge=0)
    connector_count_on_seam: int = Field(gt=0)
    male_tile_id: str
    female_tile_id: str
    male_role: str = "male"
    female_role: str = "female"
    insertion_axis: str
    assembly_direction: str = "+Z: lower the female tile over the male dovetail"
    seam_coordinate_mm: float
    center_along_seam_mm: float
    male_polygon_xy_mm: tuple[Point2D, ...]
    male_protrusion_polygon_xy_mm: tuple[Point2D, ...]
    female_cavity_polygon_xy_mm: tuple[Point2D, ...]
    female_probe_polygon_xy_mm: tuple[Point2D, ...]
    male_z_range_mm: tuple[float, float]
    female_cavity_z_range_mm: tuple[float, float]


class ConnectorPlan(BaseModel):
    """Canonical connector identity, ownership, polarity, and placement plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _CONNECTOR_PLAN_SCHEMA_VERSION
    layout_id: str
    source_tile_mesh_assembly_sha256: Sha256Hex
    row_origin: str = "north"
    column_origin: str = "west"
    east_axis: str = "+X = East"
    north_axis: str = "+Y = North"
    up_axis: str = "+Z = Up"
    ownership_rule: str = (
        "west tile owns male on east-west seams; north tile owns male on north-south seams"
    )
    polarity_rule: str = "male on west/north; bottom-open female cavity on east/south"
    terrain_surface_rule: str = "all connector booleans remain below the terrain top surface"
    tile_count: int = Field(gt=0)
    seam_count: int = Field(ge=0)
    connector_count: int = Field(ge=0)
    policy: ConnectorPolicy
    connectors: tuple[ConnectorPlacement, ...]

    @model_validator(mode="after")
    def validate_connectors(self) -> Self:
        identifiers = [item.connector_id for item in self.connectors]
        if identifiers != sorted(identifiers) or len(set(identifiers)) != len(identifiers):
            raise ValueError("connector placements must be unique and sorted by connector id")
        seams = {item.seam_id for item in self.connectors}
        if self.connector_count != len(self.connectors) or self.seam_count != len(seams):
            raise ValueError("connector plan counts do not match its placements")
        return self


class PrintTileValidation(BaseModel):
    """Strict global and print-local checks for one connected terrain tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _PRINT_TILE_SCHEMA_VERSION
    tile_id: str
    layout_id: str
    male_connector_ids: tuple[str, ...]
    female_connector_ids: tuple[str, ...]
    source_global_bounds_mm: BoundsMm
    connector_global_bounds_mm: BoundsMm
    expected_print_local_bounds_mm: BoundsMm
    global_to_print_local_translation_mm: tuple[float, float, float]
    print_local_to_global_translation_mm: tuple[float, float, float]
    global_format_bounds_mm: dict[str, BoundsMm]
    local_format_bounds_mm: dict[str, BoundsMm]
    global_format_triangle_counts: dict[str, int]
    local_format_triangle_counts: dict[str, int]
    expected_global_peak_coordinate_mm: tuple[float, float, float]
    expected_local_peak_coordinate_mm: tuple[float, float, float]
    global_format_peak_coordinates_mm: dict[str, tuple[float, float, float]]
    local_format_peak_coordinates_mm: dict[str, tuple[float, float, float]]
    source_volume_mm3: float = Field(gt=0)
    connector_volume_mm3: float = Field(gt=0)
    top_surface_source_vertex_count: int = Field(gt=0)
    maximum_top_surface_deviation_mm: float = Field(ge=0)
    top_surface_preserved: bool
    bed_contact_area_mm2: float = Field(gt=0)
    bed_contact_planarity_error_mm: float = Field(ge=0)
    bed_contact_flat: bool
    bottom_recesses_expected: bool
    remaining_roof_thickness_mm: float = Field(gt=0)
    minimum_wall_thickness_mm: float = Field(gt=0)
    thin_wall_check_passed: bool
    build_volume_mm: tuple[float, float, float]
    build_volume_check_passed: bool
    global_geometry: ValidationReport
    local_geometry: ValidationReport
    strict_3mf_warning_count: dict[str, int]
    bounds_match: bool
    triangle_counts_match: bool
    peak_coordinates_match: bool
    transforms_reversible: bool
    orientation_consistent: bool
    required_checks_passed: bool


class PrintTileArtifactManifest(BaseModel):
    """Checksummed global-assembly and print-local roles for one tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _PRINT_TILE_SCHEMA_VERSION
    tile_id: str
    tile_key: str
    layout_id: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    source_tile_mesh_manifest_sha256: Sha256Hex
    connector_plan_sha256: Sha256Hex
    male_connector_ids: tuple[str, ...]
    female_connector_ids: tuple[str, ...]
    files: dict[str, str]
    sha256: dict[str, Sha256Hex]
    validation: PrintTileValidation

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        required = {
            "global_stl",
            "global_3mf",
            "global_glb",
            "print_local_stl",
            "print_local_3mf",
            "print_local_glb",
            "validation",
        }
        if set(self.files) != required or set(self.sha256) != required:
            raise ValueError("print tile manifest must contain the complete v1 role set")
        if any(Path(value).name != value for value in self.files.values()):
            raise ValueError("print tile artifact paths must be local filenames")
        return self


class PrintTileAssemblyRecord(BaseModel):
    """Root record for one connector-bearing print tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_id: str
    tile_key: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    directory: str
    tile_manifest: str
    tile_manifest_sha256: Sha256Hex
    source_tile_mesh_manifest_sha256: Sha256Hex
    files: dict[str, str]
    sha256: dict[str, Sha256Hex]
    male_connector_ids: tuple[str, ...]
    female_connector_ids: tuple[str, ...]
    global_bounds_mm: BoundsMm
    print_local_bounds_mm: BoundsMm
    global_to_print_local_translation_mm: tuple[float, float, float]
    triangle_count: int = Field(gt=0)
    volume_mm3: float = Field(gt=0)


class ConnectorFitMeasurement(BaseModel):
    """Measured male presence, female clearance, and collision for one connector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_id: str
    seam_id: str
    male_tile_id: str
    female_tile_id: str
    male_expected_volume_mm3: float = Field(gt=0)
    male_missing_volume_mm3: float = Field(ge=0)
    female_cavity_residual_volume_mm3: float = Field(ge=0)
    assembled_collision_volume_mm3: float = Field(ge=0)
    volume_tolerance_mm3: float = Field(gt=0)
    lateral_clearance_per_side_mm: float = Field(gt=0)
    vertical_clearance_mm: float = Field(gt=0)
    required_checks_passed: bool


class PrintTileAssemblyValidation(BaseModel):
    """Aggregate connector fit, assembly, and print-placement evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _PRINT_VALIDATION_SCHEMA_VERSION
    layout_id: str
    tile_count: int = Field(gt=0)
    seam_count: int = Field(ge=0)
    connector_count: int = Field(ge=0)
    source_global_bounds_mm: BoundsMm
    connector_assembly_bounds_mm: BoundsMm
    source_tile_volume_sum_mm3: float = Field(gt=0)
    connector_tile_volume_sum_mm3: float = Field(gt=0)
    connector_volume_change_mm3: float
    maximum_top_surface_deviation_mm: float = Field(ge=0)
    maximum_collision_volume_mm3: float = Field(ge=0)
    maximum_male_missing_volume_mm3: float = Field(ge=0)
    maximum_female_cavity_residual_volume_mm3: float = Field(ge=0)
    minimum_lateral_clearance_per_side_mm: float = Field(gt=0)
    minimum_vertical_clearance_mm: float = Field(gt=0)
    all_tile_checks_passed: bool
    all_top_surfaces_preserved: bool
    all_bed_contacts_flat: bool
    all_thin_wall_checks_passed: bool
    all_build_volume_checks_passed: bool
    global_bounds_match: bool
    connector_fit_status: str
    collision_status: str
    required_checks_passed: bool
    connectors: tuple[ConnectorFitMeasurement, ...]


class PrintTileAssemblyManifest(BaseModel):
    """Canonical root binding connector, print-local, and assembly artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _PRINT_ASSEMBLY_SCHEMA_VERSION
    layout_id: str
    source_tile_mesh_assembly_sha256: Sha256Hex
    source_tile_set_assembly_sha256: Sha256Hex
    source_bundle_manifest_sha256: Sha256Hex
    connector_plan_path: str = "connector-plan.json"
    connector_plan_sha256: Sha256Hex
    connector_map_path: str = "connector-map.png"
    connector_map_sha256: Sha256Hex
    assembly_preview_path: str = "connector-assembly.global.glb"
    assembly_preview_sha256: Sha256Hex
    assembly_validation_path: str = "print-tile-assembly-validation.json"
    assembly_validation_sha256: Sha256Hex
    row_origin: str = "north"
    column_origin: str = "west"
    east_axis: str = "+X = East"
    north_axis: str = "+Y = North"
    up_axis: str = "+Z = Up"
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    seam_count: int = Field(ge=0)
    connector_count: int = Field(ge=0)
    label_contract: str = "tile and connector ids are recorded in metadata and connector-map.png"
    tiles: tuple[PrintTileAssemblyRecord, ...]

    @model_validator(mode="after")
    def validate_tiles(self) -> Self:
        rows, columns = self.tile_grid_shape
        expected = [(row, column) for row in range(rows) for column in range(columns)]
        actual = [(tile.row, tile.column) for tile in self.tiles]
        if self.tile_count != rows * columns or len(self.tiles) != self.tile_count:
            raise ValueError("print tile assembly count does not match its grid")
        if actual != expected or len({tile.tile_id for tile in self.tiles}) != self.tile_count:
            raise ValueError("print tile records must be unique and in row-major order")
        return self


class PrintTileSetResult(BaseModel):
    """Published paths for one deterministic connector-bearing print tile set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    connector_plan_path: Path
    assembly_manifest_path: Path
    assembly_validation_path: Path
    connector_map_path: Path
    assembly_preview_path: Path
    tile_manifest_paths: tuple[Path, ...]


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
        raise ConfigurationError(f"print tile path escapes output directory: {relative}")
    return candidate


def _bounds(mesh: trimesh.Trimesh) -> BoundsMm:
    value = np.asarray(mesh.bounds, dtype=np.float64)
    return (
        float(value[0, 0]),
        float(value[0, 1]),
        float(value[0, 2]),
        float(value[1, 0]),
        float(value[1, 1]),
        float(value[1, 2]),
    )


def _inspection_bounds(inspection: ThreeMFInspection) -> BoundsMm:
    minimum, maximum = inspection.bounds_mm
    return (*minimum, *maximum)


def _load_stl(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="stl", force="mesh", process=True)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"reopened print tile STL is not a mesh: {path}")
    loaded.units = "mm"
    return loaded


def _load_glb(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="glb", force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"reopened print tile GLB is not a mesh: {path}")
    loaded.units = "mm"
    return loaded


def _peak(mesh: trimesh.Trimesh) -> tuple[float, float, float]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    maximum_z = float(np.max(vertices[:, 2]))
    candidates = vertices[np.isclose(vertices[:, 2], maximum_z, atol=1e-6, rtol=0.0)]
    selected = candidates[np.lexsort((candidates[:, 1], candidates[:, 0]))][0]
    return float(selected[0]), float(selected[1]), float(selected[2])


def _closest_3mf_peak(
    inspection: ThreeMFInspection, expected: tuple[float, float, float]
) -> tuple[float, float, float]:
    return min(
        inspection.peak_coordinates_mm,
        key=lambda value: (value[0] - expected[0]) ** 2 + (value[1] - expected[1]) ** 2,
    )


def _required_topology(report: ValidationReport) -> bool:
    return bool(
        report.finite_vertices
        and report.finite_face_normals
        and report.watertight
        and report.winding_consistent
        and report.manifold
        and report.positive_volume
        and report.dimensions_within_tolerance
        and report.connected_components == 1
        and report.degenerate_faces == 0
        and report.duplicate_faces == 0
    )


def derive_connector_policy(
    printer_profile: PrinterProfile, *, base_thickness_mm: float
) -> ConnectorPolicy:
    """Derive printable connector dimensions from one explicit printer profile."""
    profile = printer_profile.model_copy(deep=True)
    tolerance = float(profile.connector_tolerance_mm)
    minimum_clearance = max(0.05, profile.nozzle_diameter_mm * 0.25)
    maximum_clearance = max(profile.nozzle_diameter_mm, profile.minimum_feature_mm * 0.75)
    if tolerance < minimum_clearance or tolerance > maximum_clearance:
        raise ConfigurationError(
            "connector_tolerance_mm is outside the verified clearance range "
            f"[{minimum_clearance:.6f}, {maximum_clearance:.6f}] mm for "
            f"{profile.profile_id}; choose a value inside that range"
        )
    wall = max(
        2.0 * profile.nozzle_diameter_mm,
        profile.minimum_feature_mm,
        3.0 * profile.layer_height_mm,
    )
    minimum_height = max(
        2.0 * profile.nozzle_diameter_mm,
        profile.minimum_feature_mm,
        4.0 * profile.layer_height_mm,
    )
    desired_height = max(
        4.0 * profile.nozzle_diameter_mm,
        3.0 * profile.minimum_feature_mm,
        6.0 * profile.layer_height_mm,
    )
    available_height = float(base_thickness_mm) - wall - tolerance
    if available_height < minimum_height:
        raise ConfigurationError(
            "base thickness cannot contain a printable bottom connector and roof; "
            f"requires at least {minimum_height + wall + tolerance:.6f} mm for "
            f"{profile.profile_id}"
        )
    male_height = min(desired_height, available_height)
    female_height = male_height + tolerance
    roof = float(base_thickness_mm) - female_height
    neck = max(8.0 * profile.nozzle_diameter_mm, 6.0 * profile.minimum_feature_mm)
    head = max(
        neck + 2.0 * profile.minimum_feature_mm,
        12.0 * profile.nozzle_diameter_mm,
        10.0 * profile.minimum_feature_mm,
    )
    depth = max(8.0 * profile.nozzle_diameter_mm, 6.0 * profile.minimum_feature_mm)
    anchor = max(2.0 * profile.nozzle_diameter_mm, profile.minimum_feature_mm)
    edge_margin = max(1.5 * head, 4.0 * wall)
    spacing = max(100.0 * profile.nozzle_diameter_mm, 80.0 * profile.minimum_feature_mm)
    padding = max(0.05, tolerance * 0.25)
    return ConnectorPolicy(
        printer_profile=profile,
        base_thickness_mm=base_thickness_mm,
        total_lateral_clearance_mm=tolerance,
        per_side_lateral_clearance_mm=tolerance / 2.0,
        vertical_clearance_mm=tolerance,
        minimum_verified_clearance_mm=minimum_clearance,
        maximum_verified_clearance_mm=maximum_clearance,
        fit_classification="verified-clearance-fit",
        minimum_wall_thickness_mm=wall,
        male_height_mm=male_height,
        female_cavity_height_mm=female_height,
        remaining_roof_thickness_mm=roof,
        anchor_depth_mm=anchor,
        insertion_depth_mm=depth,
        male_neck_width_mm=neck,
        male_head_width_mm=head,
        female_neck_width_mm=neck + tolerance,
        female_head_width_mm=head + tolerance,
        edge_margin_mm=edge_margin,
        maximum_connector_spacing_mm=spacing,
        boolean_opening_padding_mm=padding,
        decision_reasons=(
            "minimum wall is max(2x nozzle, minimum feature, 3x layer height)",
            "connector height is bounded by printable height and the remaining base roof",
            "neck, head, and insertion depth are printer-feature multiples",
            "profile connector tolerance is total clearance and is split equally per side",
            "female cavity opens through the bottom for support-free vertical assembly",
        ),
    )


def _connector_centers(
    start: float, stop: float, policy: ConnectorPolicy, *, reverse: bool
) -> tuple[float, ...]:
    length = stop - start
    minimum_length = 2.0 * policy.edge_margin_mm + policy.female_head_width_mm
    if length < minimum_length:
        raise ConfigurationError(
            f"tile seam length {length:.6f} mm is too short for the connector policy; "
            f"requires at least {minimum_length:.6f} mm"
        )
    usable = length - 2.0 * policy.edge_margin_mm
    count = max(1, math.ceil(usable / policy.maximum_connector_spacing_mm))
    values = tuple(
        start + policy.edge_margin_mm + usable * (index + 1) / (count + 1) for index in range(count)
    )
    return tuple(reversed(values)) if reverse else values


def _map_local_polygon(
    local: tuple[Point2D, ...], *, direction: str, seam: float, center: float
) -> tuple[Point2D, ...]:
    if direction == "east-west":
        return tuple((seam + normal, center + along) for normal, along in local)
    if direction == "north-south":
        return tuple((center + along, seam - normal) for normal, along in local)
    raise AssertionError(f"unexpected connector direction: {direction}")


def _placement(
    *,
    male: TerrainTile,
    female: TerrainTile,
    direction: str,
    index: int,
    count: int,
    seam: float,
    center: float,
    policy: ConnectorPolicy,
) -> ConnectorPlacement:
    neck = policy.male_neck_width_mm / 2.0
    head = policy.male_head_width_mm / 2.0
    female_neck = policy.female_neck_width_mm / 2.0
    female_head = policy.female_head_width_mm / 2.0
    depth = policy.insertion_depth_mm
    clearance = policy.total_lateral_clearance_mm
    opening = policy.boolean_opening_padding_mm
    male_local = (
        (-policy.anchor_depth_mm, -neck),
        (0.0, -neck),
        (depth, -head),
        (depth, head),
        (0.0, neck),
        (-policy.anchor_depth_mm, neck),
    )
    male_protrusion_local = ((0.0, -neck), (depth, -head), (depth, head), (0.0, neck))
    female_local = (
        (-opening, -female_neck),
        (0.0, -female_neck),
        (depth + clearance, -female_head),
        (depth + clearance, female_head),
        (0.0, female_neck),
        (-opening, female_neck),
    )
    female_probe_local = (
        (0.0, -female_neck),
        (depth + clearance, -female_head),
        (depth + clearance, female_head),
        (0.0, female_neck),
    )
    seam_id = f"seam-{male.tile_id}-{direction}-{female.tile_id}"
    identifier = f"connector-{male.tile_id}-{direction}-{female.tile_id}-k{index:04d}"
    return ConnectorPlacement(
        connector_id=identifier,
        seam_id=seam_id,
        direction=direction,
        index=index,
        connector_count_on_seam=count,
        male_tile_id=male.tile_id,
        female_tile_id=female.tile_id,
        insertion_axis="+X East" if direction == "east-west" else "-Y South",
        seam_coordinate_mm=seam,
        center_along_seam_mm=center,
        male_polygon_xy_mm=_map_local_polygon(
            male_local, direction=direction, seam=seam, center=center
        ),
        male_protrusion_polygon_xy_mm=_map_local_polygon(
            male_protrusion_local, direction=direction, seam=seam, center=center
        ),
        female_cavity_polygon_xy_mm=_map_local_polygon(
            female_local, direction=direction, seam=seam, center=center
        ),
        female_probe_polygon_xy_mm=_map_local_polygon(
            female_probe_local, direction=direction, seam=seam, center=center
        ),
        male_z_range_mm=(0.0, policy.male_height_mm),
        female_cavity_z_range_mm=(-opening, policy.female_cavity_height_mm),
    )


def plan_connectors(
    layout: TileLayout,
    *,
    source_tile_mesh_assembly_sha256: str,
    policy: ConnectorPolicy,
) -> ConnectorPlan:
    """Plan stable male/female dovetails for every east and south adjacency."""
    tile_by_id = {tile.tile_id: tile for tile in layout.tiles}
    placements: list[ConnectorPlacement] = []
    for tile in layout.tiles:
        if tile.east_neighbor is not None:
            female = tile_by_id[tile.east_neighbor]
            centers = _connector_centers(
                tile.physical_bounds_mm.y_min,
                tile.physical_bounds_mm.y_max,
                policy,
                reverse=True,
            )
            for index, center in enumerate(centers):
                placements.append(
                    _placement(
                        male=tile,
                        female=female,
                        direction="east-west",
                        index=index,
                        count=len(centers),
                        seam=tile.physical_bounds_mm.x_max,
                        center=center,
                        policy=policy,
                    )
                )
        if tile.south_neighbor is not None:
            female = tile_by_id[tile.south_neighbor]
            centers = _connector_centers(
                tile.physical_bounds_mm.x_min,
                tile.physical_bounds_mm.x_max,
                policy,
                reverse=False,
            )
            for index, center in enumerate(centers):
                placements.append(
                    _placement(
                        male=tile,
                        female=female,
                        direction="north-south",
                        index=index,
                        count=len(centers),
                        seam=tile.physical_bounds_mm.y_min,
                        center=center,
                        policy=policy,
                    )
                )
    placements.sort(key=lambda item: item.connector_id)
    return ConnectorPlan(
        layout_id=layout.layout_id,
        source_tile_mesh_assembly_sha256=source_tile_mesh_assembly_sha256,
        tile_count=layout.tile_count,
        seam_count=len({item.seam_id for item in placements}),
        connector_count=len(placements),
        policy=policy,
        connectors=tuple(placements),
    )


def _polygon_prism(
    polygon_xy: tuple[Point2D, ...], z_range_mm: tuple[float, float]
) -> trimesh.Trimesh:
    polygon = Polygon(polygon_xy)
    if (
        len(polygon_xy) < 3
        or not polygon.is_valid
        or polygon.is_empty
        or polygon.area <= 0.0
        or len(polygon.interiors) != 0
    ):
        raise ValueError("connector polygon must be a simple non-empty outline")
    z_min, z_max = z_range_mm
    if z_max <= z_min:
        raise ValueError("connector prism z range is empty")
    value = trimesh.creation.extrude_polygon(
        polygon,
        z_max - z_min,
        engine="earcut",
    )
    if not isinstance(value, trimesh.Trimesh):
        raise MeshValidationError("connector polygon extrusion returned no mesh")
    mesh = value
    mesh.apply_translation((0.0, 0.0, z_min))
    mesh.units = "mm"
    if not bool(mesh.is_watertight and mesh.is_winding_consistent) or float(mesh.volume) <= 0:
        raise MeshValidationError("connector prism topology is invalid")
    return mesh


def _boolean_result(value: Any, *, operation: str) -> trimesh.Trimesh:
    if not isinstance(value, trimesh.Trimesh) or len(value.faces) == 0:
        raise MeshValidationError(f"manifold connector {operation} returned no mesh")
    value.units = "mm"
    if not bool(value.is_watertight and value.is_winding_consistent) or float(value.volume) <= 0:
        raise MeshValidationError(f"manifold connector {operation} returned invalid topology")
    return value


def _apply_connectors(
    source: trimesh.Trimesh,
    *,
    male: tuple[ConnectorPlacement, ...],
    female: tuple[ConnectorPlacement, ...],
    policy: ConnectorPolicy,
) -> trimesh.Trimesh:
    result = source.copy()
    if male:
        additions = [_polygon_prism(item.male_polygon_xy_mm, item.male_z_range_mm) for item in male]
        result = _boolean_result(
            trimesh.boolean.union(
                [result, *additions], engine=cast(Any, policy.boolean_engine), check_volume=True
            ),
            operation="union",
        )
    if female:
        cutters = [
            _polygon_prism(item.female_cavity_polygon_xy_mm, item.female_cavity_z_range_mm)
            for item in female
        ]
        result = _boolean_result(
            trimesh.boolean.difference(
                [result, *cutters], engine=cast(Any, policy.boolean_engine), check_volume=True
            ),
            operation="difference",
        )
    return result


def _tile_connectors(
    plan: ConnectorPlan, tile_id: str
) -> tuple[tuple[ConnectorPlacement, ...], tuple[ConnectorPlacement, ...]]:
    male = tuple(item for item in plan.connectors if item.male_tile_id == tile_id)
    female = tuple(item for item in plan.connectors if item.female_tile_id == tile_id)
    return male, female


def _top_surface_deviation(
    source: trimesh.Trimesh, connected: trimesh.Trimesh, *, base_thickness_mm: float
) -> tuple[int, float]:
    source_vertices = np.asarray(source.vertices, dtype=np.float64)
    connected_vertices = np.asarray(connected.vertices, dtype=np.float64)
    top = source_vertices[source_vertices[:, 2] >= base_thickness_mm - _GEOMETRY_TOLERANCE_MM]
    if len(top) == 0:
        raise MeshValidationError("source tile has no terrain-top vertices")
    distances, _ = KDTree(connected_vertices).query(top, k=1)
    return len(top), float(np.max(distances))


def _bed_contact(mesh: trimesh.Trimesh) -> tuple[float, float, bool]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    minimum_z = float(np.min(vertices[:, 2]))
    triangles = vertices[faces]
    on_bed = np.all(
        np.isclose(triangles[:, :, 2], minimum_z, atol=_GEOMETRY_TOLERANCE_MM, rtol=0.0),
        axis=1,
    ) & (normals[:, 2] < -1.0 + 1e-9)
    if not bool(np.any(on_bed)):
        return 0.0, float("inf"), False
    selected = triangles[on_bed]
    cross_products = np.cross(selected[:, 1] - selected[:, 0], selected[:, 2] - selected[:, 0])
    area = float(np.sum(np.linalg.norm(cross_products, axis=1) / 2.0))
    planarity = float(np.ptp(selected[:, :, 2]))
    return (
        area,
        planarity,
        bool(
            abs(minimum_z) <= _GEOMETRY_TOLERANCE_MM
            and area > 0.0
            and planarity <= _GEOMETRY_TOLERANCE_MM
        ),
    )


def _translated_bounds(bounds: BoundsMm, translation: tuple[float, float, float]) -> BoundsMm:
    return (
        bounds[0] + translation[0],
        bounds[1] + translation[1],
        bounds[2] + translation[2],
        bounds[3] + translation[0],
        bounds[4] + translation[1],
        bounds[5] + translation[2],
    )


def _measure_tile_validation(
    tile: TerrainTile,
    *,
    plan: ConnectorPlan,
    source_mesh: trimesh.Trimesh,
    global_stl_path: Path,
    global_3mf_path: Path,
    global_glb_path: Path,
    local_stl_path: Path,
    local_3mf_path: Path,
    local_glb_path: Path,
) -> PrintTileValidation:
    male, female = _tile_connectors(plan, tile.tile_id)
    global_stl = _load_stl(global_stl_path)
    global_glb = _load_glb(global_glb_path)
    local_stl = _load_stl(local_stl_path)
    local_glb = _load_glb(local_glb_path)
    global_3mf = inspect_3mf(global_3mf_path)
    local_3mf = inspect_3mf(local_3mf_path)
    connector_bounds = _bounds(global_stl)
    translation = (
        -connector_bounds[0],
        -connector_bounds[1],
        -connector_bounds[2],
    )
    inverse = (-translation[0], -translation[1], -translation[2])
    local_bounds = _translated_bounds(connector_bounds, translation)
    expected_dimensions = (
        connector_bounds[3] - connector_bounds[0],
        connector_bounds[4] - connector_bounds[1],
        connector_bounds[5] - connector_bounds[2],
    )
    global_geometry = validate_mesh(
        global_stl,
        expected_dimensions_mm=expected_dimensions,
        dimension_tolerance_mm=_BOUNDS_TOLERANCE_MM,
        flat_bottom_tolerance_mm=0.01,
    )
    local_geometry = validate_mesh(
        local_stl,
        expected_dimensions_mm=expected_dimensions,
        dimension_tolerance_mm=_BOUNDS_TOLERANCE_MM,
        flat_bottom_tolerance_mm=0.01,
    )
    global_format_bounds = {
        "stl": connector_bounds,
        "3mf": _inspection_bounds(global_3mf),
        "glb": _bounds(global_glb),
    }
    local_format_bounds = {
        "stl": _bounds(local_stl),
        "3mf": _inspection_bounds(local_3mf),
        "glb": _bounds(local_glb),
    }
    global_triangles = {
        "stl": len(global_stl.faces),
        "3mf": int(global_3mf.triangle_count),
        "glb": len(global_glb.faces),
    }
    local_triangles = {
        "stl": len(local_stl.faces),
        "3mf": int(local_3mf.triangle_count),
        "glb": len(local_glb.faces),
    }
    expected_global_peak = _peak(source_mesh)
    expected_local_peak = (
        expected_global_peak[0] + translation[0],
        expected_global_peak[1] + translation[1],
        expected_global_peak[2] + translation[2],
    )
    global_peaks = {
        "stl": _peak(global_stl),
        "3mf": _closest_3mf_peak(global_3mf, expected_global_peak),
        "glb": _peak(global_glb),
    }
    local_peaks = {
        "stl": _peak(local_stl),
        "3mf": _closest_3mf_peak(local_3mf, expected_local_peak),
        "glb": _peak(local_glb),
    }
    top_count, top_deviation = _top_surface_deviation(
        source_mesh,
        global_stl,
        base_thickness_mm=plan.policy.base_thickness_mm,
    )
    bed_area, bed_error, bed_flat = _bed_contact(local_stl)
    build_x, build_y, build_z = plan.policy.printer_profile.build_volume_mm
    build_volume_passed = bool(
        local_bounds[3] <= build_x + _BOUNDS_TOLERANCE_MM
        and local_bounds[4] <= build_y + _BOUNDS_TOLERANCE_MM
        and local_bounds[5] <= build_z + _BOUNDS_TOLERANCE_MM
    )
    bounds_match = bool(
        all(
            np.allclose(value, connector_bounds, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
            for value in global_format_bounds.values()
        )
        and all(
            np.allclose(value, local_bounds, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
            for value in local_format_bounds.values()
        )
    )
    triangle_counts_match = bool(
        len(set(global_triangles.values())) == 1
        and len(set(local_triangles.values())) == 1
        and global_triangles == local_triangles
    )
    peak_coordinates_match = bool(
        all(
            np.allclose(value, expected_global_peak, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
            for value in global_peaks.values()
        )
        and all(
            np.allclose(value, expected_local_peak, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
            for value in local_peaks.values()
        )
    )
    transforms_reversible = bool(
        np.allclose(
            np.asarray(translation) + np.asarray(inverse),
            (0.0, 0.0, 0.0),
            atol=1e-12,
            rtol=0.0,
        )
    )
    metadata_global = global_3mf.metadata
    metadata_local = local_3mf.metadata
    orientation_consistent = bool(
        metadata_global.get("customXMLNS0:tile_id") == tile.tile_id
        and metadata_global.get("customXMLNS0:coordinate_frame") == "global-assembly"
        and metadata_local.get("customXMLNS0:tile_id") == tile.tile_id
        and metadata_local.get("customXMLNS0:coordinate_frame") == "print-local"
        and metadata_global.get("customXMLNS0:east_axis") == "+X = East"
        and metadata_global.get("customXMLNS0:north_axis") == "+Y = North"
        and metadata_local.get("customXMLNS0:east_axis") == "+X = East"
        and metadata_local.get("customXMLNS0:north_axis") == "+Y = North"
    )
    thin_wall = bool(
        plan.policy.remaining_roof_thickness_mm >= plan.policy.minimum_wall_thickness_mm - 1e-12
        and plan.policy.male_neck_width_mm >= plan.policy.printer_profile.minimum_feature_mm
        and plan.policy.male_height_mm >= plan.policy.printer_profile.minimum_feature_mm
    )
    required = bool(
        _required_topology(global_geometry)
        and _required_topology(local_geometry)
        and bounds_match
        and triangle_counts_match
        and peak_coordinates_match
        and transforms_reversible
        and orientation_consistent
        and top_deviation <= _GEOMETRY_TOLERANCE_MM
        and bed_flat
        and thin_wall
        and build_volume_passed
        and global_3mf.strict_warning_count == 0
        and local_3mf.strict_warning_count == 0
    )
    return PrintTileValidation(
        tile_id=tile.tile_id,
        layout_id=plan.layout_id,
        male_connector_ids=tuple(item.connector_id for item in male),
        female_connector_ids=tuple(item.connector_id for item in female),
        source_global_bounds_mm=_bounds(source_mesh),
        connector_global_bounds_mm=connector_bounds,
        expected_print_local_bounds_mm=local_bounds,
        global_to_print_local_translation_mm=translation,
        print_local_to_global_translation_mm=inverse,
        global_format_bounds_mm=global_format_bounds,
        local_format_bounds_mm=local_format_bounds,
        global_format_triangle_counts=global_triangles,
        local_format_triangle_counts=local_triangles,
        expected_global_peak_coordinate_mm=expected_global_peak,
        expected_local_peak_coordinate_mm=expected_local_peak,
        global_format_peak_coordinates_mm=global_peaks,
        local_format_peak_coordinates_mm=local_peaks,
        source_volume_mm3=float(source_mesh.volume),
        connector_volume_mm3=float(global_stl.volume),
        top_surface_source_vertex_count=top_count,
        maximum_top_surface_deviation_mm=top_deviation,
        top_surface_preserved=top_deviation <= _GEOMETRY_TOLERANCE_MM,
        bed_contact_area_mm2=bed_area,
        bed_contact_planarity_error_mm=bed_error,
        bed_contact_flat=bed_flat,
        bottom_recesses_expected=bool(female),
        remaining_roof_thickness_mm=plan.policy.remaining_roof_thickness_mm,
        minimum_wall_thickness_mm=plan.policy.minimum_wall_thickness_mm,
        thin_wall_check_passed=thin_wall,
        build_volume_mm=plan.policy.printer_profile.build_volume_mm,
        build_volume_check_passed=build_volume_passed,
        global_geometry=global_geometry,
        local_geometry=local_geometry,
        strict_3mf_warning_count={
            "global": global_3mf.strict_warning_count,
            "print_local": local_3mf.strict_warning_count,
        },
        bounds_match=bounds_match,
        triangle_counts_match=triangle_counts_match,
        peak_coordinates_match=peak_coordinates_match,
        transforms_reversible=transforms_reversible,
        orientation_consistent=orientation_consistent,
        required_checks_passed=required,
    )


def _intersection_volume(first: trimesh.Trimesh, second: trimesh.Trimesh) -> float:
    result = trimesh.boolean.intersection(
        [first, second], engine=cast(Any, "manifold"), check_volume=True
    )
    if result is None:
        return 0.0
    if isinstance(result, trimesh.Trimesh):
        if len(result.faces) == 0:
            return 0.0
        vertices = np.asarray(result.vertices, dtype=np.float64)
        triangles = vertices[np.asarray(result.faces, dtype=np.int64)]
        signed = np.einsum(
            "ij,ij->i",
            triangles[:, 0],
            np.cross(triangles[:, 1], triangles[:, 2]),
        )
        return max(0.0, abs(float(np.sum(signed))) / 6.0)
    raise MeshValidationError("manifold connector intersection returned an unexpected value")


def _measure_connector_fit(
    placement: ConnectorPlacement,
    *,
    male_tile_mesh: trimesh.Trimesh,
    female_tile_mesh: trimesh.Trimesh,
    policy: ConnectorPolicy,
) -> ConnectorFitMeasurement:
    male = _polygon_prism(
        placement.male_protrusion_polygon_xy_mm,
        placement.male_z_range_mm,
    )
    female_probe = _polygon_prism(
        placement.female_probe_polygon_xy_mm,
        (0.0, policy.female_cavity_height_mm),
    )
    male_volume = float(male.volume)
    male_present = _intersection_volume(male, male_tile_mesh)
    missing = max(0.0, male_volume - male_present)
    cavity_residual = _intersection_volume(female_probe, female_tile_mesh)
    collision = _intersection_volume(male, female_tile_mesh)
    tolerance = max(1e-4, male_volume * _VOLUME_RELATIVE_TOLERANCE)
    passed = bool(missing <= tolerance and cavity_residual <= tolerance and collision <= tolerance)
    return ConnectorFitMeasurement(
        connector_id=placement.connector_id,
        seam_id=placement.seam_id,
        male_tile_id=placement.male_tile_id,
        female_tile_id=placement.female_tile_id,
        male_expected_volume_mm3=male_volume,
        male_missing_volume_mm3=missing,
        female_cavity_residual_volume_mm3=cavity_residual,
        assembled_collision_volume_mm3=collision,
        volume_tolerance_mm3=tolerance,
        lateral_clearance_per_side_mm=policy.per_side_lateral_clearance_mm,
        vertical_clearance_mm=policy.vertical_clearance_mm,
        required_checks_passed=passed,
    )


def _measure_assembly(
    layout: TileLayout,
    *,
    plan: ConnectorPlan,
    source_meshes: dict[str, trimesh.Trimesh],
    connector_meshes: dict[str, trimesh.Trimesh],
    validations: dict[str, PrintTileValidation],
) -> PrintTileAssemblyValidation:
    fit = tuple(
        _measure_connector_fit(
            placement,
            male_tile_mesh=connector_meshes[placement.male_tile_id],
            female_tile_mesh=connector_meshes[placement.female_tile_id],
            policy=plan.policy,
        )
        for placement in plan.connectors
    )
    all_bounds = np.asarray([_bounds(mesh) for mesh in connector_meshes.values()])
    connector_bounds: BoundsMm = (
        float(np.min(all_bounds[:, 0])),
        float(np.min(all_bounds[:, 1])),
        float(np.min(all_bounds[:, 2])),
        float(np.max(all_bounds[:, 3])),
        float(np.max(all_bounds[:, 4])),
        float(np.max(all_bounds[:, 5])),
    )
    source_bounds_array = np.asarray([_bounds(mesh) for mesh in source_meshes.values()])
    source_bounds: BoundsMm = (
        float(np.min(source_bounds_array[:, 0])),
        float(np.min(source_bounds_array[:, 1])),
        float(np.min(source_bounds_array[:, 2])),
        float(np.max(source_bounds_array[:, 3])),
        float(np.max(source_bounds_array[:, 4])),
        float(np.max(source_bounds_array[:, 5])),
    )
    source_volume = float(sum(float(mesh.volume) for mesh in source_meshes.values()))
    connector_volume = float(sum(float(mesh.volume) for mesh in connector_meshes.values()))
    all_tiles = all(item.required_checks_passed for item in validations.values())
    all_fit = all(item.required_checks_passed for item in fit)
    bounds_match = bool(
        np.allclose(connector_bounds, source_bounds, atol=_BOUNDS_TOLERANCE_MM, rtol=0.0)
    )
    required = bool(all_tiles and all_fit and bounds_match)
    return PrintTileAssemblyValidation(
        layout_id=layout.layout_id,
        tile_count=layout.tile_count,
        seam_count=plan.seam_count,
        connector_count=plan.connector_count,
        source_global_bounds_mm=source_bounds,
        connector_assembly_bounds_mm=connector_bounds,
        source_tile_volume_sum_mm3=source_volume,
        connector_tile_volume_sum_mm3=connector_volume,
        connector_volume_change_mm3=connector_volume - source_volume,
        maximum_top_surface_deviation_mm=max(
            item.maximum_top_surface_deviation_mm for item in validations.values()
        ),
        maximum_collision_volume_mm3=max(
            (item.assembled_collision_volume_mm3 for item in fit), default=0.0
        ),
        maximum_male_missing_volume_mm3=max(
            (item.male_missing_volume_mm3 for item in fit), default=0.0
        ),
        maximum_female_cavity_residual_volume_mm3=max(
            (item.female_cavity_residual_volume_mm3 for item in fit), default=0.0
        ),
        minimum_lateral_clearance_per_side_mm=plan.policy.per_side_lateral_clearance_mm,
        minimum_vertical_clearance_mm=plan.policy.vertical_clearance_mm,
        all_tile_checks_passed=all_tiles,
        all_top_surfaces_preserved=all(item.top_surface_preserved for item in validations.values()),
        all_bed_contacts_flat=all(item.bed_contact_flat for item in validations.values()),
        all_thin_wall_checks_passed=all(
            item.thin_wall_check_passed for item in validations.values()
        ),
        all_build_volume_checks_passed=all(
            item.build_volume_check_passed for item in validations.values()
        ),
        global_bounds_match=bounds_match,
        connector_fit_status="passed" if all_fit else "failed",
        collision_status="passed" if all_fit else "failed",
        required_checks_passed=required,
        connectors=fit,
    )


def _render_connector_map(
    layout: TileLayout, plan: ConnectorPlan, path: Path, *, width_px: int = 1200
) -> tuple[int, int]:
    header = 112
    margin = 72
    plot_width = width_px - 2 * margin
    model_width, model_depth = layout.model_size_mm
    plot_height = max(320, round(plot_width * model_depth / model_width))
    height_px = header + plot_height + 2 * margin
    image = Image.new("RGB", (width_px, height_px), (244, 246, 247))
    draw = ImageDraw.Draw(image)
    draw.text((margin, 20), f"TopoForge connector map: {layout.layout_id}", fill=(24, 31, 36))
    draw.text(
        (margin, 48),
        "Male: west/north tile | Female: east/south tile | assemble female downward (+Z)",
        fill=(68, 78, 85),
    )
    draw.text(
        (margin, 74),
        (
            f"{plan.connector_count} connectors | clearance "
            f"{plan.policy.total_lateral_clearance_mm:.3f} mm"
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
        draw.text(
            ((x0 + x1 - label_width) / 2, (y0 + y1 - label_height) / 2),
            tile.tile_id,
            fill=(255, 255, 255),
        )
    for connector in plan.connectors:
        if connector.direction == "east-west":
            x = left + round(plot_width * connector.seam_coordinate_mm / model_width)
            y = top + round(plot_height * (1.0 - connector.center_along_seam_mm / model_depth))
            draw.polygon(((x - 9, y), (x - 1, y - 6), (x - 1, y + 6)), fill=(10, 10, 10))
            draw.ellipse((x + 2, y - 6, x + 14, y + 6), outline=(255, 255, 255), width=3)
        else:
            x = left + round(plot_width * connector.center_along_seam_mm / model_width)
            y = top + round(plot_height * (1.0 - connector.seam_coordinate_mm / model_depth))
            draw.polygon(((x, y - 9), (x - 6, y - 1), (x + 6, y - 1)), fill=(10, 10, 10))
            draw.ellipse((x - 6, y + 2, x + 6, y + 14), outline=(255, 255, 255), width=3)
    north_x = width_px - margin
    draw.line((north_x, top + 56, north_x, top), fill=(24, 31, 36), width=4)
    draw.polygon(
        ((north_x, top), (north_x - 9, top + 16), (north_x + 9, top + 16)),
        fill=(24, 31, 36),
    )
    draw.text((north_x - 5, top + 62), "N", fill=(24, 31, 36))
    temporary = path.with_name(f".{path.name}.tmp")
    image.save(temporary, format="PNG", optimize=True)
    temporary.replace(path)
    with Image.open(path) as reopened:
        reopened.verify()
    return width_px, height_px


def _source_records(
    mesh_set: Path, tile_set: Path, bundle: Path
) -> tuple[TileLayout, TileMeshAssemblyManifest, ConnectorPolicy]:
    verify_tile_mesh_set(mesh_set, tile_set, bundle)
    layout = read_tile_layout(mesh_set / "tile-layout.json")
    mesh_manifest = TileMeshAssemblyManifest.model_validate_json(
        (mesh_set / "tile-mesh-assembly-manifest.json").read_text(encoding="utf-8")
    )
    config = load_build_config(bundle / "build_config.resolved.yaml")
    if abs(config.base_thickness_mm - mesh_manifest.scaling.base_thickness_mm) > 1e-9:
        raise ConfigurationError("source build config and scaling disagree on base thickness")
    policy = derive_connector_policy(
        config.printer_profile,
        base_thickness_mm=config.base_thickness_mm,
    )
    return layout, mesh_manifest, policy


def _tile_record(
    tile: TerrainTile,
    *,
    plan: ConnectorPlan,
    plan_sha256: str,
    source_record: Any,
    mesh_set: Path,
    output_tile_dir: Path,
) -> tuple[PrintTileArtifactManifest, PrintTileAssemblyRecord, trimesh.Trimesh, trimesh.Trimesh]:
    source_stl = _resolve_relative(mesh_set, source_record.files["model_stl"])
    source_mesh = _load_stl(source_stl)
    male, female = _tile_connectors(plan, tile.tile_id)
    connected = _apply_connectors(
        source_mesh,
        male=male,
        female=female,
        policy=plan.policy,
    )
    output_tile_dir.mkdir(parents=True)
    translation = (
        -float(connected.bounds[0, 0]),
        -float(connected.bounds[0, 1]),
        -float(connected.bounds[0, 2]),
    )
    local = connected.copy()
    local.apply_translation(translation)
    connector_ids = tuple(item.connector_id for item in (*male, *female))
    common_metadata = {
        "layout_id": plan.layout_id,
        "tile_id": tile.tile_id,
        "tile_key": tile.tile_key,
        "east_axis": "+X = East",
        "north_axis": "+Y = North",
        "up_axis": "+Z = Up",
        "connector_schema": plan.schema_version,
        "connector_ids": json.dumps(connector_ids, separators=(",", ":")),
        "male_connector_ids": json.dumps(
            [item.connector_id for item in male], separators=(",", ":")
        ),
        "female_connector_ids": json.dumps(
            [item.connector_id for item in female], separators=(",", ":")
        ),
        "terrain_surface_transform": "none; connector booleans remain below base top",
    }
    global_stl = export_stl(connected, output_tile_dir / "model.connector.global.stl")
    global_3mf = export_3mf(
        connected,
        output_tile_dir / "model.connector.global.3mf",
        object_name=f"TopoForge {tile.tile_id} connector global",
        metadata={**common_metadata, "coordinate_frame": "global-assembly"},
    )
    global_glb = export_glb(connected, output_tile_dir / "preview.connector.global.glb")
    local_stl = export_stl(local, output_tile_dir / "model.print-local.stl")
    local_3mf = export_3mf(
        local,
        output_tile_dir / "model.print-local.3mf",
        object_name=f"TopoForge {tile.tile_id} print local",
        metadata={
            **common_metadata,
            "coordinate_frame": "print-local",
            "global_to_print_local_translation_mm": json.dumps(translation, separators=(",", ":")),
            "print_local_to_global_translation_mm": json.dumps(
                tuple(-value for value in translation), separators=(",", ":")
            ),
        },
    )
    local_glb = export_glb(local, output_tile_dir / "preview.print-local.glb")
    validation = _measure_tile_validation(
        tile,
        plan=plan,
        source_mesh=source_mesh,
        global_stl_path=global_stl,
        global_3mf_path=global_3mf,
        global_glb_path=global_glb,
        local_stl_path=local_stl,
        local_3mf_path=local_3mf,
        local_glb_path=local_glb,
    )
    if not validation.required_checks_passed:
        raise MeshValidationError(f"connector print tile validation failed: {tile.tile_id}")
    validation_path = _write_canonical_json(
        output_tile_dir / "print_tile_validation.json", validation
    )
    files = {
        "global_stl": global_stl.name,
        "global_3mf": global_3mf.name,
        "global_glb": global_glb.name,
        "print_local_stl": local_stl.name,
        "print_local_3mf": local_3mf.name,
        "print_local_glb": local_glb.name,
        "validation": validation_path.name,
    }
    checksums = {role: _sha256(output_tile_dir / name) for role, name in files.items()}
    artifact = PrintTileArtifactManifest(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        layout_id=plan.layout_id,
        row=tile.row,
        column=tile.column,
        source_tile_mesh_manifest_sha256=source_record.tile_mesh_manifest_sha256,
        connector_plan_sha256=plan_sha256,
        male_connector_ids=validation.male_connector_ids,
        female_connector_ids=validation.female_connector_ids,
        files=files,
        sha256=checksums,
        validation=validation,
    )
    manifest_path = _write_canonical_json(output_tile_dir / "print_tile_manifest.json", artifact)
    directory = f"tiles/{tile.tile_id}"
    record = PrintTileAssemblyRecord(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        row=tile.row,
        column=tile.column,
        directory=directory,
        tile_manifest=f"{directory}/print_tile_manifest.json",
        tile_manifest_sha256=_sha256(manifest_path),
        source_tile_mesh_manifest_sha256=source_record.tile_mesh_manifest_sha256,
        files={role: f"{directory}/{name}" for role, name in files.items()},
        sha256=checksums,
        male_connector_ids=validation.male_connector_ids,
        female_connector_ids=validation.female_connector_ids,
        global_bounds_mm=validation.connector_global_bounds_mm,
        print_local_bounds_mm=validation.expected_print_local_bounds_mm,
        global_to_print_local_translation_mm=validation.global_to_print_local_translation_mm,
        triangle_count=validation.global_geometry.triangle_count,
        volume_mm3=validation.connector_volume_mm3,
    )
    return artifact, record, source_mesh, _load_stl(global_stl)


def generate_print_tile_set(
    mesh_set_dir: Path,
    source_tile_set_dir: Path,
    source_bundle_dir: Path,
    output_dir: Path,
) -> PrintTileSetResult:
    """Generate connector-bearing global and print-local tile artifacts."""
    mesh_set = mesh_set_dir.expanduser().resolve()
    tile_set = source_tile_set_dir.expanduser().resolve()
    bundle = source_bundle_dir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise ConfigurationError(f"print tile destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    layout, source_manifest, policy = _source_records(mesh_set, tile_set, bundle)
    source_mesh_manifest_hash = _sha256(mesh_set / "tile-mesh-assembly-manifest.json")
    plan = plan_connectors(
        layout,
        source_tile_mesh_assembly_sha256=source_mesh_manifest_hash,
        policy=policy,
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.topoforge-stage-", dir=output.parent))
    try:
        plan_path = _write_canonical_json(staging / "connector-plan.json", plan)
        plan_sha256 = _sha256(plan_path)
        _render_connector_map(layout, plan, staging / "connector-map.png")
        source_record_by_id = {record.tile_id: record for record in source_manifest.tiles}
        records: list[PrintTileAssemblyRecord] = []
        validations: dict[str, PrintTileValidation] = {}
        source_meshes: dict[str, trimesh.Trimesh] = {}
        connector_meshes: dict[str, trimesh.Trimesh] = {}
        for tile in layout.tiles:
            _, record, source_mesh, connector_mesh = _tile_record(
                tile,
                plan=plan,
                plan_sha256=plan_sha256,
                source_record=source_record_by_id[tile.tile_id],
                mesh_set=mesh_set,
                output_tile_dir=staging / "tiles" / tile.tile_id,
            )
            records.append(record)
            validation_value = _read_canonical_json(
                staging / record.files["validation"], PrintTileValidation
            )
            if not isinstance(validation_value, PrintTileValidation):
                raise AssertionError("unexpected print tile validation model")
            validations[tile.tile_id] = validation_value
            source_meshes[tile.tile_id] = source_mesh
            connector_meshes[tile.tile_id] = connector_mesh
        preview_mesh = trimesh.util.concatenate(
            [connector_meshes[tile.tile_id] for tile in layout.tiles]
        )
        export_glb(preview_mesh, staging / "connector-assembly.global.glb")
        assembly_validation = _measure_assembly(
            layout,
            plan=plan,
            source_meshes=source_meshes,
            connector_meshes=connector_meshes,
            validations=validations,
        )
        if not assembly_validation.required_checks_passed:
            raise MeshValidationError("connector tile assembly validation failed")
        validation_path = _write_canonical_json(
            staging / "print-tile-assembly-validation.json", assembly_validation
        )
        manifest = PrintTileAssemblyManifest(
            layout_id=layout.layout_id,
            source_tile_mesh_assembly_sha256=source_mesh_manifest_hash,
            source_tile_set_assembly_sha256=_sha256(tile_set / "assembly_manifest.json"),
            source_bundle_manifest_sha256=_sha256(bundle / "build_manifest.json"),
            connector_plan_sha256=plan_sha256,
            connector_map_sha256=_sha256(staging / "connector-map.png"),
            assembly_preview_sha256=_sha256(staging / "connector-assembly.global.glb"),
            assembly_validation_sha256=_sha256(validation_path),
            tile_grid_shape=layout.tile_grid_shape,
            tile_count=layout.tile_count,
            seam_count=plan.seam_count,
            connector_count=plan.connector_count,
            tiles=tuple(records),
        )
        _write_canonical_json(staging / "print-tile-assembly-manifest.json", manifest)
        verify_print_tile_set(staging, mesh_set, tile_set, bundle)
        staging.replace(output)
        return PrintTileSetResult(
            output_dir=output,
            connector_plan_path=output / "connector-plan.json",
            assembly_manifest_path=output / "print-tile-assembly-manifest.json",
            assembly_validation_path=output / "print-tile-assembly-validation.json",
            connector_map_path=output / "connector-map.png",
            assembly_preview_path=output / "connector-assembly.global.glb",
            tile_manifest_paths=tuple(output / record.tile_manifest for record in records),
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_print_tile_set(
    print_set_dir: Path,
    source_mesh_set_dir: Path,
    source_tile_set_dir: Path,
    source_bundle_dir: Path,
) -> dict[str, Any]:
    """Strictly reopen connector artifacts and remeasure fit and placement."""
    root = print_set_dir.expanduser().resolve()
    mesh_set = source_mesh_set_dir.expanduser().resolve()
    tile_set = source_tile_set_dir.expanduser().resolve()
    bundle = source_bundle_dir.expanduser().resolve()
    required = (
        root / "connector-plan.json",
        root / "connector-map.png",
        root / "connector-assembly.global.glb",
        root / "print-tile-assembly-validation.json",
        root / "print-tile-assembly-manifest.json",
    )
    if not all(path.is_file() and path.stat().st_size > 0 for path in required):
        raise ConfigurationError("print tile set is missing a required root artifact")
    layout, source_manifest, policy = _source_records(mesh_set, tile_set, bundle)
    manifest_value = _read_canonical_json(
        root / "print-tile-assembly-manifest.json", PrintTileAssemblyManifest
    )
    plan_value = _read_canonical_json(root / "connector-plan.json", ConnectorPlan)
    validation_value = _read_canonical_json(
        root / "print-tile-assembly-validation.json", PrintTileAssemblyValidation
    )
    if not isinstance(manifest_value, PrintTileAssemblyManifest):
        raise AssertionError("unexpected print assembly manifest model")
    if not isinstance(plan_value, ConnectorPlan):
        raise AssertionError("unexpected connector plan model")
    if not isinstance(validation_value, PrintTileAssemblyValidation):
        raise AssertionError("unexpected print assembly validation model")
    manifest = manifest_value
    plan = plan_value
    reported_validation = validation_value
    source_mesh_manifest_hash = _sha256(mesh_set / "tile-mesh-assembly-manifest.json")
    expected_plan = plan_connectors(
        layout,
        source_tile_mesh_assembly_sha256=source_mesh_manifest_hash,
        policy=policy,
    )
    if plan != expected_plan:
        raise ConfigurationError("connector plan does not match source layout and printer policy")
    if (
        manifest.layout_id != layout.layout_id
        or manifest.source_tile_mesh_assembly_sha256 != source_mesh_manifest_hash
        or manifest.source_tile_set_assembly_sha256 != _sha256(tile_set / "assembly_manifest.json")
        or manifest.source_bundle_manifest_sha256 != _sha256(bundle / "build_manifest.json")
        or manifest.connector_plan_sha256 != _sha256(root / "connector-plan.json")
        or manifest.connector_map_sha256 != _sha256(root / "connector-map.png")
        or manifest.assembly_preview_sha256 != _sha256(root / "connector-assembly.global.glb")
        or manifest.assembly_validation_sha256
        != _sha256(root / "print-tile-assembly-validation.json")
        or manifest.tile_grid_shape != layout.tile_grid_shape
        or manifest.tile_count != layout.tile_count
        or manifest.seam_count != plan.seam_count
        or manifest.connector_count != plan.connector_count
    ):
        raise ConfigurationError("print tile assembly manifest does not match source identities")
    with Image.open(root / manifest.connector_map_path) as image:
        image.verify()
    _load_glb(root / manifest.assembly_preview_path)
    source_record_by_id = {record.tile_id: record for record in source_manifest.tiles}
    source_meshes: dict[str, trimesh.Trimesh] = {}
    connector_meshes: dict[str, trimesh.Trimesh] = {}
    validations: dict[str, PrintTileValidation] = {}
    for tile, record in zip(layout.tiles, manifest.tiles, strict=True):
        expected_directory = f"tiles/{tile.tile_id}"
        expected_manifest = f"{expected_directory}/print_tile_manifest.json"
        if (
            record.tile_id != tile.tile_id
            or record.tile_key != tile.tile_key
            or (record.row, record.column) != (tile.row, tile.column)
            or record.directory != expected_directory
            or record.tile_manifest != expected_manifest
        ):
            raise ConfigurationError(f"print tile root record mismatch: {tile.tile_id}")
        tile_dir = _resolve_relative(root, record.directory)
        artifact_path = _resolve_relative(root, record.tile_manifest)
        if (
            artifact_path.parent != tile_dir
            or _sha256(artifact_path) != record.tile_manifest_sha256
        ):
            raise ConfigurationError(f"print tile manifest checksum mismatch: {tile.tile_id}")
        artifact_value = _read_canonical_json(artifact_path, PrintTileArtifactManifest)
        if not isinstance(artifact_value, PrintTileArtifactManifest):
            raise AssertionError("unexpected print tile artifact model")
        artifact = artifact_value
        source_record = source_record_by_id[tile.tile_id]
        if (
            artifact.tile_id != tile.tile_id
            or artifact.tile_key != tile.tile_key
            or artifact.layout_id != layout.layout_id
            or (artifact.row, artifact.column) != (tile.row, tile.column)
            or artifact.source_tile_mesh_manifest_sha256 != source_record.tile_mesh_manifest_sha256
            or artifact.connector_plan_sha256 != manifest.connector_plan_sha256
            or artifact.sha256 != record.sha256
            or artifact.male_connector_ids != record.male_connector_ids
            or artifact.female_connector_ids != record.female_connector_ids
            or record.files
            != {role: f"{expected_directory}/{name}" for role, name in artifact.files.items()}
        ):
            raise ConfigurationError(f"print tile artifact identity mismatch: {tile.tile_id}")
        for role, name in artifact.files.items():
            local_path = tile_dir / name
            assembly_path = _resolve_relative(root, record.files[role])
            if local_path != assembly_path or _sha256(local_path) != artifact.sha256[role]:
                raise ConfigurationError(
                    f"print tile artifact checksum mismatch: {tile.tile_id}/{role}"
                )
        source_mesh = _load_stl(_resolve_relative(mesh_set, source_record.files["model_stl"]))
        measured = _measure_tile_validation(
            tile,
            plan=plan,
            source_mesh=source_mesh,
            global_stl_path=tile_dir / artifact.files["global_stl"],
            global_3mf_path=tile_dir / artifact.files["global_3mf"],
            global_glb_path=tile_dir / artifact.files["global_glb"],
            local_stl_path=tile_dir / artifact.files["print_local_stl"],
            local_3mf_path=tile_dir / artifact.files["print_local_3mf"],
            local_glb_path=tile_dir / artifact.files["print_local_glb"],
        )
        if measured != artifact.validation or not measured.required_checks_passed:
            raise MeshValidationError(f"print tile validation changed on reopen: {tile.tile_id}")
        if (
            record.global_bounds_mm != measured.connector_global_bounds_mm
            or record.print_local_bounds_mm != measured.expected_print_local_bounds_mm
            or record.global_to_print_local_translation_mm
            != measured.global_to_print_local_translation_mm
            or record.triangle_count != measured.global_geometry.triangle_count
            or abs(record.volume_mm3 - measured.connector_volume_mm3) > 1e-9
        ):
            raise ConfigurationError(f"print tile summary mismatch: {tile.tile_id}")
        validations[tile.tile_id] = measured
        source_meshes[tile.tile_id] = source_mesh
        connector_meshes[tile.tile_id] = _load_stl(tile_dir / artifact.files["global_stl"])
    measured_assembly = _measure_assembly(
        layout,
        plan=plan,
        source_meshes=source_meshes,
        connector_meshes=connector_meshes,
        validations=validations,
    )
    if measured_assembly != reported_validation or not measured_assembly.required_checks_passed:
        raise MeshValidationError("print tile assembly validation changed on reopen")
    return {
        "status": "verified",
        "output_dir": str(root),
        "layout_id": layout.layout_id,
        "tile_grid_shape": layout.tile_grid_shape,
        "tile_count": layout.tile_count,
        "seam_count": plan.seam_count,
        "connector_count": plan.connector_count,
        "connector_fit_status": measured_assembly.connector_fit_status,
        "collision_status": measured_assembly.collision_status,
        "maximum_top_surface_deviation_mm": (measured_assembly.maximum_top_surface_deviation_mm),
        "minimum_lateral_clearance_per_side_mm": (
            measured_assembly.minimum_lateral_clearance_per_side_mm
        ),
        "minimum_vertical_clearance_mm": measured_assembly.minimum_vertical_clearance_mm,
        "connector_map": str(root / manifest.connector_map_path),
        "assembly_preview": str(root / manifest.assembly_preview_path),
        "required_checks_passed": True,
    }
