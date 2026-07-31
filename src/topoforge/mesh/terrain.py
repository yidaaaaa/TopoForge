"""Deterministic terrain mesh construction.

The Phase 1 constructor deliberately uses an explicit regular-grid topology
instead of relying on mesh repair.  Every top sample has a matching bottom
vertex, which makes the bottom triangulation and the four boundary walls share
exactly the same edges.
"""

from __future__ import annotations

from typing import cast

import numpy as np
import numpy.typing as npt
import trimesh

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.int64]


def build_rectangular_terrain_mesh(
    elevation_mm: npt.ArrayLike,
    *,
    width_mm: float,
    depth_mm: float,
    base_thickness_mm: float,
) -> trimesh.Trimesh:
    """Build a closed rectangular terrain solid in millimetres.

    ``elevation_mm`` is a uniformly sampled two-dimensional array of absolute
    manufacturing Z coordinates.  The values are preserved exactly; the flat
    bottom is constructed at ``z=0``.  The caller therefore controls where a
    sea-level or custom reference plane falls in the printable solid.

    Array row zero is the south edge and maps to ``y=0``; column zero is the
    west edge and maps to ``x=0``.  Thus +X is East, +Y is North, and +Z is Up.
    Each grid cell uses the same lower-left to upper-right diagonal.  The stable vertex
    and face order makes repeated construction byte-for-byte deterministic at
    the NumPy topology level.

    Args:
        elevation_mm: Finite, positive 2-D top-surface Z coordinates in
            millimetres.  At least two rows and two columns are required.
        width_mm: Finished model extent along x.
        depth_mm: Finished model extent along y.
        base_thickness_mm: Configured base reference, retained in mesh metadata.

    Returns:
        A watertight, winding-consistent :class:`trimesh.Trimesh` with positive
        volume and explicit ``mm`` units.

    Raises:
        ValueError: If inputs are non-finite, incorrectly shaped, or not
            strictly positive where required.
        RuntimeError: If an internal topology invariant is violated.
    """

    heights = _validated_heightfield(elevation_mm)
    width = _positive_finite("width_mm", width_mm)
    depth = _positive_finite("depth_mm", depth_mm)
    base_thickness = _positive_finite("base_thickness_mm", base_thickness_mm)

    rows, columns = heights.shape
    minimum_height = float(np.min(heights))
    if minimum_height <= 0.0:
        msg = (
            "elevation_mm top-surface coordinates must stay above the z=0 bottom, "
            f"got minimum {minimum_height!r}"
        )
        raise ValueError(msg)

    x_coordinates = np.linspace(0.0, width, columns, dtype=np.float64)
    y_coordinates = np.linspace(0.0, depth, rows, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(x_coordinates, y_coordinates)

    top_vertices = np.column_stack(
        (
            grid_x.reshape(-1),
            grid_y.reshape(-1),
            heights.reshape(-1),
        )
    )
    bottom_vertices = np.column_stack(
        (
            grid_x.reshape(-1),
            grid_y.reshape(-1),
            np.zeros(rows * columns, dtype=np.float64),
        )
    )
    vertices: FloatArray = np.vstack((top_vertices, bottom_vertices))
    faces = _rectangular_faces(rows=rows, columns=columns)

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False, validate=False)
    mesh.units = "mm"
    metadata = cast(  # pyright: ignore[reportUnknownMemberType]
        dict[str, object],
        mesh.metadata,  # pyright: ignore[reportUnknownMemberType]
    )
    metadata["topoforge"] = {
        "geometry": "rectangular_heightfield",
        "units": "mm",
        "heightfield_shape": [rows, columns],
        "width_mm": width,
        "depth_mm": depth,
        "base_thickness_mm": base_thickness,
        "source_elevation_min_mm": float(np.min(heights)),
        "source_elevation_max_mm": float(np.max(heights)),
        "east_axis": "+X",
        "north_axis": "+Y",
        "up_axis": "+Z",
        "south_edge_row": 0,
    }

    _assert_closed_positive_mesh(mesh)
    return mesh


def _validated_heightfield(elevation_mm: npt.ArrayLike) -> FloatArray:
    heights = np.asarray(elevation_mm, dtype=np.float64)
    if heights.ndim != 2:
        msg = f"elevation_mm must be a 2-D array, got {heights.ndim} dimensions"
        raise ValueError(msg)
    if heights.shape[0] < 2 or heights.shape[1] < 2:
        msg = f"elevation_mm must be at least 2 x 2, got {heights.shape}"
        raise ValueError(msg)
    if not bool(np.all(np.isfinite(heights))):
        msg = "elevation_mm must contain only finite values"
        raise ValueError(msg)
    return heights


def _positive_finite(name: str, value: float) -> float:
    converted = float(value)
    if not np.isfinite(converted) or converted <= 0.0:
        msg = f"{name} must be finite and greater than zero, got {value!r}"
        raise ValueError(msg)
    return converted


def _rectangular_faces(*, rows: int, columns: int) -> IntArray:
    """Return consistently wound faces for top, bottom, and boundary walls."""

    vertex_count_per_layer = rows * columns
    faces: list[tuple[int, int, int]] = []

    for row in range(rows - 1):
        for column in range(columns - 1):
            top_lower_left = row * columns + column
            top_lower_right = top_lower_left + 1
            top_upper_left = (row + 1) * columns + column
            top_upper_right = top_upper_left + 1

            # Top faces point toward +z.
            faces.extend(
                (
                    (top_lower_left, top_lower_right, top_upper_right),
                    (top_lower_left, top_upper_right, top_upper_left),
                )
            )

            bottom_lower_left = top_lower_left + vertex_count_per_layer
            bottom_lower_right = top_lower_right + vertex_count_per_layer
            bottom_upper_left = top_upper_left + vertex_count_per_layer
            bottom_upper_right = top_upper_right + vertex_count_per_layer

            # The same diagonal in reverse order makes bottom faces point -z.
            faces.extend(
                (
                    (bottom_lower_left, bottom_upper_right, bottom_lower_right),
                    (bottom_lower_left, bottom_upper_left, bottom_upper_right),
                )
            )

    # Counter-clockwise perimeter as viewed from +z.  With this traversal the
    # wall triangles below point away from the solid on every side.
    perimeter: list[int] = list(range(columns))
    perimeter.extend(row * columns + (columns - 1) for row in range(1, rows))
    perimeter.extend((rows - 1) * columns + column for column in range(columns - 2, -1, -1))
    perimeter.extend(row * columns for row in range(rows - 2, 0, -1))

    for index, top_start in enumerate(perimeter):
        top_end = perimeter[(index + 1) % len(perimeter)]
        bottom_start = top_start + vertex_count_per_layer
        bottom_end = top_end + vertex_count_per_layer
        faces.extend(
            (
                (top_start, bottom_start, bottom_end),
                (top_start, bottom_end, top_end),
            )
        )

    return np.asarray(faces, dtype=np.int64)


def _assert_closed_positive_mesh(mesh: trimesh.Trimesh) -> None:
    if not bool(mesh.is_watertight):
        msg = "rectangular terrain topology is unexpectedly not watertight"
        raise RuntimeError(msg)
    if not bool(mesh.is_winding_consistent):
        msg = "rectangular terrain topology has inconsistent face winding"
        raise RuntimeError(msg)
    volume = float(mesh.volume)
    if not np.isfinite(volume) or volume <= 0.0:
        msg = f"rectangular terrain topology has non-positive volume: {volume!r}"
        raise RuntimeError(msg)
