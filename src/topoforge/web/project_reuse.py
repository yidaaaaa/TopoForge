"""Read-only preparation of a new project from its complete saved launch."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import JobCreateRequest


class ProjectReuseIssue(BaseModel):
    """A retained local dependency the user may need to restore or replace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    path: Path
    code: Literal["missing-input", "outside-input-roots", "workspace-dependency"]
    message: str


class ProjectReuseResponse(BaseModel):
    """An editable copy suggestion that has not created or executed a project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["topoforge-project-reuse-v1"] = "topoforge-project-reuse-v1"
    source_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    original_workspace_dir: Path
    suggested_workspace_name: str
    request: JobCreateRequest
    issues: tuple[ProjectReuseIssue, ...] = ()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _dependency_issues(
    manager: LocalJobManager,
    request: JobCreateRequest,
    original_workspace: Path,
) -> tuple[ProjectReuseIssue, ...]:
    launch = request.launch
    dependencies: list[tuple[str, Path, bool]] = []
    if launch.global_source is None:
        dependencies.append(("launch.build.dem_path", launch.build.dem_path, True))
        if launch.build.source_acquisition_manifest is not None:
            dependencies.append(
                (
                    "launch.build.source_acquisition_manifest",
                    launch.build.source_acquisition_manifest,
                    True,
                )
            )
    if launch.overlay is not None:
        dependencies.extend(
            (f"launch.overlay.sources.{index}.path", source.path, True)
            for index, source in enumerate(launch.overlay.sources)
            if source.path is not None
        )
    if launch.slicing_enabled:
        dependencies.extend(
            (f"launch.slicer_settings.{index}", path, False)
            for index, path in enumerate(launch.slicer_settings)
        )
        dependencies.extend(
            (f"launch.slicer_filaments.{index}", path, False)
            for index, path in enumerate(launch.slicer_filaments)
        )
    issues: list[ProjectReuseIssue] = []
    for field, raw_path, check_input_root in dependencies:
        path = _absolute(raw_path)
        try:
            resolved = path.resolve(strict=True)
            available = resolved.is_file()
        except (OSError, RuntimeError):
            resolved = path
            available = False
        if not available:
            issues.append(
                ProjectReuseIssue(
                    field=field,
                    path=path,
                    code="missing-input",
                    message="Restore this input file or select a replacement before generating.",
                )
            )
        elif original_workspace in resolved.parents:
            issues.append(
                ProjectReuseIssue(
                    field=field,
                    path=path,
                    code="workspace-dependency",
                    message="Keep the original workspace: the copied project uses this input file.",
                )
            )
        elif check_input_root and not any(
            root == resolved or root in resolved.parents for root in manager.config.input_roots
        ):
            issues.append(
                ProjectReuseIssue(
                    field=field,
                    path=path,
                    code="outside-input-roots",
                    message=(
                        "This retained input is outside the file browser roots; use its original "
                        "path or configure an input root to select a replacement."
                    ),
                )
            )
    return tuple(issues)


def prepare_project_reuse(manager: LocalJobManager, job_id: str) -> ProjectReuseResponse:
    """Preserve every launch option while suggesting a distinct, protected workspace."""
    record, original = manager.read_request_for_reuse(job_id)
    original_workspace = _absolute(record.workspace_dir)
    prefix = re.sub(r"[^A-Za-z0-9._-]", "-", original_workspace.name).strip(".-")
    name = f"{prefix[:50] or 'terrain-model'}-copy-{uuid4().hex[:8]}"
    destination = manager.config.workspace_root / name
    while os.path.lexists(destination):
        name = f"{prefix[:50] or 'terrain-model'}-copy-{uuid4().hex[:8]}"
        destination = manager.config.workspace_root / name
    launch = original.launch
    build = launch.build.model_copy(update={"output_dir": destination})
    global_source = launch.global_source
    if global_source is not None:
        # This is the unused acquisition placeholder, not the acquired raster.
        # Keep the provider/AOI contract instead of silently switching to local data.
        build = build.model_copy(update={"dem_path": destination / "global-source-managed.tif"})
        cache = _absolute(global_source.cache_dir)
        if cache == original_workspace or original_workspace in cache.parents:
            global_source = global_source.model_copy(
                update={"cache_dir": destination / cache.relative_to(original_workspace)}
            )
    request = JobCreateRequest(
        launch=launch.model_copy(
            update={
                "workspace_dir": destination,
                "build": build,
                "global_source": global_source,
            }
        ),
        reuse_source_job_id=job_id,
    )
    return ProjectReuseResponse(
        source_job_id=job_id,
        original_workspace_dir=original_workspace,
        suggested_workspace_name=name,
        request=request,
        issues=_dependency_issues(manager, original, original_workspace),
    )
