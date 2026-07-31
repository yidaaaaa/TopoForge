from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from typer.testing import CliRunner

from topoforge.models import BuildConfig, DatasetType, ResourceBudgetMode, VerticalScaleMode

cli_module = importlib.import_module("topoforge.cli.app")
runner = CliRunner()


def _capture_build(monkeypatch: Any) -> list[BuildConfig]:
    captured: list[BuildConfig] = []

    def fake_build(config: BuildConfig) -> SimpleNamespace:
        captured.append(config)
        return SimpleNamespace(
            output_dir=config.output_dir,
            provenance={"scaling": {"vertical_exaggeration": config.vertical_exaggeration}},
            validation={
                "dimensions_mm": [config.model_width_mm, config.model_depth_mm or 1.0, 10.0],
                "watertight": True,
                "manifold": True,
                "required_checks_passed": True,
            },
            artifacts={},
        )

    monkeypatch.setattr(cli_module, "build_local_terrain", fake_build)
    return captured


def _write_config(path: Path) -> None:
    payload = {
        "dem_path": "yaml-source.tif",
        "output_dir": "outputs/from-yaml",
        "model_width_mm": 111.0,
        "model_depth_mm": 77.0,
        "base_thickness_mm": 4.0,
        "max_height_mm": 39.0,
        "vertical_scale_mode": "natural",
        "vertical_exaggeration": 2.5,
        "max_estimated_triangles": 123456,
        "resource_budget_mode": "strict",
        "dataset_type": "dsm",
        "dataset_name": "YAML dataset",
        "dataset_version": "v-yaml",
        "acquisition_period": "2020-2021",
        "source_urls": ["https://example.test/yaml"],
        "vertical_crs": "EPSG:5773",
        "vertical_datum": "EGM96",
        "data_license": "YAML-LICENSE",
        "attribution": "YAML attribution",
        "printer_profile": {
            "profile_id": "yaml-printer",
            "build_volume_mm": [200.0, 200.0, 200.0],
            "nozzle_diameter_mm": 0.6,
            "layer_height_mm": 0.3,
            "minimum_feature_mm": 0.7,
            "preferred_mesh_sampling_mm": 0.8,
            "minimum_base_thickness_mm": 2.0,
            "connector_tolerance_mm": 0.25,
        },
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_config_defaults_are_not_overwritten_by_cli_defaults(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config_path = tmp_path / "build.yaml"
    _write_config(config_path)
    captured = _capture_build(monkeypatch)

    result = runner.invoke(cli_module.app, ["build", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    resolved = captured.pop()
    assert resolved.dem_path == Path("yaml-source.tif")
    assert resolved.output_dir == Path("outputs/from-yaml")
    assert resolved.model_width_mm == 111.0
    assert resolved.model_depth_mm == 77.0
    assert resolved.base_thickness_mm == 4.0
    assert resolved.max_height_mm == 39.0
    assert resolved.vertical_scale_mode is VerticalScaleMode.NATURAL
    assert resolved.max_estimated_triangles == 123_456
    assert resolved.resource_budget_mode is ResourceBudgetMode.STRICT
    assert resolved.dataset_type is DatasetType.DSM
    assert resolved.dataset_version == "v-yaml"
    assert resolved.source_urls == ["https://example.test/yaml"]
    assert resolved.printer_profile.profile_id == "yaml-printer"


def test_explicit_cli_options_override_all_corresponding_yaml_fields(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config_path = tmp_path / "build.yaml"
    _write_config(config_path)
    captured = _capture_build(monkeypatch)
    args = [
        "build",
        "--config",
        str(config_path),
        "--dem",
        "cli-source.tif",
        "--output",
        "outputs/from-cli",
        "--size-mm",
        "150",
        "0",
        "--base-mm",
        "5",
        "--max-height-mm",
        "44",
        "--vertical-scale",
        "custom",
        "--vertical-exaggeration",
        "3.5",
        "--max-estimated-triangles",
        "200000",
        "--resource-budget-mode",
        "adapt",
        "--printer-profile",
        "bambu-p2s-0.4",
        "--dataset-type",
        "dtm",
        "--dataset-name",
        "CLI dataset",
        "--dataset-version",
        "v-cli",
        "--acquisition-period",
        "2024",
        "--source-url",
        "https://example.test/one",
        "--source-url",
        "https://example.test/two",
        "--vertical-crs",
        "EPSG:3855",
        "--vertical-datum",
        "EGM2008",
        "--data-license",
        "CLI-LICENSE",
        "--attribution",
        "CLI attribution",
    ]

    result = runner.invoke(cli_module.app, args)

    assert result.exit_code == 0, result.output
    resolved = captured.pop()
    assert resolved.dem_path == Path("cli-source.tif")
    assert resolved.output_dir == Path("outputs/from-cli")
    assert resolved.model_width_mm == 150.0
    assert resolved.model_depth_mm is None
    assert resolved.base_thickness_mm == 5.0
    assert resolved.max_height_mm == 44.0
    assert resolved.vertical_scale_mode is VerticalScaleMode.CUSTOM
    assert resolved.vertical_exaggeration == 3.5
    assert resolved.max_estimated_triangles == 200_000
    assert resolved.resource_budget_mode is ResourceBudgetMode.ADAPT
    assert resolved.printer_profile.profile_id == "bambu-p2s-0.4"
    assert resolved.dataset_type is DatasetType.DTM
    assert resolved.dataset_name == "CLI dataset"
    assert resolved.dataset_version == "v-cli"
    assert resolved.acquisition_period == "2024"
    assert resolved.source_urls == ["https://example.test/one", "https://example.test/two"]
    assert resolved.vertical_crs == "EPSG:3855"
    assert resolved.vertical_datum == "EGM2008"
    assert resolved.data_license == "CLI-LICENSE"
    assert resolved.attribution == "CLI attribution"


def test_build_without_profile_uses_bambu_p2s_default(monkeypatch: Any) -> None:
    captured = _capture_build(monkeypatch)

    result = runner.invoke(cli_module.app, ["build", "--dem", "source.tif"])

    assert result.exit_code == 0, result.output
    assert captured.pop().printer_profile.profile_id == "bambu-p2s-0.4"


def test_cli_custom_sampling_and_bbox_are_resolved(monkeypatch: Any) -> None:
    captured = _capture_build(monkeypatch)

    result = runner.invoke(
        cli_module.app,
        [
            "build",
            "--dem",
            "source.tif",
            "--sampling-mode",
            "custom",
            "--mesh-sampling-mm",
            "0.75",
            "--max-grid-cells",
            "50000",
            "--max-estimated-triangles",
            "120000",
            "--resource-budget-mode",
            "strict",
            "--max-estimated-memory-mb",
            "256",
            "--bbox",
            "170",
            "-10",
            "-170",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    resolved = captured.pop()
    assert resolved.sampling_mode.value == "custom"
    assert resolved.mesh_sampling_mm == 0.75
    assert resolved.max_grid_cells == 50_000
    assert resolved.max_estimated_triangles == 120_000
    assert resolved.resource_budget_mode is ResourceBudgetMode.STRICT
    assert resolved.max_estimated_memory_mb == 256.0
    assert resolved.aoi is not None
    assert resolved.aoi.bbox_wgs84 == (170.0, -10.0, -170.0, 10.0)


def test_cli_center_radius_aoi_is_resolved(monkeypatch: Any) -> None:
    captured = _capture_build(monkeypatch)

    result = runner.invoke(
        cli_module.app,
        [
            "build",
            "--dem",
            "source.tif",
            "--center",
            "101.8",
            "29.6",
            "--radius-m",
            "10000",
        ],
    )

    assert result.exit_code == 0, result.output
    resolved = captured.pop()
    assert resolved.aoi is not None
    assert resolved.aoi.center_wgs84 == (101.8, 29.6)
    assert resolved.aoi.radius_m == 10_000.0


def test_fetch_dem_defaults_to_auto_and_records_selection_policy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from topoforge.models import DatasetMetadata

    captured: list[Any] = []

    class FakeAcquisition:
        def __init__(self) -> None:
            self.provider_id = "copernicus-aws"
            self.dataset = DatasetMetadata(
                provider="copernicus-aws",
                dataset_name="fixture DSM",
                dataset_type=DatasetType.DSM,
                horizontal_crs="EPSG:4326",
                license="TEST-LICENSE",
                attribution="fixture",
            )
            self.aoi = {"kind": "bbox"}
            self.plan = SimpleNamespace(model_dump=lambda mode: {"product": "fixture"})
            self.raster_path = tmp_path / "source.tif"
            self.acquisition_manifest_path = tmp_path / "source.json"

    def fake_selection(**kwargs: Any) -> SimpleNamespace:
        captured.append(kwargs["policy"])
        return SimpleNamespace(
            acquisition=FakeAcquisition(),
            trace=SimpleNamespace(model_dump=lambda mode: {"selected_provider": "copernicus-aws"}),
        )

    monkeypatch.setattr(cli_module, "ProviderAcquisition", FakeAcquisition)
    monkeypatch.setattr(cli_module, "fetch_with_provider_selection", fake_selection)

    result = runner.invoke(
        cli_module.app,
        [
            "fetch-dem",
            "--output",
            str(tmp_path / "requested.tif"),
            "--bbox",
            "101.2",
            "29.2",
            "101.3",
            "29.3",
            "--terrain-mode",
            "dtm",
            "--allow-semantic-fallback",
            "--preferred-provider",
            "copernicus-aws",
        ],
    )

    assert result.exit_code == 0, result.output
    policy = captured.pop()
    assert policy.requested_provider_id == "auto"
    assert policy.requested_terrain_mode.value == "dtm"
    assert policy.allow_semantic_fallback is True
    assert policy.preferred_provider_ids == ["copernicus-aws"]
    assert '"selected_provider": "copernicus-aws"' in result.output


def test_fetch_dem_retains_explicit_provider_mode(tmp_path: Path, monkeypatch: Any) -> None:
    captured: list[Any] = []

    class FakeAcquisition:
        def __init__(self) -> None:
            self.provider_id = "copernicus-aws"
            self.dataset = SimpleNamespace(model_dump=lambda mode: {})
            self.aoi: dict[str, Any] = {}
            self.plan = SimpleNamespace(model_dump=lambda mode: {})
            self.raster_path = tmp_path / "source.tif"
            self.acquisition_manifest_path = tmp_path / "source.json"

    def fake_selection(**kwargs: Any) -> SimpleNamespace:
        captured.append(kwargs["policy"])
        return SimpleNamespace(
            acquisition=FakeAcquisition(),
            trace=SimpleNamespace(model_dump=lambda mode: {}),
        )

    monkeypatch.setattr(cli_module, "ProviderAcquisition", FakeAcquisition)
    monkeypatch.setattr(cli_module, "fetch_with_provider_selection", fake_selection)

    result = runner.invoke(
        cli_module.app,
        [
            "fetch-dem",
            "--output",
            str(tmp_path / "requested.tif"),
            "--bbox",
            "101.2",
            "29.2",
            "101.3",
            "29.3",
            "--provider",
            "copernicus-aws",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured.pop().requested_provider_id == "copernicus-aws"
