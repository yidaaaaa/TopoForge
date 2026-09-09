"""Bounded, conforming triangulation of overlay footprints on the terrain grid."""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt
import trimesh
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry

from topoforge.exceptions import ConfigurationError

if TYPE_CHECKING:
    from topoforge.overlays.geometry import TerrainSurface


_COORDINATE_TOLERANCE_MM = 1e-9


def _polygons(geometry: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(geometry, Polygon) and geometry.area > 0:
        yield geometry
    elif isinstance(geometry, MultiPolygon | GeometryCollection):
        for child in geometry.geoms:
            yield from _polygons(child)


def _check_budget(face_count: int, max_triangles: int) -> None:
    if face_count > max_triangles:
        raise ConfigurationError(
            f"overlay request exceeds remaining max_triangles={max_triangles}; "
            "reduce the footprint or terrain sampling density, or increase max_triangles"
        )


def _terrain_patches(polygon: Polygon, surface: TerrainSurface) -> Iterator[Polygon]:
    """Visit only occupied row strips, then intersect the two fixed cell triangles."""
    dx_mm = surface.model_width_mm / (surface.columns - 1)
    dy_mm = surface.model_depth_mm / (surface.rows - 1)
    _, south, _, north = polygon.bounds
    first_row = max(0, math.floor(south / dy_mm))
    last_row = min(surface.rows - 2, math.floor(north / dy_mm))
    for row in range(first_row, last_row + 1):
        y0, y1 = row * dy_mm, (row + 1) * dy_mm
        strip = polygon.intersection(box(0, y0, surface.model_width_mm, y1))
        # Splitting by occupied pieces avoids scanning the full bounding rectangle of
        # a long diagonal road or widely separated sides of a polygon with a hole.
        for piece in _polygons(strip):
            west, _, east, _ = piece.bounds
            first_column = max(0, math.floor(west / dx_mm))
            last_column = min(surface.columns - 2, math.floor(east / dx_mm))
            for column in range(first_column, last_column + 1):
                x0, x1 = column * dx_mm, (column + 1) * dx_mm
                for corners in (
                    ((x0, y0), (x1, y0), (x1, y1)),
                    ((x0, y0), (x1, y1), (x0, y1)),
                ):
                    yield from _polygons(piece.intersection(Polygon(corners)))


def _patch_triangles(polygon: Polygon) -> Iterator[npt.NDArray[np.float64]]:
    """Keep collinear ring vertices that earcut may otherwise omit at shared seams."""
    # Remove numerically collinear ears before triangulation, then restore their
    # original boundary nodes below. Otherwise earcut can produce a near-zero
    # area ear at a contour corner lying almost exactly on a terrain diagonal.
    simplified = polygon.simplify(_COORDINATE_TOLERANCE_MM, preserve_topology=True)
    vertices, faces = trimesh.creation.triangulate_polygon(simplified, engine="earcut")
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int64)
    boundary_vertices = np.unique(
        np.concatenate(
            [np.asarray(ring.coords) for ring in (polygon.exterior, *polygon.interiors)]
        ),
        axis=0,
    )
    retained = {tuple(point) for point in vertices[np.unique(faces)]}
    unused = np.asarray(
        [point for point in boundary_vertices if tuple(point) not in retained], dtype=np.float64
    ).reshape((-1, 2))
    for face in faces:
        triangle = vertices[face]
        ab, ac = triangle[1] - triangle[0], triangle[2] - triangle[0]
        if ab[0] * ac[1] - ab[1] * ac[0] < 0:
            triangle = triangle[::-1]
        ring: list[npt.NDArray[np.float64]] = []
        for index in range(3):
            start, end = triangle[index], triangle[(index + 1) % 3]
            ring.append(start)
            if len(unused) == 0:
                continue
            direction = end - start
            length_squared = float(direction @ direction)
            if length_squared == 0:
                continue
            offsets = unused - start
            fractions = offsets @ direction / length_squared
            cross = offsets[:, 0] * direction[1] - offsets[:, 1] * direction[0]
            candidates = np.flatnonzero(
                (fractions * math.sqrt(length_squared) > _COORDINATE_TOLERANCE_MM)
                & ((1 - fractions) * math.sqrt(length_squared) > _COORDINATE_TOLERANCE_MM)
                & (np.abs(cross) <= _COORDINATE_TOLERANCE_MM * math.sqrt(length_squared))
            )
            ring.extend(unused[candidates[np.argsort(fractions[candidates])]])
        if len(ring) == 3:
            yield triangle
        else:
            # A central fan retains all seam subdivisions without zero-area ears.
            center = triangle.mean(axis=0)
            for index, start in enumerate(ring):
                yield np.asarray((center, start, ring[(index + 1) % len(ring)]))


