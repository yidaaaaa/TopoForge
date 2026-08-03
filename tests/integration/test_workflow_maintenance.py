from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.models import BuildConfig, SamplingMode
from topoforge.overlays import (
    OverlayConfig,
    OverlayFormat,
    OverlayKind,
    OverlaySourceConfig,
)
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.workflow import (
    WorkflowLaunchConfig,
    create_workflow_backup,
    execute_workflow_launch,
    inspect_workflow_workspace,
    read_workflow_launch_config,
    restore_workflow_backup,
    verify_workflow_backup,
    write_workflow_launch_config,
)
from topoforge.workflow import maintenance as workflow_maintenance

runner = CliRunner()


def test_storage_cleanup_backup_restore_and_offline_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "workflow"
    overlay_path = tmp_path / "road.geojson"
    overlay_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[500010.0, 3299990.0], [500290.0, 3299790.0]],
                        },
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    launch = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=60.0,
            max_height_mm=25.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
        ),
        overlay=OverlayConfig(
            sources=(
                OverlaySourceConfig(
                    source_id="road",
                    kind=OverlayKind.ROAD,
                    format=OverlayFormat.GEOJSON,
                    path=overlay_path,
                    source_crs="EPSG:32648",
                    dataset_name="maintenance road fixture",
                    license="CC0-1.0",
                    attribution="TopoForge tests",
                ),
            )
        ),
        maximum_tile_width_mm=40.0,
        maximum_tile_depth_mm=35.0,
        slicing_enabled=False,
    )
    launch_path = write_workflow_launch_config(launch)

    pre_run_storage = runner.invoke(app, ["storage", str(launch_path)])
    assert pre_run_storage.exit_code == 0, pre_run_storage.output
    pre_run_payload = json.loads(pre_run_storage.output)
    assert pre_run_payload["estimate_basis"] == "configured_resource_ceilings"
    assert pre_run_payload["estimated_additional_bytes"] > 0
    assert pre_run_payload["backup_input_bytes"] >= source.stat().st_size

    execution = execute_workflow_launch(launch)
    assert execution.summary.metrics["storage"]["estimate_basis"] == (
        "completed_workflow_measurements"
    )
    assert (workspace / "workflow-storage.json").is_file()

    stale = workspace / "stages" / "10-build" / "stale-identity" / "retained.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"obsolete-stage-bytes")
    reviewed = runner.invoke(app, ["cleanup", str(workspace)])
    assert reviewed.exit_code == 0, reviewed.output
    review_payload = json.loads(reviewed.output)
    assert review_payload["status"] == "review"
    assert review_payload["reclaimable_bytes"] == len(b"obsolete-stage-bytes")
    assert [item["path"] for item in review_payload["candidates"]] == [
        "stages/10-build/stale-identity"
    ]
    assert stale.is_file()

    rejected = runner.invoke(
        app,
        [
            "cleanup",
            str(workspace),
            "--apply",
            "--confirm-workflow-id",
            "wrong-id",
        ],
    )
    assert rejected.exit_code == 2
    assert stale.is_file()

    applied = runner.invoke(
        app,
        [
            "cleanup",
            str(workspace),
            "--apply",
            "--confirm-workflow-id",
            execution.summary.workflow_id,
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert not stale.exists()
    assert inspect_workflow_workspace(workspace).required_checks_passed is True

    first_archive = tmp_path / "workflow-backup-1.zip"
    second_archive = tmp_path / "workflow-backup-2.zip"
    first_backup = create_workflow_backup(workspace, first_archive)
    second_backup = create_workflow_backup(workspace, second_archive)
    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_backup.archive_sha256 == second_backup.archive_sha256
    assert verify_workflow_backup(first_archive) == first_backup.manifest
    monkeypatch.setattr(workflow_maintenance, "__version__", "99.0.0")
    assert verify_workflow_backup(first_archive) == first_backup.manifest
    assert sum(item.kind == "external" for item in first_backup.manifest.files) >= 2

    tampered = tmp_path / "workflow-backup-tampered.zip"
    with (
        zipfile.ZipFile(first_archive, "r") as original,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as changed,
    ):
        changed_name = next(name for name in original.namelist() if name.startswith("workspace/"))
        for name in original.namelist():
            payload = original.read(name)
            changed.writestr(name, payload + b"tamper" if name == changed_name else payload)
    tampered_result = runner.invoke(
        app,
        ["restore", str(tampered), "--output", str(tmp_path / "tampered-restore")],
    )
    assert tampered_result.exit_code == 2

    source.unlink()
    restored_workspace = tmp_path / "restored-workflow"
    restored = restore_workflow_backup(first_archive, restored_workspace)
    assert restored.required_checks_passed is True
    assert restored.external_directory == restored_workspace / "backup-external"
    restored_launch = read_workflow_launch_config(restored_workspace / "workflow-launch.yaml")
    assert restored_launch.build.dem_path.is_file()
    assert restored_workspace in restored_launch.build.dem_path.parents
    assert restored_launch.overlay is not None
    assert restored_launch.overlay.sources[0].path is not None
    assert restored_launch.overlay.sources[0].path.is_file()
    assert restored_workspace in restored_launch.overlay.sources[0].path.parents
    assert inspect_workflow_workspace(restored_workspace).required_checks_passed is True

    resumed = execute_workflow_launch(restored_launch)
    assert resumed.summary.required_checks_passed is True
    assert resumed.workflow.completed_stages
