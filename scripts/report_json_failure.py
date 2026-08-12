#!/usr/bin/env python3
"""Emit a bounded GitHub annotation from a structured CI failure report."""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from typing import Any

_MAX_REPORT_BYTES = 16 * 1024 * 1024
_MAX_ANNOTATION_CHARACTERS = 3500
_MAX_SUMMARY_CHARACTERS = 12_000


def _escape_workflow_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _bounded_annotation(value: str) -> str:
    escaped = _escape_workflow_command(value)
    if len(escaped) <= _MAX_ANNOTATION_CHARACTERS:
        return escaped
    marker = "%0A...[diagnostic truncated]...%0A"
    leading = 256
    trailing = _MAX_ANNOTATION_CHARACTERS - leading - len(marker)
    return f"{escaped[:leading]}{marker}{escaped[-trailing:]}"


def _failure_report(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        return f"structured CI failure report is unavailable: {exc}"
    if path.is_symlink() or not path.is_file():
        return f"structured CI failure report is not a regular file: {path}"
    if metadata.st_size > _MAX_REPORT_BYTES:
        return f"structured CI failure report exceeds {_MAX_REPORT_BYTES} bytes: {path}"
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"structured CI failure report is unreadable: {exc}"
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return "structured CI failure report has no error object"
    error = payload["error"]
    error_type = error.get("type")
    message = error.get("message")
    if not isinstance(error_type, str) or not isinstance(message, str):
        return "structured CI failure report has invalid error details"
    return f"{error_type}: {message}"[:_MAX_SUMMARY_CHARACTERS]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    report = _failure_report(args.report)
    print(f"::error title={_escape_workflow_command(args.title)}::{_bounded_annotation(report)}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(f"### {html.escape(args.title)}\n\n<pre>{html.escape(report)}</pre>\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
