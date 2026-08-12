from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from topoforge.exceptions import ConfigurationError
from topoforge.models import (
    AreaOfInterestInput,
    BuildConfig,
    DatasetMetadata,
    DatasetType,
    SamplingMode,
    TerrainMode,
)
from topoforge.providers import ProviderSelectionTrace
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.workflow import (
    GlobalAcquisitionConfig,
    WorkflowLaunchConfig,
    create_workflow_backup,
    execute_workflow_launch,
    read_workflow_launch_config,
    restore_workflow_backup,
    write_workflow_launch_config,
)
from topoforge.workflow import local as workflow_local


@pytest.fixture(scope="module")
def completed_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("completed-workflow-verifier")
    source = create_synthetic_geotiff(
        root / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=8,
        columns=10,
        pixel_size_m=20.0,
    )
    workspace = root / "workflow"
    execute_workflow_launch(
        WorkflowLaunchConfig(
            workspace_dir=workspace,
            build=BuildConfig(
                dem_path=source,
                output_dir=workspace,
                model_width_mm=40.0,
                max_height_mm=20.0,
                sampling_mode=SamplingMode.SOURCE_PRESERVING,
                max_grid_cells=10_000,
            ),
            maximum_tile_width_mm=100.0,
            maximum_tile_depth_mm=100.0,
            slicing_enabled=False,
        )
    )
    return workspace


def _copy_workspace(template: Path, destination: Path) -> Path:
    workspace = destination / "workflow"
    shutil.copytree(template, workspace)
    launch_path = workspace / "workflow-launch.yaml"
    launch = read_workflow_launch_config(launch_path)
    write_workflow_launch_config(
        launch.model_copy(
            update={
                "workspace_dir": workspace.resolve(),
                "build": launch.build.model_copy(update={"output_dir": workspace.resolve()}),
            }
        ),
        launch_path,
    )
    return workspace


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_canonical(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(
        (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    )


def test_verify_completed_workflow_reopens_every_artifact_stage(
    completed_workspace: Path,
) -> None:
    evidence = workflow_local.verify_completed_workflow(completed_workspace)

    assert evidence["status"] == "verified"
    assert evidence["stages"] == [
        "source",
        "build",
        "layout",
        "extract",
        "mesh",
        "connect",
    ]
    assert evidence["external_processes_executed"] is False
    assert evidence["required_checks_passed"] is True


def test_verify_completed_workflow_rejects_resealed_stage_identity_and_path(
    tmp_path: Path,
    completed_workspace: Path,
) -> None:
    workspace = _copy_workspace(completed_workspace, tmp_path)
    manifest_path = workspace / "workflow-manifest.json"
    manifest = _json_object(manifest_path)
    build = next(record for record in manifest["stages"] if record["name"] == "build")
    old_output = workspace / build["output_path"]
    substituted_identity = "0" * 64
    new_output = old_output.with_name(substituted_identity)
    old_output.rename(new_output)
    build["identity_sha256"] = substituted_identity
    build["output_path"] = new_output.relative_to(workspace).as_posix()
    build["manifest_path"] = (new_output / "build_manifest.json").relative_to(workspace).as_posix()
    _write_canonical(manifest_path, manifest)

    with pytest.raises(ConfigurationError, match="build stage identity changed"):
        workflow_local.verify_completed_workflow(workspace)


def test_verify_completed_workflow_rejects_resealed_request_and_workflow_id(
    tmp_path: Path,
    completed_workspace: Path,
) -> None:
    workspace = _copy_workspace(completed_workspace, tmp_path)
    request_path = workspace / "workflow-request.json"
    request = _json_object(request_path)
    request["maximum_tile_width_mm"] = 99.0
    _write_canonical(request_path, request)
    request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()
    workflow_id = f"local-{request_sha256[:24]}"

    manifest_path = workspace / "workflow-manifest.json"
    manifest = _json_object(manifest_path)
    manifest["request_sha256"] = request_sha256
    manifest["workflow_id"] = workflow_id
    _write_canonical(manifest_path, manifest)
    status_path = workspace / "workflow-status.json"
    status = _json_object(status_path)
    status["workflow_id"] = workflow_id
    _write_canonical(status_path, status)

    with pytest.raises(ConfigurationError, match="request does not match the saved launch"):
        workflow_local.verify_completed_workflow(workspace)


def test_verify_completed_workflow_rejects_noncanonical_saved_launch(
    tmp_path: Path,
    completed_workspace: Path,
) -> None:
    workspace = _copy_workspace(completed_workspace, tmp_path)
    launch_path = workspace / "workflow-launch.yaml"
    launch_path.write_text(
        launch_path.read_text(encoding="utf-8") + "# unbound comment\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="launch is non-canonical or changed"):
        workflow_local.verify_completed_workflow(workspace)


def test_verify_completed_workflow_reopens_relocated_restore_artifacts(
    tmp_path: Path,
    completed_workspace: Path,
) -> None:
    archive = tmp_path / "workflow.zip"
    create_workflow_backup(completed_workspace, archive)
    restored = tmp_path / "restored-workflow"
    restore_workflow_backup(archive, restored)

    with pytest.raises(ConfigurationError, match=r"identity|request"):
        workflow_local.verify_completed_workflow(restored)
    evidence = workflow_local.verify_completed_workflow(
        restored,
        verify_request_identity=False,
    )

    assert evidence["status"] == "verified"
    assert evidence["external_processes_executed"] is False
    assert evidence["required_checks_passed"] is True

    archive.unlink()
    diagnostic = workflow_local.verify_completed_workflow(
        restored,
        verify_request_identity=False,
    )
    assert diagnostic["required_checks_passed"] is True


def test_verify_completed_workflow_rejects_resealed_source_manifest(
    tmp_path: Path,
    completed_workspace: Path,
) -> None:
    workspace = _copy_workspace(completed_workspace, tmp_path)
    manifest_path = workspace / "workflow-manifest.json"
    manifest = _json_object(manifest_path)
    source_record = next(record for record in manifest["stages"] if record["name"] == "source")
    source_path = workspace / source_record["manifest_path"]
    source = _json_object(source_path)
    source["source_size_bytes"] += 1
    _write_canonical(source_path, source)
    source_record["manifest_sha256"] = sha256_file(source_path)
    _write_canonical(manifest_path, manifest)

    with pytest.raises(ConfigurationError, match="source manifest no longer matches"):
        workflow_local.verify_completed_workflow(workspace)


def test_canonical_writer_ignores_predictable_legacy_temp_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "evidence.json"
    external = tmp_path / "external.txt"
    external.write_text("preserve\n", encoding="utf-8")
    legacy_temporary = tmp_path / ".evidence.json.tmp"
    try:
        legacy_temporary.symlink_to(external)
    except OSError:
        pytest.skip("host cannot create symlink fixture")

    workflow_local._write_canonical(destination, {"required_checks_passed": True})

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert legacy_temporary.is_symlink()
    assert _json_object(destination) == {"required_checks_passed": True}


def test_relocated_global_acquire_is_reverified_without_network(tmp_path: Path) -> None:
    config = GlobalAcquisitionConfig(
        aoi=AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.21, 29.21)),
        requested_provider_id="fixture-global",
        terrain_mode=TerrainMode.DSM,
        preferred_provider_ids=("fixture-global",),
        cache_dir=tmp_path / "cache",
        min_request_interval_seconds=0.0,
    )
    acquisition_dir = tmp_path / "relocated" / "stages" / "00-acquire" / ("a" * 64)
    acquisition_dir.mkdir(parents=True)
    raster_path = acquisition_dir / "global-aoi.tif"
    values = np.arange(16, dtype=np.float32).reshape(4, 4) + 800.0
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        height=4,
        width=4,
        count=1,
        dtype="float32",
        crs="EPSG:32648",
        transform=from_origin(500_000.0, 3_300_000.0, 30.0, 30.0),
        nodata=np.nan,
    ) as dataset:
        dataset.write(values, 1)
    dataset_metadata = DatasetMetadata(
        provider="fixture-global",
        dataset_name="Fixture Global DSM",
        dataset_version="fixture-v1",
        dataset_type=DatasetType.DSM,
        horizontal_resolution_m=30.0,
        horizontal_crs="EPSG:32648",
        vertical_crs="fixture-height",
        vertical_datum="fixture-datum",
        license="TEST-LICENSE",
        attribution="TopoForge offline fixture",
        acquisition_period="2026-01",
        download_time="2026-01-01T00:00:00Z",
        source_urls=["https://fixture.invalid/dem.tif"],
    )
    selection = ProviderSelectionTrace(
        policy=config.selection_policy(),
        evaluations=[],
        ranked_provider_ids=["fixture-global"],
        fetch_attempts=[],
        selected_provider="fixture-global",
        selected_dataset=dataset_metadata.dataset_name,
        outcome="selected",
    )
    normalized_aoi = config.normalized_aoi()
    raster_sha256 = sha256_file(raster_path)
    provider_manifest_path = raster_path.with_suffix(
        raster_path.suffix + ".source_acquisition.json"
    )
    old_root = Path("/original/global-workflow/stages/00-acquire") / ("a" * 64)
    provider_manifest = {
        "raster_path": str(old_root / raster_path.name),
        "acquisition_manifest_path": str(old_root / provider_manifest_path.name),
        "output_raster_sha256": raster_sha256,
        "output_source_nodata_pixels": 0,
        "aoi": normalized_aoi.model_dump(mode="json"),
        "dataset": dataset_metadata.model_dump(mode="json"),
        "provider_id": "fixture-global",
        "provider_selection": selection.model_dump(mode="json"),
        "quality_masks": [],
    }
    provider_manifest_path.write_text(
        json.dumps(provider_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stage_manifest_path = acquisition_dir / "acquire.json"
    _write_canonical(
        stage_manifest_path,
        {
            "schema_version": "topoforge-global-acquisition-stage-v1",
            "acquisition_identity": config.identity_payload(),
            "raster_path": str(old_root / raster_path.name),
            "raster_sha256": raster_sha256,
            "acquisition_manifest_path": str(old_root / provider_manifest_path.name),
            "acquisition_manifest_sha256": sha256_file(provider_manifest_path),
            "dataset": dataset_metadata.model_dump(mode="json"),
            "normalized_aoi": normalized_aoi.model_dump(mode="json"),
            "provider_selection": selection.model_dump(mode="json"),
            "quality_masks": [],
            "required_checks_passed": True,
        },
    )

    evidence = workflow_local._verify_relocated_global_source(
        config,
        acquisition_dir,
        stage_manifest_path,
    )

    assert evidence.raster_path == raster_path
    assert evidence.acquisition_manifest_path == provider_manifest_path
    assert evidence.provider_selection.selected_provider == "fixture-global"
    assert evidence.required_checks_passed is True
