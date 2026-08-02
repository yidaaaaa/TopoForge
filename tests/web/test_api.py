from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from topoforge.exceptions import ConfigurationError
from topoforge.overlays import (
    OverlayConfig,
    OverlayFormat,
    OverlayKind,
    OverlaySourceConfig,
    write_overlay_config,
)
from topoforge.web.api import create_app, verify_static_assets
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import WebAppConfig
from topoforge.workflow import write_workflow_launch_config

from .conftest import make_job_request


def test_health_capabilities_static_app_and_security_headers(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    app = create_app(web_config, static_dir=web_static_dir)
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["loopback_only"] is True
        assert health.json()["languages"] == ["zh-CN", "en"]
        content_security_policy = health.headers["content-security-policy"]
        assert "default-src 'self'" in content_security_policy
        assert "connect-src 'self' https://tile.openstreetmap.org" in content_security_policy

        capabilities = client.get("/api/v1/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["map"]["library"] == "MapLibre"
        assert capabilities.json()["preview"]["library"] == "Three.js"

        index = client.get("/")
        assert index.status_code == 200
        assert "TopoForge Web" in index.text
        assert client.get("/missing/client/route").status_code == 200

        rejected_host = client.get(
            "/api/v1/health",
            headers={"host": "outside.example"},
        )
        assert rejected_host.status_code == 400


def test_aoi_normalization_and_launch_validation_reuse_core_contracts(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    app = create_app(web_config, static_dir=web_static_dir)
    request = make_job_request(web_config, name="validate")
    with TestClient(app) as client:
        bbox = client.post(
            "/api/v1/aoi/normalize",
            json={"bbox_wgs84": [179.8, -1.0, -179.7, 1.0]},
        )
        assert bbox.status_code == 200
        assert bbox.json()["crosses_antimeridian"] is True
        assert bbox.json()["kind"] == "bbox"

        radius = client.post(
            "/api/v1/aoi/normalize",
            json={"center_wgs84": [85.0, 28.0], "radius_m": 5000.0},
        )
        assert radius.status_code == 200
        assert radius.json()["kind"] == "center-radius"
        assert radius.json()["area_m2"] > 0

        validation = client.post(
            "/api/v1/jobs/validate",
            json=request.model_dump(mode="json"),
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True
        assert validation.json()["expected_stages"][-1] == "connect"


def test_bambu_validation_requires_and_reuses_server_tool_configuration(
    web_config: WebAppConfig,
    web_static_dir: Path,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "BambuStudio.AppImage"
    executable.write_text(
        "#!/bin/sh\nprintf 'BambuStudio-02.07.01.62:\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    machine = tmp_path / "machine.json"
    process = tmp_path / "process.json"
    filament = tmp_path / "filament.json"
    for path in (machine, process, filament):
        path.write_text("{}\n", encoding="utf-8")

    base_request = make_job_request(web_config, name="bambu-validation")
    explicit_profiles = base_request.model_copy(
        update={
            "launch": base_request.launch.model_copy(
                update={
                    "slicing_enabled": True,
                    "slicer_name": "bambu-studio",
                    "slicer_settings": (machine, process),
                    "slicer_filaments": (filament,),
                    "project_evidence_enabled": True,
                }
            )
        }
    )
    missing_app = create_app(web_config, static_dir=web_static_dir)
    with TestClient(missing_app) as client:
        validation = client.post(
            "/api/v1/jobs/validate",
            json=explicit_profiles.model_dump(mode="json"),
        )
        assert validation.status_code == 422
        assert "--bambu-studio-executable" in validation.json()["detail"]["message"]
        submission = client.post(
            "/api/v1/jobs",
            json=explicit_profiles.model_dump(mode="json"),
        )
        assert submission.status_code == 422

    configured = WebAppConfig.model_validate(
        {
            **web_config.model_dump(),
            "bambu_studio_executable": executable,
            "bambu_machine_profile": machine,
            "bambu_process_profile": process,
            "bambu_filament_profile": filament,
        }
    )
    implicit_profiles = base_request.model_copy(
        update={
            "launch": base_request.launch.model_copy(
                update={
                    "slicing_enabled": True,
                    "slicer_name": "bambu-studio",
                    "project_evidence_enabled": True,
                }
            )
        }
    )
    configured_app = create_app(configured, static_dir=web_static_dir)
    with TestClient(configured_app) as client:
        validation = client.post(
            "/api/v1/jobs/validate",
            json=implicit_profiles.model_dump(mode="json"),
        )
        assert validation.status_code == 200
        slicer = validation.json()["slicer"]
        assert slicer["name"] == "BambuStudio"
        assert slicer["version"] == "02.07.01.62"
        assert slicer["status"] == "available"
        assert Path(slicer["executable"]) == executable.resolve()
        assert validation.json()["expected_stages"][-1] == "project"


def test_input_listing_and_traversal_rejection(
    web_config: WebAppConfig,
    web_static_dir: Path,
    tmp_path: Path,
) -> None:
    root = web_config.input_roots[0]
    (root / "terrain.tif").write_bytes(b"fixture")
    app = create_app(web_config, static_dir=web_static_dir)
    with TestClient(app) as client:
        roots = client.get("/api/v1/files")
        assert roots.status_code == 200
        assert roots.json()["entries"][0]["path"] == str(root.resolve())

        listing = client.get("/api/v1/files", params={"path": str(root)})
        assert listing.status_code == 200
        assert listing.json()["entries"][0]["name"] == "terrain.tif"

        outside = client.get("/api/v1/files", params={"path": str(tmp_path)})
        assert outside.status_code == 422
        assert outside.json()["detail"]["code"] == "configuration-error"


def test_asset_manifest_and_job_api_not_found_contract(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    report = verify_static_assets(web_static_dir)
    assert report["required_checks_passed"] is True
    assert report["languages"] == ["zh-CN", "en"]

    manager = LocalJobManager(web_config)
    app = create_app(
        web_config,
        manager=manager,
        static_dir=web_static_dir,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/jobs/" + "0" * 32)
        assert response.status_code == 404


def test_config_loader_reopens_launch_and_overlay_yaml(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    root = web_config.input_roots[0]
    request = make_job_request(web_config, name="config-load")
    launch_path = write_workflow_launch_config(
        request.launch,
        root / "workflow-launch.yaml",
    )
    overlay_path = write_overlay_config(
        OverlayConfig(
            sources=(
                OverlaySourceConfig(
                    source_id="contours",
                    kind=OverlayKind.CONTOUR,
                    format=OverlayFormat.GENERATED_CONTOURS,
                    dataset_name="Synthetic contours",
                    license="fixture",
                    attribution="TopoForge tests",
                    contour_interval_m=20.0,
                ),
            )
        ),
        root / "overlay.yaml",
    )
    app = create_app(web_config, static_dir=web_static_dir)
    with TestClient(app) as client:
        launch = client.post(
            "/api/v1/config/load",
            json={"kind": "launch", "path": str(launch_path)},
        )
        assert launch.status_code == 200
        assert Path(launch.json()["workspace_dir"]) == request.launch.workspace_dir
        assert launch.json()["build"]["sampling_mode"] == "source-preserving"

        overlay = client.post(
            "/api/v1/config/load",
            json={"kind": "overlay", "path": str(overlay_path)},
        )
        assert overlay.status_code == 200
        assert overlay.json()["sources"][0]["source_id"] == "contours"
        assert overlay.json()["sources"][0]["format"] == "generated-contours"


def test_static_asset_tamper_is_rejected(web_static_dir: Path) -> None:
    (web_static_dir / "assets" / "app.js").write_text(
        "document.body.dataset.ready = 'tampered';\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="checksum changed"):
        verify_static_assets(web_static_dir)


def test_completed_job_maintenance_routes_backup_restore_and_cleanup(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    app = create_app(web_config, static_dir=web_static_dir)
    request = make_job_request(web_config, name="api-maintenance")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/jobs",
            json=request.model_dump(mode="json"),
        )
        assert created.status_code == 201
        job_id = created.json()["job_id"]
        deadline = time.monotonic() + 90
        completed: dict[str, object] | None = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200
            payload = response.json()
            if payload["state"] in {"completed", "failed", "cancelled"}:
                completed = payload
                break
            time.sleep(0.05)
        assert completed is not None
        assert completed["state"] == "completed"
        workspace = Path(str(completed["workspace_dir"]))
        stale = workspace / "stages" / "99-unused" / "api-stale"
        stale.mkdir(parents=True)
        (stale / "payload.bin").write_bytes(b"api-unreferenced-stage")

        maintenance = client.get(f"/api/v1/jobs/{job_id}/maintenance")
        assert maintenance.status_code == 200
        overview = maintenance.json()
        assert overview["required_checks_passed"] is True
        assert overview["cleanup"]["reclaimable_bytes"] > 0
        workflow_id = overview["cleanup"]["workflow_id"]

        backup_response = client.post(f"/api/v1/jobs/{job_id}/backup")
        assert backup_response.status_code == 201
        backup = backup_response.json()
        assert backup["required_checks_passed"] is True
        backup_id = backup["backup_id"]

        backups = client.get("/api/v1/backups")
        assert backups.status_code == 200
        assert backups.json()[0]["backup_id"] == backup_id

        download = client.get(f"/api/v1/backups/{backup_id}")
        assert download.status_code == 200
        assert download.headers["x-topoforge-backup-sha256"] == backup["archive_sha256"]
        assert len(download.content) == backup["archive_size_bytes"]

        restored = client.post(
            f"/api/v1/backups/{backup_id}/restore",
            json={},
        )
        assert restored.status_code == 201
        restored_payload = restored.json()
        assert restored_payload["state"] == "completed"
        assert restored_payload["summary"]["workflow_id"] == workflow_id
        assert restored_payload["workspace_dir"] != str(workspace)

        rejected = client.post(
            f"/api/v1/jobs/{job_id}/cleanup",
            json={"confirm_workflow_id": "wrong"},
        )
        assert rejected.status_code == 422

        cleaned = client.post(
            f"/api/v1/jobs/{job_id}/cleanup",
            json={"confirm_workflow_id": workflow_id},
        )
        assert cleaned.status_code == 200
        assert cleaned.json()["removed_paths"] == ["stages/99-unused/api-stale"]
        assert not stale.exists()

        rejected_delete = client.request(
            "DELETE",
            f"/api/v1/jobs/{job_id}",
            json={"confirm_job_id": "0" * 32, "delete_workspace": True},
        )
        assert rejected_delete.status_code == 422
        assert workspace.is_dir()

        deleted = client.request(
            "DELETE",
            f"/api/v1/jobs/{job_id}",
            json={"confirm_job_id": job_id, "delete_workspace": True},
        )
        assert deleted.status_code == 200
        deletion = deleted.json()
        assert deletion["previous_state"] == "completed"
        assert deletion["workspace_removed"] is True
        assert deletion["workspace_retained"] is False
        assert deletion["deleted_job_record_bytes"] > 0
        assert deletion["deleted_workspace_bytes"] > 0
        assert deletion["backups_preserved"] is True
        assert deletion["required_checks_passed"] is True
        assert not workspace.exists()
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404

        retained_backup = client.get(f"/api/v1/backups/{backup_id}")
        assert retained_backup.status_code == 200
        assert retained_backup.content == download.content
