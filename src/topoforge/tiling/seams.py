"""Deterministic numerical seam measurements for extracted terrain tiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

import numpy as np
import rasterio
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError, RasterProcessingError
from topoforge.tiling.layout import GridWindow, TerrainTile, TileLayout

_SEAM_SCHEMA_VERSION = "topoforge-tile-seam-report-v1"
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SeamDirection = Literal["east-west", "north-south"]


class TileSeamComparison(BaseModel):
    """Measured equality for one adjacent tile pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seam_id: str
    direction: SeamDirection
    first_tile_id: str
    second_tile_id: str
    shared_core_window: GridWindow
    overlap_window: GridWindow
    shared_core_sample_count: int = Field(gt=0)
    overlap_sample_count: int = Field(gt=0)
    crs_match: bool
    transform_alignment_max_error_m: float = Field(ge=0)
    core_elevation_max_abs_difference_m: float = Field(ge=0)
    core_elevation_mean_abs_difference_m: float = Field(ge=0)
    core_elevation_mismatch_count: int = Field(ge=0)
    overlap_elevation_max_abs_difference_m: float = Field(ge=0)
    overlap_elevation_mean_abs_difference_m: float = Field(ge=0)
    overlap_elevation_mismatch_count: int = Field(ge=0)
    core_mask_mismatch_count: int = Field(ge=0)
    overlap_mask_mismatch_count: int = Field(ge=0)
    required_checks_passed: bool


