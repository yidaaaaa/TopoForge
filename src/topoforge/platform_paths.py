"""Filesystem path strategies delegated by the shared platform adapter."""

from __future__ import annotations

from pathlib import Path


def macos_application_data_root(*, home: Path | None = None) -> Path:
    """Return the per-user TopoForge Application Support root on macOS."""
    user_home = Path.home() if home is None else home
    return user_home.expanduser() / "Library" / "Application Support" / "TopoForge"
