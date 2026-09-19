from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
import trimesh
from affine import Affine
from shapely.geometry import GeometryCollection, LineString, Polygon, box
from shapely.ops import unary_union

from topoforge.exceptions import ConfigurationError
from topoforge.models import ScalingResult
from topoforge.overlays import OverlaySourceConfig
from topoforge.overlays import draping as draping_module
from topoforge.overlays.draping import draped_polygon_mesh, surface_mapping_error_mm
from topoforge.overlays.geometry import ModelOverlayFeature, TerrainSurface, build_layer_mesh


def _surface(heights: npt.NDArray[np.float64]) -> TerrainSurface:
    scaling = ScalingResult(
        horizontal_scale_mm_per_m=1,
        model_width_mm=60,
        model_depth_mm=40,
        base_thickness_mm=3,
        baseline_elevation_m=0,
        robust_low_elevation_m=0,
        robust_high_elevation_m=30,
        policy_vertical_exaggeration=1,
        vertical_exaggeration=1,
        height_limit_mm=60,
        height_limit_applied=False,
        predicted_min_z_mm=float(np.min(heights)),
        predicted_max_z_mm=float(np.max(heights)),
        scale_mode="natural",
    )
    return TerrainSurface(
        processed_crs="EPSG:32648",
        transform=Affine.identity(),
        elevations_m_north=np.flipud(heights).astype(np.float32),
        elevations_mm_south=heights.copy(),
        original_nodata_north=np.zeros(heights.shape, dtype=bool),
        scaling=scaling,
        model_width_mm=60,
        model_depth_mm=40,
        metric_bounds_at_samples=(0, 0, 60, 40),
        nodata_geometry_mm=GeometryCollection(),
    )


def _terrain_triangles(surface: TerrainSurface) -> list[npt.NDArray[np.float64]]:
    x = np.linspace(0, surface.model_width_mm, surface.columns)
    y = np.linspace(0, surface.model_depth_mm, surface.rows)
    triangles = []
    for row in range(surface.rows - 1):
        for column in range(surface.columns - 1):
            for corners in (
                ((row, column), (row, column + 1), (row + 1, column + 1)),
                ((row, column), (row + 1, column + 1), (row + 1, column)),
            ):
                triangles.append(
                    np.asarray(
                        [(x[c], y[r], surface.elevations_mm_south[r, c]) for r, c in corners]
                    )
                )
    return triangles


def _assert_conforms(mesh: trimesh.Trimesh, surface: TerrainSurface, footprint: Polygon) -> None:
    """Intersect independently constructed terrain facets, then solve their plane equations."""
    facets = _terrain_triangles(surface)
    facet_polygons = [Polygon(triangle[:, :2]) for triangle in facets]
    top_polygons = []
    for triangle, normal in zip(mesh.triangles, mesh.face_normals, strict=True):
        if abs(normal[2]) < 1e-12:
            continue
        projected = Polygon(triangle[:, :2])
        if normal[2] > 0:
            top_polygons.append(projected)
        areas = [projected.intersection(facet).area for facet in facet_polygons]
        index = int(np.argmax(areas))
        assert areas[index] == pytest.approx(projected.area, abs=1e-8)
        facet = facets[index]
        plane = np.linalg.solve(np.column_stack((facet[:, :2], np.ones(3))), facet[:, 2])
        centroid = triangle.mean(axis=0)
        expected = float(np.r_[centroid[:2], 1] @ plane)
        offset = 0.4 if normal[2] > 0 else -0.2
        assert centroid[2] == pytest.approx(expected + offset, abs=1e-8)
    assert unary_union(top_polygons).symmetric_difference(footprint).area < 1e-8
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert len(mesh.split()) == 1
    assert mesh.volume == pytest.approx(footprint.area * 0.6, rel=1e-9)
    assert np.all(mesh.nondegenerate_faces())
    assert np.all(mesh.unique_faces())
    assert len(np.unique(mesh.vertices, axis=0)) == len(mesh.vertices)


@pytest.mark.parametrize("terrain", ["hill", "valley", "checkerboard", "diagonal"])
def test_overlay_faces_conform_to_each_terrain_plane_and_are_deterministic(terrain: str) -> None:
    yy, xx = np.mgrid[-2:3, -3:4]
    heights = 3 + 25 * np.exp(-(xx**2 + yy**2) / 3)
    if terrain == "valley":
        heights = 34 - heights
    elif terrain == "checkerboard":
        heights = (3 + 25 * ((xx + yy) % 2)).astype(float)
    elif terrain == "diagonal":
        heights = np.asarray(((3.0, 30.0), (30.0, 3.0)))
    surface = _surface(heights)
    footprint = (
        LineString(((0, 8), (30, 30), (60, 12)))
        .buffer(0.4, cap_style="flat", join_style="mitre")
        .intersection(surface.model_bounds)
    )
    original = surface.elevations_mm_south.copy()
    first, error = draped_polygon_mesh(
        footprint, surface, raised_height_mm=0.4, embed_depth_mm=0.2, max_triangles=10_000
    )
    second, _ = draped_polygon_mesh(
        footprint, surface, raised_height_mm=0.4, embed_depth_mm=0.2, max_triangles=10_000
    )
    _assert_conforms(first, surface, footprint)
    assert error < 1e-8
    np.testing.assert_array_equal(first.vertices, second.vertices)
    np.testing.assert_array_equal(first.faces, second.faces)
    np.testing.assert_array_equal(surface.elevations_mm_south, original)


