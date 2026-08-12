#!/usr/bin/env python3
"""Emit a bounded GitHub annotation and step summary from pytest JUnit XML."""

from __future__ import annotations

import argparse
import os
import xml.etree.ElementTree as ET
from pathlib import Path

_MAX_JUNIT_BYTES = 8 * 1024 * 1024
_MAX_FAILURES = 12
_MAX_OUTPUT_CHARACTERS = 12_000


def _escape_workflow_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _failure_report(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"pytest JUnit diagnostic is unavailable: {exc}"
    if size > _MAX_JUNIT_BYTES:
        return (
            f"pytest JUnit diagnostic exceeds the {_MAX_JUNIT_BYTES}-byte reporting limit: {path}"
        )
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        return f"pytest JUnit diagnostic is unreadable: {exc}"

    failures: list[str] = []
    for case in root.iter("testcase"):
        outcome = case.find("failure")
        if outcome is None:
            outcome = case.find("error")
        if outcome is None:
            continue
        class_name = case.get("classname", "")
        test_name = case.get("name", "unknown-test")
        node = f"{class_name}::{test_name}" if class_name else test_name
        location = case.get("file")
        line = case.get("line")
        if location:
            node = f"{location}{f':{line}' if line else ''} {node}"
        detail = (outcome.text or outcome.get("message") or "no traceback recorded").strip()
        failures.append(f"FAILED {node}\n{detail}")
        if len(failures) >= _MAX_FAILURES:
            break

    if not failures:
        return "pytest failed, but its bounded JUnit report contains no failure or error case"
    report = "\n\n".join(failures)
    if len(report) > _MAX_OUTPUT_CHARACTERS:
        report = report[: _MAX_OUTPUT_CHARACTERS - 24] + "\n...[report truncated]"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    report = _failure_report(args.junit)
    escaped_title = _escape_workflow_command(args.title)
    escaped_report = _escape_workflow_command(report)
    print(f"::error title={escaped_title}::{escaped_report}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(f"### {args.title}\n\n```text\n{report}\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
