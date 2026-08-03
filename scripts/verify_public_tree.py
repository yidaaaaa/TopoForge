#!/usr/bin/env python3
"""Reject local operator, agent, and runtime state from the public Git tree."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "topoforge-public-tree-v1"
FORBIDDEN_EXACT_PATHS = frozenset({"AGENTS.md"})
FORBIDDEN_ROOTS = frozenset({".agent", ".agents", ".codex", "cache", "downloads", "outputs"})
FORBIDDEN_PATH_GLOBS = ("artifacts/verification/topoforge-main-*.json",)
FORBIDDEN_CONTENT_MARKERS = (b"outputs/" + b"local-web",)


def tracked_paths(repository_root: Path) -> list[str]:
    """Return normalized paths tracked by the repository index."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))
    return sorted(
        item.decode("utf-8", errors="strict") for item in completed.stdout.split(b"\0") if item
    )


def audit_public_tree(repository_root: Path, paths: Iterable[str]) -> dict[str, Any]:
    """Audit tracked paths and contents without reading ignored local state."""
    root = repository_root.resolve()
    normalized_paths = sorted(set(paths))
    violations: list[dict[str, str]] = []
    for raw_path in normalized_paths:
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            violations.append({"path": raw_path, "reason": "unsafe tracked path"})
            continue
        if raw_path in FORBIDDEN_EXACT_PATHS:
            violations.append({"path": raw_path, "reason": "agent instruction file is local-only"})
            continue
        if path.parts and path.parts[0] in FORBIDDEN_ROOTS:
            violations.append({"path": raw_path, "reason": "local-only root is tracked"})
            continue
        if any(fnmatch.fnmatchcase(raw_path, pattern) for pattern in FORBIDDEN_PATH_GLOBS):
            violations.append({"path": raw_path, "reason": "machine-local verification is tracked"})
            continue

        file_path = root.joinpath(*path.parts)
        if not file_path.is_file():
            continue
        payload = file_path.read_bytes()
        for marker in FORBIDDEN_CONTENT_MARKERS:
            if marker in payload:
                violations.append(
                    {
                        "path": raw_path,
                        "reason": f"contains local runtime marker {marker.decode('ascii')}",
                    }
                )

    return {
        "schema_version": SCHEMA_VERSION,
        "tracked_file_count": len(normalized_paths),
        "violations": violations,
        "required_checks_passed": not violations,
    }


def main() -> int:
    """Run the public-tree audit and optionally retain the JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    repository_root = args.repository_root.resolve()
    report = audit_public_tree(repository_root, tracked_paths(repository_root))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["required_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