def surface_mapping_error_mm(
    mesh: trimesh.Trimesh,
    surface: TerrainSurface,
    *,
    raised_height_mm: float,
    embed_depth_mm: float,
) -> float:
    """Measure top/bottom face interiors against terrain, including serialization error.

    Samples include every vertex, centroid and three barycentric interior points. Vertical
    boundary walls have no top/bottom surface contract and are excluded.
    """
    maximum_error = 0.0
    coordinate_tolerance_mm = (
        2 * np.finfo(np.float32).eps * max(surface.model_width_mm, surface.model_depth_mm, 1.0)
    )
    for first in range(0, len(mesh.faces), 4096):
        triangles = np.asarray(mesh.triangles[first : first + 4096])
        if not np.all(np.isfinite(triangles)):
            raise ConfigurationError("overlay has non-finite coordinates; rebuild this overlay")
        if (
            np.any(triangles[:, :, :2] < -coordinate_tolerance_mm)
            or np.any(triangles[:, :, 0] > surface.model_width_mm + coordinate_tolerance_mm)
            or np.any(triangles[:, :, 1] > surface.model_depth_mm + coordinate_tolerance_mm)
        ):
            raise ConfigurationError(
                "overlay extends outside the terrain; clip the source to the model bounds"
            )
        ab = triangles[:, 1, :2] - triangles[:, 0, :2]
        ac = triangles[:, 2, :2] - triangles[:, 0, :2]
        signed_area = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
        sloped = signed_area != 0
        triangles = triangles[sloped]
        if len(triangles) == 0:
            continue
        offsets = np.where(signed_area[sloped] > 0, raised_height_mm, -embed_depth_mm)
        for weights in (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
            (1 / 3, 1 / 3, 1 / 3),
            (0.6, 0.2, 0.2),
            (0.2, 0.6, 0.2),
            (0.2, 0.2, 0.6),
        ):
            points = np.einsum("ijk,j->ik", triangles, weights)
            # Float32 STL coordinates may round the model boundary outward.
            xy = points[:, :2].copy()
            xy[:, 0] = np.clip(xy[:, 0], 0, surface.model_width_mm)
            xy[:, 1] = np.clip(xy[:, 1], 0, surface.model_depth_mm)
            expected = surface.surface_z_mm(xy) + offsets
            maximum_error = max(maximum_error, float(np.max(np.abs(points[:, 2] - expected))))
    return maximum_error


