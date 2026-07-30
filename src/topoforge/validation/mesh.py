"""Geometry validation for manufacturing meshes."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
import trimesh

from topoforge.validation.models import ValidationReport


def validate_mesh(
    mesh: trimesh.Trimesh,
    *,
    expected_dimensions_mm: tuple[float, float, float] | None = None,
    dimension_tolerance_mm: float = 0.05,
    flat_bottom_tolerance_mm: float = 0.01,
) -> ValidationReport:
    """Measure the Phase 1 mesh invariants without mutating the input.

    Self-intersection is deliberately reported as ``not_fully_checked``.  The
    Trimesh backend used here does not expose an exhaustive, robust triangle
    self-intersection predicate, so absence of a check is never presented as a
    pass.
    """

    dimension_tolerance = _non_negative_finite("dimension_tolerance_mm", dimension_tolerance_mm)
    bottom_tolerance = _non_negative_finite("flat_bottom_tolerance_mm", flat_bottom_tolerance_mm)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    dimensions = _dimensions(vertices)
    expected_dimensions = _expected_dimensions(expected_dimensions_mm)
    dimension_error: tuple[float, float, float] | None = None
    dimensions_within_tolerance: bool | None = None
    if expected_dimensions is not None:
        dimension_error = (
            abs(dimensions[0] - expected_dimensions[0]),
            abs(dimensions[1] - expected_dimensions[1]),
            abs(dimensions[2] - expected_dimensions[2]),
        )
        dimensions_within_tolerance = max(dimension_error) <= dimension_tolerance

    finite_vertices = bool(np.all(np.isfinite(vertices)))
    face_normals = np.asarray(mesh.face_normals, dtype=np.float64)
    finite_face_normals = bool(np.all(np.isfinite(face_normals)))
    degenerate_faces = _count_degenerate_faces(vertices, faces)
    duplicate_faces = _count_duplicate_faces(faces)
    manifold = _is_closed_edge_manifold(faces)
    flat_bottom, bottom_error, base_thickness = _bottom_measurements(
        vertices=vertices,
        faces=faces,
        face_normals=face_normals,
        tolerance_mm=bottom_tolerance,
    )

    volume = float(mesh.volume)
    return ValidationReport(
        dimensions_mm=dimensions,
        expected_dimensions_mm=expected_dimensions,
        dimension_error_mm=dimension_error,
        dimensions_within_tolerance=dimensions_within_tolerance,
        finite_vertices=finite_vertices,
        finite_face_normals=finite_face_normals,
        watertight=bool(mesh.is_watertight),
        winding_consistent=bool(mesh.is_winding_consistent),
        manifold=manifold,
        positive_volume=bool(np.isfinite(volume) and volume > 0.0),
        volume_mm3=volume,
        connected_components=_connected_components(mesh),
        degenerate_faces=degenerate_faces,
        duplicate_faces=duplicate_faces,
        flat_bottom=flat_bottom,
        bottom_planarity_error_mm=bottom_error,
        minimum_base_thickness_mm=base_thickness,
        triangle_count=len(faces),
        self_intersection_status="not_fully_checked",
    )


def write_validation_report(report: ValidationReport, path: str | Path) -> Path:
    """Write a deterministic, newline-terminated validation JSON document."""

    destination = Path(path)
    if destination.suffix.lower() != ".json":
        msg = f"expected a .json validation path, got {destination}"
        raise ValueError(msg)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump_json(indent=2) + "\n"
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(destination)
    return destination


def _dimensions(vertices: npt.NDArray[np.float64]) -> tuple[float, float, float]:
    if len(vertices) == 0:
        return (0.0, 0.0, 0.0)
    extents = np.ptp(vertices, axis=0)
    return (float(extents[0]), float(extents[1]), float(extents[2]))


def _expected_dimensions(
    value: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    if value is None:
        return None
    converted = tuple(float(item) for item in value)
    if len(converted) != 3 or not bool(np.all(np.isfinite(converted))):
        msg = "expected_dimensions_mm must contain three finite values"
        raise ValueError(msg)
    if any(item < 0.0 for item in converted):
        msg = "expected_dimensions_mm values must be non-negative"
        raise ValueError(msg)
    return converted


def _non_negative_finite(name: str, value: float) -> float:
    converted = float(value)
    if not np.isfinite(converted) or converted < 0.0:
        msg = f"{name} must be finite and non-negative, got {value!r}"
        raise ValueError(msg)
    return converted


def _count_degenerate_faces(vertices: npt.NDArray[np.float64], faces: npt.NDArray[np.int64]) -> int:
    if len(faces) == 0:
        return 0
    repeated_vertex = (
        (faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 2] == faces[:, 0])
    )
    triangles = vertices[faces]
    cross_products = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    doubled_areas = np.linalg.norm(cross_products, axis=1)
    scale = max(float(np.max(np.ptp(vertices, axis=0), initial=0.0)), 1.0)
    area_threshold = max(1e-12, scale * scale * np.finfo(np.float64).eps * 16.0)
    return int(np.count_nonzero(repeated_vertex | (doubled_areas <= area_threshold)))


def _count_duplicate_faces(faces: npt.NDArray[np.int64]) -> int:
    if len(faces) == 0:
        return 0
    canonical = np.sort(faces, axis=1)
    _, counts = np.unique(canonical, axis=0, return_counts=True)
    return int(np.sum(np.maximum(counts - 1, 0)))


def _is_closed_edge_manifold(faces: npt.NDArray[np.int64]) -> bool:
    if len(faces) == 0:
        return False
    edges = faces[:, ((0, 1), (1, 2), (2, 0))].reshape(-1, 2)
    canonical_edges = np.sort(edges, axis=1)
    _, counts = np.unique(canonical_edges, axis=0, return_counts=True)
    return bool(np.all(counts == 2))


def _bottom_measurements(
    *,
    vertices: npt.NDArray[np.float64],
    faces: npt.NDArray[np.int64],
    face_normals: npt.NDArray[np.float64],
    tolerance_mm: float,
) -> tuple[bool, float | None, float | None]:
    if len(vertices) == 0 or len(faces) == 0 or len(face_normals) != len(faces):
        return (False, None, None)

    finite_normals = np.all(np.isfinite(face_normals), axis=1)
    downward_faces = finite_normals & (face_normals[:, 2] <= -1.0 + 1e-9)
    if not bool(np.any(downward_faces)):
        return (False, None, None)

    bottom_vertex_indices = np.unique(faces[downward_faces].reshape(-1))
    bottom_z: npt.NDArray[np.float64] = vertices[bottom_vertex_indices, 2]
    global_bottom_z = float(np.min(vertices[:, 2]))
    bottom_spread = float(np.max(bottom_z) - np.min(bottom_z))
    bottom_offset = float(np.max(np.abs(bottom_z - global_bottom_z)))
    bottom_error = max(bottom_spread, bottom_offset)
    flat_bottom = bool(np.isfinite(bottom_error) and bottom_error <= tolerance_mm)

    upward_faces = finite_normals & (face_normals[:, 2] > 0.0)
    if not bool(np.any(upward_faces)):
        return (flat_bottom, bottom_error, None)
    top_vertex_indices = np.unique(faces[upward_faces].reshape(-1))
    base_thickness = float(np.min(vertices[top_vertex_indices, 2]) - global_bottom_z)
    if not np.isfinite(base_thickness) or base_thickness < 0.0:
        base_thickness = None
    return (flat_bottom, bottom_error, base_thickness)


def _connected_components(mesh: trimesh.Trimesh) -> int:
    if len(mesh.faces) == 0:
        return 0
    return int(mesh.body_count)  # pyright: ignore[reportUnknownArgumentType]
