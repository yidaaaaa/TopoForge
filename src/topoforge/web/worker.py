"""Isolated child-process entry point for one TopoForge workflow launch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from topoforge.web.models import JobCreateRequest, JobError, WorkerResult, utc_now
from topoforge.workflow import execute_workflow_launch


def _atomic_write(path: Path, result: WorkerResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


def run_worker(request_path: Path, result_path: Path) -> int:
    """Execute one persisted request and atomically publish its terminal result."""
    try:
        request = JobCreateRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        execution = execute_workflow_launch(request.launch)
        result = WorkerResult(
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
        _atomic_write(result_path, result)
        return 0
    except Exception as exc:
        result = WorkerResult(
            ok=False,
            exit_code=2,
            completed_at=utc_now(),
            error=_failure(exc),
        )
        _atomic_write(result_path, result)
        return 2


def main() -> int:
    """Parse worker paths and return a process exit status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    return run_worker(
        args.request.expanduser().resolve(),
        args.result.expanduser().resolve(),
    )


if __name__ == "__main__":
    sys.exit(main())
