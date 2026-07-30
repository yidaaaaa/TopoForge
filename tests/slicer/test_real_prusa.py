"""Conditional evidence test against an installed PrusaSlicer executable."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from topoforge.validation.slicers import PrusaSlicerAdapter, SliceStatus

PRUSA_SLICER = shutil.which("prusa-slicer")


@pytest.mark.integration
@pytest.mark.slicer
@pytest.mark.skipif(PRUSA_SLICER is None, reason="prusa-slicer is not installed")
def test_real_prusa_slices_deterministic_cube(tmp_path: Path) -> None:
    model = tmp_path / "cube.stl"
    _write_cube_stl(model, size_mm=10)
    output = tmp_path / "cube.gcode"

    result = PrusaSlicerAdapter(PRUSA_SLICER).slice(
        model,
        output,
        extra_args=("--layer-height", "0.20", "--fill-density", "15%"),
        timeout_seconds=120,
    )

    assert result.status is SliceStatus.SUCCEEDED, result.stderr
    assert result.exit_code == 0
    assert result.gcode_generated is True
    assert output.stat().st_size > 1000
    assert result.metrics.layer_count is not None
    assert result.metrics.layer_count > 1
    assert result.metrics.filament_used_mm is not None
    assert result.metrics.estimated_time_seconds is not None


def _write_cube_stl(path: Path, *, size_mm: int) -> None:
    vertices = (
        (0, 0, 0),
        (size_mm, 0, 0),
        (size_mm, size_mm, 0),
        (0, size_mm, 0),
        (0, 0, size_mm),
        (size_mm, 0, size_mm),
        (size_mm, size_mm, size_mm),
        (0, size_mm, size_mm),
    )
    faces = (
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    )
    lines = ["solid cube"]
    for face in faces:
        lines.extend((" facet normal 0 0 0", "  outer loop"))
        lines.extend(
            f"   vertex {vertices[index][0]} {vertices[index][1]} {vertices[index][2]}"
            for index in face
        )
        lines.extend(("  endloop", " endfacet"))
    lines.append("endsolid cube")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
