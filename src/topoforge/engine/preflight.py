"""Deterministic manufacturing resource and build-volume preflight."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig, RasterResult, ScalingResult
from topoforge.raster import process_local_raster
from topoforge.scaling import resolve_scaling


class ManufacturingPreflightReport(BaseModel):
    """Resolved resource, scale, and printer-fit evidence before mesh export."""

    model_config = ConfigDict(extra="forbid")

    status: str
    printer_profile_id: str
    printer_build_volume_mm: tuple[float, float, float]
    resolved_model_dimensions_mm: tuple[float, float, float]
    build_volume_passed: bool
    build_volume_utilization: dict[str, float]
    build_volume_headroom_mm: dict[str, float]
    base_thickness_mm: float
    terrain_height_budget_mm: float
    source_grid_shape: tuple[int, int]
    processed_grid_shape: tuple[int, int]
    source_grid_cells: int = Field(gt=0)
    processed_grid_cells: int = Field(gt=0)
    max_grid_cells: int = Field(gt=0)
    grid_cell_budget_passed: bool
    grid_cell_budget_utilization: float = Field(ge=0)
    estimated_triangle_count: int = Field(gt=0)
    maximum_estimated_triangles: int = Field(gt=0)
    triangle_budget_passed: bool
    triangle_budget_utilization: float = Field(ge=0)
    estimated_memory_mb: float = Field(gt=0)
    maximum_estimated_memory_mb: float = Field(gt=0)
    memory_budget_passed: bool
    memory_budget_utilization: float = Field(ge=0)
    physical_sample_spacing_xy_mm: tuple[float, float]
    sampling_mode: str
    resource_budget_mode: str
    vertical_scale_mode: str
    policy_vertical_exaggeration: float = Field(gt=0)
    resolved_vertical_exaggeration: float = Field(gt=0)
    height_limit_applied: bool
    height_limit_passed: bool
    decisions: list[str]
    warnings: list[str]
    suggested_actions: list[str]


def evaluate_manufacturing_preflight(
    raster: RasterResult,
    scaling: ScalingResult,
    config: BuildConfig,
) -> ManufacturingPreflightReport:
    """Measure resource usage and printer fit from the same resolved build state."""
    build_x, build_y, build_z = config.printer_profile.build_volume_mm
    resolved_dimensions = (
        scaling.model_width_mm,
        scaling.model_depth_mm,
        scaling.predicted_max_z_mm,
    )
    utilization = {
        "x": scaling.model_width_mm / build_x,
        "y": scaling.model_depth_mm / build_y,
        "z": scaling.predicted_max_z_mm / build_z,
    }
    headroom = {
        "x": build_x - scaling.model_width_mm,
        "y": build_y - scaling.model_depth_mm,
        "z": build_z - scaling.predicted_max_z_mm,
    }
    processed_cells = raster.processed_grid_shape[0] * raster.processed_grid_shape[1]
    source_cells = raster.source_grid_shape[0] * raster.source_grid_shape[1]
    triangle_limit = config.max_estimated_triangles or (config.max_grid_cells * 4 - 4)
    build_volume_passed = all(value <= 1.0 + 1e-9 for value in utilization.values())
    grid_cell_budget_passed = processed_cells <= config.max_grid_cells
    triangle_budget_passed = raster.estimated_triangle_count <= triangle_limit
    memory_budget_passed = raster.estimated_memory_mb <= config.max_estimated_memory_mb + 1e-9
    height_limit_passed = scaling.predicted_max_z_mm <= scaling.height_limit_mm + 0.05
    failed_gates = [
        name
        for name, passed in (
            ("printer build volume", build_volume_passed),
            ("grid cell budget", grid_cell_budget_passed),
            ("triangle budget", triangle_budget_passed),
            ("memory budget", memory_budget_passed),
            ("model height limit", height_limit_passed),
        )
        if not passed
    ]
    if failed_gates:
        joined = ", ".join(failed_gates)
        raise ConfigurationError(f"manufacturing preflight failed hard gates: {joined}")
    warnings = list(raster.sampling_warnings)
    actions: list[str] = []
    for axis, value in utilization.items():
        if value >= 0.9:
            warnings.append(
                f"model uses {value * 100:.1f}% of printer {axis.upper()} build dimension"
            )
            actions.append(
                f"reduce model {axis.upper()} dimension or select a larger printer profile"
            )
    if raster.estimated_memory_mb / config.max_estimated_memory_mb >= 0.9:
        warnings.append("estimated memory uses at least 90% of the configured budget")
        actions.append("increase max_estimated_memory_mb or use coarser sampling")
    if processed_cells / config.max_grid_cells >= 0.9:
        warnings.append("processed grid uses at least 90% of max_grid_cells")
        actions.append("increase max_grid_cells or use coarser sampling")
    if raster.estimated_triangle_count / triangle_limit >= 0.9:
        warnings.append("estimated mesh uses at least 90% of the triangle budget")
        actions.append("increase max_estimated_triangles or use coarser sampling")
    if scaling.height_limit_applied:
        warnings.append("vertical exaggeration was reduced to satisfy the hard model-height limit")
        actions.append(
            "increase max_height_mm, lower minimum vertical exaggeration, or reduce model width"
        )
    decisions = [
        *raster.sampling_decision_reasons,
        (
            f"resolved footprint is {scaling.model_width_mm:.6f} x "
            f"{scaling.model_depth_mm:.6f} mm inside printer volume "
            f"{build_x:.6f} x {build_y:.6f} x {build_z:.6f} mm"
        ),
        (
            f"terrain height budget is "
            f"{scaling.height_limit_mm - scaling.base_thickness_mm:.6f} mm above a "
            f"{scaling.base_thickness_mm:.6f} mm base"
        ),
        (
            f"vertical policy requested {scaling.policy_vertical_exaggeration:.9g}x and "
            f"resolved {scaling.vertical_exaggeration:.9g}x"
        ),
        (
            f"estimated resources are {processed_cells} cells, "
            f"{raster.estimated_triangle_count} triangles, "
            f"{raster.estimated_memory_mb:.6f} MiB"
        ),
    ]
    return ManufacturingPreflightReport(
        status="passed-with-warnings" if warnings else "passed",
        printer_profile_id=config.printer_profile.profile_id,
        printer_build_volume_mm=config.printer_profile.build_volume_mm,
        resolved_model_dimensions_mm=resolved_dimensions,
        build_volume_passed=build_volume_passed,
        build_volume_utilization=utilization,
        build_volume_headroom_mm=headroom,
        base_thickness_mm=scaling.base_thickness_mm,
        terrain_height_budget_mm=scaling.height_limit_mm - scaling.base_thickness_mm,
        source_grid_shape=raster.source_grid_shape,
        processed_grid_shape=raster.processed_grid_shape,
        source_grid_cells=source_cells,
        processed_grid_cells=processed_cells,
        max_grid_cells=config.max_grid_cells,
        grid_cell_budget_passed=grid_cell_budget_passed,
        grid_cell_budget_utilization=processed_cells / config.max_grid_cells,
        estimated_triangle_count=raster.estimated_triangle_count,
        maximum_estimated_triangles=triangle_limit,
        triangle_budget_passed=triangle_budget_passed,
        triangle_budget_utilization=raster.estimated_triangle_count / triangle_limit,
        estimated_memory_mb=raster.estimated_memory_mb,
        maximum_estimated_memory_mb=config.max_estimated_memory_mb,
        memory_budget_passed=memory_budget_passed,
        memory_budget_utilization=raster.estimated_memory_mb / config.max_estimated_memory_mb,
        physical_sample_spacing_xy_mm=raster.physical_sample_spacing_xy_mm,
        sampling_mode=config.sampling_mode.value,
        resource_budget_mode=config.resource_budget_mode.value,
        vertical_scale_mode=config.vertical_scale_mode.value,
        policy_vertical_exaggeration=scaling.policy_vertical_exaggeration,
        resolved_vertical_exaggeration=scaling.vertical_exaggeration,
        height_limit_applied=scaling.height_limit_applied,
        height_limit_passed=height_limit_passed,
        decisions=decisions,
        warnings=warnings,
        suggested_actions=list(dict.fromkeys(actions)),
    )


def preflight_local_terrain(config: BuildConfig) -> ManufacturingPreflightReport:
    """Run raster/scaling preflight without publishing a build output directory."""
    source = config.dem_path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="topoforge-preflight-") as raw_directory:
        temporary = Path(raw_directory)
        temporary_config = config.model_copy(update={"dem_path": source, "output_dir": temporary})
        processed = process_local_raster(temporary_config)
        scaling = resolve_scaling(processed.elevations_m, processed.report, config)
        return evaluate_manufacturing_preflight(processed.report, scaling, config)
