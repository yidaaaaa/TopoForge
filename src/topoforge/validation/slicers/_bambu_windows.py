"""Windows-specific discovery for an official Bambu Studio installation."""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from pathlib import Path

_EXECUTABLE_NAMES = ("bambu-studio.exe", "BambuStudio.exe")
_PROFILE_ENVIRONMENT_KEY = "TOPOFORGE_BAMBU_PROFILES"


def windows_bambu_studio_candidates(
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    """Return ordered standard Windows x64 Bambu Studio executable paths."""
    values = os.environ if environ is None else environ
    installation_roots: list[Path] = []
    for key in ("ProgramW6432", "ProgramFiles", "PROGRAMFILES"):
        value = values.get(key)
        if value:
            installation_roots.append(Path(value).expanduser() / "Bambu Studio")
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        installation_roots.append(Path(local_app_data).expanduser() / "Programs" / "Bambu Studio")

    candidates: list[Path] = []
    seen: set[str] = set()
    for root in installation_roots:
        for name in _EXECUTABLE_NAMES:
            candidate = root / name
            identity = str(candidate).casefold()
            if identity not in seen:
                candidates.append(candidate)
                seen.add(identity)
    return tuple(candidates)


def discover_windows_bambu_studio(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
) -> Path | None:
    """Return the first usable Bambu Studio executable in standard Windows roots."""
    system_name = platform.system() if system is None else system
    if system_name != "Windows":
        return None
    for candidate in windows_bambu_studio_candidates(environ=environ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def discover_bambu_profiles_root(
    executable: Path | None,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve an override or the official profile tree beside the executable."""
    values = os.environ if environ is None else environ
    configured = explicit
    if configured is None:
        raw = values.get(_PROFILE_ENVIRONMENT_KEY)
        if raw:
            configured = Path(raw)
    candidates: list[Path] = []
    if configured is not None:
        candidates.append(configured.expanduser())
    elif executable is not None:
        candidates.append(
            executable.expanduser().resolve().parent / "resources" / "profiles" / "BBL"
        )
    for candidate in candidates:
        resolved = candidate.resolve()
        if all((resolved / kind).is_dir() for kind in ("machine", "process", "filament")):
            return resolved
    return None
