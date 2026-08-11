from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from topoforge.models import AreaOfInterestInput, BuildConfig, SamplingMode
from topoforge.raster import process_local_raster


def test_geographic_source_aoi_is_transformed_from_wgs84_before_windowing(
    tmp_path: Path,
) -> None:
    longitude_wgs84 = -100.0
    latitude_wgs84 = 40.0
    longitude_native, latitude_native = Transformer.from_crs(
        "EPSG:4326",
        "EPSG:4267",
        always_xy=True,
    ).transform(longitude_wgs84, latitude_wgs84)
    longitude_shift = abs(longitude_native - longitude_wgs84)
    if longitude_shift < 0.0001:
        pytest.skip("host PROJ data cannot demonstrate the WGS84-to-NAD27 datum shift")

    pixel_size = 0.00001
    half_cells = 6
    assert not (
        longitude_native - half_cells * pixel_size
        <= longitude_wgs84
        <= longitude_native + half_cells * pixel_size
    )
    source = tmp_path / "nad27-source.tif"
    values = np.arange(12 * 12, dtype=np.float32).reshape((12, 12))
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=12,
        height=12,
        count=1,
        dtype="float32",
        crs="EPSG:4267",
        transform=from_origin(
            longitude_native - half_cells * pixel_size,
            latitude_native + half_cells * pixel_size,
            pixel_size,
            pixel_size,
        ),
    ) as dataset:
        dataset.write(values, 1)

    result = process_local_raster(
        BuildConfig(
            dem_path=source,
            output_dir=tmp_path / "output",
            aoi=AreaOfInterestInput(
                bbox_wgs84=(
                    longitude_wgs84 - 2 * pixel_size,
                    latitude_wgs84 - 2 * pixel_size,
                    longitude_wgs84 + 2 * pixel_size,
                    latitude_wgs84 + 2 * pixel_size,
                )
            ),
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
        )
    )

    assert min(result.report.source_grid_shape) >= 2
    assert result.report.aoi is not None
    clip = result.report.aoi["clip"]
    assert isinstance(clip, dict)
    assert clip["coverage_status"] == "within-source"
