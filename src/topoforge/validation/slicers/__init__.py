"""External headless slicer adapters."""

from topoforge.validation.slicers.bambu import BambuStudioAdapter
from topoforge.validation.slicers.base import (
    CommandExecution,
    CommandRunner,
    ResolvedPrintSettings,
    SliceMetrics,
    SlicerAdapter,
    SlicerAvailability,
    SliceResult,
    SlicerInfo,
    SlicerProfile,
    SliceStatus,
    parse_gcode_generator,
    parse_gcode_metrics,
    parse_gcode_settings,
)
from topoforge.validation.slicers.discovery import discover_slicers, select_slicer
from topoforge.validation.slicers.orca import OrcaSlicerAdapter
from topoforge.validation.slicers.prusa import PrusaSlicerAdapter

__all__ = [
    "BambuStudioAdapter",
    "CommandExecution",
    "CommandRunner",
    "OrcaSlicerAdapter",
    "PrusaSlicerAdapter",
    "ResolvedPrintSettings",
    "SliceMetrics",
    "SliceResult",
    "SliceStatus",
    "SlicerAdapter",
    "SlicerAvailability",
    "SlicerInfo",
    "SlicerProfile",
    "discover_slicers",
    "parse_gcode_generator",
    "parse_gcode_metrics",
    "parse_gcode_settings",
    "select_slicer",
]
