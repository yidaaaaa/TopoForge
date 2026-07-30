from pathlib import Path

import numpy as np
import pytest

from topoforge.exporters.three_mf import export_3mf, inspect_3mf
from topoforge.mesh import build_rectangular_terrain_mesh


def test_3mf_round_trip_preserves_units_dimensions_and_topology(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    output = export_3mf(mesh, tmp_path / "model.3mf", metadata={"dataset_type": "dtm"})
    inspected = inspect_3mf(output)
    assert inspected.unit == "millimeter"
    assert inspected.object_count == 1
    assert inspected.object_names == ("TopoForge terrain",)
    assert inspected.strict_warning_count == 0
    assert inspected.lib3mf_version == (2, 5, 0)
    assert inspected.build_item_count == 1
    assert inspected.vertex_count == len(mesh.vertices)
    assert inspected.triangle_count == len(mesh.faces)
    assert inspected.dimensions_mm == pytest.approx(tuple(mesh.extents), abs=1e-9)


def test_repeated_3mf_exports_are_byte_identical(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    first = export_3mf(mesh, tmp_path / "first.3mf", metadata={"dataset_type": "dtm"})
    second = export_3mf(mesh, tmp_path / "second.3mf", metadata={"dataset_type": "dtm"})
    assert first.read_bytes() == second.read_bytes()
