"""Local GeoTIFF processing with explicit CRS and conservative NoData handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.errors import RasterioError
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from scipy import ndimage

from topoforge.exceptions import RasterProcessingError
from topoforge.models import BuildConfig, DatasetMetadata, RasterResult
from topoforge.util.hashing import sha256_file

FloatArray = npt.NDArray[np.float32]
BoolArray = npt.NDArray[np.bool_]


@dataclass(frozen=True, slots=True)
class ProcessedRaster:
    """In-memory elevations plus the serializable processing report."""

    elevations_m: FloatArray
    original_nodata_mask: BoolArray
    transform: Affine
    crs: CRS
    report: RasterResult


def _is_north_up(transform: Affine) -> bool:
    return abs(transform.b) < 1e-12 and abs(transform.d) < 1e-12


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


def _largest_true_rectangle(mask: BoolArray) -> tuple[int, int, int, int]:
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
    top, bottom, left, right = _largest_true_rectangle(coverage)
    if bottom - top < 2 or right - left < 2:
        raise RasterProcessingError("Metric reprojection left fewer than 2 x 2 covered cells")
    cropped = elevations[top:bottom, left:right]
    cropped_transform = transform * Affine.translation(left, top)
    return np.asarray(cropped, dtype=np.float32), cropped_transform


def _read_to_metric_grid(path: Path) -> tuple[FloatArray, Affine, CRS, dict[str, str]]:
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
        source_array = source.read(1, masked=True).astype(np.float32)
        source_data = np.asarray(source_array.filled(np.nan), dtype=np.float32)
        tags = {str(key): str(value) for key, value in source.tags().items()}
        source_bounds = (
            source.bounds.left,
            source.bounds.bottom,
            source.bounds.right,
            source.bounds.top,
        )
        if _uses_metre_axes(source_crs) and _is_north_up(source.transform):
            return source_data, source.transform, source_crs, tags

        target_crs = _choose_metric_crs(source_crs, source_bounds)
        transform, width, height = calculate_default_transform(
            source.crs,
            target_crs,
            source.width,
            source.height,
            *source_bounds,
        )
        destination = np.full((height, width), np.nan, dtype=np.float32)
        coverage = np.zeros((height, width), dtype=np.uint8)
        reproject(
            source=source_data,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=transform,
            dst_crs=target_crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        reproject(
            source=np.ones(source_data.shape, dtype=np.uint8),
            destination=coverage,
            src_transform=source.transform,
            src_crs=source.crs,
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
        return destination, transform, target_crs, tags


def _resample_to_cell_budget(
    elevations: FloatArray,
    transform: Affine,
    crs: CRS,
    max_grid_cells: int,
) -> tuple[FloatArray, Affine]:
    rows, columns = elevations.shape
    if rows * columns <= max_grid_cells:
        return elevations, transform
    reduction = np.sqrt((rows * columns) / max_grid_cells)
    target_rows = max(2, int(rows / reduction))
    target_columns = max(2, int(columns / reduction))
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
            PROCESSING="metric reprojection; conservative small-hole nearest interpolation",
        )
    with rasterio.open(mask_path, "w", dtype="uint8", **common) as target:
        target.write(original_mask.astype(np.uint8), 1)
        target.update_tags(MASK_MEANING="1=source/reprojection NoData before interpolation")


def process_local_raster(config: BuildConfig) -> ProcessedRaster:
    """Read, normalize, conservatively fill, and persist a local elevation raster."""
    source_path = config.dem_path.expanduser().resolve()
    if not source_path.is_file():
        raise RasterProcessingError(f"DEM file does not exist: {source_path}")
    elevations, transform, crs, tags = _read_to_metric_grid(source_path)
    elevations, transform = _resample_to_cell_budget(
        elevations,
        transform,
        crs,
        config.max_grid_cells,
    )
    filled, original_mask, interpolated_fraction = _fill_small_nodata(
        elevations,
        config.nodata_max_fraction,
        config.nodata_max_hole_pixels,
    )
    output_dir = config.output_dir.expanduser().resolve()
    processed_path = output_dir / "processed_dem.tif"
    mask_path = output_dir / "original_nodata_mask.tif"
    _write_processed_raster(processed_path, mask_path, filled, original_mask, transform, crs)

    rows, columns = filled.shape
    pixel_x_m = float(abs(transform.a))
    pixel_y_m = float(abs(transform.e))
    ground_width_m = pixel_x_m * columns
    ground_depth_m = pixel_y_m * rows
    finite_count = int(np.count_nonzero(np.isfinite(elevations)))
    original_nodata_fraction = 1.0 - finite_count / elevations.size
    metadata = DatasetMetadata(
        provider="local",
        dataset_name=config.dataset_name or source_path.stem,
        dataset_version=(
            config.dataset_version
            if config.dataset_version != "unknown"
            else tags.get("DATASET_VERSION", "unknown")
        ),
        dataset_type=config.dataset_type,
        horizontal_resolution_m=float((pixel_x_m + pixel_y_m) / 2.0),
        horizontal_crs=crs.to_string(),
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
        download_time="not-applicable-local-input",
        source_urls=(
            config.source_urls
            if config.source_urls
            else [tags["SOURCE_URL"]]
            if tags.get("SOURCE_URL")
            else []
        ),
        checksums={source_path.name: sha256_file(source_path)},
    )
    report = RasterResult(
        path=processed_path,
        original_nodata_mask_path=mask_path,
        array_shape=(rows, columns),
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
        metadata=metadata,
    )
    return ProcessedRaster(filled, original_mask, transform, crs, report)
