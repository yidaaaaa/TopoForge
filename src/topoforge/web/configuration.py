"""Strict loader for local launch and overlay configuration files."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from topoforge.exceptions import ConfigurationError
from topoforge.overlays import read_overlay_config
from topoforge.web.jobs import LocalJobManager
from topoforge.workflow import read_workflow_launch_config


class LocalConfigKind(StrEnum):
    """Supported local configuration roles exposed to the Web form."""

    OVERLAY = "overlay"
    LAUNCH = "launch"


class LocalConfigLoadRequest(BaseModel):
    """One explicit path and typed configuration role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LocalConfigKind
    path: Path


def load_local_config(
    manager: LocalJobManager,
    request: LocalConfigLoadRequest,
) -> dict[str, Any]:
    """Load one configured-root YAML file through existing strict readers."""
    path = request.path.expanduser().resolve()
    listing = manager.list_files(path.parent)
    if not any(
        entry.kind == "file" and entry.selectable and Path(entry.path) == path
        for entry in listing.entries
    ):
        raise ConfigurationError("configuration path is not an exposed selectable input file")
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ConfigurationError("local configuration must use a .yaml or .yml suffix")
    if request.kind is LocalConfigKind.OVERLAY:
        return read_overlay_config(path).model_dump(mode="json")
    return read_workflow_launch_config(path).model_dump(mode="json")
