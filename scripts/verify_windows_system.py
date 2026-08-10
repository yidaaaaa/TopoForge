#!/usr/bin/env python3
"""Verify native Web jobs, process recovery, backup, and artifact reopen."""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import topoforge
from topoforge.exporters.three_mf import ThreeMFInspection, inspect_3mf
from topoforge.models import BuildConfig, ResourceBudgetMode, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import JobCreateRequest, JobRecord, JobState, WebAppConfig
from topoforge.web.processes import (
    process_is_alive,
    terminate_process_tree,
    worker_process_options,
)
from topoforge.workflow import WorkflowLaunchConfig

SCHEMA_VERSION = "topoforge-windows-system-verification-v1"
_TERMINAL_STATES = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}


def _job_request(config: WebAppConfig, *, name: str) -> JobCreateRequest:
    input_root = config.input_roots[0]
    source = input_root / f"{name} terrain.tif"
    create_synthetic_geotiff(
        source,
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    workspace = config.workspace_root / f"{name} workspace"
    return JobCreateRequest(
        launch=WorkflowLaunchConfig(
            workspace_dir=workspace,
            build=BuildConfig(
                dem_path=source,
                output_dir=workspace,
                model_width_mm=40.0,
                max_height_mm=20.0,
                sampling_mode=SamplingMode.SOURCE_PRESERVING,
                max_grid_cells=10_000,
                max_estimated_triangles=50_000,
                resource_budget_mode=ResourceBudgetMode.STRICT,
            ),
            maximum_tile_width_mm=180.0,
            maximum_tile_depth_mm=180.0,
            slicing_enabled=False,
        )
    )


def _wait_for_terminal(
    manager: LocalJobManager,
    job_id: str,
    *,
    timeout_seconds: float,
) -> JobRecord:
    deadline = time.monotonic() + timeout_seconds
    last = manager.get(job_id)
    while time.monotonic() < deadline:
        last = manager.get(job_id)
        if last.state in _TERMINAL_STATES:
            return last
        time.sleep(manager.config.poll_interval_seconds)
    raise TimeoutError(
        f"Web job {job_id} remained {last.state.value!r} after {timeout_seconds} seconds"
    )


def _three_mf_report(inspection: ThreeMFInspection) -> dict[str, Any]:
    return {
        "unit": inspection.unit,
        "object_count": inspection.object_count,
        "build_item_count": inspection.build_item_count,
        "vertex_count": inspection.vertex_count,
        "triangle_count": inspection.triangle_count,
        "dimensions_mm": list(inspection.dimensions_mm),
        "strict_warning_count": inspection.strict_warning_count,
        "lib3mf_version": list(inspection.lib3mf_version),
    }


def _stop_active_job(manager: LocalJobManager, job_id: str | None) -> None:
    if job_id is None:
        return
    with contextlib.suppress(Exception):
        record = manager.get(job_id)
        if record.state in {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING}:
            manager.cancel(job_id)
            _wait_for_terminal(manager, job_id, timeout_seconds=20.0)


def _complete_recover_backup_restore(config: WebAppConfig) -> dict[str, Any]:
    submitted_id: str | None = None
    first = LocalJobManager(config)
    first.start()
    try:
        submitted = first.submit(_job_request(config, name="completed"))
        submitted_id = submitted.job_id
        completed = _wait_for_terminal(first, submitted.job_id, timeout_seconds=180.0)
        if completed.state is not JobState.COMPLETED:
            detail = completed.error.message if completed.error is not None else "no error detail"
            raise RuntimeError(f"native Web job finished as {completed.state.value}: {detail}")
        if completed.summary is None or completed.summary.required_checks_passed is not True:
            raise RuntimeError("native Web job has no verified workflow summary")
        if completed.ready_stages != completed.expected_stages:
            raise RuntimeError("native Web job did not complete every expected stage")
    finally:
        _stop_active_job(first, submitted_id)
        first.close()

    recovered_manager = LocalJobManager(config)
    recovered_manager.start()
    try:
        recovered = recovered_manager.get(completed.job_id)
        if recovered.state is not JobState.COMPLETED or recovered.summary is None:
            raise RuntimeError("completed Web job did not recover after manager restart")

        model_path, model_artifact = recovered_manager.artifact_path(
            recovered.job_id,
            "model_3mf",
        )
        if model_artifact.sha256 is None:
            raise RuntimeError("completed Web job 3MF has no recorded SHA-256")
        if sha256_file(model_path) != model_artifact.sha256:
            raise RuntimeError("completed Web job 3MF SHA-256 changed")
        inspection = inspect_3mf(model_path)

        backup = recovered_manager.create_backup(recovered.job_id)
        backup_path, reopened_backup = recovered_manager.backup_archive_path(backup.backup_id)
        if (
            not backup.required_checks_passed
            or reopened_backup != backup
            or sha256_file(backup_path) != backup.archive_sha256
        ):
            raise RuntimeError("completed Web job backup did not strictly reopen")

        restored = recovered_manager.restore_backup(
            backup.backup_id,
            workspace_name="restored-system-workspace",
        )
        if restored.state is not JobState.COMPLETED or restored.summary is None:
            raise RuntimeError("restored Web job is not completed")
        if restored.summary.workflow_id != recovered.summary.workflow_id:
            raise RuntimeError("restored Web workflow identity changed")
        restored_path, restored_artifact = recovered_manager.artifact_path(
            restored.job_id,
            "model_3mf",
        )
        if restored_artifact.sha256 != model_artifact.sha256:
            raise RuntimeError("restored Web 3MF checksum differs from the original")
        restored_inspection = inspect_3mf(restored_path)
        if restored_inspection.dimensions_mm != inspection.dimensions_mm:
            raise RuntimeError("restored Web 3MF dimensions differ from the original")

        events = recovered_manager.read_events(recovered.job_id)
        restored_events = recovered_manager.read_events(restored.job_id)
        if not events or events[-1].message_key != "job.completed":
            raise RuntimeError("completed Web job event log did not recover")
        if not restored_events or restored_events[-1].message_key != "job.restored":
            raise RuntimeError("restored Web job event log is incomplete")

        return {
            "completed_job": {
                "job_id": recovered.job_id,
                "workflow_id": recovered.summary.workflow_id,
                "workspace": str(recovered.workspace_dir),
                "exit_code": recovered.exit_code,
                "expected_stages": [stage.value for stage in recovered.expected_stages],
                "ready_stages": [stage.value for stage in recovered.ready_stages],
                "artifact_sha256": model_artifact.sha256,
                "three_mf": _three_mf_report(inspection),
                "event_count": len(events),
                "required_checks_passed": True,
            },
            "restart_recovery": {
                "state": recovered.state.value,
                "summary_reopened": True,
                "artifact_reopened": True,
                "required_checks_passed": True,
            },
            "backup_restore": {
                "backup_id": backup.backup_id,
                "archive_sha256": backup.archive_sha256,
                "archive_size_bytes": backup.archive_size_bytes,
                "file_count": backup.file_count,
                "restored_job_id": restored.job_id,
                "restored_workspace": str(restored.workspace_dir),
                "restored_artifact_sha256": restored_artifact.sha256,
                "restored_three_mf": _three_mf_report(restored_inspection),
                "required_checks_passed": True,
            },
        }
    finally:
        recovered_manager.close()


class _SlowJobManager(LocalJobManager):
    def _start_job(self, record: JobRecord) -> None:
        process = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    "import time; time.sleep(300)",
                ],
                cwd=self.config.workspace_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **worker_process_options(),
            ),
        )
        self._processes[record.job_id] = process
        self._write_record(
            record.model_copy(
                update={
                    "state": JobState.RUNNING,
                    "pid": process.pid,
                    "current_stage": record.expected_stages[0],
                }
            ),
            message_key="job.started",
        )