@pytest.mark.parametrize("small_hole", [False, True])
def test_holes_and_collinear_boundary_nodes_survive_terrain_seams(small_hole: bool) -> None:
    surface = _surface(np.asarray(((3.0, 20, 8), (15, 4, 20), (3, 30, 3))))
    # The small hole lies within a single facet; the large one crosses grid seams.
    hole = box(18, 2, 20, 3) if small_hole else box(10, 10, 40, 25)
    footprint = Polygon(
        ((0, 0), (15, 0), (30, 0), (45, 0), (60, 0), (60, 40), (30, 40), (0, 40)),
        holes=[list(hole.exterior.coords)],
    )
    mesh, error = draped_polygon_mesh(
        footprint, surface, raised_height_mm=0.4, embed_depth_mm=0.2, max_triangles=10_000
    )
    _assert_conforms(mesh, surface, footprint)
    assert error < 1e-8


def test_multiple_features_share_a_layer_budget() -> None:
    surface = _surface(np.full((3, 4), 10.0))
    source = OverlaySourceConfig(
        source_id="roads",
        kind="road",
        format="geojson",
        path=Path("unused.geojson"),
        dataset_name="synthetic",
        license="CC0",
        attribution="test",
    )
    features = tuple(
        ModelOverlayFeature(str(y), LineString(((1, y), (59, y))), {}) for y in (5, 35)
    )
    mesh, error, overlap, _ = build_layer_mesh(
        surface,
        source,
        features,
        minimum_feature_mm=0.4,
        allow_original_nodata=False,
    )
    assert len(mesh.split()) == 2
    assert error < 1e-8
    assert overlap == 0
    with pytest.raises(ConfigurationError, match="max_triangles"):
        build_layer_mesh(
            surface,
            source,
            features,
            minimum_feature_mm=0.4,
            allow_original_nodata=False,
            max_triangles=len(mesh.faces) - 1,
        )


def test_face_interior_measurement_detects_the_original_vertex_only_extrusion() -> None:
    heights = np.full((5, 7), 3.0)
    heights[2, 3] = 30
    surface = _surface(heights)
    broken = trimesh.creation.extrude_polygon(box(0, 19, 60, 21), height=0.6, engine="earcut")
    z = surface.surface_z_mm(broken.vertices[:, :2])
    broken.vertices[:, 2] = z + np.where(broken.vertices[:, 2] > 0.3, 0.4, -0.2)
    assert broken.is_watertight
    assert surface_mapping_error_mm(broken, surface, raised_height_mm=0.4, embed_depth_mm=0.2) > 10


def test_triangle_budget_stops_during_refinement_before_constructing_solid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _surface(np.full((101, 101), 10.0))
    original = draping_module._patch_triangles
    triangulated = 0

    def counting(polygon: Polygon) -> Iterator[npt.NDArray[np.float64]]:
        nonlocal triangulated
        triangulated += 1
        yield from original(polygon)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("budget must be enforced before allocating the solid")

    monkeypatch.setattr(draping_module, "_patch_triangles", counting)
    monkeypatch.setattr(trimesh, "Trimesh", forbidden)
    with pytest.raises(ConfigurationError, match="max_triangles"):
        draped_polygon_mesh(
            box(0, 0, 60, 40),
            surface,
            raised_height_mm=0.4,
            embed_depth_mm=0.2,
            max_triangles=12,
        )
    assert triangulated <= 6


def test_contour_corner_almost_on_a_grid_diagonal_has_no_sliver_or_fold() -> None:
    from dataclasses import replace

    surface = _surface(np.full((15, 20), 10.0))
    surface = replace(
        surface,
        model_depth_mm=45,
        scaling=surface.scaling.model_copy(update={"model_depth_mm": 45}),
    )
    # Retained generated contour: its inner corner misses the exact grid
    # diagonal by ~1e-10 mm. Decimal rounding alone splits near-equal seam
    # vertices across bins and earcut can create a numerically collinear ear.
    footprint = Polygon(
        (
            (58.42105263157282, 24.107142857043073),
            (58.42105263157282, 24.407142857043073),
            (59.99999684210343, 24.407142857043073),
            (59.99999684210343, 23.807142857043072),
            (58.72105263157282, 23.807142857043072),
            (58.72105263157282, 21.192857142840513),
            (59.99999684210343, 21.192857142840513),
            (59.99999684210343, 20.59285714284051),
            (58.12105263157282, 20.59285714284051),
            (58.12105263157282, 24.107142857043073),
        )
    )
    first, error = draped_polygon_mesh(
        footprint, surface, raised_height_mm=0.4, embed_depth_mm=0.2, max_triangles=10_000
    )
    repeated, _ = draped_polygon_mesh(
        footprint, surface, raised_height_mm=0.4, embed_depth_mm=0.2, max_triangles=10_000
    )
    _assert_conforms(first, surface, footprint)
    assert error < 1e-8
    np.testing.assert_array_equal(first.vertices, repeated.vertices)
    np.testing.assert_array_equal(first.faces, repeated.faces)
    assert len(np.unique(first.faces)) == len(first.vertices)
