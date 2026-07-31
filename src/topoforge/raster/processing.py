"""Local GeoTIFF processing with AOI clipping and printer-aware sampling."""

from __future__ import annotations

import json
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


def _is_north_up(transform: Affine) -> bool:
    return abs(transform.b) < 1e-12 and abs(transform.d) < 1e-12 and transform.e < 0


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
    transform: Affine,
) -> tuple[FloatArray, Affine]:
    """Remove reprojection-only corner gaps using the largest covered rectangle."""
    if bool(np.all(coverage)):
        return elevations, transform
    top, bottom, left, right = largest_true_rectangle(coverage)
    if bottom - top < 2 or right - left < 2:
        raise RasterProcessingError("Metric reprojection left fewer than 2 x 2 covered cells")
    cropped = elevations[top:bottom, left:right]
    cropped_transform = transform * Affine.translation(left, top)
    return np.asarray(cropped, dtype=np.float32), cropped_transform


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


def _combine_windows(windows: list[Window], full_window: Window) -> Window:
    column_start = max(int(full_window.col_off), min(int(window.col_off) for window in windows))
    row_start = max(int(full_window.row_off), min(int(window.row_off) for window in windows))
    column_stop = min(
        int(full_window.col_off + full_window.width),
        max(int(window.col_off + window.width) for window in windows),
    )
    row_stop = min(
        int(full_window.row_off + full_window.height),
        max(int(window.row_off + window.height) for window in windows),
    )
    return Window.from_slices(
        (row_start, row_stop),
        (column_start, column_stop),
    )


def _window_for_aoi(source: DatasetReader, aoi: AreaOfInterest) -> tuple[Window, str]:
    windows: list[Window] = []
    for bounds_wgs84 in _aoi_bbox_parts(aoi):
        geometry_wgs84 = box(*bounds_wgs84)
        transformed = transform_geom(
            "EPSG:4326",
            source.crs,
            geometry_wgs84.__geo_interface__,
            precision=15,
        )
        try:
            candidate = geometry_window(source, [transformed])
        except WindowError:
            continue
        windows.append(candidate)
    if not windows:
        raise RasterProcessingError(
            "AOI does not intersect the local raster; choose overlapping WGS84 bounds"
        )
    full_window = Window.from_slices((0, source.height), (0, source.width))
    selected = _combine_windows(windows, full_window)
    if selected.width < 2 or selected.height < 2:
        raise RasterProcessingError("AOI intersection contains fewer than 2 x 2 raster cells")

    source_geometry_wgs84 = shape(
        transform_geom(
            source.crs,
            "EPSG:4326",
            box(*source.bounds).__geo_interface__,
            precision=15,
        )
    )
    requested_geometry = shape(aoi.normalized_geometry_geojson)
    coverage_status = (
        "within-source"
        if source_geometry_wgs84.covers(requested_geometry)
        else "partial-source-overlap"
    )
    return selected, coverage_status


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
        coverage_status = "full-source"
        selected_window = Window.from_slices((0, source.height), (0, source.width))
        if aoi is not None:
            selected_window, coverage_status = _window_for_aoi(source, aoi)
        source_array = source.read(1, window=selected_window, masked=True).astype(np.float32)
        source_data = np.asarray(source_array.filled(np.nan), dtype=np.float32)
        if source_data.shape[0] < 2 or source_data.shape[1] < 2:
            raise RasterProcessingError("Selected source raster contains fewer than 2 x 2 cells")
        if not bool(np.any(np.isfinite(source_data))):
            raise RasterProcessingError("Selected AOI intersects only source NoData cells")
        selected_transform = window_transform(selected_window, source.transform)
        selected_bounds = cast(
            tuple[float, float, float, float],
            tuple(float(value) for value in window_bounds(selected_window, source.transform)),
        )
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
            aoi_report["clip"] = {
                "coverage_status": coverage_status,
                "source_full_grid_shape": list(full_shape),
                "selected_source_grid_shape": list(source_data.shape),
                "source_pixel_window": [
                    int(selected_window.col_off),
                    int(selected_window.row_off),
                    int(selected_window.width),
                    int(selected_window.height),
                ],
                "selected_pixel_bounds_native": list(selected_bounds),
                "selected_pixel_bounds_wgs84": list(bounds_wgs84),
                "pixel_aligned_crop": True,
                "silent_expansion": False,
            }
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
) -> tuple[FloatArray, Affine, CRS, _SourceFacts]:
    source_data, source_transform, source_crs, facts = _read_source(path, aoi)
    if aoi is not None:
        target_crs = CRS.from_user_input(aoi.target_local_crs)
    else:
        target_crs = _choose_metric_crs(source_crs, facts.native_bounds)
    if source_crs == target_crs and _uses_metre_axes(source_crs) and _is_north_up(source_transform):
        return source_data, source_transform, source_crs, facts
    if aoi is None and _uses_metre_axes(source_crs) and _is_north_up(source_transform):
        return source_data, source_transform, source_crs, facts

    transform, width, height = calculate_default_transform(
        source_crs,
        target_crs,
        source_data.shape[1],
        source_data.shape[0],
        *facts.native_bounds,
    )
    destination = np.full((height, width), np.nan, dtype=np.float32)
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
    destination, transform = _crop_to_source_coverage(
        destination,
        coverage.astype(bool),
        transform,
    )
    return destination, transform, target_crs, facts


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
                "metric reprojection; deterministic sampling; conservative small-hole interpolation"
            ),
            ORIENTATION="row 0 is north; model export maps north to +Y",
            ESTIMATED_TRIANGLE_COUNT=str(decision.estimated_triangle_count),
        )
    with rasterio.open(mask_path, "w", dtype="uint8", **common) as target:
        target.write(original_mask.astype(np.uint8), 1)
        target.update_tags(MASK_MEANING="1=source/reprojection NoData before interpolation")


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
    elevations, transform, crs, source = _read_to_metric_grid(source_path, normalized_aoi)
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
    elevations, transform = _resample_to_shape(elevations, transform, crs, decision.target_shape)
    filled, original_mask, interpolated_fraction = _fill_small_nodata(
        elevations,
        config.nodata_max_fraction,
        config.nodata_max_hole_pixels,
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
    finite_count = int(np.count_nonzero(np.isfinite(elevations)))
    original_nodata_fraction = 1.0 - finite_count / elevations.size
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
