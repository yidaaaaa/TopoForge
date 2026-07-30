"""Structured validation results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SelfIntersectionStatus = Literal["passed", "failed", "not_fully_checked"]


class ValidationReport(BaseModel):
    """Machine-readable measurements for one triangle mesh."""

    model_config = ConfigDict(frozen=True)

    units: Literal["mm"] = "mm"
    dimensions_mm: tuple[float, float, float]
    expected_dimensions_mm: tuple[float, float, float] | None = None
    dimension_error_mm: tuple[float, float, float] | None = None
    dimensions_within_tolerance: bool | None = None
    finite_vertices: bool
    finite_face_normals: bool
    watertight: bool
    winding_consistent: bool
    manifold: bool
    positive_volume: bool
    volume_mm3: float
    connected_components: int = Field(ge=0)
    degenerate_faces: int = Field(ge=0)
    duplicate_faces: int = Field(ge=0)
    flat_bottom: bool
    bottom_planarity_error_mm: float | None = Field(default=None, ge=0.0)
    minimum_base_thickness_mm: float | None = Field(default=None, ge=0.0)
    triangle_count: int = Field(ge=0)
    self_intersection_status: SelfIntersectionStatus = "not_fully_checked"
    self_intersection_method: str = (
        "No exhaustive robust self-intersection predicate is available in the Phase 1 backend."
    )
