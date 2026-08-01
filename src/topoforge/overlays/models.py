"""Typed contracts for local provenance-aware terrain overlays."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OverlayKind(StrEnum):
    """Semantic role of one local overlay source."""

    GPX = "gpx"
    ROAD = "road"
    RIVER = "river"
    CONTOUR = "contour"
    LABEL = "label"
    COAST = "coast"


class OverlayFormat(StrEnum):
    """Supported local source representation."""

    GPX = "gpx"
    GEOJSON = "geojson"
    GENERATED_CONTOURS = "generated-contours"


_DEFAULT_COLORS = {
    OverlayKind.GPX: "#d1495b",
    OverlayKind.ROAD: "#e9c46a",
    OverlayKind.RIVER: "#277da1",
    OverlayKind.CONTOUR: "#7f5539",
    OverlayKind.LABEL: "#111111",
    OverlayKind.COAST: "#0096c7",
}


class OverlayStyle(BaseModel):
    """Manufacturing and preview dimensions for one overlay layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    line_width_mm: float = Field(default=0.8, gt=0)
    raised_height_mm: float = Field(default=0.4, gt=0)
    embed_depth_mm: float = Field(default=0.2, ge=0)
    label_font_height_mm: float = Field(default=4.0, gt=0)
    simplify_tolerance_mm: float = Field(default=0.0, ge=0)


class OverlaySourceConfig(BaseModel):
    """One local vector source or DEM-derived contour request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    kind: OverlayKind
    format: OverlayFormat
    path: Path | None = None
    source_crs: str = "EPSG:4326"
    dataset_name: str
    dataset_version: str = "unknown"
    license: str = Field(min_length=1)
    attribution: str = Field(min_length=1)
    source_urls: tuple[str, ...] = ()
    acquisition_period: str = "unknown"
    label_property: str = "name"
    contour_interval_m: float | None = Field(default=None, gt=0)
    contour_min_elevation_m: float | None = None
    contour_max_elevation_m: float | None = None
    style: OverlayStyle = Field(default_factory=OverlayStyle)

    @model_validator(mode="after")
    def validate_source(self) -> OverlaySourceConfig:
        """Reject ambiguous source/format combinations."""
        if self.format is OverlayFormat.GENERATED_CONTOURS:
            if self.kind is not OverlayKind.CONTOUR:
                raise ValueError("generated-contours format requires kind=contour")
            if self.path is not None:
                raise ValueError("generated-contours must not specify path")
            if self.contour_interval_m is None:
                raise ValueError("generated-contours requires contour_interval_m")
            if (
                self.contour_min_elevation_m is not None
                and self.contour_max_elevation_m is not None
                and self.contour_min_elevation_m >= self.contour_max_elevation_m
            ):
                raise ValueError("contour minimum elevation must be below maximum elevation")
        else:
            if self.path is None:
                raise ValueError(f"{self.format.value} overlay requires a local path")
            if self.contour_interval_m is not None:
                raise ValueError("contour_interval_m is only valid for generated-contours")
        if self.format is OverlayFormat.GPX:
            if self.kind is not OverlayKind.GPX:
                raise ValueError("gpx format requires kind=gpx")
            if self.source_crs.upper() not in {"EPSG:4326", "OGC:CRS84"}:
                raise ValueError("GPX coordinates must use WGS84 longitude/latitude")
        if self.kind is OverlayKind.LABEL and self.format is not OverlayFormat.GEOJSON:
            raise ValueError("label overlays currently require GeoJSON point features")
        return self

    @property
    def resolved_color(self) -> str:
        """Return an explicit stable color for preview artifacts."""
        return self.style.color or _DEFAULT_COLORS[self.kind]


class OverlayConfig(BaseModel):
    """Complete local overlay request independent of output paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[OverlaySourceConfig, ...] = Field(min_length=1)
    allow_original_nodata: bool = False
    clip_to_model: bool = True
    max_features: int = Field(default=20_000, ge=1)
    max_triangles: int = Field(default=2_000_000, ge=12)
    preview_width_px: int = Field(default=1200, ge=320, le=4096)

    @model_validator(mode="after")
    def validate_sources(self) -> OverlayConfig:
        """Require stable unique layer identities."""
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("overlay source_id values must be unique")
        return self


class OverlaySourceRecord(BaseModel):
    """Checksum-bound source and clipping measurements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    kind: OverlayKind
    format: OverlayFormat
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    source_crs: str
    processed_crs: str
    dataset_name: str
    dataset_version: str
    license: str
    attribution: str
    source_urls: tuple[str, ...]
    acquisition_period: str
    input_feature_count: int = Field(ge=0)
    output_feature_count: int = Field(ge=0)
    clipped_feature_count: int = Field(ge=0)
    dropped_feature_count: int = Field(ge=0)
    input_length_m: float = Field(ge=0)
    clipped_length_m: float = Field(ge=0)
    contour_levels_m: tuple[float, ...] = ()


class OverlayLayerRecord(BaseModel):
    """Strictly reopened manufacturing object for one source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    kind: OverlayKind
    object_name: str
    stl_path: str
    stl_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_count: int = Field(ge=1)
    vertex_count: int = Field(ge=4)
    triangle_count: int = Field(ge=4)
    connected_components: int = Field(ge=1)
    volume_mm3: float = Field(gt=0)
    bounds_mm: tuple[tuple[float, float, float], tuple[float, float, float]]
    watertight: bool
    winding_consistent: bool
    positive_volume: bool
    maximum_surface_mapping_error_mm: float = Field(ge=0)
    original_nodata_overlap_mm2: float = Field(ge=0)
    color: str


class OverlayValidation(BaseModel):
    """Measured Phase 7 overlay validation report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-overlay-validation-v1"
    source_bundle: str
    source_build_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_terrain_sha256_before: dict[str, str]
    source_terrain_sha256_after: dict[str, str]
    terrain_artifacts_unchanged: bool
    coordinate_system: str
    orientation_transform: str
    source_records: tuple[OverlaySourceRecord, ...]
    layers: tuple[OverlayLayerRecord, ...]
    total_feature_count: int = Field(ge=1)
    total_triangle_count: int = Field(ge=4)
    combined_3mf_object_count: int = Field(ge=2)
    combined_3mf_build_item_count: int = Field(ge=1)
    combined_3mf_components_object_count: int = Field(ge=1)
    combined_3mf_component_count: int = Field(ge=2)
    combined_3mf_base_material_group_count: int = Field(ge=1)
    combined_3mf_material_assigned_object_count: int = Field(ge=2)
    combined_3mf_triangle_count: int = Field(ge=4)
    combined_3mf_strict_warning_count: int = Field(ge=0)
    combined_glb_geometry_count: int = Field(ge=2)
    preview_size_px: tuple[int, int]
    minimum_feature_mm: float = Field(gt=0)
    minimum_feature_checks_passed: bool
    original_nodata_check_passed: bool
    bounds_check_passed: bool
    layer_geometry_checks_passed: bool
    format_reopen_checks_passed: bool
    deterministic_contract: str
    terrain_surface_modified: bool
    required_checks_passed: bool


class OverlayManifest(BaseModel):
    """Checksum manifest for a completed overlay bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-overlay-manifest-v1"
    topoforge_version: str
    source_bundle: str
    source_build_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overlay_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_identities: tuple[dict[str, Any], ...]
    artifacts: dict[str, str]
    sha256: dict[str, str]
    layer_artifacts: dict[str, str]
    required_checks_passed: bool
