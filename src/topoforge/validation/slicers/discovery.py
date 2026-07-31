"""Preferred slicer discovery with OrcaSlicer-first fallback."""

from __future__ import annotations

from pathlib import Path

from topoforge.validation.slicers.bambu import BambuStudioAdapter
from topoforge.validation.slicers.base import (
    CommandRunner,
    SlicerAdapter,
    SlicerAvailability,
    run_command,
)
from topoforge.validation.slicers.orca import OrcaSlicerAdapter
from topoforge.validation.slicers.prusa import PrusaSlicerAdapter


def discover_slicers(
    *,
    bambu_executable: str | Path | None = None,
    orca_executable: str | Path | None = None,
    prusa_executable: str | Path | None = None,
    runner: CommandRunner = run_command,
) -> tuple[BambuStudioAdapter, OrcaSlicerAdapter, PrusaSlicerAdapter]:
    """Return Bambu Studio, OrcaSlicer, and PrusaSlicer in preference order."""
    return (
        BambuStudioAdapter(bambu_executable, runner=runner),
        OrcaSlicerAdapter(orca_executable, runner=runner),
        PrusaSlicerAdapter(prusa_executable, runner=runner),
    )


def select_slicer(
    *,
    bambu_executable: str | Path | None = None,
    orca_executable: str | Path | None = None,
    prusa_executable: str | Path | None = None,
    runner: CommandRunner = run_command,
) -> SlicerAdapter:
    """Select the first working slicer, retaining an unavailable result if none work."""
    adapters = discover_slicers(
        bambu_executable=bambu_executable,
        orca_executable=orca_executable,
        prusa_executable=prusa_executable,
        runner=runner,
    )
    for adapter in adapters:
        if adapter.probe().status is SlicerAvailability.AVAILABLE:
            return adapter
    return adapters[0]
