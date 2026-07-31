from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import process_local_raster


def test_peak_loss_and_shift_are_measured_when_custom_sampling_is_coarse(tmp_path: Path) -> None:
    values = np.zeros((80, 100), dtype=np.float32)
    values[21, 73] = 1000.0
    source = tmp_path / "impulse.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=100,
        height=80,
        count=1,
        dtype="float32",
        crs="EPSG:32647",
        transform=from_origin(500_000.0, 3_300_000.0, 30.0, 30.0),
    ) as dataset:
        dataset.write(values, 1)

    result = process_local_raster(
        BuildConfig(
            dem_path=source,
            output_dir=tmp_path / "out",
            model_width_mm=100.0,
            sampling_mode=SamplingMode.CUSTOM,
            mesh_sampling_mm=10.0,
            max_grid_cells=10_000,
        )
    )

    assert result.report.raw_elevation_max_m == 1000.0
    assert result.report.processed_elevation_max_m < 1000.0
    assert result.report.peak_elevation_loss_m > 0.0
    assert result.report.peak_horizontal_shift_m > 0.0
    assert result.report.terrain_fidelity_status == "failed-threshold"
    assert result.report.terrain_fidelity_passed is False
    assert result.report.sampling_decision_reasons