def _restart_and_cancel_worker(config: WebAppConfig) -> dict[str, Any]:
    first = _SlowJobManager(config)
    recovered: LocalJobManager | None = None
    process: subprocess.Popen[bytes] | None = None
    submitted_id: str | None = None
    first.start()
    try:
        submitted = first.submit(_job_request(config, name="cancelled"))
        submitted_id = submitted.job_id
        process = first._processes[submitted.job_id]
        running = first.get(submitted.job_id)
        if running.state is not JobState.RUNNING or running.pid != process.pid:
            raise RuntimeError("isolated cancellation worker did not enter running state")
        pid = process.pid

        first.close()
        recovered = LocalJobManager(config)
        recovered.start()
        recovered_running = recovered.get(submitted.job_id)
        if recovered_running.state is not JobState.RUNNING or recovered_running.pid != pid:
            raise RuntimeError("running worker did not recover after manager restart")

        cancelling = recovered.cancel(submitted.job_id)
        if cancelling.state is not JobState.CANCELLING:
            raise RuntimeError("recovered worker did not enter cancelling state")
        cancelled = _wait_for_terminal(recovered, submitted.job_id, timeout_seconds=30.0)
        if cancelled.state is not JobState.CANCELLED:
            raise RuntimeError(f"recovered worker finished as {cancelled.state.value}")
        process.wait(timeout=15.0)
        if process_is_alive(pid):
            raise RuntimeError("cancelled worker process remains alive")

        events = recovered.read_events(submitted.job_id)
        keys = [event.message_key for event in events]
        required_keys = {"job.queued", "job.started", "job.cancelling", "job.cancelled"}
        if not required_keys <= set(keys):
            raise RuntimeError(f"worker lifecycle event log is incomplete: {keys}")
        return {
            "job_id": submitted.job_id,
            "pid": pid,
            "worker_options": worker_process_options(),
            "recovered_state": recovered_running.state.value,
            "cancelling_state": cancelling.state.value,
            "terminal_state": cancelled.state.value,
            "process_alive_after_cancel": False,
            "event_keys": keys,
            "required_checks_passed": True,
        }
    finally:
        if recovered is not None:
            recovered.close()
        else:
            _stop_active_job(first, submitted_id)
        first.close()
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                terminate_process_tree(process.pid)
            with contextlib.suppress(Exception):
                process.wait(timeout=15.0)


