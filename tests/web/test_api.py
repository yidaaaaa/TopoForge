from __future__ import annotations

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
        assert "default-src 'self'" in health.headers["content-security-policy"]

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
