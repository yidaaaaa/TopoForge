from __future__ import annotations

import numpy as np
import pytest

from topoforge.mesh import build_rectangular_terrain_mesh


def _terrain_cases() -> list[np.ndarray]:
    flat = np.full((4, 6), 850.0, dtype=np.float64)

    slope_x, slope_y = np.meshgrid(
        np.linspace(0.0, 9.0, 7),
        np.linspace(0.0, 4.0, 5),
    )
    slope = 1_200.0 + slope_x + slope_y

    hill_x, hill_y = np.meshgrid(
        np.linspace(-1.0, 1.0, 11),
        np.linspace(-1.0, 1.0, 9),
    )
    hill = 500.0 + 18.0 * np.exp(-3.0 * (hill_x**2 + hill_y**2))

    return [flat, slope, hill]


@pytest.mark.parametrize(
    "heightfield",
    _terrain_cases(),
    ids=("flat", "slope", "gaussian-hill"),
)
def test_rectangular_terrain_mesh_dimensions_and_solid_invariants(
    heightfield: np.ndarray,
) -> None:
    width_mm = 120.0
    depth_mm = 75.0
    base_thickness_mm = 3.2

    top_z_mm = base_thickness_mm + heightfield - float(np.min(heightfield))
    mesh = build_rectangular_terrain_mesh(
        top_z_mm,
        width_mm=width_mm,
        depth_mm=depth_mm,
        base_thickness_mm=base_thickness_mm,
    )

    expected_height_mm = float(np.max(top_z_mm))
    np.testing.assert_allclose(mesh.bounds[0], (0.0, 0.0, 0.0), atol=1e-12)
    np.testing.assert_allclose(
        mesh.extents,
        (width_mm, depth_mm, expected_height_mm),
        atol=1e-12,
    )
    assert mesh.units == "mm"
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.is_volume
    assert mesh.volume > 0.0
    assert mesh.body_count == 1
    assert mesh.euler_number == 2
    assert len(mesh.faces) == 4 * (heightfield.size - 1)


def test_absolute_top_surface_offset_is_preserved() -> None:
    top_z_mm = np.array([[103.0, 113.0], [123.0, 133.0]])
    mesh = build_rectangular_terrain_mesh(
        top_z_mm,
        width_mm=40.0,
        depth_mm=30.0,
        base_thickness_mm=3.0,
    )

    assert float(np.min(mesh.vertices[:, 2])) == 0.0
    assert float(np.max(mesh.vertices[:, 2])) == 133.0
    top_vertex_z = mesh.vertices[: top_z_mm.size, 2].reshape(top_z_mm.shape)
    np.testing.assert_allclose(top_vertex_z, top_z_mm, atol=0.0)


def test_flat_heightfield_has_requested_base_volume() -> None:
    width_mm = 40.0
    depth_mm = 30.0
    base_thickness_mm = 2.5
    mesh = build_rectangular_terrain_mesh(
        np.full((3, 4), base_thickness_mm),
        width_mm=width_mm,
        depth_mm=depth_mm,
        base_thickness_mm=base_thickness_mm,
    )

    assert np.isclose(mesh.volume, width_mm * depth_mm * base_thickness_mm)
    downward = mesh.face_normals[:, 2] < -0.999999
    bottom_vertex_indices = np.unique(mesh.faces[downward])
    np.testing.assert_allclose(mesh.vertices[bottom_vertex_indices, 2], 0.0, atol=1e-12)


def test_mesh_topology_is_deterministic() -> None:
    heightfield = 3.0 + np.arange(30, dtype=np.float64).reshape(5, 6)
    arguments = {
        "width_mm": 100.0,
        "depth_mm": 60.0,
        "base_thickness_mm": 3.0,
    }

    first = build_rectangular_terrain_mesh(heightfield, **arguments)
    second = build_rectangular_terrain_mesh(heightfield, **arguments)

    np.testing.assert_array_equal(first.vertices, second.vertices)
    np.testing.assert_array_equal(first.faces, second.faces)


@pytest.mark.parametrize(
    ("heightfield", "arguments", "message"),
    [
        (np.zeros((2, 2, 2)), {}, "2-D"),
        (np.zeros((1, 4)), {}, "at least 2 x 2"),
        (np.array([[0.0, np.nan], [1.0, 2.0]]), {}, "finite"),
        (np.zeros((2, 2)), {"width_mm": 0.0}, "width_mm"),
        (np.zeros((2, 2)), {"depth_mm": -1.0}, "depth_mm"),
        (np.zeros((2, 2)), {"base_thickness_mm": np.inf}, "base_thickness_mm"),
        (np.zeros((2, 2)), {}, "above the z=0 bottom"),
    ],
)
def test_mesh_rejects_invalid_inputs(
    heightfield: np.ndarray,
    arguments: dict[str, float],
    message: str,
) -> None:
    dimensions = {
        "width_mm": 20.0,
        "depth_mm": 10.0,
        "base_thickness_mm": 2.0,
    }
    dimensions.update(arguments)

    with pytest.raises(ValueError, match=message):
        build_rectangular_terrain_mesh(heightfield, **dimensions)
