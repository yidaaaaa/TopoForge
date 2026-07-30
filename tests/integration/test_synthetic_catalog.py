from pathlib import Path

import pytest

from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.models import BuildConfig, DatasetType, VerticalScaleMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff, process_local_raster
from topoforge.scaling import apply_vertical_scale, resolve_scaling
from topoforge.validation import validate_mesh


@pytest.mark.parametrize("terrain", list(SyntheticTerrain))
def test_every_synthetic_dem_reaches_a_valid_closed_mesh(
    tmp_path: Path,
    terrain: SyntheticTerrain,
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / f"{terrain.value}.tif",
        terrain,
        rows=18,
        columns=24,
        pixel_size_m=20.0,
    )
    config = BuildConfig(
        dem_path=source,
        output_dir=tmp_path / f"processed-{terrain.value}",
        model_width_mm=120.0,
        max_height_mm=38.0,
        vertical_scale_mode=VerticalScaleMode.AUTO_PERCEPTUAL,
        dataset_type=DatasetType.DTM,
        data_license="Apache-2.0 synthetic fixture",
    )
    processed = process_local_raster(config)
    scaling = resolve_scaling(processed.elevations_m, processed.report, config)
    mesh = build_rectangular_terrain_mesh(
        apply_vertical_scale(processed.elevations_m, scaling),
        width_mm=scaling.model_width_mm,
        depth_mm=scaling.model_depth_mm,
        base_thickness_mm=scaling.base_thickness_mm,
    )
    report = validate_mesh(
        mesh,
        expected_dimensions_mm=(
            scaling.model_width_mm,
            scaling.model_depth_mm,
            float(mesh.extents[2]),
        ),
    )
    assert report.watertight
    assert report.manifold
    assert report.positive_volume
    assert report.dimensions_within_tolerance
