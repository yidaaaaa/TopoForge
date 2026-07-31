"""Core typed models with explicit geospatial and manufacturing units."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatasetType(StrEnum):
    """The physical surface represented by an elevation dataset."""

    DTM = "dtm"
    DSM = "dsm"
    BATHYMETRY = "bathymetry"
    MIXED = "mixed-surface"
    UNKNOWN = "unknown"


class TerrainMode(StrEnum):
    """Requested terrain semantics."""

    BEST_AVAILABLE = "best-available"
    DTM = "dtm"
    DSM = "dsm"
    BATHYMETRY = "bathymetry"


class VerticalScaleMode(StrEnum):
    """Policy used to map ground relief into model height."""

    NATURAL = "natural"
    FIT_HEIGHT = "fit-height"
    AUTO_PERCEPTUAL = "auto-perceptual"
    CUSTOM = "custom"


class BaselineMode(StrEnum):
    """Reference elevation used at the top of the printable base."""

    MINIMUM = "minimum"
    SEA_LEVEL = "sea-level"
    CUSTOM = "custom"
    LOW_PERCENTILE = "low-percentile"


class SamplingMode(StrEnum):
    """Policy used to select the processed terrain grid."""

    PRINT_AWARE = "print-aware"
    SOURCE_PRESERVING = "source-preserving"
    CUSTOM = "custom"


class AreaOfInterestInput(BaseModel):
    """User-supplied WGS84 bbox or geodesic center-radius request."""

    model_config = ConfigDict(extra="forbid")

    bbox_wgs84: tuple[float, float, float, float] | None = None
    center_wgs84: tuple[float, float] | None = None
    radius_m: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_shape(self) -> AreaOfInterestInput:
        """Require exactly one complete AOI input form."""
        has_bbox = self.bbox_wgs84 is not None
        has_center = self.center_wgs84 is not None or self.radius_m is not None
        if has_bbox == has_center:
            raise ValueError("AOI requires exactly one of bbox_wgs84 or center_wgs84 + radius_m")
        if has_center and (self.center_wgs84 is None or self.radius_m is None):
            raise ValueError("center_wgs84 and radius_m must be supplied together")
        return self


class AreaOfInterest(BaseModel):
    """Normalized WGS84 area geometry and metric processing CRS."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    user_input: dict[str, object]
    normalized_geometry_geojson: dict[str, object]
    bounds_wgs84: tuple[float, float, float, float]
    target_local_crs: str
    crosses_antimeridian: bool = False
    area_m2: float = Field(gt=0)
    normalization_method: str


class DatasetMetadata(BaseModel):
    """Dataset semantics and provenance that must survive every build."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "local"
    dataset_name: str
    dataset_version: str = "unknown"
    dataset_type: DatasetType = DatasetType.UNKNOWN
    horizontal_resolution_m: float | None = Field(default=None, gt=0)
    horizontal_crs: str
    vertical_crs: str = "unknown"
    vertical_datum: str = "unknown"
    license: str = "user-supplied; verify source terms"
    attribution: str = "Provided by the user"
    acquisition_period: str = "unknown"
    download_time: str = "unknown"
    source_urls: list[str] = Field(default_factory=list)
    checksums: dict[str, str] = Field(default_factory=dict)


class PrinterProfile(BaseModel):
    """Physical limits and sampling hints for a printer/nozzle setup."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = "bambu-p2s-0.4"
    build_volume_mm: tuple[float, float, float] = (256.0, 256.0, 256.0)
    nozzle_diameter_mm: float = Field(default=0.4, gt=0)
    layer_height_mm: float = Field(default=0.2, gt=0)
    minimum_feature_mm: float = Field(default=0.5, gt=0)
    preferred_mesh_sampling_mm: float = Field(default=0.5, gt=0)
    minimum_base_thickness_mm: float = Field(default=2.0, gt=0)
    connector_tolerance_mm: float = Field(default=0.2, ge=0)


