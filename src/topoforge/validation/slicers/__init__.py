"""External headless slicer adapters."""

from topoforge.validation.slicers.base import (
    CommandExecution,
    CommandRunner,
    SliceMetrics,
    SlicerAdapter,
    SlicerAvailability,
    SliceResult,
    SlicerInfo,
    SlicerProfile,
    SliceStatus,
    parse_gcode_generator,
    parse_gcode_metrics,
)
from topoforge.validation.slicers.discovery import discover_slicers, select_slicer
from topoforge.validation.slicers.orca import OrcaSlicerAdapter
from topoforge.validation.slicers.prusa import PrusaSlicerAdapter

__all__ = [
    "CommandExecution",
    "CommandRunner",
    "OrcaSlicerAdapter",
    "PrusaSlicerAdapter",
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
    "select_slicer",
]