def verify_windows_system(
    work_root: Path,
    *,
    require_windows: bool = False,
) -> dict[str, Any]:
    """Run native job, recovery, process, backup, and artifact acceptance."""
    system = platform.system()
    machine = platform.machine()
    if require_windows and system != "Windows":
        raise RuntimeError("--require-windows requires a native Windows host")
    if require_windows and machine.casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("--require-windows requires a native Windows x64 host")

    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    path_probe = root / "system path with spaces" / "地形"
    input_root = path_probe / "inputs"
    input_root.mkdir(parents=True)
    config = WebAppConfig(
        state_dir=path_probe / "state",
        workspace_root=path_probe / "workspaces",
        input_roots=(input_root,),
        max_concurrent_jobs=1,
        poll_interval_seconds=0.05,
    )

    completed = _complete_recover_backup_restore(config)
    process_lifecycle = _restart_and_cancel_worker(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": {
            "system": system,
            "release": platform.release(),
            "version": platform.version(),
            "machine": machine,
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "topoforge": topoforge.__version__,
            "native_windows_required": require_windows,
            "native_windows_verified": system == "Windows",
        },
        "path_contract": {
            "root": str(path_probe),
            "contains_spaces": " " in str(path_probe),
            "contains_non_ascii": any(ord(character) > 127 for character in str(path_probe)),
            "required_checks_passed": True,
        },
        **completed,
        "process_lifecycle": process_lifecycle,
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """Run native system acceptance and retain success or failure evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report_path = args.report.expanduser().resolve()
    try:
        if args.work_root is not None:
            report = verify_windows_system(
                args.work_root,
                require_windows=args.require_windows,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="topoforge-windows-system-") as temporary:
                report = verify_windows_system(
                    Path(temporary) / "verification",
                    require_windows=args.require_windows,
                )
    except Exception as exc:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "topoforge": topoforge.__version__,
                "native_windows_required": args.require_windows,
            },
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "required_checks_passed": False,
        }
        _write_report(report_path, failure)
        raise
    _write_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
