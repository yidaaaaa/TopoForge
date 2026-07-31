from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from topoforge.exceptions import RasterProcessingError
from topoforge.models import AreaOfInterestInput, BuildConfig, SamplingMode
from topoforge.raster import process_local_raster


def _write_geographic(
    path: Path,
    *,
    nodata_pixel: tuple[int, int] | None = None,
) -> Path:
    values = np.arange(100 * 120, dtype=np.float32).reshape(100, 120)
    nodata = -9999.0
    if nodata_pixel is not None:
        values[nodata_pixel] = nodata
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=120,
        height=100,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(100.0, 30.0, 0.01, 0.01),
        nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)
    return path


def _config(source: Path, output: Path, bbox: tuple[float, float, float, float]) -> BuildConfig:
    return BuildConfig(
        dem_path=source,
        output_dir=output,
        aoi=AreaOfInterestInput(bbox_wgs84=bbox),
        sampling_mode=SamplingMode.SOURCE_PRESERVING,
        max_grid_cells=100_000,
    )


def test_bbox_aoi_crops_local_raster_and_records_pixel_coverage(tmp_path: Path) -> None:
    source = _write_geographic(tmp_path / "source.tif")
    result = process_local_raster(_config(source, tmp_path / "out", (100.2, 29.2, 100.8, 29.8)))

    assert result.report.source_grid_shape[0] < 100
    assert result.report.source_grid_shape[1] < 120
    assert result.report.aoi is not None
    assert result.report.aoi["clip"]["coverage_status"] == "within-source"
    assert result.report.aoi["clip"]["silent_expansion"] is False
    assert result.crs.is_projected


def test_partially_outside_aoi_is_clipped_and_explicitly_reported(tmp_path: Path) -> None:
    source = _write_geographic(tmp_path / "source.tif")
    result = process_local_raster(_config(source, tmp_path / "out", (99.8, 29.4, 100.4, 30.2)))

    assert result.report.aoi is not None
    assert result.report.aoi["clip"]["coverage_status"] == "partial-source-overlap"
    assert result.report.source_grid_shape[0] < 100
    assert result.report.source_grid_shape[1] < 120


def test_fully_outside_aoi_is_rejected(tmp_path: Path) -> None:
    source = _write_geographic(tmp_path / "source.tif")
    with pytest.raises(RasterProcessingError, match="does not intersect"):
        process_local_raster(_config(source, tmp_path / "out", (110.0, 40.0, 111.0, 41.0)))


def test_aoi_intersecting_small_nodata_hole_preserves_and_reports_mask(tmp_path: Path) -> None:
    source = _write_geographic(tmp_path / "source.tif", nodata_pixel=(50, 60))
    result = process_local_raster(_config(source, tmp_path / "out", (100.4, 29.3, 100.8, 29.7)))

    assert result.report.original_nodata_fraction > 0.0
    assert result.report.interpolated_fraction > 0.0
    assert bool(np.any(result.original_nodata_mask))
    assert bool(np.all(np.isfinite(result.elevations_m)))
