"""Local GeoTIFF processing with AOI clipping and printer-aware sampling."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import rasterio
from affine import Affine
from pyproj import CRS, Geod, Transformer
from rasterio.enums import Resampling
from rasterio.errors import RasterioError, WindowError
from rasterio.features import geometry_window
from rasterio.io import DatasetReader
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject, transform_bounds, transform_geom
from rasterio.windows import Window
from rasterio.windows import bounds as window_bounds
from rasterio.windows import transform as window_transform
from scipy import ndimage
from shapely.geometry import box, shape

from topoforge.exceptions import RasterProcessingError
from topoforge.models import AreaOfInterest, BuildConfig, DatasetMetadata, RasterResult
from topoforge.raster.aoi import aoi_provenance, normalize_area_of_interest
from topoforge.raster.sampling import SamplingDecision, resolve_sampling_decision
from topoforge.util.hashing import sha256_file

FloatArray = npt.NDArray[np.float32]
BoolArray = npt.NDArray[np.bool_]
_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True, slots=True)
class ProcessedRaster:
    """In-memory elevations plus the serializable processing report."""

    elevations_m: FloatArray
    original_nodata_mask: BoolArray
    transform: Affine
    crs: CRS
    report: RasterResult


@dataclass(frozen=True, slots=True)
class _SourceFacts:
    full_shape: tuple[int, int]
    selected_shape: tuple[int, int]
    crs: CRS
    transform: Affine
    native_bounds: tuple[float, float, float, float]
    bounds_wgs84: tuple[float, float, float, float]
    horizontal_resolution_m: float
    raw_elevation_min_m: float
    raw_elevation_max_m: float
    raw_peak_coordinate: dict[str, object]
    tags: dict[str, str]
    aoi_report: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class _SourceSelection:
    """One pixel-aligned source selection, possibly split at the antimeridian."""

    windows: tuple[Window, ...]
    coverage_status: str
    multipart_antimeridian: bool = False


def _is_north_up(transform: Affine) -> bool:
    return (
        transform.a > 0
        and transform.e < 0
        and abs(transform.b) < 1e-12
        and abs(transform.d) < 1e-12
    )


def _uses_metre_axes(crs: CRS) -> bool:
    if not crs.is_projected or not crs.axis_info:
        return False
    return all(abs((axis.unit_conversion_factor or 0.0) - 1.0) < 1e-9 for axis in crs.axis_info[:2])


def _choose_metric_crs(source_crs: CRS, bounds: tuple[float, float, float, float]) -> CRS:
    try:
        west, south, east, north = transform_bounds(
            source_crs,
            "EPSG:4326",
            *bounds,
            densify_pts=21,
        )
    except Exception as exc:  # rasterio/PROJ exposes environment-specific errors
        msg = "Could not transform raster bounds to longitude/latitude; verify the GeoTIFF CRS"
        raise RasterProcessingError(msg) from exc
    lon = (west + east) / 2.0
    lat = (south + north) / 2.0
    if -80.0 <= lat <= 84.0 and east - west <= 12.0:
        zone = min(60, max(1, int((lon + 180.0) // 6.0) + 1))
        epsg = 32600 + zone if lat >= 0 else 32700 + zone
        return CRS.from_epsg(epsg)
    return CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat:.10f} +lon_0={lon:.10f} +datum=WGS84 +units=m +no_defs"
    )


def largest_true_rectangle(mask: BoolArray) -> tuple[int, int, int, int]:
    """Return top, bottom, left, right for the largest all-true rectangle."""
    rows, columns = mask.shape
    heights = np.zeros(columns, dtype=np.int64)
    best_area = 0
    best = (0, 0, 0, 0)
    for row in range(rows):
        heights = np.where(mask[row], heights + 1, 0)
        stack: list[tuple[int, int]] = []
        for column in range(columns + 1):
            height = int(heights[column]) if column < columns else 0
            start = column
            while stack and stack[-1][1] > height:
                index, previous_height = stack.pop()
                area = previous_height * (column - index)
                if area > best_area:
                    best_area = area
                    best = (row - previous_height + 1, row + 1, index, column)
                start = index
            if not stack or stack[-1][1] < height:
                stack.append((start, height))
    if best_area == 0:
        raise RasterProcessingError("Reprojection produced no covered metric raster cells")
    return best


def _crop_to_source_coverage(
    elevations: FloatArray,
    coverage: BoolArray,
    nodata_mask: BoolArray,
    transform: Affine,
) -> tuple[FloatArray, BoolArray, Affine]:
    """Remove reprojection-only corner gaps using the largest covered rectangle."""
    if bool(np.all(coverage)):
        return elevations, nodata_mask, transform
    top, bottom, left, right = largest_true_rectangle(coverage)
    if bottom - top < 2 or right - left < 2:
        raise RasterProcessingError("Metric reprojection left fewer than 2 x 2 covered cells")
    cropped = elevations[top:bottom, left:right]
    cropped_mask = nodata_mask[top:bottom, left:right]
    cropped_transform = transform * Affine.translation(left, top)
    return (
        np.asarray(cropped, dtype=np.float32),
        np.asarray(cropped_mask, dtype=np.bool_),
        cropped_transform,
    )


def _pixel_resolution_m(transform: Affine, crs: CRS, shape_: tuple[int, int]) -> float:
    pixel_x = float(np.hypot(transform.a, transform.d))
    pixel_y = float(np.hypot(transform.b, transform.e))
    if _uses_metre_axes(crs):
        return (pixel_x + pixel_y) / 2.0
    rows, columns = shape_
    center_row = (rows - 1) / 2.0
    center_column = (columns - 1) / 2.0
    points = [
        transform * (center_column + 0.5, center_row + 0.5),
        transform * (center_column + 1.5, center_row + 0.5),
        transform * (center_column + 0.5, center_row + 1.5),
    ]
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon0, lat0 = to_wgs84.transform(*points[0])
    lon_x, lat_x = to_wgs84.transform(*points[1])
    lon_y, lat_y = to_wgs84.transform(*points[2])
    distance_x = abs(float(_GEOD.inv(lon0, lat0, lon_x, lat_x)[2]))
    distance_y = abs(float(_GEOD.inv(lon0, lat0, lon_y, lat_y)[2]))
    return (distance_x + distance_y) / 2.0


def _coordinate_record(
    transform: Affine,
    crs: CRS,
    *,
    row: int,
    column: int,
) -> dict[str, object]:
    x = float(transform.a * (column + 0.5) + transform.b * (row + 0.5) + transform.c)
    y = float(transform.d * (column + 0.5) + transform.e * (row + 0.5) + transform.f)
    longitude, latitude = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(x, y)
    return {
        "row": row,
        "column": column,
        "x": float(x),
        "y": float(y),
        "crs": crs.to_string(),
        "longitude": float(longitude),
        "latitude": float(latitude),
    }


def _peak_record(elevations: FloatArray, transform: Affine, crs: CRS) -> dict[str, object]:
    finite = np.where(np.isfinite(elevations), elevations, -np.inf)
    flat_index = int(np.argmax(finite))
    row, column = np.unravel_index(flat_index, elevations.shape)
    return _coordinate_record(transform, crs, row=int(row), column=int(column))


def _aoi_bbox_parts(aoi: AreaOfInterest) -> list[tuple[float, float, float, float]]:
    west, south, east, north = aoi.bounds_wgs84
    if aoi.crosses_antimeridian:
        return [(west, south, 180.0, north), (-180.0, south, east, north)]
    return [(west, south, east, north)]


def _geographic_longitude_period(crs: CRS) -> float:
    """Return one complete longitude revolution in the CRS angular unit."""
    for axis in crs.axis_info:
        if axis.direction.casefold() not in {"east", "west"}:
            continue
        factor = axis.unit_conversion_factor
        if factor is not None and math.isfinite(factor) and factor > 0:
            return math.tau / factor
    raise RasterProcessingError(
        "Geographic source CRS has no usable longitude angular unit; reproject the "
        "source to a conventional longitude/latitude grid before building"
    )


def _geographic_bounds_in_source_domain(
    source: DatasetReader,
    bounds_native: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Map one native geographic bbox into the longitude domain used by a source raster."""
    west, south, east, north = bounds_native
    source_crs = CRS.from_user_input(source.crs)
    period = _geographic_longitude_period(source_crs)
    candidates = [(west + shift, south, east + shift, north) for shift in (-period, 0.0, period)]
    source_left, _, source_right, _ = source.bounds
    source_west = min(float(source_left), float(source_right))
    source_east = max(float(source_left), float(source_right))

    def overlap_width(candidate: tuple[float, float, float, float]) -> float:
        return max(
            0.0,
            min(candidate[2], source_east) - max(candidate[0], source_west),
        )

    return max(candidates, key=overlap_width)