class BuildConfig(BaseModel):
    """Resolved configuration for a local raster build."""

    model_config = ConfigDict(extra="forbid")

    dem_path: Path
    output_dir: Path
    model_width_mm: float = Field(default=180.0, gt=0)
    model_depth_mm: float | None = Field(default=None, gt=0)
    base_thickness_mm: float = Field(default=3.0, gt=0)
    terrain_mode: TerrainMode = TerrainMode.BEST_AVAILABLE
    dataset_type: DatasetType = DatasetType.UNKNOWN
    dataset_name: str | None = None
    dataset_version: str = "unknown"
    acquisition_period: str = "unknown"
    source_urls: list[str] = Field(default_factory=list)
    vertical_crs: str = "unknown"
    vertical_datum: str = "unknown"
    data_license: str = "user-supplied; verify source terms"
    attribution: str = "Provided by the user"
    source_provider: str = "local"
    source_download_time: str = "not-applicable-local-input"
    source_checksums: dict[str, str] = Field(default_factory=dict)
    source_acquisition_manifest: Path | None = None
    reference_peak_elevation_m: float | None = None
    reference_peak_elevation_note: str | None = None
    vertical_scale_mode: VerticalScaleMode = VerticalScaleMode.AUTO_PERCEPTUAL
    vertical_exaggeration: float = Field(default=1.0, gt=0)
    max_height_mm: float = Field(default=45.0, gt=0)
    min_vertical_exaggeration: float = Field(default=0.1, gt=0)
    max_vertical_exaggeration: float = Field(default=50.0, gt=0)
    robust_low_percentile: float = Field(default=0.5, ge=0, lt=50)
    robust_high_percentile: float = Field(default=99.5, gt=50, le=100)
    baseline_mode: BaselineMode = BaselineMode.MINIMUM
    baseline_elevation_m: float | None = None
    nodata_max_fraction: float = Field(default=0.05, ge=0, le=1)
    nodata_max_hole_pixels: int = Field(default=256, ge=0)
    sampling_mode: SamplingMode = SamplingMode.PRINT_AWARE
    mesh_sampling_mm: float | None = Field(default=None, gt=0)
    max_grid_cells: int = Field(default=1_500_000, ge=16)
    max_estimated_memory_mb: float = Field(default=1024.0, gt=0)
    aoi: AreaOfInterestInput | None = None
    printer_profile: PrinterProfile = Field(default_factory=PrinterProfile)
    output_formats: list[str] = Field(default_factory=lambda: ["stl", "3mf", "glb"])

    @model_validator(mode="after")
    def validate_cross_fields(self) -> BuildConfig:
        """Reject ambiguous scaling and unsafe physical settings."""
        if self.robust_low_percentile >= self.robust_high_percentile:
            msg = "robust_low_percentile must be below robust_high_percentile"
            raise ValueError(msg)
        if self.baseline_mode is BaselineMode.CUSTOM and self.baseline_elevation_m is None:
            msg = "baseline_elevation_m is required when baseline_mode is custom"
            raise ValueError(msg)
        if self.vertical_scale_mode is VerticalScaleMode.CUSTOM and self.vertical_exaggeration <= 0:
            msg = "vertical_exaggeration must be positive for custom scaling"
            raise ValueError(msg)
        if self.base_thickness_mm < self.printer_profile.minimum_base_thickness_mm:
            msg = (
                f"base_thickness_mm must be at least "
                f"{self.printer_profile.minimum_base_thickness_mm} mm for "
                f"{self.printer_profile.profile_id}"
            )
            raise ValueError(msg)
        if self.max_height_mm > self.printer_profile.build_volume_mm[2]:
            msg = "max_height_mm exceeds the printer build height"
            raise ValueError(msg)
        if self.sampling_mode is not SamplingMode.CUSTOM and self.mesh_sampling_mm is not None:
            msg = "mesh_sampling_mm is only valid when sampling_mode is custom"
            raise ValueError(msg)
        unknown_formats = set(self.output_formats) - {"stl", "3mf", "glb"}
        if unknown_formats:
            msg = f"unsupported output formats: {', '.join(sorted(unknown_formats))}"
            raise ValueError(msg)
        return self


class RasterResult(BaseModel):
    """Processed raster measurements and provenance."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    path: Path
    original_nodata_mask_path: Path | None = None
    array_shape: tuple[int, int]
    source_grid_shape: tuple[int, int]
    processed_grid_shape: tuple[int, int]
    transform: tuple[float, float, float, float, float, float]
    crs: str
    nodata: float | None
    ground_width_m: float = Field(gt=0)
    ground_depth_m: float = Field(gt=0)
    pixel_size_x_m: float = Field(gt=0)
    pixel_size_y_m: float = Field(gt=0)
    valid_fraction: float = Field(ge=0, le=1)
    interpolated_fraction: float = Field(ge=0, le=1)
    original_nodata_fraction: float = Field(ge=0, le=1)
    elevation_min_m: float
    elevation_max_m: float
    source_horizontal_resolution_m: float = Field(gt=0)
    processed_horizontal_resolution_m: float = Field(gt=0)
    downsampling_factor: float = Field(ge=1)
    physical_sample_spacing_mm: float = Field(gt=0)
    physical_sample_spacing_xy_mm: tuple[float, float]
    estimated_triangle_count: int = Field(gt=0)
    estimated_memory_mb: float = Field(gt=0)
    raw_elevation_min_m: float
    raw_elevation_max_m: float
    processed_elevation_min_m: float
    processed_elevation_max_m: float
    peak_elevation_loss_m: float = Field(ge=0)
    raw_peak_coordinate: dict[str, object]
    processed_peak_coordinate: dict[str, object]
    peak_horizontal_shift_m: float = Field(ge=0)
    sampling_decision_reasons: list[str]
    sampling_warnings: list[str] = Field(default_factory=list)
    terrain_fidelity_status: str
    terrain_fidelity_passed: bool
    peak_elevation_loss_threshold_m: float = Field(gt=0)
    peak_horizontal_shift_threshold_m: float = Field(gt=0)
    source_bounds: dict[str, object]
    aoi: dict[str, object] | None = None
    metadata: DatasetMetadata


class ScalingResult(BaseModel):
    """Resolved physical scale for a mesh build."""

    horizontal_scale_mm_per_m: float = Field(gt=0)
    model_width_mm: float = Field(gt=0)
    model_depth_mm: float = Field(gt=0)
    base_thickness_mm: float = Field(gt=0)
    baseline_elevation_m: float
    robust_low_elevation_m: float
    robust_high_elevation_m: float
    policy_vertical_exaggeration: float = Field(gt=0)
    vertical_exaggeration: float = Field(gt=0)
    height_limit_mm: float = Field(gt=0)
    height_limit_applied: bool
    predicted_min_z_mm: float
    predicted_max_z_mm: float
    scale_mode: VerticalScaleMode


class BuildManifest(BaseModel):
    """Summary linking configuration, raster, geometry, and generated files."""

    model_config = ConfigDict(extra="forbid")

    topoforge_version: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    config_sha256: str
    source_sha256: str
    mesh_sha256: str
    artifacts: dict[str, str]
