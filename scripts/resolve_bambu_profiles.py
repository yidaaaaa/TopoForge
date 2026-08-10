#!/usr/bin/env python3
"""Resolve Bambu Studio JSON preset inheritance/includes for CLI use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from topoforge.validation.slicers._bambu_profiles import (
    BambuPreset,
    ProfileKind,
    flattened_profile_bytes,
    load_profile_index,
    resolve_profile,
)


def load_index(profiles_root: Path) -> dict[str, BambuPreset]:
    """Retain the script's original callable entry point for local tooling."""
    return load_profile_index(profiles_root)


def resolve(
    name: str,
    index: dict[str, BambuPreset],
    stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Retain the script's original resolver entry point for tests and users."""
    del stack
    return resolve_profile(name, index).payload


def emit(
    name: str,
    expected_kind: ProfileKind,
    out_path: Path,
    index: dict[str, BambuPreset],
) -> None:
    profile = resolve_profile(name, index, expected_kind=expected_kind)
    payload = flattened_profile_bytes(profile)
    temporary = out_path.with_name(f".{out_path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(out_path)
    data = json.loads(payload)
    print(f"{expected_kind}: {name} -> {out_path} ({len(data)} keys)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--process", required=True)
    parser.add_argument("--filament", required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    index = load_index(args.profiles_root)
    emit(args.machine, "machine", args.output_dir / "machine.json", index)
    emit(args.process, "process", args.output_dir / "process.json", index)
    emit(args.filament, "filament", args.output_dir / "filament.json", index)


if __name__ == "__main__":
    main()
