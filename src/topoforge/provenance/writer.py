"""Stable JSON and HTML report writers."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> Path:
    """Atomically write stable, UTF-8, newline-terminated JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def write_validation_html(path: Path, report: dict[str, Any]) -> Path:
    """Write a dependency-free validation report for human review."""
    table_rows: list[str] = []
    for key, value in sorted(report.items()):
        escaped_key = html.escape(str(key))
        escaped_value = html.escape(json.dumps(value, ensure_ascii=False, default=str))
        table_rows.append(f"<tr><th>{escaped_key}</th><td><code>{escaped_value}</code></td></tr>")
    rows = "\n".join(table_rows)
    status = (
        "PASS"
        if all(
            report.get(key) is True
            for key in (
                "finite_vertices",
                "watertight",
                "winding_consistent",
                "manifold",
                "positive_volume",
                "flat_bottom",
                "dimensions_within_tolerance",
            )
        )
        else "FAIL"
    )
    styles = """
body { font: 15px system-ui; margin: 2rem; max-width: 1100px; color: #1d252b; }
h1 { margin-bottom: .2rem; }
.status { font-weight: 700; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; border: 1px solid #ccd4d9; padding: .55rem; }
th { width: 32%; background: #f2f5f6; }
code { white-space: pre-wrap; }
""".strip()
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TopoForge validation</title>
<style>{styles}</style>
</head>
<body>
<h1>TopoForge validation</h1>
<p class="status">Required checks: {status}</p>
<p>Self-intersection status is literal. An unavailable exhaustive test is not shown as passed.</p>
<table>{rows}</table>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)
    return path
