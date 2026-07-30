from pathlib import Path

import numpy as np
import rasterio

from topoforge.engine import build_local_terrain
from topoforge.models import BuildConfig, DatasetType, VerticalScaleMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file


def test_same_raster_and_settings_produce_identical_geometry(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=24,
        columns=30,
        pixel_size_m=12.0,
    )
    common = {
        "dem_path": source,
        "model_width_mm": 150.0,
        "base_thickness_mm": 3.0,
        "vertical_scale_mode": VerticalScaleMode.AUTO_PERCEPTUAL,
        "dataset_type": DatasetType.DTM,
        "data_license": "Apache-2.0 synthetic fixture",
    }
    first = build_local_terrain(BuildConfig(output_dir=tmp_path / "first", **common))
    second = build_local_terrain(BuildConfig(output_dir=tmp_path / "second", **common))

    assert sha256_file(first.artifacts["model_stl"]) == sha256_file(second.artifacts["model_stl"])
    assert sha256_file(first.artifacts["model_3mf"]) == sha256_file(second.artifacts["model_3mf"])
    assert sha256_file(first.artifacts["preview_glb"]) == sha256_file(
        second.artifacts["preview_glb"]
    )
    assert first.validation["dimensions_mm"] == second.validation["dimensions_mm"]
    assert first.validation["volume_mm3"] == second.validation["volume_mm3"]
    with rasterio.open(first.artifacts["processed_dem"]) as first_raster:
        first_values = first_raster.read(1)
    with rasterio.open(second.artifacts["processed_dem"]) as second_raster:
        second_values = second_raster.read(1)
    np.testing.assert_array_equal(first_values, second_values)
