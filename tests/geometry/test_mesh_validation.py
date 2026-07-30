from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import trimesh

from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.validation import validate_mesh, write_validation_report


def test_validation_reports_measured_geometry_and_unchecked_intersections() -> None:
    x, y = np.meshgrid(np.linspace(-1.0, 1.0, 7), np.linspace(-1.0, 1.0, 5))
    hill = 22.0 * np.exp(-4.0 * (x**2 + y**2))
    width_mm = 110.0
    depth_mm = 70.0
    base_thickness_mm = 3.5
    expected_dimensions = (
        width_mm,
        depth_mm,
        base_thickness_mm + float(np.ptp(hill)),
    )
    top_z_mm = base_thickness_mm + hill - float(np.min(hill))
    mesh = build_rectangular_terrain_mesh(
        top_z_mm,
        width_mm=width_mm,
        depth_mm=depth_mm,
        base_thickness_mm=base_thickness_mm,
    )

    report = validate_mesh(mesh, expected_dimensions_mm=expected_dimensions)

    assert report.units == "mm"
    np.testing.assert_allclose(report.dimensions_mm, expected_dimensions, atol=1e-12)
    assert report.dimension_error_mm is not None
    np.testing.assert_allclose(report.dimension_error_mm, (0.0, 0.0, 0.0), atol=1e-12)
    assert report.dimensions_within_tolerance is True
    assert report.finite_vertices
    assert report.finite_face_normals
    assert report.watertight
    assert report.winding_consistent
    assert report.manifold
    assert report.positive_volume
    assert np.isclose(report.volume_mm3, mesh.volume)
    assert report.connected_components == 1
    assert report.degenerate_faces == 0
    assert report.duplicate_faces == 0
    assert report.flat_bottom
    assert report.bottom_planarity_error_mm == 0.0
    assert report.minimum_base_thickness_mm is not None
    assert np.isclose(report.minimum_base_thickness_mm, base_thickness_mm)
    assert report.triangle_count == len(mesh.faces)
    assert report.self_intersection_status == "not_fully_checked"


def test_validation_counts_duplicate_and_degenerate_faces() -> None:
    mesh = build_rectangular_terrain_mesh(
        2.0 + np.arange(9, dtype=np.float64).reshape(3, 3),
        width_mm=30.0,
        depth_mm=20.0,
        base_thickness_mm=2.0,
    )
    faces = np.vstack(
        (
            mesh.faces,
            mesh.faces[0],
            np.array((mesh.faces[1, 0], mesh.faces[1, 0], mesh.faces[1, 2])),
        )
    )
    malformed = trimesh.Trimesh(vertices=mesh.vertices.copy(), faces=faces, process=False)

    report = validate_mesh(malformed)

    assert report.duplicate_faces == 1
    assert report.degenerate_faces == 1
    assert not report.manifold


def test_validation_report_json_round_trip(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.full((2, 2), 2.0),
        width_mm=25.0,
        depth_mm=15.0,
        base_thickness_mm=2.0,
    )
    report = validate_mesh(mesh)
    destination = tmp_path / "reports" / "validation.json"

    assert write_validation_report(report, destination) == destination
    raw = json.loads(destination.read_text(encoding="utf-8"))
    assert raw["watertight"] is True
    assert raw["minimum_base_thickness_mm"] == 2.0
    assert raw["self_intersection_status"] == "not_fully_checked"
    assert destination.read_bytes().endswith(b"\n")
