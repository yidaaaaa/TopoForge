"""Mesh validation API."""

from topoforge.validation.bambu_projects import (
    BambuProjectEvidenceResult,
    generate_bambu_project_evidence,
    verify_bambu_project_evidence,
)
from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate
from topoforge.validation.mesh import validate_mesh, write_validation_report
from topoforge.validation.models import SelfIntersectionStatus, ValidationReport

__all__ = [
    "BambuProjectEvidenceResult",
    "SelfIntersectionStatus",
    "ValidationReport",
    "evaluate_bambu_p2s_release_gate",
    "generate_bambu_project_evidence",
    "validate_mesh",
    "verify_bambu_project_evidence",
    "write_validation_report",
]
