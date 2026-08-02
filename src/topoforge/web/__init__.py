"""Loopback-only Web adapter over the shared TopoForge workflow core."""

from topoforge.web.api import create_app
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import (
    JobArtifact,
    JobCreateRequest,
    JobError,
    JobEvent,
    JobRecord,
    JobState,
    WebAppConfig,
)
from topoforge.web.server import is_loopback_host, run_web_server, verify_web_installation

__all__ = [
    "JobArtifact",
    "JobCreateRequest",
    "JobError",
    "JobEvent",
    "JobRecord",
    "JobState",
    "LocalJobManager",
    "WebAppConfig",
    "create_app",
    "is_loopback_host",
    "run_web_server",
    "verify_web_installation",
]
