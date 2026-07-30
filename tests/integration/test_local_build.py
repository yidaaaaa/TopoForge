from pathlib import Path

import pytest

from topoforge.engine import build_local_terrain, verify_artifact_bundle
from topoforge.exceptions import MeshValidationError
from topoforge.models import BuildConfig, DatasetType, VerticalScaleMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff


def test_synthetic_geotiff_to_complete_validated_bundle(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "gaussian-hill.tif",
        SyntheticTerrain.GAUSSIAN_HILL,
        rows=36,
        columns=48,
        pixel_size_m=15.0,
    )
    output = tmp_path / "build"
    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=180.0,
            base_thickness_mm=3.0,
            max_height_mm=42.0,
            vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
            dataset_type=DatasetType.DTM,
            dataset_name="Deterministic Gaussian hill",
            dataset_version="fixture-v1",
            acquisition_period="2026-07-31",
            source_urls=["https://example.test/synthetic-dem"],
            data_license="Apache-2.0 synthetic fixture",
            attribution="TopoForge analytic fixture",
        )
    )

    expected_names = {
        "model.stl",
        "model.3mf",
        "preview.glb",
        "processed_dem.tif",
        "original_nodata_mask.tif",
        "provenance.json",
        "validation.json",
        "validation.html",
        "build_config.resolved.yaml",
        "preview.png",
        "build_manifest.json",
    }
    assert {path.name for path in result.artifacts.values()} == expected_names
    assert all(path.is_file() and path.stat().st_size > 0 for path in result.artifacts.values())
    assert result.validation["required_checks_passed"] is True
    assert result.validation["watertight"] is True
    assert result.validation["manifold"] is True
    assert result.validation["connected_components"] == 1
    assert result.validation["dimensions_mm"][0] == pytest.approx(180.0, abs=0.05)
    assert result.validation["dimensions_mm"][1] == pytest.approx(135.0, abs=0.05)
    assert result.validation["dimensions_mm"][2] <= 42.0 + 0.05
    assert result.validation["height_limit_passed"] is True
    assert result.validation["minimum_base_thickness_passed"] is True
    assert result.validation["triangle_budget_passed"] is True
    assert result.validation["bottom_planarity_error_mm"] <= 0.01
    dataset = result.provenance["dataset"]
    assert dataset["dataset_version"] == "fixture-v1"
    assert dataset["acquisition_period"] == "2026-07-31"
    assert dataset["download_time"] == "not-applicable-local-input"
    assert dataset["source_urls"] == ["https://example.test/synthetic-dem"]
    assert "nearest valid cells" in " ".join(result.provenance["processing"]["pipeline"])
    evidence = verify_artifact_bundle(output)
    assert evidence["required_checks_passed"] is True
    assert evidence["dataset_name"] == "Deterministic Gaussian hill"

    preview_path = result.artifacts["preview_png"]
    preview_path.write_bytes(preview_path.read_bytes() + b"tamper")
    with pytest.raises(MeshValidationError, match="checksum mismatch"):
        verify_artifact_bundle(output)
