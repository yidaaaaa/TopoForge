"""Platform-specific filesystem defaults kept outside core manufacturing logic."""

from __future__ import annotations

import os
import platform
import stat
from collections.abc import Mapping
from pathlib import Path

from topoforge.platform_paths import macos_application_data_root

_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def stat_result_is_link_like(result: object) -> bool:
    """Return whether an lstat result is a symlink or Windows reparse point."""
    mode = getattr(result, "st_mode", 0)
    attributes = getattr(result, "st_file_attributes", 0)
    return stat.S_ISLNK(mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def path_is_link_like(path: Path) -> bool:
    """Detect symlinks, junctions, and other reparse points without following them."""
    return stat_result_is_link_like(path.lstat())


def _system_name(system: str | None) -> str:
    return platform.system() if system is None else system


def windows_application_data_root(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the per-user non-roaming TopoForge application-data root."""
    values = os.environ if environ is None else environ
    configured = values.get("LOCALAPPDATA")
    if configured:
        return Path(configured).expanduser() / "TopoForge"
    user_home = Path.home() if home is None else home
    return user_home.expanduser() / "AppData" / "Local" / "TopoForge"


def default_web_state_dir(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the durable Web state default without creating directories."""
    system_name = _system_name(system)
    if system_name == "Windows":
        return windows_application_data_root(environ=environ, home=home) / "state"
    if system_name == "Darwin":
        return macos_application_data_root(home=home) / "state"
    return Path("~/.topoforge/web")


def default_web_workspace_root(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the workflow workspace default without creating directories."""
    system_name = _system_name(system)
    if system_name == "Windows":
        return windows_application_data_root(environ=environ, home=home) / "workspaces"
    if system_name == "Darwin":
        return macos_application_data_root(home=home) / "workspaces"
    return Path("topoforge-workspaces")


def default_web_input_roots(
    *,
    system: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> tuple[Path, ...]:
    """Return a useful local input-browser boundary for the active platform."""
    if _system_name(system) == "Windows":
        return ((Path.home() if home is None else home).expanduser(),)
    return (Path.cwd() if cwd is None else cwd,)
