"""Typed local Web job, progress, error, and artifact contracts."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.workflow import (
    WorkflowCleanupPlan,
    WorkflowLaunchConfig,
    WorkflowRunSummary,
    WorkflowStage,
    WorkflowStorageEstimate,
)

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


class WorkflowBackupRecord(BaseModel):
    """Strictly reopened local workflow backup exposed by the Web adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-backup-v1"
    backup_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_id: str
    original_workspace: Path
    archive_size_bytes: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=1)
    download_url: str
    required_checks_passed: bool


class JobMaintenanceOverview(BaseModel):
    """Measured storage, cleanup, and backup state for one completed job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    storage: WorkflowStorageEstimate
    cleanup: WorkflowCleanupPlan
    backups: tuple[WorkflowBackupRecord, ...] = ()
    required_checks_passed: bool


class JobDeleteRequest(BaseModel):
    """Exact job-id confirmation and workspace policy for terminal job deletion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirm_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    delete_workspace: bool = False


class JobDeleteResult(BaseModel):
    """Measured result of removing one terminal job record and optional workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-job-delete-v1"
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    previous_state: JobState
    workspace: Path
    workspace_existed: bool
    workspace_removed: bool
    workspace_retained: bool
    deleted_job_record_bytes: int = Field(ge=0)
    deleted_workspace_bytes: int = Field(ge=0)
    reclaimed_bytes: int = Field(ge=0)
    backups_preserved: bool
    required_checks_passed: bool


class JobBatchDeleteMode(StrEnum):
    """Supported reviewable actions for one terminal-job batch."""

    RECORD_ONLY = "record-only"
    QUARANTINE_WORKSPACE = "quarantine-workspace"
    BACKUP_AND_QUARANTINE = "backup-and-quarantine"


class JobBatchDeletePlanRequest(BaseModel):
    """Selected terminal jobs and the requested review mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    mode: JobBatchDeleteMode

    @model_validator(mode="after")
    def validate_job_ids(self) -> JobBatchDeletePlanRequest:
        """Require unique canonical Web job identifiers."""
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("batch job ids must be unique")
        invalid = [
            job_id
            for job_id in self.job_ids
            if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id)
        ]
        if invalid:
            raise ValueError("batch job ids must be 32 lowercase hexadecimal characters")
        return self


class JobBatchDeleteApplyRequest(JobBatchDeletePlanRequest):
    """Exact plan confirmation required before applying a reviewed batch."""

    confirm_plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class JobBatchDeletePlanItem(BaseModel):
    """Measured deletion eligibility and blockers for one selected job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: JobState
    workspace: Path
    workspace_existed: bool
    job_record_bytes: int = Field(ge=0)
    workspace_bytes: int = Field(ge=0)
    workspace_reference_job_ids: tuple[str, ...] = ()
    unselected_reference_job_ids: tuple[str, ...] = ()
    verified_backup_ids: tuple[str, ...] = ()
    eligible: bool
    blockers: tuple[str, ...] = ()


class JobBatchDeletePlan(BaseModel):
    """Deterministic, measured review record for one terminal-job batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-job-batch-delete-plan-v1"
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: JobBatchDeleteMode
    job_ids: tuple[str, ...]
    items: tuple[JobBatchDeletePlanItem, ...]
    selected_job_count: int = Field(ge=1)
    eligible_job_count: int = Field(ge=0)
    unique_workspace_count: int = Field(ge=0)
    job_record_bytes: int = Field(ge=0)
    workspace_bytes: int = Field(ge=0)
    total_target_bytes: int = Field(ge=0)
    backup_job_ids: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    required_checks_passed: bool


class JobTrashWorkspace(BaseModel):
    """Original and same-filesystem quarantine identity for one workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    original_workspace: Path
    quarantined_workspace: Path | None = None
    workspace_existed: bool
    size_bytes: int = Field(ge=0)


class JobTrashRecord(BaseModel):
    """Strictly reopenable recovery record for one applied batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-job-trash-v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: JobBatchDeleteMode
    created_at: datetime
    purge_after: datetime
    job_ids: tuple[str, ...]
    job_record_bytes: int = Field(ge=0)
    workspaces: tuple[JobTrashWorkspace, ...]
    backup_ids: tuple[str, ...] = ()
    total_quarantined_bytes: int = Field(ge=0)
    backups_preserved: bool
    required_checks_passed: bool


class JobTrashTransactionMove(BaseModel):
    """One exact source, temporary, and published path in a trash transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Path
    temporary: Path
    destination: Path


class JobTrashTransaction(BaseModel):
    """Durable intent used to recover an interrupted trash publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-job-trash-transaction-v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state_temporary: Path
    state_destination: Path
    workspace_temporary: Path
    workspace_destination: Path
    job_moves: tuple[JobTrashTransactionMove, ...]
    workspace_moves: tuple[JobTrashTransactionMove, ...]
    trash_record: JobTrashRecord


class JobTrashActionRequest(BaseModel):
    """Exact batch-id confirmation for restore or permanent purge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirm_batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class JobTrashActionResult(BaseModel):
    """Measured result of restoring or permanently purging one batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "topoforge-web-job-trash-action-v1"
    batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    action: str
    job_ids: tuple[str, ...]
    workspace_count: int = Field(ge=0)
    affected_bytes: int = Field(ge=0)
    backups_preserved: bool
    required_checks_passed: bool


class WorkflowCleanupRequest(BaseModel):
    """Exact workflow-id confirmation required before cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirm_workflow_id: str


class WorkflowRestoreRequest(BaseModel):
    """Optional safe workspace name for a restored backup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class WebAppConfig(BaseModel):
    """Filesystem and concurrency boundaries for the loopback application."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_dir: Path = Path("~/.topoforge/web")
    workspace_root: Path = Path("topoforge-workspaces")
    input_roots: tuple[Path, ...] = (Path.cwd(),)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=4)
    bambu_studio_executable: Path | None = None
    bambu_machine_profile: Path | None = None
    bambu_process_profile: Path | None = None
    bambu_filament_profile: Path | None = None
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
        profiles = (
            resolved.bambu_machine_profile,
            resolved.bambu_process_profile,
            resolved.bambu_filament_profile,
        )
        if any(path is not None for path in profiles) and not all(
            path is not None for path in profiles
        ):
            raise ValueError(
                "bambu_machine_profile, bambu_process_profile, and "
                "bambu_filament_profile must be configured together"
            )
        for label, path in (
            ("bambu_studio_executable", resolved.bambu_studio_executable),
            ("bambu_machine_profile", resolved.bambu_machine_profile),
            ("bambu_process_profile", resolved.bambu_process_profile),
            ("bambu_filament_profile", resolved.bambu_filament_profile),
        ):
            if path is not None and (
                not path.is_file()
                or (label.endswith("executable") and not os.access(path, os.X_OK))
            ):
                raise ValueError(f"{label} does not identify a usable file: {path}")
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
                "bambu_studio_executable": (
                    None
                    if self.bambu_studio_executable is None
                    else self.bambu_studio_executable.expanduser().resolve()
                ),
                "bambu_machine_profile": (
                    None
                    if self.bambu_machine_profile is None
                    else self.bambu_machine_profile.expanduser().resolve()
                ),
                "bambu_process_profile": (
                    None
                    if self.bambu_process_profile is None
                    else self.bambu_process_profile.expanduser().resolve()
                ),
                "bambu_filament_profile": (
                    None
                    if self.bambu_filament_profile is None
                    else self.bambu_filament_profile.expanduser().resolve()
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