class TileSeamReport(BaseModel):
    """Canonical numerical seam report for one complete tile layout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SEAM_SCHEMA_VERSION
    layout_id: str
    source_bundle_manifest_sha256: Sha256Hex
    elevation_tolerance_m: float = Field(ge=0)
    seam_count: int = Field(ge=0)
    expected_seam_count: int = Field(ge=0)
    total_shared_core_sample_count: int = Field(ge=0)
    total_overlap_sample_count: int = Field(ge=0)
    maximum_core_elevation_difference_m: float = Field(ge=0)
    maximum_overlap_elevation_difference_m: float = Field(ge=0)
    total_core_elevation_mismatch_count: int = Field(ge=0)
    total_overlap_elevation_mismatch_count: int = Field(ge=0)
    total_core_mask_mismatch_count: int = Field(ge=0)
    total_overlap_mask_mismatch_count: int = Field(ge=0)
    maximum_transform_alignment_error_m: float = Field(ge=0)
    all_crs_match: bool
    terrain_seam_status: Literal["passed", "failed"]
    required_checks_passed: bool
    seams: list[TileSeamComparison]

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.seam_count != len(self.seams) or self.seam_count != self.expected_seam_count:
            raise ValueError("seam count does not match the layout adjacency count")
        if len({seam.seam_id for seam in self.seams}) != len(self.seams):
            raise ValueError("seam ids must be unique")
        passed = all(seam.required_checks_passed for seam in self.seams)
        if self.required_checks_passed != passed:
            raise ValueError("seam report pass status does not match seam measurements")
        if self.terrain_seam_status != ("passed" if passed else "failed"):
            raise ValueError("terrain_seam_status does not match required checks")
        return self


@dataclass(frozen=True)
class _TileRasterData:
    elevations: np.ndarray[Any, Any]
    mask: np.ndarray[Any, Any]
    crs: str
    transform: tuple[float, float, float, float, float, float]


def _transform_tuple(transform: Any) -> tuple[float, float, float, float, float, float]:
    return tuple(float(transform[index]) for index in range(6))  # type: ignore[return-value]


def _load_tile_raster(tile: TerrainTile, dem_path: Path, mask_path: Path) -> _TileRasterData:
    with rasterio.open(dem_path) as dem, rasterio.open(mask_path) as mask:
        expected_shape = (
            tile.sampling_window.row_stop - tile.sampling_window.row_start,
            tile.sampling_window.column_stop - tile.sampling_window.column_start,
        )
        if (
            dem.count != 1
            or mask.count != 1
            or dem.crs is None
            or dem.crs != mask.crs
            or dem.shape != expected_shape
            or mask.shape != expected_shape
            or not dem.transform.almost_equals(mask.transform)
        ):
            raise RasterProcessingError(f"tile seam raster alignment is invalid: {tile.tile_id}")
        elevations = dem.read(1)
        mask_values = mask.read(1)
        if not np.all(np.isfinite(elevations)) or not np.all(np.isin(mask_values, (0, 1))):
            raise RasterProcessingError(f"tile seam raster values are invalid: {tile.tile_id}")
        return _TileRasterData(
            elevations=elevations,
            mask=mask_values,
            crs=str(dem.crs),
            transform=_transform_tuple(dem.transform),
        )


def _intersection(first: GridWindow, second: GridWindow) -> GridWindow:
    values = (
        max(first.row_start, second.row_start),
        min(first.row_stop, second.row_stop),
        max(first.column_start, second.column_start),
        min(first.column_stop, second.column_stop),
    )
    if values[1] <= values[0] or values[3] <= values[2]:
        raise ConfigurationError("adjacent tile sampling windows do not overlap")
    return GridWindow(
        row_start=values[0], row_stop=values[1], column_start=values[2], column_stop=values[3]
    )


def _shared_core_window(
    first: TerrainTile, second: TerrainTile, direction: SeamDirection
) -> GridWindow:
    if direction == "east-west":
        seam_column = first.core_cell_window.column_stop
        if seam_column != second.core_cell_window.column_start:
            raise ConfigurationError("east-west tiles do not share a core boundary column")
        return GridWindow(
            row_start=max(first.core_sample_window.row_start, second.core_sample_window.row_start),
            row_stop=min(first.core_sample_window.row_stop, second.core_sample_window.row_stop),
            column_start=seam_column,
            column_stop=seam_column + 1,
        )
    seam_row = first.core_cell_window.row_stop
    if seam_row != second.core_cell_window.row_start:
        raise ConfigurationError("north-south tiles do not share a core boundary row")
    return GridWindow(
        row_start=seam_row,
        row_stop=seam_row + 1,
        column_start=max(
            first.core_sample_window.column_start, second.core_sample_window.column_start
        ),
        column_stop=min(
            first.core_sample_window.column_stop, second.core_sample_window.column_stop
        ),
    )


def _local_values(
    values: np.ndarray[Any, Any], sampling_window: GridWindow, global_window: GridWindow
) -> np.ndarray[Any, Any]:
    row_start = global_window.row_start - sampling_window.row_start
    row_stop = global_window.row_stop - sampling_window.row_start
    column_start = global_window.column_start - sampling_window.column_start
    column_stop = global_window.column_stop - sampling_window.column_start
    return values[row_start:row_stop, column_start:column_stop]


def _coordinate(
    transform: tuple[float, float, float, float, float, float],
    sampling_window: GridWindow,
    global_row: int,
    global_column: int,
) -> tuple[float, float]:
    column = global_column - sampling_window.column_start + 0.5
    row = global_row - sampling_window.row_start + 0.5
    a, b, c, d, e, f = transform
    return a * column + b * row + c, d * column + e * row + f


def _alignment_error(
    first: TerrainTile,
    second: TerrainTile,
    first_data: _TileRasterData,
    second_data: _TileRasterData,
    overlap: GridWindow,
) -> float:
    maximum = 0.0
    for row in sorted({overlap.row_start, overlap.row_stop - 1}):
        for column in sorted({overlap.column_start, overlap.column_stop - 1}):
            first_xy = _coordinate(first_data.transform, first.sampling_window, row, column)
            second_xy = _coordinate(second_data.transform, second.sampling_window, row, column)
            maximum = max(
                maximum,
                float(np.hypot(first_xy[0] - second_xy[0], first_xy[1] - second_xy[1])),
            )
    return maximum


def _differences(
    first: np.ndarray[Any, Any], second: np.ndarray[Any, Any], tolerance_m: float
) -> tuple[float, float, int]:
    difference = np.abs(first.astype(np.float64) - second.astype(np.float64))
    return (
        float(np.max(difference)),
        float(np.mean(difference)),
        int(np.count_nonzero(difference > tolerance_m)),
    )


def _compare_pair(
    first: TerrainTile,
    second: TerrainTile,
    *,
    direction: SeamDirection,
    first_data: _TileRasterData,
    second_data: _TileRasterData,
    elevation_tolerance_m: float,
) -> TileSeamComparison:
    overlap = _intersection(first.sampling_window, second.sampling_window)
    core = _shared_core_window(first, second, direction)
    core_first = _local_values(first_data.elevations, first.sampling_window, core)
    core_second = _local_values(second_data.elevations, second.sampling_window, core)
    overlap_first = _local_values(first_data.elevations, first.sampling_window, overlap)
    overlap_second = _local_values(second_data.elevations, second.sampling_window, overlap)
    core_max, core_mean, core_mismatches = _differences(
        core_first, core_second, elevation_tolerance_m
    )
    overlap_max, overlap_mean, overlap_mismatches = _differences(
        overlap_first, overlap_second, elevation_tolerance_m
    )
    core_mask_mismatches = int(
        np.count_nonzero(
            _local_values(first_data.mask, first.sampling_window, core)
            != _local_values(second_data.mask, second.sampling_window, core)
        )
    )
    overlap_mask_mismatches = int(
        np.count_nonzero(
            _local_values(first_data.mask, first.sampling_window, overlap)
            != _local_values(second_data.mask, second.sampling_window, overlap)
        )
    )
    transform_error = _alignment_error(first, second, first_data, second_data, overlap)
    crs_match = first_data.crs == second_data.crs
    passed = (
        crs_match
        and transform_error <= 1e-9
        and core_mismatches == 0
        and overlap_mismatches == 0
        and core_mask_mismatches == 0
        and overlap_mask_mismatches == 0
    )
    return TileSeamComparison(
        seam_id=f"seam-{first.tile_id}-{direction}-{second.tile_id}",
        direction=direction,
        first_tile_id=first.tile_id,
        second_tile_id=second.tile_id,
        shared_core_window=core,
        overlap_window=overlap,
        shared_core_sample_count=(core.row_stop - core.row_start)
        * (core.column_stop - core.column_start),
        overlap_sample_count=(overlap.row_stop - overlap.row_start)
        * (overlap.column_stop - overlap.column_start),
        crs_match=crs_match,
        transform_alignment_max_error_m=transform_error,
        core_elevation_max_abs_difference_m=core_max,
        core_elevation_mean_abs_difference_m=core_mean,
        core_elevation_mismatch_count=core_mismatches,
        overlap_elevation_max_abs_difference_m=overlap_max,
        overlap_elevation_mean_abs_difference_m=overlap_mean,
        overlap_elevation_mismatch_count=overlap_mismatches,
        core_mask_mismatch_count=core_mask_mismatches,
        overlap_mask_mismatch_count=overlap_mask_mismatches,
        required_checks_passed=passed,
    )


def measure_tile_seams(
    layout: TileLayout,
    *,
    dem_paths: dict[str, Path],
    mask_paths: dict[str, Path],
    source_bundle_manifest_sha256: str,
    elevation_tolerance_m: float = 0.0,
) -> TileSeamReport:
    """Measure every east/south adjacency once in stable row-major order."""
    if elevation_tolerance_m < 0:
        raise ConfigurationError("elevation_tolerance_m must be non-negative")
    expected_ids = {tile.tile_id for tile in layout.tiles}
    if set(dem_paths) != expected_ids or set(mask_paths) != expected_ids:
        raise ConfigurationError("seam raster maps must contain every layout tile exactly once")
    data = {
        tile.tile_id: _load_tile_raster(tile, dem_paths[tile.tile_id], mask_paths[tile.tile_id])
        for tile in layout.tiles
    }
    tile_by_id = {tile.tile_id: tile for tile in layout.tiles}
    seams: list[TileSeamComparison] = []
    for tile in layout.tiles:
        for neighbor_id, direction in (
            (tile.east_neighbor, cast(SeamDirection, "east-west")),
            (tile.south_neighbor, cast(SeamDirection, "north-south")),
        ):
            if neighbor_id is None:
                continue
            neighbor = tile_by_id[neighbor_id]
            seams.append(
                _compare_pair(
                    tile,
                    neighbor,
                    direction=cast(SeamDirection, direction),
                    first_data=data[tile.tile_id],
                    second_data=data[neighbor_id],
                    elevation_tolerance_m=elevation_tolerance_m,
                )
            )
    expected_count = layout.tile_grid_shape[0] * max(0, layout.tile_grid_shape[1] - 1)
    expected_count += max(0, layout.tile_grid_shape[0] - 1) * layout.tile_grid_shape[1]
    passed = all(seam.required_checks_passed for seam in seams)
    return TileSeamReport(
        layout_id=layout.layout_id,
        source_bundle_manifest_sha256=source_bundle_manifest_sha256,
        elevation_tolerance_m=elevation_tolerance_m,
        seam_count=len(seams),
        expected_seam_count=expected_count,
        total_shared_core_sample_count=sum(item.shared_core_sample_count for item in seams),
        total_overlap_sample_count=sum(item.overlap_sample_count for item in seams),
        maximum_core_elevation_difference_m=max(
            (item.core_elevation_max_abs_difference_m for item in seams), default=0.0
        ),
        maximum_overlap_elevation_difference_m=max(
            (item.overlap_elevation_max_abs_difference_m for item in seams), default=0.0
        ),
        total_core_elevation_mismatch_count=sum(
            item.core_elevation_mismatch_count for item in seams
        ),
        total_overlap_elevation_mismatch_count=sum(
            item.overlap_elevation_mismatch_count for item in seams
        ),
        total_core_mask_mismatch_count=sum(item.core_mask_mismatch_count for item in seams),
        total_overlap_mask_mismatch_count=sum(item.overlap_mask_mismatch_count for item in seams),
        maximum_transform_alignment_error_m=max(
            (item.transform_alignment_max_error_m for item in seams), default=0.0
        ),
        all_crs_match=all(item.crs_match for item in seams),
        terrain_seam_status="passed" if passed else "failed",
        required_checks_passed=passed,
        seams=seams,
    )
