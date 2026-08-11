"""Isolated child-process entry point for one TopoForge workflow launch."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

from topoforge.web.models import (
    JobCreateRequest,
    JobError,
    WorkerReady,
    WorkerResult,
    utc_now,
)
from topoforge.web.processes import (
    enable_current_process_containment,
    process_group_id,
    process_identity,
)
from topoforge.web.security import (
    canonical_json_bytes,
    read_owned_regular_bytes,
    write_exclusive_owned_regular_bytes,
)
from topoforge.workflow import execute_workflow_launch

_WORKER_GATE_SCHEMA_VERSION = "topoforge-web-worker-launch-gate-v3"
_MAX_GATE_BYTES = 4096
_MAX_REQUEST_BYTES = 1024 * 1024


def _atomic_write(
    path: Path,
    result: WorkerResult,
    *,
    jobs_root: Path,
    jobs_root_identity: tuple[int, int],
) -> None:
    write_exclusive_owned_regular_bytes(
        path,
        canonical_json_bytes(result),
        root=jobs_root,
        root_identity=jobs_root_identity,
        context="worker result",
    )


def _failure(exc: Exception) -> JobError:
    message = str(exc) or exc.__class__.__name__
    return JobError(
        code="workflow-execution-failed",
        message=message,
        corrective_action=(
            "Review the retained worker log and workflow failure record, correct the input, "
            "then submit or resume the saved launch."
        ),
        exception_type=exc.__class__.__name__,
    )


def _canonical_gate_bytes(value: dict[str, object]) -> bytes:
    return canonical_json_bytes(value)


def _current_gate_bytes(
    *,
    ready: WorkerReady,
    worker_ready_sha256: str,
) -> bytes:
    return _canonical_gate_bytes(
        {
            "schema_version": _WORKER_GATE_SCHEMA_VERSION,
            "job_id": ready.job_id,
            "launch_nonce": ready.launch_nonce,
            "request_sha256": ready.request_sha256,
            "worker_ready_sha256": worker_ready_sha256,
            "pid": ready.pid,
            "process_identity": ready.process_identity,
            "process_group_id": ready.process_group_id,
        }
    )


def _wait_for_launch_gate(
    *,
    gate_path: Path,
    ready: WorkerReady,
    worker_ready_sha256: str,
    jobs_root: Path,
    jobs_root_identity: tuple[int, int],
    parent_pid: int,
    parent_identity: str,
    timeout_seconds: float,
) -> None:
    """Wait until the manager durably authorizes exactly this isolated worker."""
    if parent_pid < 1 or not parent_identity:
        raise RuntimeError("worker launch gate arguments are invalid")
    expected_bytes = _current_gate_bytes(
        ready=ready,
        worker_ready_sha256=worker_ready_sha256,
    )
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        try:
            payload = read_owned_regular_bytes(
                gate_path,
                root=jobs_root,
                root_identity=jobs_root_identity,
                context="worker launch gate",
                max_bytes=_MAX_GATE_BYTES,
            )
        except FileNotFoundError:
            payload = None
        except (OSError, ValueError) as exc:
            raise RuntimeError("worker launch gate is unsafe or unreadable") from exc
        if payload is not None:
            if payload != expected_bytes:
                raise RuntimeError("worker launch gate is non-canonical or has the wrong identity")
            return
        observed_parent = process_identity(parent_pid)
        if observed_parent != parent_identity:
            raise RuntimeError(
                "worker launch parent exited or changed identity before gate release"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError("worker launch gate timed out before workflow release")
        time.sleep(0.01)


def run_worker(
    request_path: Path,
    result_path: Path,
    *,
    gate_path: Path,
    ready_path: Path,
    jobs_root: Path,
    jobs_root_identity: tuple[int, int],
    launch_nonce: str,
    request_sha256: str,
    parent_pid: int,
    parent_identity: str,
    gate_timeout_seconds: float,
) -> int:
    """Execute one persisted request and atomically publish its terminal result."""
    job_id = request_path.parent.name
    worker_ready_sha256: str | None = None
    launch_gate_sha256: str | None = None
    try:
        job_dir = request_path.parent
        if (
            job_dir.parent != jobs_root
            or result_path.parent != job_dir
            or gate_path.parent != job_dir
            or ready_path.parent != job_dir
            or len(launch_nonce) != 32
            or any(character not in "0123456789abcdef" for character in launch_nonce)
            or len(request_sha256) != 64
            or any(character not in "0123456789abcdef" for character in request_sha256)
            or jobs_root_identity[0] < 0
            or jobs_root_identity[1] < 0
        ):
            raise RuntimeError("worker paths, identities, or launch arguments are invalid")
        enable_current_process_containment()
        request_payload = read_owned_regular_bytes(
            request_path,
            root=jobs_root,
            root_identity=jobs_root_identity,
            context="worker request",
            max_bytes=_MAX_REQUEST_BYTES,
        )
        observed_request_sha256 = hashlib.sha256(request_payload).hexdigest()
        if observed_request_sha256 != request_sha256:
            raise RuntimeError("worker request changed after the launch attempt was created")
        request = JobCreateRequest.model_validate_json(request_payload)
        if request_payload != canonical_json_bytes(request):
            raise RuntimeError("worker request is not canonical")
        worker_pid = os.getpid()
        worker_identity = process_identity(worker_pid)
        worker_group = process_group_id(worker_pid)
        if worker_identity is None or worker_group is None or worker_group != worker_pid:
            raise RuntimeError("worker could not verify its own process identity and containment")
        ready = WorkerReady(
            job_id=job_id,
            launch_nonce=launch_nonce,
            request_sha256=request_sha256,
            pid=worker_pid,
            process_identity=worker_identity,
            process_group_id=worker_group,
            jobs_root_device=jobs_root_identity[0],
            jobs_root_inode=jobs_root_identity[1],
        )
        ready_payload = canonical_json_bytes(ready)
        worker_ready_sha256 = hashlib.sha256(ready_payload).hexdigest()
        write_exclusive_owned_regular_bytes(
            ready_path,
            ready_payload,
            root=jobs_root,
            root_identity=jobs_root_identity,
            context="worker containment-ready record",
        )
        expected_gate = _current_gate_bytes(
            ready=ready,
            worker_ready_sha256=worker_ready_sha256,
        )
        launch_gate_sha256 = hashlib.sha256(expected_gate).hexdigest()
        _wait_for_launch_gate(
            gate_path=gate_path,
            ready=ready,
            worker_ready_sha256=worker_ready_sha256,
            jobs_root=jobs_root,
            jobs_root_identity=jobs_root_identity,
            parent_pid=parent_pid,
            parent_identity=parent_identity,
            timeout_seconds=gate_timeout_seconds,
        )
        execution = execute_workflow_launch(request.launch)
        result = WorkerResult(
            job_id=job_id,
            launch_nonce=launch_nonce,
            request_sha256=request_sha256,
            worker_ready_sha256=worker_ready_sha256,
            launch_gate_sha256=launch_gate_sha256,
            ok=True,
            exit_code=0,
            completed_at=utc_now(),
            summary=execution.summary,
            details={
                "launch_config_path": str(execution.launch_config_path),
                "summary_path": str(execution.summary_path),
                "report_path": str(execution.report_path),
            },
        )
        _atomic_write(
            result_path,
            result,
            jobs_root=jobs_root,
            jobs_root_identity=jobs_root_identity,
        )
        return 0
    except Exception as exc:
        if worker_ready_sha256 is None or launch_gate_sha256 is None:
            return 2
        result = WorkerResult(
            job_id=job_id,
            launch_nonce=launch_nonce,
            request_sha256=request_sha256,
            worker_ready_sha256=worker_ready_sha256,
            launch_gate_sha256=launch_gate_sha256,
            ok=False,
            exit_code=2,
            completed_at=utc_now(),
            error=_failure(exc),
        )
        _atomic_write(
            result_path,
            result,
            jobs_root=jobs_root,
            jobs_root_identity=jobs_root_identity,
        )
        return 2


def main() -> int:
    """Parse worker paths and return a process exit status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--jobs-root-device", type=int, required=True)
    parser.add_argument("--jobs-root-inode", type=int, required=True)
    parser.add_argument("--launch-nonce", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-identity", required=True)
    parser.add_argument("--gate-timeout-seconds", type=float, required=True)
    args = parser.parse_args()
    jobs_root = Path(os.path.abspath(args.jobs_root.expanduser()))
    return run_worker(
        Path(os.path.abspath(args.request.expanduser())),
        Path(os.path.abspath(args.result.expanduser())),
        gate_path=Path(os.path.abspath(args.gate.expanduser())),
        ready_path=Path(os.path.abspath(args.ready.expanduser())),
        jobs_root=jobs_root,
        jobs_root_identity=(args.jobs_root_device, args.jobs_root_inode),
        launch_nonce=args.launch_nonce,
        request_sha256=args.request_sha256,
        parent_pid=args.parent_pid,
        parent_identity=args.parent_identity,
        gate_timeout_seconds=args.gate_timeout_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