def draped_polygon_mesh(
    polygon: Polygon,
    surface: TerrainSurface,
    *,
    raised_height_mm: float,
    embed_depth_mm: float,
    max_triangles: int,
) -> tuple[trimesh.Trimesh, float]:
    """Build one closed solid whose faces conform to the terrain's triangle planes."""
    boundary_vertices = (
        len(polygon.exterior.coords) - 1 + sum(len(ring.coords) - 1 for ring in polygon.interiors)
    )
    # Even before terrain intersections, both caps and all footprint walls are
    # required. Reject oversized source polygons before GEOS/earcut refinement.
    _check_budget(4 * boundary_vertices + 4 * len(polygon.interiors) - 4, max_triangles)
    points: list[tuple[float, float]] = []
    point_buckets: dict[tuple[int, int], list[int]] = {}
    faces: list[tuple[int, int, int]] = []

    def vertex_id(x: float, y: float) -> int:
        # Intersections on opposite sides of a seam can differ by roundoff.
        # Compare distance across neighboring buckets; rounding alone can put
        # almost identical points on opposite sides of a decimal bin boundary.
        key = (math.floor(x / _COORDINATE_TOLERANCE_MM), math.floor(y / _COORDINATE_TOLERANCE_MM))
        for bx in range(key[0] - 1, key[0] + 2):
            for by in range(key[1] - 1, key[1] + 2):
                for candidate in point_buckets.get((bx, by), ()):
                    px, py = points[candidate]
                    if math.hypot(x - px, y - py) <= _COORDINATE_TOLERANCE_MM:
                        return candidate
        index = len(points)
        points.append((x, y))
        point_buckets.setdefault(key, []).append(index)
        return index

    def canonical_ring(coordinates: npt.ArrayLike) -> list[tuple[float, float]]:
        ring_ids: list[int] = []
        for x, y in np.asarray(coordinates):
            index = vertex_id(float(x), float(y))
            if not ring_ids or ring_ids[-1] != index:
                ring_ids.append(index)
        if len(ring_ids) > 1 and ring_ids[-1] == ring_ids[0]:
            ring_ids.pop()
        return [points[index] for index in ring_ids]

    for original_patch in _terrain_patches(polygon, surface):
        # Collapse roundoff-sized seam edges BEFORE earcut and collinear-node
        # reinsertion. Doing it after triangulation can fold a fan onto itself.
        exterior = canonical_ring(original_patch.exterior.coords)
        if len(exterior) < 3:
            continue
        interiors = [canonical_ring(ring.coords) for ring in original_patch.interiors]
        patch = Polygon(exterior, holes=[ring for ring in interiors if len(ring) >= 3])
        if patch.area == 0:
            continue
        if not patch.is_valid:
            raise ConfigurationError(
                "overlay boundary collapses at terrain precision; simplify the source footprint"
            )
        ring_vertices = (
            len(patch.exterior.coords) - 1 + sum(len(ring.coords) - 1 for ring in patch.interiors)
        )
        minimum_faces = ring_vertices + 2 * len(patch.interiors) - 2
        _check_budget(2 * (len(faces) + minimum_faces), max_triangles)
        for triangle in _patch_triangles(patch):
            ids: list[int] = []
            for x, y in triangle:
                ids.append(vertex_id(float(x), float(y)))
            if len(set(ids)) < 3:
                continue
            _check_budget(2 * (len(faces) + 1), max_triangles)
            faces.append((ids[0], ids[1], ids[2]))
    if not faces:
        raise ConfigurationError("overlay footprint produced no faces; enlarge the footprint")
    used_vertices, remapped_faces = np.unique(
        np.asarray(faces, dtype=np.int64), return_inverse=True
    )
    planar_faces = remapped_faces.reshape((-1, 3))
    xy = np.asarray(points, dtype=np.float64)[used_vertices]
    # Count directed edges once. Interior seams must be paired; only the original
    # footprint boundary may receive walls (including holes).
    edges = planar_faces[:, ((0, 1), (1, 2), (2, 0))].reshape((-1, 2))
    _, indices, counts = np.unique(
        np.sort(edges, axis=1), axis=0, return_index=True, return_counts=True
    )
    if np.any(counts > 2):
        raise ConfigurationError("overlay triangulation overlaps; simplify the source footprint")
    boundary = edges[indices[counts == 1]]
    for edge in boundary:
        midpoint = xy[edge].mean(axis=0)
        if polygon.boundary.distance(Point(midpoint)) > 1e-8:
            raise ConfigurationError(
                "overlay triangulation has an unpaired terrain seam; simplify the source footprint"
            )
    _check_budget(2 * len(faces) + 2 * len(boundary), max_triangles)
    z = surface.surface_z_mm(xy)
    vertex_count = len(xy)
    vertices = np.concatenate(
        (np.column_stack((xy, z + raised_height_mm)), np.column_stack((xy, z - embed_depth_mm)))
    )
    walls = [
        face
        for a, b in boundary
        for face in ((b, a, a + vertex_count), (b, a + vertex_count, b + vertex_count))
    ]
    solid_faces = np.concatenate(
        (planar_faces, planar_faces[:, ::-1] + vertex_count, np.asarray(walls, dtype=np.int64))
    )
    mesh = trimesh.Trimesh(vertices=vertices, faces=solid_faces, process=False, validate=False)
    if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
        raise ConfigurationError(
            "draped overlay is not a closed positive solid; simplify the footprint"
        )
    if not bool(np.all(mesh.nondegenerate_faces())) or not bool(np.all(mesh.unique_faces())):
        raise ConfigurationError(
            "draped overlay has degenerate/duplicate faces; simplify the footprint"
        )
    expected_volume = polygon.area * (raised_height_mm + embed_depth_mm)
    if not math.isclose(float(mesh.volume), expected_volume, rel_tol=1e-8, abs_tol=1e-8):
        raise ConfigurationError(
            "draped overlay volume differs from its footprint; simplify the source"
        )
    error = surface_mapping_error_mm(
        mesh, surface, raised_height_mm=raised_height_mm, embed_depth_mm=embed_depth_mm
    )
    if error > 1e-6:
        raise ConfigurationError(
            "overlay does not conform to the terrain; simplify the source footprint"
        )
    return mesh, error
