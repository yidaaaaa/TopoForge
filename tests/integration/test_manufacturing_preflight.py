from pathlib import Path

import pytest

from topoforge.engine import build_local_terrain, preflight_local_terrain
from topoforge.models import BuildConfig, ResourceBudgetMode, SamplingMode, VerticalScaleMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff


def test_preflight_resolves_resources_without_publishing_output(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.GAUSSIAN_HILL,
        rows=80,
        columns=100,
        pixel_size_m=10.0,
    )
    output = tmp_path / "not-created"
    report = preflight_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=120.0,
            max_height_mm=35.0,
            vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
            max_grid_cells=50_000,
            max_estimated_triangles=150_000,
            max_estimated_memory_mb=64.0,
        )
    )

    assert report.status in {"passed", "passed-with-warnings"}
    assert report.estimated_triangle_count <= 150_000
    assert report.estimated_memory_mb <= 64.0
    assert report.resolved_model_dimensions_mm[0] == pytest.approx(120.0)
    assert report.build_volume_headroom_mm["x"] > 0
    assert report.build_volume_passed is True
    assert report.grid_cell_budget_passed is True
    assert report.triangle_budget_passed is True
    assert report.memory_budget_passed is True
    assert report.height_limit_passed is True
    assert not output.exists()


def test_build_publishes_the_exact_preflight_as_a_manifest_role(tmp_path: Path) -> None:
    import json

    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=24,
        columns=30,
        pixel_size_m=20.0,
    )
    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=tmp_path / "bundle",
            model_width_mm=80.0,
            max_height_mm=30.0,
            max_grid_cells=20_000,
        )
    )

    artifact = result.artifacts["manufacturing_preflight"]
    preflight = json.loads(artifact.read_text(encoding="utf-8"))
    manifest = json.loads(result.artifacts["manifest"].read_text(encoding="utf-8"))
    assert preflight == result.validation["manufacturing_preflight"]
    assert preflight == result.provenance["manufacturing_preflight"]
    assert manifest["artifacts"]["manufacturing_preflight"] == ("manufacturing_preflight.json")


def test_strict_preflight_leaves_no_partial_output(tmp_path: Path) -> None:
    from topoforge.exceptions import ConfigurationError

    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.GAUSSIAN_HILL,
        rows=200,
        columns=240,
        pixel_size_m=10.0,
    )
    output = tmp_path / "strict-output"
    with pytest.raises(ConfigurationError, match="strict resource budget rejected"):
        preflight_local_terrain(
            BuildConfig(
                dem_path=source,
                output_dir=output,
                sampling_mode=SamplingMode.SOURCE_PRESERVING,
                resource_budget_mode=ResourceBudgetMode.STRICT,
                max_grid_cells=1_000,
            )
        )
    assert not output.exists()


def test_config_rejects_impossible_printer_dimensions_before_raster_work(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="build width"):
        BuildConfig(
            dem_path=tmp_path / "missing.tif",
            output_dir=tmp_path / "out",
            model_width_mm=300.0,
        )
    with pytest.raises(ValueError, match="must exceed base"):
        BuildConfig(
            dem_path=tmp_path / "missing.tif",
            output_dir=tmp_path / "out",
            base_thickness_mm=3.0,
            max_height_mm=3.0,
        )
