"""Bounded public diagnostics for hosted CI failures."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.report_json_failure import (
    _MAX_ANNOTATION_CHARACTERS,
    _bounded_annotation,
    _failure_report,
)


def test_structured_failure_report_preserves_type_and_message(tmp_path: Path) -> None:
    report = tmp_path / "failure.json"
    report.write_text(
        json.dumps({"error": {"type": "RuntimeError", "message": "native build failed"}}),
        encoding="utf-8",
    )

    assert _failure_report(report) == "RuntimeError: native build failed"


def test_structured_failure_annotation_is_escaped_and_bounded() -> None:
    annotation = _bounded_annotation("prefix\n" + "%" * 5000 + "\ntail")

    assert len(annotation) <= _MAX_ANNOTATION_CHARACTERS
    assert "%0A" in annotation
    assert "diagnostic truncated" in annotation
    assert annotation.endswith("tail")
