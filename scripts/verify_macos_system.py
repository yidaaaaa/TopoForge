#!/usr/bin/env python3
"""Exercise the packaged macOS app, Web worker lifecycle, and recovery contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema

from topoforge.raster.sampling import triangle_count_for_shape

if __package__:
    from scripts.macos_app import (
        CLI_LAUNCHER_PATH,
        DEFAULT_CONFIG,
        SYSTEM_SCHEMA_VERSION,
        WEB_LAUNCHER_PATH,
        canonical_json_bytes,
        load_config,
        write_json_with_sha256,
    )
    from scripts.verify_macos_app import _poison_host_tls_environment, execute_archive
else:
    from macos_app import (  # type: ignore[import-not-found]
        CLI_LAUNCHER_PATH,
        DEFAULT_CONFIG,
        SYSTEM_SCHEMA_VERSION,
        WEB_LAUNCHER_PATH,
        canonical_json_bytes,
        load_config,
        write_json_with_sha256,
    )
    from verify_macos_app import (  # type: ignore[import-not-found]
        _poison_host_tls_environment,
        execute_archive,
    )

TARGETS = {
    "macos-15-arm64": 15,
    "macos-26-arm64": 26,
}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
EVIDENCE_SCHEMA = (
    Path(__file__).resolve().parents[1] / "packaging" / ("macos-app-evidence.schema.json")
)
_RECOVERY_RASTER_SHAPE = (768, 768)
_COPERNICUS_AOI_BBOX = (101.2, 29.2, 101.205, 29.205)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[Any, dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
            headers = {key.casefold(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Web API {method} {path} failed with HTTP {exc.code}: {detail}"
        ) from exc
    content_type = headers.get("content-type", "")
    if "json" in content_type:
        return json.loads(content), headers
    return content, headers


def _wait_health(base_url: str, process: subprocess.Popen[bytes], timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"packaged Web server stopped early with exit code {process.returncode}"
            )
        try:
            payload, _headers = _http(base_url, "/api/v1/health", timeout=2)
            if payload.get("status") == "ok":
                return
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(0.2)
    raise TimeoutError("packaged Web server did not become healthy on loopback")


def _start_server(
    launcher: Path,
    *,
    root: Path,
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], str, Any]:
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = root / f"web-{port}.log"
    log = log_path.open("xb")
    process = subprocess.Popen(
        [
            str(launcher),
            "--no-open",
            "--port",
            str(port),
            "--state-dir",
            str(root / "state"),
            "--workspace-root",
            str(root / "workspaces"),
            "--input-root",
            str(root / "inputs"),
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_health(base_url, process)
    except Exception:
        process.kill()
        process.wait(timeout=15)
        log.close()
        raise
    return process, base_url, log


def _stop_server(process: subprocess.Popen[bytes] | None, log: Any) -> None:
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
    if log is not None:
        log.close()


def _run_cli_json(
    cli: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    command = [str(cli), *arguments]
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    record = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise RuntimeError(
            f"packaged CLI failed with exit code {completed.returncode}: {arguments}\n"
            f"{completed.stderr[-4000:] or completed.stdout[-4000:]}"
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"packaged CLI emitted a non-object: {arguments}")
    return value, record


def _job_request(source: Path, workspace: Path, *, cells: int, triangles: int) -> dict[str, Any]:
    return {
        "launch": {
            "workspace_dir": str(workspace),
            "build": {
                "dem_path": str(source),
                "output_dir": str(workspace),
                "model_width_mm": 48.0,
                "base_thickness_mm": 3.0,
                "max_height_mm": 20.0,
                "sampling_mode": "source-preserving",
                "max_grid_cells": cells,
                "max_estimated_triangles": triangles,
                "max_estimated_memory_mb": 1024.0,
                "resource_budget_mode": "strict",
            },
            "maximum_tile_width_mm": 180.0,
            "maximum_tile_depth_mm": 180.0,
            "slicing_enabled": False,
        }
    }


def _recovery_job_request(source: Path, workspace: Path) -> dict[str, Any]:
    cells = _RECOVERY_RASTER_SHAPE[0] * _RECOVERY_RASTER_SHAPE[1]
    return _job_request(
        source,
        workspace,
        cells=cells,
        triangles=triangle_count_for_shape(_RECOVERY_RASTER_SHAPE),
    )


def _copernicus_job_request(root: Path, workspace: Path) -> dict[str, Any]:
    return {
        "launch": {
            "workspace_dir": str(workspace),
            "build": {
                "dem_path": str(root / "inputs" / "unused global source placeholder.tif"),
                "output_dir": str(workspace),
                "model_width_mm": 48.0,
                "base_thickness_mm": 3.0,
                "max_height_mm": 20.0,
                "terrain_mode": "dsm",
                "sampling_mode": "source-preserving",
                "max_grid_cells": 10_000,
                "max_estimated_triangles": 50_000,
                "max_estimated_memory_mb": 1024.0,
                "resource_budget_mode": "strict",
            },
            "global_source": {
                "aoi": {"bbox_wgs84": list(_COPERNICUS_AOI_BBOX)},
                "requested_provider_id": "copernicus-aws",
                "terrain_mode": "dsm",
                "allow_semantic_fallback": False,
                "preferred_provider_ids": [],
                "cache_dir": str(root / "provider cache"),
                "timeout_seconds": 30.0,
                "max_attempts": 4,
                "min_request_interval_seconds": 0.2,
            },
            "maximum_tile_width_mm": 180.0,
            "maximum_tile_depth_mm": 180.0,
            "slicing_enabled": False,
        }
    }


def _wait_job(base_url: str, job_id: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job, _headers = _http(base_url, f"/api/v1/jobs/{job_id}")
        if job["state"] in TERMINAL_STATES:
            return job
        time.sleep(0.25)
    raise TimeoutError(f"Web job did not reach a terminal state: {job_id}")


def _wait_active(base_url: str, job_id: str, *, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job, _headers = _http(base_url, f"/api/v1/jobs/{job_id}")
        if job["state"] == "running" and job.get("pid"):
            return job
        if job["state"] in TERMINAL_STATES:
            raise RuntimeError(f"recovery probe became terminal before restart: {job['state']}")
        time.sleep(0.1)
    raise TimeoutError("recovery probe did not enter the running state")


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_process_reaped(pid: int, *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            return
        time.sleep(0.1)
    raise RuntimeError(f"cancelled packaged worker process remains alive: {pid}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_artifact_reopen_passed(role: str, result: dict[str, Any]) -> bool:
    if role == "model_3mf":
        positive_counts = ("object_count", "build_item_count", "vertex_count", "triangle_count")
        return (
            result.get("unit") == "millimeter"
            and all(
                type(result.get(field)) is int and result[field] > 0 for field in positive_counts
            )
            and type(result.get("strict_warning_count")) is int
            and result["strict_warning_count"] == 0
        )
    if role in {"model_stl", "preview_glb"}:
        required_true = (
            "finite_vertices",
            "finite_face_normals",
            "watertight",
            "winding_consistent",
            "manifold",
            "positive_volume",
            "flat_bottom",
        )
        exact_counts = {
            "connected_components": 1,
            "degenerate_faces": 0,
            "duplicate_faces": 0,
        }
        return (
            all(result.get(field) is True for field in required_true)
            and all(
                type(result.get(field)) is int and result[field] == expected
                for field, expected in exact_counts.items()
            )
            and type(result.get("triangle_count")) is int
            and result["triangle_count"] > 0
        )
    return False


def verify_web_lifecycle(
    app: Path,
    *,
    work_root: Path,
) -> dict[str, Any]:
    """Run one completed job plus restart/cancel and backup/restore through the packaged app."""
    root = work_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for directory in (root / "inputs", root / "workspaces"):
        directory.mkdir(parents=True)
    fake_home = root / "home with spaces" / "用户"
    fake_home.mkdir(parents=True)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(fake_home),
            "CFFIXED_USER_HOME": str(fake_home),
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    _poison_host_tls_environment(environment, root)
    cli = app / CLI_LAUNCHER_PATH
    web = app / WEB_LAUNCHER_PATH
    commands: list[dict[str, Any]] = []
    normal_dem = root / "inputs" / "synthetic terrain 地形.tif"
    _result, command = _run_cli_json(
        cli,
        [
            "synthetic",
            "--output",
            str(normal_dem),
            "--terrain",
            "saddle",
            "--rows",
            "24",
            "--columns",
            "32",
            "--pixel-size-m",
            "20",
        ],
        cwd=root,
        environment=environment,
    )
    commands.append(command)

    server: subprocess.Popen[bytes] | None = None
    log: Any = None
    recovery_job_id: str | None = None
    try:
        server, base_url, log = _start_server(web, root=root, environment=environment)
        request = _job_request(
            normal_dem,
            root / "workspaces" / "synthetic Web job 地形",
            cells=20_000,
            triangles=80_000,
        )
        validated, _headers = _http(
            base_url, "/api/v1/jobs/validate", method="POST", payload=request
        )
        if validated.get("valid") is not True:
            raise RuntimeError("packaged Web job validation did not pass")
        created, _headers = _http(base_url, "/api/v1/jobs", method="POST", payload=request)
        completed = _wait_job(base_url, created["job_id"], timeout=180)
        if completed["state"] != "completed":
            raise RuntimeError(f"packaged Web synthetic job failed: {completed.get('error')}")

        workspace = Path(completed["workspace_dir"])
        artifacts = {item["artifact_id"]: item for item in completed["artifacts"]}
        reopen: dict[str, Any] = {}
        for role, command_name in (
            ("model_stl", "validate"),
            ("model_3mf", "inspect"),
            ("preview_glb", "inspect"),
        ):
            artifact = artifacts.get(role)
            if artifact is None or artifact.get("kind") != "file":
                raise RuntimeError(f"packaged Web job did not publish {role}")
            path = workspace / artifact["relative_path"]
            if _sha256(path) != artifact["sha256"]:
                raise RuntimeError(f"packaged Web artifact SHA-256 changed: {role}")
            result, command = _run_cli_json(
                cli,
                [command_name, str(path)],
                cwd=root,
                environment=environment,
            )
            commands.append(command)
            if not _strict_artifact_reopen_passed(role, result):
                raise RuntimeError(f"strict packaged artifact reopen failed: {role}")
            reopen[role] = result

        provider_request = _copernicus_job_request(
            root,
            root / "workspaces" / "Copernicus Web job 地形",
        )
        provider_validated, _headers = _http(
            base_url,
            "/api/v1/jobs/validate",
            method="POST",
            payload=provider_request,
        )
        if (
            provider_validated.get("valid") is not True
            or provider_validated.get("expected_stages", [None])[0] != "acquire"
        ):
            raise RuntimeError("packaged Copernicus Web job validation did not pass")
        provider_created, _headers = _http(
            base_url,
            "/api/v1/jobs",
            method="POST",
            payload=provider_request,
        )
        provider_completed = _wait_job(
            base_url,
            provider_created["job_id"],
            timeout=900,
        )
        if provider_completed["state"] != "completed":
            raise RuntimeError(
                "packaged Copernicus Web job failed: "
                + json.dumps(provider_completed.get("error"), ensure_ascii=False, sort_keys=True)
            )
        provider_summary = provider_completed.get("summary")
        provider_metrics = (
            provider_summary.get("metrics") if isinstance(provider_summary, dict) else None
        )
        if (
            not isinstance(provider_summary, dict)
            or provider_summary.get("source_mode") != "global"
            or not isinstance(provider_metrics, dict)
            or provider_metrics.get("selected_provider") != "copernicus-aws"
            or not isinstance(provider_metrics.get("raster_sha256"), str)
            or len(provider_metrics["raster_sha256"]) != 64
            or not isinstance(provider_metrics.get("acquisition_manifest_sha256"), str)
            or len(provider_metrics["acquisition_manifest_sha256"]) != 64
        ):
            raise RuntimeError("packaged Copernicus Web job did not retain provider evidence")

        backup, _headers = _http(
            base_url,
            f"/api/v1/jobs/{completed['job_id']}/backup",
            method="POST",
            payload={},
            timeout=60,
        )
        if backup.get("required_checks_passed") is not True:
            raise RuntimeError("packaged Web backup verification did not pass")
        restored, _headers = _http(
            base_url,
            f"/api/v1/backups/{backup['backup_id']}/restore",
            method="POST",
            payload={},
            timeout=90,
        )
        if (
            restored.get("state") != "completed"
            or restored.get("workspace_dir") == completed.get("workspace_dir")
            or restored.get("summary", {}).get("workflow_id")
            != completed.get("summary", {}).get("workflow_id")
        ):
            raise RuntimeError("packaged Web backup restore identity changed")

        recovery_dem = root / "inputs" / "recovery terrain.tif"
        _result, command = _run_cli_json(
            cli,
            [
                "synthetic",
                "--output",
                str(recovery_dem),
                "--terrain",
                "gaussian-hill",
                "--rows",
                str(_RECOVERY_RASTER_SHAPE[0]),
                "--columns",
                str(_RECOVERY_RASTER_SHAPE[1]),
                "--pixel-size-m",
                "20",
            ],
            cwd=root,
            environment=environment,
        )
        commands.append(command)
        recovery_request = _recovery_job_request(
            recovery_dem,
            root / "workspaces" / "restart recovery job",
        )
        recovery, _headers = _http(
            base_url, "/api/v1/jobs", method="POST", payload=recovery_request
        )
        recovery_job_id = recovery["job_id"]
        running = _wait_active(base_url, recovery_job_id)
        original_pid = running["pid"]
        _stop_server(server, log)
        server = None
        log = None

        server, base_url, log = _start_server(web, root=root, environment=environment)
        recovered, _headers = _http(base_url, f"/api/v1/jobs/{recovery_job_id}")
        if recovered["state"] != "running" or recovered.get("pid") != original_pid:
            error = recovered.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            error_type = error.get("exception_type") if isinstance(error, dict) else None
            error_message = (
                str(error.get("message", ""))[-2000:] if isinstance(error, dict) else None
            )
            raise RuntimeError(
                "packaged Web manager did not recover the same running worker after restart: "
                f"expected_pid={original_pid!r}; state={recovered.get('state')!r}; "
                f"observed_pid={recovered.get('pid')!r}; error_code={error_code!r}; "
                f"error_type={error_type!r}; error_message={error_message!r}"
            )
        cancelling, _headers = _http(
            base_url, f"/api/v1/jobs/{recovery_job_id}/cancel", method="POST", payload={}
        )
        if cancelling["state"] not in {"cancelling", "cancelled"}:
            raise RuntimeError("packaged Web recovery job did not accept cancellation")
        cancelled = _wait_job(base_url, recovery_job_id, timeout=45)
        if cancelled["state"] != "cancelled" or cancelled.get("pid") is not None:
            raise RuntimeError("packaged Web recovery worker was not fully cancelled")
        _wait_process_reaped(original_pid)
        recovery_job_id = None
        return {
            "completed_job": {
                "job_id": completed["job_id"],
                "workflow_id": completed["summary"]["workflow_id"],
                "artifact_sha256": {
                    role: artifacts[role]["sha256"]
                    for role in ("model_stl", "model_3mf", "preview_glb")
                },
            },
            "strict_reopen": reopen,
            "copernicus_provider": {
                "job_id": provider_completed["job_id"],
                "workflow_id": provider_summary["workflow_id"],
                "aoi": provider_validated["normalized_aoi"],
                "selected_provider": provider_metrics["selected_provider"],
                "dataset_name": provider_metrics["dataset_name"],
                "raster_sha256": provider_metrics["raster_sha256"],
                "acquisition_manifest_sha256": provider_metrics["acquisition_manifest_sha256"],
                "quality_mask_count": provider_metrics["quality_mask_count"],
                "required_checks_passed": True,
            },
            "backup_restore": {
                "backup_id": backup["backup_id"],
                "archive_sha256": backup["archive_sha256"],
                "restored_job_id": restored["job_id"],
                "required_checks_passed": True,
            },
            "restart_cancellation": {
                "original_worker_pid": original_pid,
                "same_worker_recovered": True,
                "terminal_state": "cancelled",
                "process_reaped": True,
                "required_checks_passed": True,
            },
            "server_binding": "127.0.0.1",
            "commands": commands,
            "required_checks_passed": True,
        }
    finally:
        if recovery_job_id is not None and server is not None and server.poll() is None:
            try:
                _http(
                    base_url,
                    f"/api/v1/jobs/{recovery_job_id}/cancel",
                    method="POST",
                    payload={},
                    timeout=10,
                )
                _wait_job(base_url, recovery_job_id, timeout=30)
            except Exception:
                pass
        _stop_server(server, log)


def validate_evidence_report(
    report: dict[str, Any],
    *,
    schema_path: Path = EVIDENCE_SCHEMA,
) -> None:
    """Validate one complete acceptance report against the tracked evidence schema."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(report)
    archive_verification = report["archive_verification"]
    if report["source"] != archive_verification["source"]:
        raise ValueError("system evidence source differs from archive verification")
    if report["archive"] != archive_verification["archive"]:
        raise ValueError("system evidence archive differs from archive verification")
    if report["app_payload_sha256"] != archive_verification["contents"]["payload_sha256"]:
        raise ValueError("system evidence app payload differs from archive verification")
    expected_major = TARGETS[report["target_id"]]
    if report["host"]["macos_major"] != expected_major:
        raise ValueError("system evidence target differs from the native host")


