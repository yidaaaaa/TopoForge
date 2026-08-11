#!/usr/bin/env python3
"""Generate retained Phase 11 Web project lifecycle evidence."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from topoforge import __version__
from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.web.api import create_app
from topoforge.web.models import JobCreateRequest, WebAppConfig
from topoforge.workflow import WorkflowLaunchConfig


def _model_sha256(job: dict[str, Any]) -> str:
    for artifact in job["artifacts"]:
        if artifact["artifact_id"] == "model_3mf":
            return str(artifact["sha256"])
    raise ValueError("completed Web job has no model_3mf artifact")


def _wait_for_completion(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job["state"] in {"completed", "failed", "cancelled"}:
            if job["state"] != "completed":
                raise RuntimeError(json.dumps(job, indent=2, sort_keys=True))
            return dict(job)
        time.sleep(0.05)
    raise TimeoutError(f"Web job did not complete within 120 seconds: {job_id}")


def verify_lifecycle(root: Path) -> dict[str, Any]:
    """Execute one complete local Web maintenance lifecycle below a new root."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    inputs = root / "inputs"
    state = root / "state"
    workspaces = root / "workspaces"
    inputs.mkdir()
    source = inputs / "phase11-lifecycle.tif"
    create_synthetic_geotiff(
        source,
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    workspace = workspaces / "phase11-original"
    launch = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=64.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
            max_estimated_triangles=50_000,
            max_estimated_memory_mb=1024.0,
            resource_budget_mode="strict",
        ),
        maximum_tile_width_mm=32.0,
        maximum_tile_depth_mm=32.0,
        overlap_cells=1,
        slicing_enabled=False,
    )
    config = WebAppConfig(
        state_dir=state,
        workspace_root=workspaces,
        input_roots=(inputs,),
        poll_interval_seconds=0.05,
    )
    request = JobCreateRequest(launch=launch)
    statuses: dict[str, int] = {}

    with TestClient(create_app(config), base_url="http://localhost") as client:
        created_response = client.post(
            "/api/v1/jobs",
            json=request.model_dump(mode="json"),
        )
        statuses["create_job"] = created_response.status_code
        created_response.raise_for_status()
        original = _wait_for_completion(client, created_response.json()["job_id"])

        stale = workspace / "stages" / "99-unused" / "phase11-stale"
        stale.mkdir(parents=True)
        stale_payload = b"phase11-unreferenced-stage"
        (stale / "payload.bin").write_bytes(stale_payload)

        maintenance_response = client.get(f"/api/v1/jobs/{original['job_id']}/maintenance")
        statuses["maintenance_before"] = maintenance_response.status_code
        maintenance_response.raise_for_status()
        maintenance_before = maintenance_response.json()
        workflow_id = maintenance_before["cleanup"]["workflow_id"]
        if maintenance_before["cleanup"]["reclaimable_bytes"] < len(stale_payload):
            raise ValueError("maintenance plan did not measure the unreferenced stage")

        first_response = client.post(f"/api/v1/jobs/{original['job_id']}/backup")
        statuses["backup_first"] = first_response.status_code
        first_response.raise_for_status()
        first_backup = first_response.json()
        second_response = client.post(f"/api/v1/jobs/{original['job_id']}/backup")
        statuses["backup_repeat"] = second_response.status_code
        second_response.raise_for_status()
        second_backup = second_response.json()
        if first_backup != second_backup:
            raise ValueError("repeated lifecycle backup did not reuse identical bytes")

        download_response = client.get(first_backup["download_url"])
        statuses["backup_download"] = download_response.status_code
        download_response.raise_for_status()
        download_sha256 = download_response.headers["X-TopoForge-Backup-SHA256"]
        if download_sha256 != first_backup["archive_sha256"]:
            raise ValueError("backup download header SHA-256 changed")
        if len(download_response.content) != first_backup["archive_size_bytes"]:
            raise ValueError("backup download byte count changed")

        rejected_response = client.post(
            f"/api/v1/jobs/{original['job_id']}/cleanup",
            json={"confirm_workflow_id": "wrong"},
        )
        statuses["cleanup_wrong_confirmation"] = rejected_response.status_code
        if rejected_response.status_code != 422:
            raise ValueError("wrong cleanup confirmation was not rejected")

        cleanup_response = client.post(
            f"/api/v1/jobs/{original['job_id']}/cleanup",
            json={"confirm_workflow_id": workflow_id},
        )
        statuses["cleanup_confirmed"] = cleanup_response.status_code
        cleanup_response.raise_for_status()
        cleanup = cleanup_response.json()
        if cleanup["removed_paths"] != ["stages/99-unused/phase11-stale"]:
            raise ValueError("cleanup removed an unexpected path")
        if stale.exists():
            raise ValueError("confirmed cleanup did not remove its reviewed candidate")

        restore_response = client.post(
            f"/api/v1/backups/{first_backup['backup_id']}/restore",
            json={},
        )
        statuses["restore"] = restore_response.status_code
        restore_response.raise_for_status()
        restored = restore_response.json()
        if restored["state"] != "completed" or restored["exit_code"] != 0:
            raise ValueError("restored workflow was not registered as completed")
        if restored["summary"]["workflow_id"] != workflow_id:
            raise ValueError("restored workflow identity changed")
        if _model_sha256(restored) != _model_sha256(original):
            raise ValueError("restored model SHA-256 changed")

        maintenance_after_response = client.get(f"/api/v1/jobs/{original['job_id']}/maintenance")
        statuses["maintenance_after"] = maintenance_after_response.status_code
        maintenance_after_response.raise_for_status()
        maintenance_after = maintenance_after_response.json()
        if maintenance_after["cleanup"]["reclaimable_bytes"] != 0:
            raise ValueError("cleanup candidate remained after confirmed cleanup")

        backups_response = client.get("/api/v1/backups")
        statuses["list_backups"] = backups_response.status_code
        backups_response.raise_for_status()
        listed = backups_response.json()
        if [item["backup_id"] for item in listed] != [first_backup["backup_id"]]:
            raise ValueError("strict backup listing changed")

    return {
        "schema_version": "topoforge-phase11-lifecycle-verification-v1",
        "topoforge_version": __version__,
        "root": str(root),
        "source": {
            "path": str(source),
            "sha256": sha256_file(source),
            "network_attempts": 0,
        },
        "original_job": {
            "job_id": original["job_id"],
            "workspace": original["workspace_dir"],
            "workflow_id": workflow_id,
            "model_3mf_sha256": _model_sha256(original),
            "exit_code": original["exit_code"],
        },
        "maintenance_before": maintenance_before,
        "backup": {
            **first_backup,
            "repeat_byte_identical": first_backup == second_backup,
            "download_sha256": download_sha256,
            "download_size_bytes": len(download_response.content),
        },
        "cleanup": cleanup,
        "restored_job": {
            "job_id": restored["job_id"],
            "workspace": restored["workspace_dir"],
            "workflow_id": restored["summary"]["workflow_id"],
            "model_3mf_sha256": _model_sha256(restored),
            "exit_code": restored["exit_code"],
        },
        "maintenance_after": maintenance_after,
        "http_statuses": statuses,
        "required_checks_passed": True,
    }


def main() -> int:
    """Run the retained Phase 11 lifecycle verification."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = verify_lifecycle(args.root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