def _clipped_window(source: DatasetReader, candidate: Window) -> Window | None:
    row_start = max(0, int(candidate.row_off))
    column_start = max(0, int(candidate.col_off))
    row_stop = min(source.height, int(candidate.row_off + candidate.height))
    column_stop = min(source.width, int(candidate.col_off + candidate.width))
    if row_stop <= row_start or column_stop <= column_start:
        return None
    return Window.from_slices((row_start, row_stop), (column_start, column_stop))


def _axis_aligned_window_for_bounds(
    source: DatasetReader,
    bounds: tuple[float, float, float, float],
) -> Window:
    """Map native bounds to a pixel window for any axis direction."""
    inverse = ~source.transform
    west, south, east, north = bounds
    pixel_corners = [
        inverse * (x, y) for x, y in ((west, south), (west, north), (east, south), (east, north))
    ]
    columns = [float(column) for column, _row in pixel_corners]
    rows = [float(row) for _column, row in pixel_corners]
    return Window.from_slices(
        (math.floor(min(rows)), math.ceil(max(rows))),
        (math.floor(min(columns)), math.ceil(max(columns))),
        boundless=True,
    )


def _window_for_aoi(source: DatasetReader, aoi: AreaOfInterest) -> _SourceSelection:
    requested_parts = _aoi_bbox_parts(aoi)
    windows: list[Window] = []
    fully_covered: list[bool] = []
    source_crs = CRS.from_user_input(source.crs)
    source_left, source_bottom, source_right, source_top = source.bounds
    source_extent = box(
        min(source_left, source_right),
        min(source_bottom, source_top),
        max(source_left, source_right),
        max(source_bottom, source_top),
    )
    for bounds_wgs84 in requested_parts:
        native_geometry = shape(
            transform_geom(
                "EPSG:4326",
                source.crs,
                box(*bounds_wgs84).__geo_interface__,
                precision=15,
            )
        )
        if source_crs.is_geographic:
            mapped_bounds = _geographic_bounds_in_source_domain(
                source, cast(tuple[float, float, float, float], native_geometry.bounds)
            )
            transformed_geometry = box(*mapped_bounds)
        else:
            transformed_geometry = native_geometry
        if math.isclose(source.transform.b, 0.0) and math.isclose(source.transform.d, 0.0):
            candidate = _axis_aligned_window_for_bounds(
                source,
                cast(tuple[float, float, float, float], transformed_geometry.bounds),
            )
        else:
            try:
                candidate = geometry_window(source, [transformed_geometry.__geo_interface__])
            except WindowError:
                continue
        selected = _clipped_window(source, candidate)
        if selected is None:
            continue
        windows.append(selected)
        fully_covered.append(source_extent.covers(transformed_geometry))
    if not windows:
        raise RasterProcessingError(
            "AOI does not intersect the local raster; choose overlapping WGS84 bounds"
        )

    multipart_antimeridian = len(requested_parts) > 1
    if len(windows) > 1:
        if not source_crs.is_geographic or not _is_north_up(source.transform):
            raise RasterProcessingError(
                "Antimeridian AOIs require a north-up geographic source raster; reproject the "
                "source to a continuous longitude grid before building"
            )
        row_start = max(int(window.row_off) for window in windows)
        row_stop = min(int(window.row_off + window.height) for window in windows)
        if row_stop <= row_start:
            raise RasterProcessingError(
                "Antimeridian source windows do not share a usable latitude range; choose a "
                "raster that continuously covers the AOI"
            )
        windows = [
            Window.from_slices(
                (row_start, row_stop),
                (int(window.col_off), int(window.col_off + window.width)),
            )
            for window in windows
        ]
        column_intervals = [
            (int(window.col_off), int(window.col_off + window.width)) for window in windows
        ]
        overlap_start = max(interval[0] for interval in column_intervals)
        overlap_stop = min(interval[1] for interval in column_intervals)
        if overlap_start < overlap_stop:
            column_start = min(interval[0] for interval in column_intervals)
            column_stop = max(interval[1] for interval in column_intervals)
            windows = [Window.from_slices((row_start, row_stop), (column_start, column_stop))]
        else:
            first_bounds = window_bounds(windows[0], source.transform)
            second_bounds = window_bounds(windows[1], source.transform)
            seam_gap = second_bounds[0] - first_bounds[2]
            longitude_period = _geographic_longitude_period(source_crs)
            half_period = longitude_period / 2.0
            wrapped_gap = ((seam_gap + half_period) % longitude_period) - half_period
            tolerance = max(longitude_period * 1e-12, abs(float(source.transform.a)) * 1e-9)
            if abs(wrapped_gap) > tolerance:
                raise RasterProcessingError(
                    "Antimeridian source windows are not continuous at the longitude seam; "
                    "normalize the raster longitude domain before building"
                )

    selected_rows = int(windows[0].height)
    selected_columns = sum(int(window.width) for window in windows)
    if selected_columns < 2 or selected_rows < 2:
        raise RasterProcessingError("AOI intersection contains fewer than 2 x 2 raster cells")
    return _SourceSelection(
        windows=tuple(windows),
        coverage_status=(
            "within-source"
            if len(fully_covered) == len(requested_parts) and all(fully_covered)
            else "partial-source-overlap"
        ),
        multipart_antimeridian=multipart_antimeridian,
    )


