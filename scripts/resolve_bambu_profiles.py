#!/usr/bin/env python3
"""Resolve Bambu Studio JSON preset inheritance/includes for CLI use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_index(profiles_root: Path) -> dict[str, tuple[str, Path, dict[str, Any]]]:
    index: dict[str, tuple[str, Path, dict[str, Any]]] = {}
    for kind in ("machine", "process", "filament"):
        directory = profiles_root / kind
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            name = data.get("name")
            if not isinstance(name, str) or not name:
                continue
            prior = index.get(name)
            if prior is not None and prior[1] != path:
                raise ValueError(f"duplicate preset name {name!r}: {prior[1]} and {path}")
            index[name] = (kind, path, data)
    return index


def resolve(
    name: str,
    index: dict[str, tuple[str, Path, dict[str, Any]]],
    stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    if name in stack:
        raise ValueError("preset dependency cycle: " + " -> ".join((*stack, name)))
    if name not in index:
        raise KeyError(f"preset dependency not found: {name!r}")
    _kind, _path, node = index[name]
    merged: dict[str, Any] = {}
    parent = node.get("inherits")
    if isinstance(parent, str) and parent:
        merged.update(resolve(parent, index, (*stack, name)))
    includes = node.get("include", [])
    if isinstance(includes, str):
        includes = [includes]
    if not isinstance(includes, list):
        raise TypeError(f"invalid include list in {name!r}: {includes!r}")
    for include in includes:
        if not isinstance(include, str):
            raise TypeError(f"invalid include name in {name!r}: {include!r}")
        merged.update(resolve(include, index, (*stack, name)))
    merged.update(node)
    return merged


def emit(
    name: str,
    expected_kind: str,
    out_path: Path,
    index: dict[str, tuple[str, Path, dict[str, Any]]],
) -> None:
    actual_kind, _source, _node = index[name]
    if actual_kind != expected_kind:
        raise ValueError(f"{name!r} is {actual_kind}, expected {expected_kind}")
    data = resolve(name, index)
    data.pop("inherits", None)
    data.pop("include", None)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
