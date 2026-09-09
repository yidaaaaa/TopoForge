from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio

from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.raster.dependencies import raster_dependency_records
from topoforge.util import sha256_file
from topoforge.workflow import (
    WorkflowLaunchConfig,
    WorkflowStage,
    create_workflow_backup,
    execute_workflow_launch,
    inspect_workflow_workspace,
    read_workflow_launch_config,
    restore_workflow_backup,
)
from topoforge.workflow import local as workflow_local


def _launch(tmp_path: Path) -> WorkflowLaunchConfig:
    source = create_synthetic_geotiff(
        tmp_path / "inputs" / "source.tif",
        SyntheticTerrain.SLOPE,
        rows=8,
        columns=8,
        pixel_size_m=20,
    )
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False), rasterio.open(source, "r+") as dataset:
        dataset.write_mask(np.full((8, 8), 255, dtype=np.uint8))
    workspace = tmp_path / "workspace"
    return WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40,
            sampling_mode="source-preserving",
        ),
    )


def _mask_count(build: Path) -> int:
    with rasterio.open(build / "original_nodata_mask.tif") as dataset:
        return int(dataset.read(1).sum())


def test_sidecar_edit_rebuilds_and_backup_restores_mask_without_original_input(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    source = launch.build.dem_path
    first = execute_workflow_launch(launch).workflow
    original_sha = sha256_file(source)
    old_build = first.stage_outputs[WorkflowStage.BUILD]
    assert _mask_count(old_build) == 0
    mask = np.full((8, 8), 255, dtype=np.uint8)
    mask[3, 3] = 0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False), rasterio.open(source, "r+") as dataset:
        dataset.write_mask(mask)
    assert sha256_file(source) == original_sha
    with pytest.raises(ConfigurationError, match=r"source|identity"):
        workflow_local.verify_completed_workflow(launch.workspace_dir)

    second = execute_workflow_launch(launch).workflow
    new_build = second.stage_outputs[WorkflowStage.BUILD]
    assert second.required_checks_passed
    assert new_build != old_build
    assert not second.reused_stages
    assert _mask_count(old_build) == 0
    assert _mask_count(new_build) == 1
    repeated = execute_workflow_launch(launch).workflow
    assert repeated.reused_stages == tuple(repeated.stage_outputs)

    archive = tmp_path / "backup.zip"
    create_workflow_backup(launch.workspace_dir, archive)
    source.parent.rename(tmp_path / "original-input-unavailable")
    restored = tmp_path / "restored"
    restore_workflow_backup(archive, restored)
    restored_launch = read_workflow_launch_config(restored / "workflow-launch.yaml")
    restored_source = restored_launch.build.dem_path
    assert restored_source.name == source.name
    assert restored_source.with_suffix(".tif.msk").is_file()
    with rasterio.open(restored_source) as dataset:
        assert int(np.count_nonzero(dataset.read_masks(1) == 0)) == 1
    assert inspect_workflow_workspace(restored).required_checks_passed


def test_sidecar_removal_changes_dependency_identity(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    source = launch.build.dem_path
    records = raster_dependency_records(source)
    assert list(records) == ["source.tif.msk"]
    source.with_suffix(".tif.msk").unlink()
    assert raster_dependency_records(source) == {}


def test_legacy_algorithm_reopens_but_new_run_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = _launch(tmp_path)
    launch.build.dem_path.with_suffix(".tif.msk").unlink()
    implementation = workflow_local._build_identity_payload

    def legacy_identity(*args: object, **kwargs: object) -> dict[str, object]:
        kwargs["algorithm_version"] = None
        return implementation(*args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(workflow_local, "_build_identity_payload", legacy_identity)
        old = execute_workflow_launch(launch).workflow
    assert workflow_local.verify_completed_workflow(launch.workspace_dir)["required_checks_passed"]
    current = execute_workflow_launch(launch).workflow
    assert current.workflow_id != old.workflow_id
    assert current.stage_outputs[WorkflowStage.BUILD] != old.stage_outputs[WorkflowStage.BUILD]
    assert WorkflowStage.BUILD not in current.reused_stages
    request = json.loads((launch.workspace_dir / "workflow-request.json").read_text())
    assert request["build"]["algorithm_version"] == workflow_local._BUILD_ALGORITHM_VERSION
