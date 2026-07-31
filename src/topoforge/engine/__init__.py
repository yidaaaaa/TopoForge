"""Unified TopoForge build service used by every interface."""

from topoforge.engine.build import (
    BuildResult,
    build_local_terrain,
    record_slice_validation,
    verify_artifact_bundle,
)
from topoforge.engine.preflight import (
    ManufacturingPreflightReport,
    evaluate_manufacturing_preflight,
    preflight_local_terrain,
)

__all__ = [
    "BuildResult",
    "ManufacturingPreflightReport",
    "build_local_terrain",
    "evaluate_manufacturing_preflight",
    "preflight_local_terrain",
    "record_slice_validation",
    "verify_artifact_bundle",
]
