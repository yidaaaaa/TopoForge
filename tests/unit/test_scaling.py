from pathlib import Path

import numpy as np
import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.models import (
    BaselineMode,
    BuildConfig,
    DatasetMetadata,
    RasterResult,
    VerticalScaleMode,
)
from topoforge.scaling import apply_vertical_scale, resolve_scaling


def raster_report() -> RasterResult:
    return RasterResult(
        path=Path("processed_dem.tif"),
        array_shape=(3, 4),
        source_grid_shape=(3, 4),
        processed_grid_shape=(3, 4),
        transform=(10.0, 0.0, 0.0, 0.0, -10.0, 0.0),
        crs="EPSG:32648",
        nodata=None,
        ground_width_m=40.0,
        ground_depth_m=30.0,
        pixel_size_x_m=10.0,
        pixel_size_y_m=10.0,
        valid_fraction=1.0,
        interpolated_fraction=0.0,
        original_nodata_fraction=0.0,
        elevation_min_m=100.0,
        elevation_max_m=200.0,
        source_horizontal_resolution_m=10.0,
        processed_horizontal_resolution_m=10.0,
        downsampling_factor=1.0,
        physical_sample_spacing_mm=1.0,
        physical_sample_spacing_xy_mm=(1.0, 1.0),
        estimated_triangle_count=44,
        estimated_memory_mb=0.01,
        raw_elevation_min_m=100.0,
        raw_elevation_max_m=200.0,
        processed_elevation_min_m=100.0,
        processed_elevation_max_m=200.0,
        peak_elevation_loss_m=0.0,
        raw_peak_coordinate={"row": 2, "column": 3},
        processed_peak_coordinate={"row": 2, "column": 3},
        peak_horizontal_shift_m=0.0,
        sampling_decision_reasons=["unit fixture preserves source grid"],
        terrain_fidelity_status="source-resolution-preserved",
        terrain_fidelity_passed=True,
        peak_elevation_loss_threshold_m=20.0,
        peak_horizontal_shift_threshold_m=30.0,
        source_bounds={"native": [0.0, 0.0, 40.0, 30.0], "native_crs": "EPSG:32648"},
        metadata=DatasetMetadata(
            dataset_name="fixture",
            horizontal_crs="EPSG:32648",
        ),
    )


def test_natural_scale_maps_metres_to_mm_and_preserves_aspect(tmp_path: Path) -> None:
    elevations = np.array(
        [[100, 110, 120, 130], [120, 140, 160, 180], [130, 150, 180, 200]], dtype=np.float32
    )
    config = BuildConfig(
        dem_path=tmp_path / "in.tif",
        output_dir=tmp_path / "out",
        model_width_mm=40.0,
        max_height_mm=120.0,
        vertical_scale_mode=VerticalScaleMode.NATURAL,
    )
    scaling = resolve_scaling(elevations, raster_report(), config)
    assert scaling.horizontal_scale_mm_per_m == pytest.approx(1.0)
    assert scaling.model_depth_mm == pytest.approx(30.0)
    assert scaling.vertical_exaggeration == pytest.approx(1.0)
    mapped = apply_vertical_scale(elevations, scaling)
    assert float(np.min(mapped)) == pytest.approx(config.base_thickness_mm)
    assert float(np.max(mapped)) == pytest.approx(103.0)


def test_fit_height_uses_robust_relief(tmp_path: Path) -> None:
    elevations = np.linspace(0.0, 100.0, 10_000, dtype=np.float32).reshape(100, 100)
    config = BuildConfig(
        dem_path=tmp_path / "in.tif",
        output_dir=tmp_path / "out",
        model_width_mm=40.0,
        max_height_mm=23.0,
        vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
    )
    report = raster_report().model_copy(update={"ground_width_m": 100.0, "ground_depth_m": 100.0})
    scaling = resolve_scaling(elevations, report, config)
    robust_relief = float(np.percentile(elevations, 99.5) - np.percentile(elevations, 0.5))
    assert robust_relief * 0.4 * scaling.policy_vertical_exaggeration == pytest.approx(20.0)
    assert scaling.height_limit_applied
    assert scaling.vertical_exaggeration < scaling.policy_vertical_exaggeration
    mapped = apply_vertical_scale(elevations, scaling)
    assert float(np.max(mapped)) == pytest.approx(23.0)
    assert scaling.predicted_max_z_mm == pytest.approx(23.0)


def test_sea_level_baseline_survives_scaling_and_mesh_construction(tmp_path: Path) -> None:
    from topoforge.mesh import build_rectangular_terrain_mesh

    elevations = np.array([[10.0, 20.0], [25.0, 30.0]], dtype=np.float32)
    report = raster_report().model_copy(
        update={
            "array_shape": (2, 2),
            "ground_width_m": 40.0,
            "ground_depth_m": 40.0,
        }
    )
    config = BuildConfig(
        dem_path=tmp_path / "in.tif",
        output_dir=tmp_path / "out",
        model_width_mm=40.0,
        max_height_mm=45.0,
        baseline_mode=BaselineMode.SEA_LEVEL,
        vertical_scale_mode=VerticalScaleMode.NATURAL,
    )
    scaling = resolve_scaling(elevations, report, config)
    mapped = apply_vertical_scale(elevations, scaling)
    np.testing.assert_allclose(mapped, elevations + 3.0)

    mesh = build_rectangular_terrain_mesh(
        mapped,
        width_mm=scaling.model_width_mm,
        depth_mm=scaling.model_depth_mm,
        base_thickness_mm=scaling.base_thickness_mm,
    )
    top_z = mesh.vertices[: elevations.size, 2].reshape(elevations.shape)
    np.testing.assert_allclose(top_z, mapped)
    assert float(mesh.extents[2]) == pytest.approx(33.0)


def test_natural_scale_rejects_a_hard_height_overrun(tmp_path: Path) -> None:
    elevations = np.array([[0.0, 50.0], [75.0, 100.0]], dtype=np.float32)
    report = raster_report().model_copy(
        update={
            "array_shape": (2, 2),
            "ground_width_m": 40.0,
            "ground_depth_m": 40.0,
        }
    )
    config = BuildConfig(
        dem_path=tmp_path / "in.tif",
        output_dir=tmp_path / "out",
        model_width_mm=40.0,
        max_height_mm=45.0,
        vertical_scale_mode=VerticalScaleMode.NATURAL,
    )
    with pytest.raises(ConfigurationError, match=r"above the hard 45\.000 mm limit"):
        resolve_scaling(elevations, report, config)


def test_conflicting_requested_depth_is_actionable(tmp_path: Path) -> None:
    config = BuildConfig(
        dem_path=tmp_path / "in.tif",
        output_dir=tmp_path / "out",
        model_width_mm=200.0,
        model_depth_mm=100.0,
    )
    with pytest.raises(ConfigurationError, match="aspect ratio"):
        resolve_scaling(np.ones((3, 4), dtype=np.float32), raster_report(), config)
