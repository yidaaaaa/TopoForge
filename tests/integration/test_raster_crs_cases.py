from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.transform import from_origin

from topoforge.exceptions import RasterProcessingError
from topoforge.models import BuildConfig
from topoforge.raster import process_local_raster


def write_raster(
    path: Path, *, crs: str | None, transform: Affine, shape: tuple[int, int] = (12, 16)
) -> Path:
    values = np.linspace(50.0, 450.0, shape[0] * shape[1], dtype=np.float32).reshape(shape)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=shape[1],
        height=shape[0],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
    ) as dataset:
        dataset.write(values, 1)
    return path


def test_geographic_raster_is_reprojected_before_mesh_units(tmp_path: Path) -> None:
    source = write_raster(
        tmp_path / "geographic.tif",
        crs="EPSG:4326",
        transform=from_origin(101.8, 30.2, 0.001, 0.001),
    )
    result = process_local_raster(BuildConfig(dem_path=source, output_dir=tmp_path / "out"))
    assert result.crs.is_projected
    assert result.report.pixel_size_x_m > 10.0
    assert result.report.ground_width_m > 1000.0
    assert np.all(np.isfinite(result.elevations_m))


def test_rotated_metric_raster_is_normalized_to_north_up_grid(tmp_path: Path) -> None:
    transform = (
        Affine.translation(500_000.0, 3_300_000.0)
        * Affine.rotation(8.0)
        * Affine.scale(10.0, -10.0)
    )
    source = write_raster(tmp_path / "rotated.tif", crs="EPSG:32648", transform=transform)
    result = process_local_raster(BuildConfig(dem_path=source, output_dir=tmp_path / "out"))
    assert abs(result.transform.b) < 1e-12
    assert abs(result.transform.d) < 1e-12
    assert np.all(np.isfinite(result.elevations_m))


def test_missing_crs_is_rejected_with_action(tmp_path: Path) -> None:
    source = write_raster(
        tmp_path / "no-crs.tif",
        crs=None,
        transform=from_origin(0.0, 100.0, 10.0, 10.0),
    )
    with pytest.raises(RasterProcessingError, match="assign the correct CRS"):
        process_local_raster(BuildConfig(dem_path=source, output_dir=tmp_path / "out"))


def test_cell_budget_downsamples_without_increasing_detail(tmp_path: Path) -> None:
    source = write_raster(
        tmp_path / "large.tif",
        crs="EPSG:32648",
        transform=from_origin(500_000.0, 3_300_000.0, 2.0, 2.0),
        shape=(100, 120),
    )
    result = process_local_raster(
        BuildConfig(dem_path=source, output_dir=tmp_path / "out", max_grid_cells=1_000)
    )
    assert result.elevations_m.size <= 1_000
    assert result.report.pixel_size_x_m > 2.0
    assert result.report.pixel_size_y_m > 2.0
