"""YAML configuration loading with explicit CLI overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from topoforge.models import BuildConfig


def load_build_config(path: Path, overrides: dict[str, Any] | None = None) -> BuildConfig:
    """Load a YAML build config and apply explicitly supplied overrides."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        msg = f"Configuration root in {path} must be a mapping"
        raise ValueError(msg)
    merged: dict[str, Any] = dict(raw)
    if overrides:
        merged.update(overrides)
    return BuildConfig.model_validate(merged)


def dump_resolved_config(config: BuildConfig, path: Path) -> None:
    """Write the fully resolved build configuration as stable YAML."""
    serializable = config.model_dump(mode="json")
    path.write_text(
        yaml.safe_dump(serializable, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
