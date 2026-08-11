from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
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


def _write_global_geographic(path: Path, *, origin_longitude: float) -> Path:
    values = np.tile(np.arange(360, dtype=np.float32), (10, 1))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=360,
        height=10,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(origin_longitude, 5.0, 1.0, 1.0),
    ) as dataset:
        dataset.write(values, 1)
    return path


@pytest.mark.parametrize(
    ("origin_longitude", "expected_columns"),
    ((-180.0, (359, 0)), (0.0, (179, 180))),
)
def test_antimeridian_aoi_reads_two_bounded_source_windows(
    tmp_path: Path,
    origin_longitude: float,
    expected_columns: tuple[int, int],
) -> None:
    source = _write_global_geographic(
        tmp_path / f"global-{origin_longitude}.tif",
        origin_longitude=origin_longitude,
    )

    result = process_local_raster(
        _config(source, tmp_path / f"out-{origin_longitude}", (179.0, -2.0, -179.0, 2.0))
    )

    assert result.report.source_grid_shape == (4, 2)
    assert result.report.ground_width_m < 300_000.0
    assert result.report.raw_elevation_min_m == pytest.approx(min(expected_columns))
    assert result.report.raw_elevation_max_m == pytest.approx(max(expected_columns))
    assert result.report.aoi is not None
    clip = result.report.aoi["clip"]
    assert clip["selection_mode"] == "multipart-antimeridian"
    assert [window[0] for window in clip["source_pixel_windows"]] == list(expected_columns)
    assert "source_pixel_window" not in clip
    assert bool(np.all(np.isfinite(result.elevations_m)))


def test_antimeridian_aoi_deduplicates_offset_source_pixels(tmp_path: Path) -> None:
    source = _write_global_geographic(
        tmp_path / "global-offset.tif",
        origin_longitude=0.25,
    )

    result = process_local_raster(
        _config(source, tmp_path / "out-offset", (179.0, -2.0, -179.0, 2.0))
    )

    assert result.report.source_grid_shape == (4, 3)
    assert result.report.ground_width_m < 400_000.0
    assert result.report.raw_elevation_min_m == pytest.approx(178.0)
    assert result.report.raw_elevation_max_m == pytest.approx(180.0)
    assert result.report.aoi is not None
    clip = result.report.aoi["clip"]
    assert clip["selection_mode"] == "multipart-antimeridian"
    assert clip["source_pixel_windows"] == [[178, 3, 3, 4]]
    assert clip["selected_source_grid_shape"] == [4, 3]
    assert "source_pixel_window" not in clip
    assert bool(np.all(np.isfinite(result.elevations_m)))


def test_single_aoi_normalizes_west_up_geographic_source(tmp_path: Path) -> None:
    values = np.array(
        [
            [100.0, 200.0, 300.0, 400.0],
            [90.0, 190.0, 290.0, 390.0],
            [80.0, 180.0, 280.0, 380.0],
            [70.0, 170.0, 270.0, 370.0],
        ],
        dtype=np.float32,
    )
    source = tmp_path / "west-up-geographic.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=Affine(-1.0, 0.0, 4.0, 0.0, -1.0, 4.0),
    ) as dataset:
        dataset.write(values, 1)

    result = process_local_raster(
        _config(source, tmp_path / "west-up-out", (0.25, 0.25, 3.75, 3.75))
    )

    assert result.report.source_grid_shape == (4, 4)
    assert result.transform.a > 0
    assert result.report.aoi is not None
    assert result.report.aoi["clip"]["selection_mode"] == "single-window"
    assert float(np.mean(result.elevations_m[:, 0])) > float(np.mean(result.elevations_m[:, -1]))
    assert bool(np.all(np.isfinite(result.elevations_m)))
