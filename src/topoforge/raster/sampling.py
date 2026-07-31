"""Printer-aware deterministic terrain-grid sampling decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass

from topoforge.models import BuildConfig, SamplingMode

_ESTIMATED_BYTES_PER_GRID_CELL = 320
_MEBIBYTE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SamplingDecision:
    """Resolved deterministic grid shape plus explainable resource estimates."""

    target_shape: tuple[int, int]
    model_depth_mm: float
    physical_spacing_xy_mm: tuple[float, float]
    estimated_triangle_count: int
    estimated_memory_mb: float
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def triangle_count_for_shape(shape: tuple[int, int]) -> int:
    """Return exact triangles for the closed regular-grid topology."""
    return 4 * shape[0] * shape[1] - 4


def _shape_for_spacing(
    source_shape: tuple[int, int],
    *,
    model_width_mm: float,
    model_depth_mm: float,
    spacing_mm: float,
) -> tuple[int, int]:
    source_rows, source_columns = source_shape
    target_columns = min(source_columns, max(2, math.floor(model_width_mm / spacing_mm) + 1))
    target_rows = min(source_rows, max(2, math.floor(model_depth_mm / spacing_mm) + 1))
    return target_rows, target_columns


def _fit_shape_to_budget(shape: tuple[int, int], maximum_cells: int) -> tuple[int, int]:
    rows, columns = shape
    if rows * columns <= maximum_cells:
        return shape
    reduction = math.sqrt((rows * columns) / maximum_cells)
    target_rows = max(2, math.floor(rows / reduction))
    target_columns = max(2, math.floor(columns / reduction))
    while target_rows * target_columns > maximum_cells:
        if target_rows / rows >= target_columns / columns and target_rows > 2:
            target_rows -= 1
        elif target_columns > 2:
            target_columns -= 1
        else:
            break
    return target_rows, target_columns


def resolve_sampling_decision(
    source_shape: tuple[int, int],
    *,
    ground_width_m: float,
    ground_depth_m: float,
    config: BuildConfig,
) -> SamplingDecision:
    """Resolve print resolution and resource budgets without ever upsampling terrain."""
    source_rows, source_columns = source_shape
    model_depth_mm = config.model_depth_mm or (
        config.model_width_mm * ground_depth_m / ground_width_m
    )
    source_spacing_x_mm = config.model_width_mm / max(source_columns - 1, 1)
    source_spacing_y_mm = model_depth_mm / max(source_rows - 1, 1)
    reasons = [
        f"source grid is {source_rows} x {source_columns}",
        (
            "source samples map to "
            f"{source_spacing_x_mm:.6f} x {source_spacing_y_mm:.6f} mm on the model"
        ),
    ]
    warnings: list[str] = []

    if config.sampling_mode is SamplingMode.PRINT_AWARE:
        printer_spacing_mm = min(
            config.printer_profile.preferred_mesh_sampling_mm,
            config.printer_profile.nozzle_diameter_mm,
            config.printer_profile.minimum_feature_mm,
        )
        requested_shape = _shape_for_spacing(
            source_shape,
            model_width_mm=config.model_width_mm,
            model_depth_mm=model_depth_mm,
            spacing_mm=printer_spacing_mm,
        )
        reasons.extend(
            (
                (
                    "print-aware spacing uses the finest of preferred mesh sampling "
                    f"({config.printer_profile.preferred_mesh_sampling_mm:.6f} mm), nozzle "
                    f"diameter ({config.printer_profile.nozzle_diameter_mm:.6f} mm), and minimum "
                    f"feature ({config.printer_profile.minimum_feature_mm:.6f} mm)"
                ),
                f"resolved printer-aware target spacing is {printer_spacing_mm:.6f} mm",
                "target shape is capped at the source grid so no terrain detail is invented",
            )
        )
    elif config.sampling_mode is SamplingMode.SOURCE_PRESERVING:
        requested_shape = source_shape
        reasons.append("source-preserving mode requested every normalized source sample")
    else:
        if config.mesh_sampling_mm is not None:
            requested_shape = _shape_for_spacing(
                source_shape,
                model_width_mm=config.model_width_mm,
                model_depth_mm=model_depth_mm,
                spacing_mm=config.mesh_sampling_mm,
            )
            reasons.extend(
                (
                    f"custom mode requested {config.mesh_sampling_mm:.6f} mm mesh spacing",
                    "custom spacing is capped at the source grid so no terrain detail is invented",
                )
            )
        else:
            requested_shape = source_shape
            reasons.append("custom mode uses max_grid_cells as the explicit sampling control")

    memory_cell_limit = max(
        16,
        int(config.max_estimated_memory_mb * _MEBIBYTE // _ESTIMATED_BYTES_PER_GRID_CELL),
    )
    effective_cell_limit = min(config.max_grid_cells, memory_cell_limit)
    target_shape = _fit_shape_to_budget(requested_shape, effective_cell_limit)
    if target_shape != requested_shape:
        reasons.append(
            f"resource budget reduced {requested_shape[0]} x {requested_shape[1]} to "
            f"{target_shape[0]} x {target_shape[1]}"
        )
        limiting_budget = (
            "max_grid_cells"
            if config.max_grid_cells <= memory_cell_limit
            else "max_estimated_memory_mb"
        )
        warnings.append(
            f"{config.sampling_mode.value} request was limited by {limiting_budget}; "
            "the processed grid does not retain every requested sample"
        )
    reasons.append(
        f"effective cell limit is {effective_cell_limit} from max_grid_cells="
        f"{config.max_grid_cells} and max_estimated_memory_mb={config.max_estimated_memory_mb:.3f}"
    )

    rows, columns = target_shape
    spacing_x_mm = config.model_width_mm / max(columns - 1, 1)
    spacing_y_mm = model_depth_mm / max(rows - 1, 1)
    estimated_memory_mb = rows * columns * _ESTIMATED_BYTES_PER_GRID_CELL / _MEBIBYTE
    return SamplingDecision(
        target_shape=target_shape,
        model_depth_mm=model_depth_mm,
        physical_spacing_xy_mm=(spacing_x_mm, spacing_y_mm),
        estimated_triangle_count=triangle_count_for_shape(target_shape),
        estimated_memory_mb=estimated_memory_mb,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )
