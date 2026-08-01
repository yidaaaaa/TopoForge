from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.config import dump_resolved_config
from topoforge.exceptions import ConfigurationError, ProviderFetchError
from topoforge.models import (
    AreaOfInterest,
    AreaOfInterestInput,
    BuildConfig,
    DatasetMetadata,
    DatasetType,
    SamplingMode,
    TerrainMode,
)
from topoforge.providers import (
    CopernicusPlan,
    CopernicusProduct,
    CoverageInfo,
    ProviderAcquisition,
    ProviderDescriptor,
)
from topoforge.util import sha256_file
from topoforge.workflow import (
    GlobalAcquisitionConfig,
    LocalWorkflowConfig,
    LocalWorkflowResult,
    LocalWorkflowStatus,
    WorkflowStage,
    WorkflowState,
    acquire_global_source,
    run_local_workflow,
    verify_global_source,
)

runner = CliRunner()


class MetricFixtureProvider:
    provider_id = "fixture-global"

    def __init__(self, *, fail_next: bool = False) -> None:
        self.fail_next = fail_next
        self.fetch_count = 0

    def probe(self, aoi: AreaOfInterest) -> CoverageInfo:
        del aoi
        return CoverageInfo(
            covered=True,
            complete=True,
            dataset_type=DatasetType.DSM,
            horizontal_resolution_m=30.0,
            estimated_download_bytes=4096,
            failure_probability="low",
            reason=["offline metric GeoTIFF fixture"],
        )

    def metadata(self) -> DatasetMetadata:
        return DatasetMetadata(
            provider=self.provider_id,
            dataset_name="Fixture Global DSM",
            dataset_version="fixture-v1",
            dataset_type=DatasetType.DSM,
            horizontal_resolution_m=30.0,
            horizontal_crs="EPSG:4326",
            vertical_crs="fixture-height",
            vertical_datum="fixture-datum",
            license="TEST-LICENSE",
            attribution="TopoForge offline fixture",
            acquisition_period="2026-01",
            download_time="2026-01-01T00:00:00Z",
            source_urls=["https://fixture.invalid/dem.tif"],
        )

    def fetch(self, aoi: AreaOfInterest, destination: Path) -> ProviderAcquisition:
        self.fetch_count += 1
        if self.fail_next:
            self.fail_next = False
            raise ProviderFetchError("synthetic acquisition interruption")

        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        target_bounds = transform_bounds(
            "EPSG:4326",
            aoi.target_local_crs,
            *aoi.bounds_wgs84,
            densify_pts=21,
        )
        height, width = 18, 22
        transform = from_bounds(
            target_bounds[0],
            target_bounds[1],
            target_bounds[2],
            target_bounds[3],
            width,
            height,
        )
        rows, columns = np.indices((height, width), dtype=np.float32)
        values = 800.0 + columns * 4.0 + rows * 2.0
        values[3, 17] += 75.0
        with rasterio.open(
            destination,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs=aoi.target_local_crs,
            transform=transform,
            nodata=np.nan,
            compress="deflate",
        ) as dataset:
            dataset.write(values.astype(np.float32), 1)
            dataset.update_tags(PROVIDER=self.provider_id, DATASET_ID="fixture-global-dsm")

        quality_path = destination.with_name(f"{destination.stem}.edm.tif")
        quality_values = np.zeros((height, width), dtype=np.uint8)
        quality_values[3, 17] = 1
        with rasterio.open(
            quality_path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="uint8",
            crs=aoi.target_local_crs,
            transform=transform,
            compress="deflate",
        ) as dataset:
            dataset.write(quality_values, 1)
            dataset.update_tags(QUALITY_MASK_ROLE="edm")

        dataset_metadata = self.metadata().model_copy(
            update={
                "horizontal_crs": aoi.target_local_crs,
                "checksums": {"fixture-source": sha256_file(destination)},
            }
        )
        manifest_path = destination.with_suffix(destination.suffix + ".source_acquisition.json")
        quality_record = {
            "role": "edm",
            "availability": "present",
            "output": {
                "path": str(quality_path),
                "sha256": sha256_file(quality_path),
                "grid_shape": [height, width],
                "crs": aoi.target_local_crs,
                "transform": list(tuple(transform)[:6]),
                "raw_values_preserved": True,
            },
        }
        acquisition = ProviderAcquisition(
            provider_id=self.provider_id,
            raster_path=destination,
            acquisition_manifest_path=manifest_path,
            dataset=dataset_metadata,
            aoi=aoi.model_dump(mode="json"),
            plan=CopernicusPlan(
                product=CopernicusProduct.GLO_30,
                dataset_id="fixture-global-dsm",
                horizontal_resolution_m=30.0,
                tiles=[],
                decisions=["offline fixture selected"],
            ),
            catalog_downloads=[],
            asset_catalog_downloads=[],
            tile_downloads=[],
            quality_masks=[quality_record],
            generated_at="2026-01-01T00:00:00Z",
        )
        payload = acquisition.model_dump(mode="json")
        payload.update(
            {
                "output_source_nodata_pixels": 0,
                "output_raster_sha256": sha256_file(destination),
                "license_url": "https://fixture.invalid/license",
            }
        )
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return acquisition


def fixture_descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="fixture-global",
        name="Fixture global DSM",
        implemented=True,
        requires_api_key=False,
        dataset_types=[DatasetType.DSM],
        notes="offline workflow fixture",
    )


def acquisition_config(aoi: AreaOfInterestInput, tmp_path: Path) -> GlobalAcquisitionConfig:
    return GlobalAcquisitionConfig(
        aoi=aoi,
        requested_provider_id="fixture-global",
        terrain_mode=TerrainMode.DSM,
        preferred_provider_ids=("fixture-global",),
        cache_dir=tmp_path / "cache",
        timeout_seconds=2.0,
        max_attempts=2,
        min_request_interval_seconds=0.0,
    )


def workflow_config(tmp_path: Path) -> LocalWorkflowConfig:
    aoi = AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.21, 29.21))
    workspace = tmp_path / "global-workflow"
    return LocalWorkflowConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=tmp_path / "unused-local-placeholder.tif",
            output_dir=workspace,
            model_width_mm=60.0,
            max_height_mm=25.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=20_000,
            aoi=AreaOfInterestInput(bbox_wgs84=(0.0, 0.0, 0.01, 0.01)),
            source_provider="stale-template-provider",
        ),
        global_source=acquisition_config(aoi, tmp_path),
        maximum_tile_width_mm=100.0,
        maximum_tile_depth_mm=100.0,
        slicing_enabled=False,
    )


@pytest.mark.parametrize(
    ("aoi", "expected_kind"),
    [
        (AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.21, 29.21)), "bbox"),
        (
            AreaOfInterestInput(center_wgs84=(101.205, 29.205), radius_m=500.0),
            "center-radius",
        ),
    ],
)
def test_global_acquisition_strictly_reopens_bbox_and_center_radius(
    tmp_path: Path,
    aoi: AreaOfInterestInput,
    expected_kind: str,
) -> None:
    provider = MetricFixtureProvider()
    config = acquisition_config(aoi, tmp_path)
    destination = tmp_path / expected_kind / "global-aoi.tif"

    evidence = acquire_global_source(
        config,
        destination,
        providers={provider.provider_id: provider},
        descriptors=[fixture_descriptor()],
    )

    assert provider.fetch_count == 1
    assert evidence.normalized_aoi.kind == expected_kind
    assert evidence.provider_selection.selected_provider == provider.provider_id
    assert evidence.provider_selection.ranked_provider_ids == [provider.provider_id]
    assert len(evidence.quality_mask_paths) == 1
    with rasterio.open(evidence.raster_path) as dataset:
        assert dataset.count == 1
        assert dataset.crs is not None and dataset.crs.is_projected
        dem_shape = dataset.shape
        dem_crs = dataset.crs
        dem_transform = dataset.transform
    with rasterio.open(evidence.quality_mask_paths[0]) as quality:
        assert quality.shape == dem_shape
        assert quality.crs == dem_crs
        assert quality.transform.almost_equals(dem_transform)
    assert verify_global_source(config, destination) == evidence


def test_global_acquisition_identity_ignores_operational_retry_and_cache_paths(
    tmp_path: Path,
) -> None:
    aoi = AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.21, 29.21))
    first = acquisition_config(aoi, tmp_path)
    second = first.model_copy(
        update={
            "cache_dir": tmp_path / "other-cache",
            "timeout_seconds": 99.0,
            "max_attempts": 7,
            "min_request_interval_seconds": 3.0,
        }
    )

    assert first.identity_payload() == second.identity_payload()


def test_global_source_rejects_raster_and_quality_mask_tampering(tmp_path: Path) -> None:
    aoi = AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.21, 29.21))
    config = acquisition_config(aoi, tmp_path)
    provider = MetricFixtureProvider()
    raster = tmp_path / "tamper-raster" / "global-aoi.tif"
    evidence = acquire_global_source(
        config,
        raster,
        providers={provider.provider_id: provider},
        descriptors=[fixture_descriptor()],
    )

    with raster.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ProviderFetchError, match="raster SHA-256 changed"):
        verify_global_source(config, raster)

    provider = MetricFixtureProvider()
    quality_raster = tmp_path / "tamper-quality" / "global-aoi.tif"
    evidence = acquire_global_source(
        config,
        quality_raster,
        providers={provider.provider_id: provider},
        descriptors=[fixture_descriptor()],
    )
    with evidence.quality_mask_paths[0].open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ProviderFetchError, match="quality mask checksum mismatch"):
        verify_global_source(config, quality_raster)


def test_global_workflow_reuses_acquisition_and_rejects_manifest_tampering(
    tmp_path: Path,
) -> None:
    config = workflow_config(tmp_path)
    provider = MetricFixtureProvider()
    providers = {provider.provider_id: provider}
    descriptors = [fixture_descriptor()]

    first = run_local_workflow(
        config,
        acquisition_providers=providers,
        acquisition_descriptors=descriptors,
    )
    assert first.workflow_id.startswith("global-")
    assert first.completed_stages == (
        WorkflowStage.ACQUIRE,
        WorkflowStage.SOURCE,
        WorkflowStage.BUILD,
        WorkflowStage.LAYOUT,
        WorkflowStage.EXTRACT,
        WorkflowStage.MESH,
        WorkflowStage.CONNECT,
    )
    assert provider.fetch_count == 1
    manifest_bytes = first.manifest_path.read_bytes()
    acquire_dir = first.stage_outputs[WorkflowStage.ACQUIRE]
    acquire_manifest = json.loads((acquire_dir / "acquire.json").read_text(encoding="utf-8"))
    source_dir = first.stage_outputs[WorkflowStage.SOURCE]
    source_record = json.loads((source_dir / "source.json").read_text(encoding="utf-8"))
    provider_manifest_path = Path(acquire_manifest["acquisition_manifest_path"])
    assert source_record["source_acquisition_manifest"]["sha256"] == sha256_file(
        provider_manifest_path
    )
    build_dir = first.stage_outputs[WorkflowStage.BUILD]
    provenance = json.loads((build_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["dataset"]["provider"] == provider.provider_id
    assert provenance["provider_selection"]["selected_provider"] == provider.provider_id
    assert provenance["processing"]["aoi"]["user_input"] == {
        "bbox_wgs84": [101.2, 29.2, 101.21, 29.21]
    }

    repeated = run_local_workflow(
        config,
        acquisition_providers=providers,
        acquisition_descriptors=descriptors,
    )
    assert repeated.completed_stages == ()
    assert repeated.reused_stages == tuple(WorkflowStage)[:-2]
    assert provider.fetch_count == 1
    assert repeated.manifest_path.read_bytes() == manifest_bytes

    provider_manifest = json.loads(provider_manifest_path.read_text(encoding="utf-8"))
    provider_manifest["tampered_note"] = "manifest bytes changed"
    provider_manifest_path.write_text(
        json.dumps(provider_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="existing acquire stage failed strict reuse"):
        run_local_workflow(
            config,
            acquisition_providers=providers,
            acquisition_descriptors=descriptors,
        )


def test_failed_global_acquisition_records_status_and_recovers(tmp_path: Path) -> None:
    config = workflow_config(tmp_path)
    provider = MetricFixtureProvider(fail_next=True)
    providers = {provider.provider_id: provider}
    descriptors = [fixture_descriptor()]

    with pytest.raises(ProviderFetchError, match="all provider fetches failed"):
        run_local_workflow(
            config,
            acquisition_providers=providers,
            acquisition_descriptors=descriptors,
        )
    failed = LocalWorkflowStatus.model_validate_json(
        (config.workspace_dir / "workflow-status.json").read_text(encoding="utf-8")
    )
    assert failed.state is WorkflowState.FAILED
    assert failed.current_stage is WorkflowStage.ACQUIRE
    assert failed.ready_stages == ()
    assert failed.failure_path is not None
    assert (config.workspace_dir / failed.failure_path).is_file()

    recovered = run_local_workflow(
        config,
        acquisition_providers=providers,
        acquisition_descriptors=descriptors,
    )
    assert recovered.completed_stages[0] is WorkflowStage.ACQUIRE
    assert recovered.required_checks_passed is True
    assert provider.fetch_count == 2


def test_run_cli_validates_and_binds_global_source_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = BuildConfig(
        dem_path=tmp_path / "unused.tif",
        output_dir=tmp_path / "workflow",
        model_width_mm=60.0,
        max_height_mm=25.0,
    )
    config_path = tmp_path / "build.yaml"
    dump_resolved_config(build, config_path)
    captured: dict[str, Any] = {}

    def fake_run(config: LocalWorkflowConfig, **kwargs: Any) -> LocalWorkflowResult:
        captured["config"] = config
        captured["kwargs"] = kwargs
        return LocalWorkflowResult(
            workspace_dir=config.workspace_dir,
            workflow_id="global-fixture",
            manifest_path=config.workspace_dir / "workflow-manifest.json",
            status_path=config.workspace_dir / "workflow-status.json",
            completed_stages=(WorkflowStage.ACQUIRE,),
            reused_stages=(),
            stage_outputs={WorkflowStage.ACQUIRE: config.workspace_dir / "stages/00-acquire"},
            required_checks_passed=True,
        )

    monkeypatch.setattr("topoforge.cli.app.run_local_workflow", fake_run)
    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_path),
            "--bbox",
            "101.2",
            "29.2",
            "101.21",
            "29.21",
            "--provider",
            "fixture-global",
            "--terrain-mode",
            "dsm",
            "--preferred-provider",
            "fixture-global",
            "--cache-dir",
            str(tmp_path / "provider-cache"),
            "--acquisition-timeout-seconds",
            "7",
            "--acquisition-max-attempts",
            "3",
            "--acquisition-min-request-interval-seconds",
            "0.25",
            "--timeout-seconds",
            "91",
            "--no-slice",
        ],
    )
    assert result.exit_code == 0, result.output
    workflow = captured["config"]
    assert isinstance(workflow, LocalWorkflowConfig)
    assert workflow.global_source is not None
    assert workflow.global_source.aoi.bbox_wgs84 == (101.2, 29.2, 101.21, 29.21)
    assert workflow.global_source.requested_provider_id == "fixture-global"
    assert workflow.global_source.terrain_mode is TerrainMode.DSM
    assert workflow.global_source.timeout_seconds == 7.0
    assert workflow.global_source.max_attempts == 3
    assert workflow.global_source.min_request_interval_seconds == 0.25
    assert workflow.slice_timeout_seconds == 91.0

    missing_aoi = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_path),
            "--provider",
            "fixture-global",
            "--no-slice",
        ],
    )
    assert missing_aoi.exit_code == 2
    assert "global acquisition options require" in missing_aoi.output

    incomplete_center = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_path),
            "--center",
            "101.2",
            "29.2",
            "--no-slice",
        ],
    )
    assert incomplete_center.exit_code == 2
    assert "center_wgs84 and radius_m must be supplied together" in incomplete_center.output