def _read_source(
    path: Path, aoi: AreaOfInterest | None
) -> tuple[FloatArray, Affine, CRS, _SourceFacts]:
    try:
        source = rasterio.open(path)
    except RasterioError as exc:
        msg = f"Could not open {path} as a raster; provide a readable GeoTIFF/DEM"
        raise RasterProcessingError(msg) from exc
    with source:
        if source.count < 1:
            raise RasterProcessingError(f"Raster {path} has no bands")
        if source.crs is None:
            raise RasterProcessingError(
                f"Raster {path} has no horizontal CRS; assign the correct CRS before building"
            )
        source_crs = CRS.from_user_input(source.crs)
        full_shape = (source.height, source.width)
        selection = _SourceSelection(
            windows=(Window.from_slices((0, source.height), (0, source.width)),),
            coverage_status="full-source",
        )
        if aoi is not None:
            selection = _window_for_aoi(source, aoi)
        source_parts = [
            np.asarray(
                source.read(1, window=window, masked=True).astype(np.float32).filled(np.nan),
                dtype=np.float32,
            )
            for window in selection.windows
        ]
        source_data = (
            source_parts[0]
            if len(source_parts) == 1
            else np.concatenate(source_parts, axis=1, dtype=np.float32)
        )
        if source_data.shape[0] < 2 or source_data.shape[1] < 2:
            raise RasterProcessingError("Selected source raster contains fewer than 2 x 2 cells")
        if not bool(np.any(np.isfinite(source_data))):
            raise RasterProcessingError("Selected AOI intersects only source NoData cells")
        selected_transform = window_transform(selection.windows[0], source.transform)
        raw_selected_bounds = tuple(
            float(value)
            for value in array_bounds(
                source_data.shape[0],
                source_data.shape[1],
                selected_transform,
            )
        )
        selected_bounds = cast(
            tuple[float, float, float, float],
            (
                min(raw_selected_bounds[0], raw_selected_bounds[2]),
                min(raw_selected_bounds[1], raw_selected_bounds[3]),
                max(raw_selected_bounds[0], raw_selected_bounds[2]),
                max(raw_selected_bounds[1], raw_selected_bounds[3]),
            ),
        )
        selected_part_bounds = [
            [float(value) for value in window_bounds(window, source.transform)]
            for window in selection.windows
        ]
        bounds_wgs84 = tuple(
            float(value)
            for value in transform_bounds(
                source.crs,
                "EPSG:4326",
                *selected_bounds,
                densify_pts=21,
            )
        )
        tags = {str(key): str(value) for key, value in source.tags().items()}
        raw_min = float(np.nanmin(source_data))
        raw_max = float(np.nanmax(source_data))
        raw_peak = _peak_record(source_data, selected_transform, source_crs)
        aoi_report: dict[str, object] | None = None
        if aoi is not None:
            aoi_report = aoi_provenance(aoi)
            source_windows = [
                [
                    int(window.col_off),
                    int(window.row_off),
                    int(window.width),
                    int(window.height),
                ]
                for window in selection.windows
            ]
            clip: dict[str, object] = {
                "coverage_status": selection.coverage_status,
                "source_full_grid_shape": list(full_shape),
                "selected_source_grid_shape": list(source_data.shape),
                "source_pixel_windows": source_windows,
                "selected_pixel_bounds_native": list(selected_bounds),
                "selected_pixel_bounds_native_parts": selected_part_bounds,
                "selected_pixel_bounds_wgs84": list(bounds_wgs84),
                "selection_mode": (
                    "multipart-antimeridian"
                    if selection.multipart_antimeridian
                    else "single-window"
                ),
                "pixel_aligned_crop": True,
                "silent_expansion": False,
            }
            if len(source_windows) == 1 and not selection.multipart_antimeridian:
                clip["source_pixel_window"] = source_windows[0]
            aoi_report["clip"] = clip
        facts = _SourceFacts(
            full_shape=full_shape,
            selected_shape=(source_data.shape[0], source_data.shape[1]),
            crs=source_crs,
            transform=selected_transform,
            native_bounds=selected_bounds,
            bounds_wgs84=cast(tuple[float, float, float, float], bounds_wgs84),
            horizontal_resolution_m=_pixel_resolution_m(
                selected_transform, source_crs, source_data.shape
            ),
            raw_elevation_min_m=raw_min,
            raw_elevation_max_m=raw_max,
            raw_peak_coordinate=raw_peak,
            tags=tags,
            aoi_report=aoi_report,
        )
        return source_data, selected_transform, source_crs, facts


