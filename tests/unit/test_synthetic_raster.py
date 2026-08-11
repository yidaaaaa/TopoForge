from pathlib import Path

import numpy as np
import pytest
import rasterio

from topoforge.models import BuildConfig, DatasetType
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff, process_local_raster
from topoforge.raster.synthetic import synthetic_elevations


@pytest.mark.parametrize("terrain", list(SyntheticTerrain))
def test_synthetic_surfaces_are_deterministic_and_finite_except_declared_holes(
    terrain: SyntheticTerrain,
) -> None:
    first = synthetic_elevations(terrain, rows=16, columns=20)
    second = synthetic_elevations(terrain, rows=16, columns=20)
    np.testing.assert_equal(first, second)
    if "nodata" not in terrain.value:
        assert np.all(np.isfinite(first))


def test_process_metric_geotiff_and_preserve_original_nodata_mask(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.NODATA_HOLE,
        rows=32,
        columns=40,
        pixel_size_m=10.0,
    )
    output = tmp_path / "output"
    result = process_local_raster(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            dataset_type=DatasetType.DTM,
            data_license="Apache-2.0 synthetic fixture",
        )
    )

    assert result.report.crs == "EPSG:32648"
    assert result.report.ground_width_m == pytest.approx(400.0)
    assert result.report.ground_depth_m == pytest.approx(320.0)
    assert result.report.original_nodata_fraction == pytest.approx(16 / (32 * 40))
    assert result.report.interpolated_fraction == pytest.approx(16 / (32 * 40))
    assert np.all(np.isfinite(result.elevations_m))
    assert result.report.path.is_file()
    assert result.report.original_nodata_mask_path is not None
    with rasterio.open(result.report.original_nodata_mask_path) as mask_dataset:
        assert int(np.sum(mask_dataset.read(1))) == 16


def test_large_or_edge_nodata_is_rejected(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(tmp_path / "source.tif", rows=16, columns=20)
    with rasterio.open(source, "r+") as dataset:
        values = dataset.read(1)
        values[:, 0] = np.nan
        dataset.write(values, 1)
    with pytest.raises(Exception, match="edge gap"):
        process_local_raster(BuildConfig(dem_path=source, output_dir=tmp_path / "output"))


def test_downsampling_preserves_source_nodata_evidence_and_enforces_policy(
    tmp_path: Path,
) -> None:
    source = create_synthetic_geotiff(tmp_path / "downsample-source.tif", rows=8, columns=8)
    with rasterio.open(source, "r+") as dataset:
        values = dataset.read(1)
        values[3, 3] = np.nan
        dataset.write(values, 1)

    result = process_local_raster(
        BuildConfig(
            dem_path=source,
            output_dir=tmp_path / "downsample-output",
            max_grid_cells=16,
        )
    )

    assert result.report.processed_grid_shape == (4, 4)
    assert result.report.original_nodata_fraction == pytest.approx(1 / 64)
    assert bool(np.any(result.original_nodata_mask))
    assert bool(np.all(np.isfinite(result.elevations_m)))

    with pytest.raises(Exception, match="safe local interpolation policy"):
        process_local_raster(
            BuildConfig(
                dem_path=source,
                output_dir=tmp_path / "rejected-downsample-output",
                max_grid_cells=16,
                nodata_max_hole_pixels=0,
            )
        )
