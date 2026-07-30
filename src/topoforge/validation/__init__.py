"""Mesh validation API."""

from topoforge.validation.mesh import validate_mesh, write_validation_report
from topoforge.validation.models import SelfIntersectionStatus, ValidationReport

__all__ = [
    "SelfIntersectionStatus",
    "ValidationReport",
    "validate_mesh",
    "write_validation_report",
]