def _read_to_metric_grid(
    path: Path, aoi: AreaOfInterest | None
) -> tuple[FloatArray, BoolArray, Affine, CRS, _SourceFacts]:
    source_data, source_transform, source_crs, facts = _read_source(path, aoi)
    source_nodata_mask = ~np.isfinite(source_data)
    if aoi is not None:
        target_crs = CRS.from_user_input(aoi.target_local_crs)
    else:
        target_crs = _choose_metric_crs(source_crs, facts.native_bounds)
    if source_crs == target_crs and _uses_metre_axes(source_crs) and _is_north_up(source_transform):
        return source_data, source_nodata_mask, source_transform, source_crs, facts
    if aoi is None and _uses_metre_axes(source_crs) and _is_north_up(source_transform):
        return source_data, source_nodata_mask, source_transform, source_crs, facts

    transform, width, height = calculate_default_transform(
        source_crs,
        target_crs,
        source_data.shape[1],
        source_data.shape[0],
        *facts.native_bounds,
    )
    destination = np.full((height, width), np.nan, dtype=np.float32)
    destination_nodata = np.zeros((height, width), dtype=np.uint8)
    coverage = np.zeros((height, width), dtype=np.uint8)
    reproject(
        source=source_data,
        destination=destination,
        src_transform=source_transform,
        src_crs=source_crs,
        src_nodata=np.nan,
        dst_transform=transform,
        dst_crs=target_crs,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    reproject(
        source=source_nodata_mask.astype(np.uint8),
        destination=destination_nodata,
        src_transform=source_transform,
        src_crs=source_crs,
        dst_transform=transform,
        dst_crs=target_crs,
        dst_nodata=0,
        resampling=Resampling.max,
    )
    reproject(
        source=np.ones(source_data.shape, dtype=np.uint8),
        destination=coverage,
        src_transform=source_transform,
        src_crs=source_crs,
        src_nodata=0,
        dst_transform=transform,
        dst_crs=target_crs,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    destination_mask = destination_nodata.astype(bool)
    destination[destination_mask] = np.nan
    destination, destination_mask, transform = _crop_to_source_coverage(
        destination,
        coverage.astype(bool),
        destination_mask,
        transform,
    )
    return destination, destination_mask, transform, target_crs, facts


def _resample_to_shape(
    elevations: FloatArray,
    transform: Affine,
    crs: CRS,
    target_shape: tuple[int, int],
) -> tuple[FloatArray, Affine]:
    rows, columns = elevations.shape
    target_rows, target_columns = target_shape
    if (rows, columns) == target_shape:
        return elevations, transform
    target = np.full((target_rows, target_columns), np.nan, dtype=np.float32)
    target_transform = transform * Affine.scale(columns / target_columns, rows / target_rows)
    reproject(
        source=elevations,
        destination=target,
        src_transform=transform,
        src_crs=crs,
        src_nodata=np.nan,
        dst_transform=target_transform,
        dst_crs=crs,
        dst_nodata=np.nan,
        resampling=Resampling.average,
    )
    return target, target_transform


def _resample_mask_to_shape(
    mask: BoolArray,
    transform: Affine,
    crs: CRS,
    target_shape: tuple[int, int],
) -> BoolArray:
    """Conservatively retain any pre-sampling NoData footprint in the output grid."""
    rows, columns = mask.shape
    target_rows, target_columns = target_shape
    if (rows, columns) == target_shape:
        return mask.copy()
    target = np.zeros((target_rows, target_columns), dtype=np.uint8)
    target_transform = transform * Affine.scale(columns / target_columns, rows / target_rows)
    reproject(
        source=mask.astype(np.uint8),
        destination=target,
        src_transform=transform,
        src_crs=crs,
        dst_transform=target_transform,
        dst_crs=crs,
        dst_nodata=0,
        resampling=Resampling.max,
    )
    return target.astype(bool)


def _fill_small_nodata(
    elevations: FloatArray,
    max_fraction: float,
    max_hole_pixels: int,
) -> tuple[FloatArray, BoolArray, float]:
    missing = ~np.isfinite(elevations)
    missing_count = int(np.count_nonzero(missing))
    if missing_count == 0:
        return elevations.copy(), missing, 0.0
    missing_fraction = missing_count / missing.size
    if missing_fraction > max_fraction:
        raise RasterProcessingError(
            f"NoData covers {missing_fraction:.2%}, above the configured {max_fraction:.2%}; "
            "crop the raster or choose another dataset"
        )
    valid = ~missing
    if not np.any(valid):
        raise RasterProcessingError("Raster contains no finite elevations")
    label_result = ndimage.label(missing, structure=np.ones((3, 3), dtype=np.uint8))
    labels, component_count = cast(tuple[npt.NDArray[np.int32], int], label_result)
    fillable = np.zeros_like(missing)
    rows, columns = missing.shape
    rejected: list[int] = []
    for component in range(1, component_count + 1):
        component_mask = labels == component
        size = int(np.count_nonzero(component_mask))
        edge = bool(
            np.any(component_mask[0, :])
            or np.any(component_mask[rows - 1, :])
            or np.any(component_mask[:, 0])
            or np.any(component_mask[:, columns - 1])
        )
        if size <= max_hole_pixels and not edge:
            fillable |= component_mask
        else:
            rejected.append(size)
    if rejected:
        largest = max(rejected)
        raise RasterProcessingError(
            f"NoData includes an edge gap or hole of {largest} pixels, above the safe local "
            "interpolation policy; crop or replace the source raster"
        )
    nearest_indices = cast(
        npt.NDArray[np.int64],
        ndimage.distance_transform_edt(
            missing,
            return_distances=False,
            return_indices=True,
        ),
    )
    filled = elevations.copy()
    filled[fillable] = elevations[tuple(index[fillable] for index in nearest_indices)]
    if not np.all(np.isfinite(filled)):
        raise RasterProcessingError("NoData resolution left non-finite elevations")
    return filled, missing, float(np.count_nonzero(fillable) / missing.size)


def _write_processed_raster(
    output_path: Path,
    mask_path: Path,
    elevations: FloatArray,
    original_mask: BoolArray,
    transform: Affine,
    crs: CRS,
    decision: SamplingDecision,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    common: dict[str, Any] = {
        "driver": "GTiff",
        "width": elevations.shape[1],
        "height": elevations.shape[0],
        "count": 1,
        "crs": crs,
        "transform": transform,
        "compress": "deflate",
    }
    with rasterio.open(output_path, "w", dtype="float32", predictor=3, **common) as target:
        target.write(elevations.astype(np.float32, copy=False), 1)
        target.update_tags(
            UNITS="metre",
            ORIGINAL_NODATA_MASK=mask_path.name,
            PROCESSING=(
                "metric reprojection; conservative small-hole interpolation; deterministic sampling"
            ),
            ORIENTATION="row 0 is north; model export maps north to +Y",
            ESTIMATED_TRIANGLE_COUNT=str(decision.estimated_triangle_count),
        )
    with rasterio.open(mask_path, "w", dtype="uint8", **common) as target:
        target.write(original_mask.astype(np.uint8), 1)
        target.update_tags(
            MASK_MEANING="1=source/reprojection NoData before interpolation, conservatively sampled"
        )


def _coordinate_value(coordinate: dict[str, object], key: str) -> float:
    value = coordinate.get(key)
    if not isinstance(value, int | float):
        raise RasterProcessingError(f"Coordinate field {key!r} is not numeric")
    return float(value)


def _peak_shift_m(raw: dict[str, object], processed: dict[str, object]) -> float:
    return abs(
        float(
            _GEOD.inv(
                _coordinate_value(raw, "longitude"),
                _coordinate_value(raw, "latitude"),
                _coordinate_value(processed, "longitude"),
                _coordinate_value(processed, "latitude"),
            )[2]
        )
    )


def process_local_raster(config: BuildConfig) -> ProcessedRaster:
    """Clip, normalize, sample, conservatively fill, and persist a local raster."""
    source_path = config.dem_path.expanduser().resolve()
    if not source_path.is_file():
        raise RasterProcessingError(f"DEM file does not exist: {source_path}")
    normalized_aoi = normalize_area_of_interest(config.aoi) if config.aoi is not None else None
    elevations, propagated_mask, transform, crs, source = _read_to_metric_grid(
        source_path, normalized_aoi
    )
    filled_metric, original_metric_mask, interpolated_fraction = _fill_small_nodata(
        elevations,
        config.nodata_max_fraction,
        config.nodata_max_hole_pixels,
    )
    if not np.array_equal(propagated_mask, original_metric_mask):
        raise RasterProcessingError(
            "Internal NoData mask propagation disagrees with the metric elevation grid"
        )
    metric_rows, metric_columns = elevations.shape
    metric_pixel_x_m = float(abs(transform.a))
    metric_pixel_y_m = float(abs(transform.e))
    metric_ground_width_m = metric_pixel_x_m * metric_columns
    metric_ground_depth_m = metric_pixel_y_m * metric_rows
    decision = resolve_sampling_decision(
        elevations.shape,
        ground_width_m=metric_ground_width_m,
        ground_depth_m=metric_ground_depth_m,
        config=config,
    )
    original_mask = _resample_mask_to_shape(
        original_metric_mask,
        transform,
        crs,
        decision.target_shape,
    )
    filled, transform = _resample_to_shape(
        filled_metric,
        transform,
        crs,
        decision.target_shape,
    )
    output_dir = config.output_dir.expanduser().resolve()
    processed_path = output_dir / "processed_dem.tif"
    mask_path = output_dir / "original_nodata_mask.tif"
    _write_processed_raster(
        processed_path,
        mask_path,
        filled,
        original_mask,
        transform,
        crs,
        decision,
    )

    rows, columns = filled.shape
    pixel_x_m = float(abs(transform.a))
    pixel_y_m = float(abs(transform.e))
    ground_width_m = pixel_x_m * columns
    ground_depth_m = pixel_y_m * rows
    original_nodata_fraction = float(
        np.count_nonzero(original_metric_mask) / original_metric_mask.size
    )
    processed_resolution_m = (pixel_x_m + pixel_y_m) / 2.0
    processed_peak = _peak_record(filled, transform, crs)
    peak_loss_m = max(0.0, source.raw_elevation_max_m - float(np.max(filled)))
    peak_shift_m = _peak_shift_m(source.raw_peak_coordinate, processed_peak)
    downsampling_factor = max(1.0, processed_resolution_m / source.horizontal_resolution_m)
    peak_loss_threshold_m = max(10.0, source.horizontal_resolution_m * 2.0)
    peak_shift_threshold_m = max(
        source.horizontal_resolution_m * 3.0,
        processed_resolution_m * 3.0,
    )
    terrain_fidelity_passed = (
        peak_loss_m <= peak_loss_threshold_m and peak_shift_m <= peak_shift_threshold_m
    )
    if not terrain_fidelity_passed:
        terrain_fidelity_status = "failed-threshold"
    elif downsampling_factor > 1.05:
        terrain_fidelity_status = "documented-downsampling"
    else:
        terrain_fidelity_status = "source-resolution-preserved"

    tags = source.tags
    tagged_source_urls: list[str] = []
    if tags.get("SOURCE_URLS"):
        try:
            parsed_urls = json.loads(tags["SOURCE_URLS"])
            if isinstance(parsed_urls, list) and all(
                isinstance(value, str) for value in parsed_urls
            ):
                tagged_source_urls = parsed_urls
        except json.JSONDecodeError:
            tagged_source_urls = []
    metadata = DatasetMetadata(
        provider=(
            config.source_provider
            if config.source_provider != "local"
            else tags.get("PROVIDER", "local")
        ),
        dataset_name=config.dataset_name or source_path.stem,
        dataset_version=(
            config.dataset_version
            if config.dataset_version != "unknown"
            else tags.get("DATASET_VERSION", "unknown")
        ),
        dataset_type=config.dataset_type,
        horizontal_resolution_m=source.horizontal_resolution_m,
        horizontal_crs=source.crs.to_string(),
        vertical_crs=(
            config.vertical_crs
            if config.vertical_crs != "unknown"
            else tags.get("VERTICAL_CRS", "unknown")
        ),
        vertical_datum=(
            config.vertical_datum
            if config.vertical_datum != "unknown"
            else tags.get("VERTICAL_DATUM", "unknown")
        ),
        license=(
            config.data_license
            if config.data_license != "user-supplied; verify source terms"
            else tags.get("LICENSE", config.data_license)
        ),
        attribution=(
            config.attribution
            if config.attribution != "Provided by the user"
            else tags.get("ATTRIBUTION", config.attribution)
        ),
        acquisition_period=(
            config.acquisition_period
            if config.acquisition_period != "unknown"
            else tags.get("ACQUISITION_PERIOD", "unknown")
        ),
        download_time=(
            config.source_download_time
            if config.source_download_time != "not-applicable-local-input"
            else tags.get("DOWNLOAD_TIME", "not-applicable-local-input")
        ),
        source_urls=(
            config.source_urls
            if config.source_urls
            else tagged_source_urls
            if tagged_source_urls
            else [tags["SOURCE_URL"]]
            if tags.get("SOURCE_URL")
            else []
        ),
        checksums=(
            config.source_checksums
            if config.source_checksums
            else {source_path.name: sha256_file(source_path)}
        ),
    )
    physical_spacing = sum(decision.physical_spacing_xy_mm) / 2.0
    report = RasterResult(
        path=processed_path,
        original_nodata_mask_path=mask_path,
        array_shape=(rows, columns),
        source_grid_shape=source.selected_shape,
        processed_grid_shape=(rows, columns),
        transform=(
            float(transform.a),
            float(transform.b),
            float(transform.c),
            float(transform.d),
            float(transform.e),
            float(transform.f),
        ),
        crs=crs.to_string(),
        nodata=None,
        ground_width_m=ground_width_m,
        ground_depth_m=ground_depth_m,
        pixel_size_x_m=pixel_x_m,
        pixel_size_y_m=pixel_y_m,
        valid_fraction=1.0,
        interpolated_fraction=interpolated_fraction,
        original_nodata_fraction=original_nodata_fraction,
        elevation_min_m=float(np.min(filled)),
        elevation_max_m=float(np.max(filled)),
        source_horizontal_resolution_m=source.horizontal_resolution_m,
        processed_horizontal_resolution_m=processed_resolution_m,
        downsampling_factor=downsampling_factor,
        physical_sample_spacing_mm=physical_spacing,
        physical_sample_spacing_xy_mm=decision.physical_spacing_xy_mm,
        estimated_triangle_count=decision.estimated_triangle_count,
        estimated_memory_mb=decision.estimated_memory_mb,
        raw_elevation_min_m=source.raw_elevation_min_m,
        raw_elevation_max_m=source.raw_elevation_max_m,
        processed_elevation_min_m=float(np.min(filled)),
        processed_elevation_max_m=float(np.max(filled)),
        peak_elevation_loss_m=peak_loss_m,
        raw_peak_coordinate=source.raw_peak_coordinate,
        processed_peak_coordinate=processed_peak,
        peak_horizontal_shift_m=peak_shift_m,
        sampling_decision_reasons=list(decision.reasons),
        sampling_warnings=list(decision.warnings),
        terrain_fidelity_status=terrain_fidelity_status,
        terrain_fidelity_passed=terrain_fidelity_passed,
        peak_elevation_loss_threshold_m=peak_loss_threshold_m,
        peak_horizontal_shift_threshold_m=peak_shift_threshold_m,
        source_bounds={
            "native": list(source.native_bounds),
            "native_crs": source.crs.to_string(),
            "wgs84": list(source.bounds_wgs84),
            "full_grid_shape": list(source.full_shape),
        },
        aoi=source.aoi_report,
        metadata=metadata,
    )
    return ProcessedRaster(filled, original_mask, transform, crs, report)