def verify_macos_system(
    archive: Path,
    *,
    config_path: Path,
    work_root: Path,
    expected_source_commit: str,
    expected_target: str,
    evidence_scope: str,
) -> dict[str, Any]:
    """Verify one source-bound archive on one frozen native target."""
    if expected_target not in TARGETS:
        raise ValueError(f"expected target must be one of {sorted(TARGETS)}")
    if evidence_scope not in {"hosted-package", "clean-system"}:
        raise ValueError("evidence scope must be hosted-package or clean-system")
    if evidence_scope == "clean-system" and os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("GitHub-hosted runners cannot emit clean-system evidence")
    config = load_config(config_path)
    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    app, archive_report = execute_archive(
        archive,
        config=config,
        work_root=root / "archive acceptance",
        expected_source_commit=expected_source_commit,
    )
    observed_major = archive_report["host"]["macos_major"]
    if observed_major != TARGETS[expected_target]:
        raise RuntimeError(
            f"host macOS major is {observed_major}, expected {TARGETS[expected_target]} "
            f"for {expected_target}"
        )
    web = verify_web_lifecycle(app, work_root=root / "web lifecycle with spaces" / "地形")
    clean = evidence_scope == "clean-system"
    report = {
        "schema_version": SYSTEM_SCHEMA_VERSION,
        "package_role": "phase13a-macos-arm64-unsigned-candidate",
        "evidence_scope": evidence_scope,
        "target_id": expected_target,
        "host": archive_report["host"],
        "source": archive_report["source"],
        "archive": archive_report["archive"],
        "app_payload_sha256": archive_report["contents"]["payload_sha256"],
        "archive_verification": archive_report,
        "web_lifecycle": web,
        "package_evidence": True,
        "hosted_package_evidence": not clean,
        "clean_system_evidence": clean,
        "signed": False,
        "notarized": False,
        "quarantine_first_launch_evidence": False,
        "gatekeeper_evidence": False,
        "bambu_phase13b_evidence": False,
        "public_support_status": "unverified",
        "required_checks_passed": True,
    }
    validate_evidence_report(report)
    return report


