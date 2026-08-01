from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.workflow import (
    WorkflowLaunchConfig,
    WorkflowStage,
    execute_workflow_launch,
    inspect_workflow_workspace,
    read_workflow_launch_config,
    write_workflow_launch_config,
)

runner = CliRunner()


def launch_config(tmp_path: Path) -> WorkflowLaunchConfig:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=16,
        columns=20,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "workflow"
    return WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=90.0,
            max_height_mm=30.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=20_000,
        ),
        maximum_tile_width_mm=50.0,
        maximum_tile_depth_mm=40.0,
        slicing_enabled=False,
    )


def test_launch_round_trip_resume_summary_and_static_browser(tmp_path: Path) -> None:
    config = launch_config(tmp_path)
    launch_path = write_workflow_launch_config(config)
    first_bytes = launch_path.read_bytes()

    assert read_workflow_launch_config(launch_path) == config
    assert write_workflow_launch_config(config).read_bytes() == first_bytes

    first = execute_workflow_launch(config)
    assert first.launch_config_path == launch_path.resolve()
    assert first.summary.source_mode == "local"
    assert first.summary.final_stage is WorkflowStage.CONNECT
    assert first.summary.completed_stages == (
        WorkflowStage.SOURCE,
        WorkflowStage.BUILD,
        WorkflowStage.LAYOUT,
        WorkflowStage.EXTRACT,
        WorkflowStage.MESH,
        WorkflowStage.CONNECT,
    )
    assert first.summary.metrics["tile_count"] == 4
    assert first.summary.metrics["connector_fit_status"] == "passed"
    assert first.summary_path.is_file()
    assert first.report_path.is_file()
    report = first.report_path.read_text(encoding="utf-8")
    assert "TopoForge workflow" in report
    assert "preview.png" in report
    assert "connector-map.png" in report
    assert "http://" not in report and "https://" not in report

    second = execute_workflow_launch(config)
    assert second.workflow.completed_stages == ()
    assert second.workflow.reused_stages == first.summary.completed_stages
    assert second.summary.reused_stages == first.summary.completed_stages
    reopened = inspect_workflow_workspace(config.workspace_dir)
    assert reopened.workflow_id == first.summary.workflow_id
    assert reopened.required_checks_passed is True

    manifest_path = second.workflow.stage_outputs[WorkflowStage.SOURCE] / "source.json"
    with manifest_path.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ConfigurationError, match="checksum changed"):
        inspect_workflow_workspace(config.workspace_dir)


def test_wizard_resume_and_browse_cli_without_server(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "wizard-source.tif",
        SyntheticTerrain.SADDLE,
        rows=16,
        columns=20,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "wizard-workflow"
    configured = runner.invoke(
        app,
        [
            "wizard",
            "--output",
            str(workspace),
            "--source",
            "local",
            "--dem",
            str(source),
            "--model-width-mm",
            "90",
            "--max-height-mm",
            "30",
            "--max-tile-size-mm",
            "50",
            "40",
            "--sampling-mode",
            "source-preserving",
            "--no-slice",
            "--no-run",
            "--yes",
        ],
    )
    assert configured.exit_code == 0, configured.output
    launch_path = workspace / "workflow-launch.yaml"
    assert launch_path.is_file()
    launch = read_workflow_launch_config(launch_path)
    assert launch.global_source is None
    assert launch.maximum_tile_width_mm == 50.0

    first = runner.invoke(app, ["resume", str(workspace)])
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["status"] == "completed"
    assert first_payload["source_mode"] == "local"
    assert first_payload["completed_stages"] == [
        "source",
        "build",
        "layout",
        "extract",
        "mesh",
        "connect",
    ]
    assert Path(first_payload["report"]).is_file()

    second = runner.invoke(app, ["resume", str(launch_path)])
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["completed_stages"] == []
    assert second_payload["reused_stages"] == first_payload["completed_stages"]

    browsed = runner.invoke(app, ["browse", str(workspace), "--no-open"])
    assert browsed.exit_code == 0, browsed.output
    browse_payload = json.loads(browsed.output)
    assert browse_payload["opened"] is False
    assert browse_payload["required_checks_passed"] is True
    assert Path(browse_payload["report"]).is_file()
    assert "preview_png" in browse_payload["artifacts"]
    assert "connector_map" in browse_payload["artifacts"]


def test_wizard_writes_global_bbox_launch_without_fetching(tmp_path: Path) -> None:
    workspace = tmp_path / "global-wizard"
    result = runner.invoke(
        app,
        [
            "wizard",
            "--output",
            str(workspace),
            "--source",
            "bbox",
            "--bbox",
            "-60.02",
            "-3.13",
            "-60.00",
            "-3.11",
            "--model-width-mm",
            "80",
            "--max-height-mm",
            "30",
            "--max-tile-size-mm",
            "80",
            "80",
            "--no-run",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    launch = read_workflow_launch_config(workspace / "workflow-launch.yaml")
    assert launch.global_source is not None
    assert launch.global_source.aoi.bbox_wgs84 == (-60.02, -3.13, -60.0, -3.11)
    assert not (workspace / "stages").exists()
