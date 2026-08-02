"""Typed local Web job, progress, error, and artifact contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.workflow import WorkflowLaunchConfig, WorkflowRunSummary, WorkflowStage

_JOB_SCHEMA_VERSION = "topoforge-web-job-v1"
_EVENT_SCHEMA_VERSION = "topoforge-web-job-event-v1"


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for operational Web records."""
    return datetime.now(UTC)


class JobState(StrEnum):
    """Lifecycle states for an isolated local workflow process."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class JobError(BaseModel):
    """Actionable failure details returned by a worker or the local adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    corrective_action: str
    exception_type: str | None = None


class JobArtifact(BaseModel):
    """Workspace-contained artifact exposed through a checksum-aware route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    relative_path: str
    filename: str
    kind: str
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    download_url: str | None = None


class JobEvent(BaseModel):
    """Monotonic event used by polling and server-sent event clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _EVENT_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    job_id: str
    occurred_at: datetime
    state: JobState
    progress_fraction: float = Field(ge=0, le=1)
    current_stage: WorkflowStage | None = None
    ready_stages: tuple[WorkflowStage, ...] = ()
    message_key: str


class JobCreateRequest(BaseModel):
    """Complete validated launch submitted to the isolated worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    launch: WorkflowLaunchConfig


class JobRecord(BaseModel):
    """Durable authoritative state for one Web-submitted workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _JOB_SCHEMA_VERSION
    job_id: str
    created_at: datetime
    updated_at: datetime
    state: JobState
    workspace_dir: Path
    expected_stages: tuple[WorkflowStage, ...]
    progress_fraction: float = Field(ge=0, le=1)
    current_stage: WorkflowStage | None = None
    ready_stages: tuple[WorkflowStage, ...] = ()
    pid: int | None = Field(default=None, ge=1)
    exit_code: int | None = None
    cancellation_requested: bool = False
    error: JobError | None = None
    summary: WorkflowRunSummary | None = None
    artifacts: tuple[JobArtifact, ...] = ()


class WebAppConfig(BaseModel):
    """Filesystem and concurrency boundaries for the loopback application."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_dir: Path = Path("~/.topoforge/web")
    workspace_root: Path = Path("topoforge-workspaces")
    input_roots: tuple[Path, ...] = (Path.cwd(),)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=4)
    poll_interval_seconds: float = Field(default=0.25, ge=0.05, le=5)

    @model_validator(mode="after")
    def validate_roots(self) -> WebAppConfig:
        """Reject overlapping state/workspace roots and empty input policy."""
        resolved = self.resolved()
        if not resolved.input_roots:
            raise ValueError("input_roots must contain at least one readable directory")
        state = resolved.state_dir
        workspace = resolved.workspace_root
        if state == workspace or state in workspace.parents or workspace in state.parents:
            raise ValueError("state_dir and workspace_root must not overlap")
        return self

    def resolved(self) -> WebAppConfig:
        """Return absolute normalized path boundaries without filesystem mutation."""
        return self.model_copy(
            update={
                "state_dir": self.state_dir.expanduser().resolve(),
                "workspace_root": self.workspace_root.expanduser().resolve(),
                "input_roots": tuple(
                    dict.fromkeys(path.expanduser().resolve() for path in self.input_roots)
                ),
            }
        )


class FileEntry(BaseModel):
    """One safe local input browser entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    path: str
    kind: str
    size_bytes: int | None = Field(default=None, ge=0)
    selectable: bool


class FileListing(BaseModel):
    """Constrained directory listing for explicit local input roots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str | None
    parent: str | None
    roots: tuple[str, ...]
    entries: tuple[FileEntry, ...]


class WorkerResult(BaseModel):
    """Atomic terminal record written by an isolated worker process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-worker-result-v1"
    ok: bool
    exit_code: int
    completed_at: datetime
    summary: WorkflowRunSummary | None = None
    error: JobError | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> WorkerResult:
        """Require exactly one success summary or failure error."""
        if self.ok != (self.summary is not None and self.error is None):
            raise ValueError("worker result success/error fields are inconsistent")
        return self