def main() -> int:
    """Run native packaged-app acceptance and retain a bounded evidence report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-target", choices=sorted(TARGETS), required=True)
    parser.add_argument(
        "--evidence-scope",
        choices=("hosted-package", "clean-system"),
        required=True,
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    try:
        report = verify_macos_system(
            args.archive.expanduser().resolve(),
            config_path=args.config.expanduser().resolve(),
            work_root=args.work_root.expanduser(),
            expected_source_commit=args.expected_source_commit,
            expected_target=args.expected_target,
            evidence_scope=args.evidence_scope,
        )
    except Exception as exc:
        failure = {
            "schema_version": SYSTEM_SCHEMA_VERSION,
            "evidence_scope": args.evidence_scope,
            "target_id": args.expected_target,
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "package_evidence": False,
            "hosted_package_evidence": False,
            "clean_system_evidence": False,
            "signed": False,
            "notarized": False,
            "quarantine_first_launch_evidence": False,
            "gatekeeper_evidence": False,
            "bambu_phase13b_evidence": False,
            "public_support_status": "unverified",
            "required_checks_passed": False,
        }
        write_json_with_sha256(report_path, failure)
        raise
    bounds = load_config(args.config)["bounds"]
    if len(canonical_json_bytes(report)) > bounds["evidence_max_bytes"]:
        raise ValueError("macOS system report exceeds its configured evidence bound")
    write_json_with_sha256(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
