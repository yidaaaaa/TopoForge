from pathlib import Path

import pytest

from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import resolve_sampling_decision, triangle_count_for_shape


def _config(**updates: object) -> BuildConfig:
    values: dict[str, object] = {
        "dem_path": Path("source.tif"),
        "output_dir": Path("output"),
        "model_width_mm": 180.0,
        "max_grid_cells": 1_500_000,
    }
    values.update(updates)
    return BuildConfig.model_validate(values)


def test_print_aware_sampling_uses_printer_limits_without_upsampling() -> None:
    decision = resolve_sampling_decision(
        (1000, 1200),
        ground_width_m=36_000.0,
        ground_depth_m=30_000.0,
        config=_config(),
    )

    assert decision.target_shape == (376, 451)
    assert decision.physical_spacing_xy_mm == pytest.approx((0.4, 0.4))
    assert decision.estimated_triangle_count == triangle_count_for_shape(decision.target_shape)
    assert decision.target_shape[0] < 1000
    assert decision.target_shape[1] < 1200
    assert not decision.warnings
    assert any("no terrain detail is invented" in reason for reason in decision.reasons)


def test_source_preserving_retains_normalized_source_grid_within_budget() -> None:
    decision = resolve_sampling_decision(
        (200, 300),
        ground_width_m=9_000.0,
        ground_depth_m=6_000.0,
        config=_config(sampling_mode=SamplingMode.SOURCE_PRESERVING),
    )

    assert decision.target_shape == (200, 300)
    assert not decision.warnings
    assert any("every normalized source sample" in reason for reason in decision.reasons)


def test_source_preserving_reports_cell_budget_limit() -> None:
    decision = resolve_sampling_decision(
        (500, 600),
        ground_width_m=18_000.0,
        ground_depth_m=15_000.0,
        config=_config(
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
        ),
    )

    assert decision.target_shape[0] * decision.target_shape[1] <= 10_000
    assert decision.warnings
    assert "max_grid_cells" in decision.warnings[0]


def test_custom_spacing_and_custom_cell_budget_are_deterministic() -> None:
    spacing_config = _config(sampling_mode=SamplingMode.CUSTOM, mesh_sampling_mm=1.0)
    first = resolve_sampling_decision(
        (500, 600),
        ground_width_m=18_000.0,
        ground_depth_m=15_000.0,
        config=spacing_config,
    )
    second = resolve_sampling_decision(
        (500, 600),
        ground_width_m=18_000.0,
        ground_depth_m=15_000.0,
        config=spacing_config,
    )
    assert first == second
    assert first.target_shape == (151, 181)

    cell_budget = resolve_sampling_decision(
        (500, 600),
        ground_width_m=18_000.0,
        ground_depth_m=15_000.0,
        config=_config(sampling_mode=SamplingMode.CUSTOM, max_grid_cells=20_000),
    )
    assert cell_budget.target_shape[0] * cell_budget.target_shape[1] <= 20_000


def test_memory_budget_can_be_the_limiting_resource() -> None:
    decision = resolve_sampling_decision(
        (1000, 1000),
        ground_width_m=30_000.0,
        ground_depth_m=30_000.0,
        config=_config(
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_estimated_memory_mb=2.0,
        ),
    )

    assert decision.estimated_memory_mb <= 2.0
    assert decision.warnings
    assert "max_estimated_memory_mb" in decision.warnings[0]


def test_explicit_triangle_budget_limits_the_processed_grid() -> None:
    decision = resolve_sampling_decision(
        (500, 600),
        ground_width_m=18_000.0,
        ground_depth_m=15_000.0,
        config=_config(
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_estimated_triangles=40_000,
        ),
    )

    assert decision.estimated_triangle_count <= 40_000
    assert decision.warnings
    assert "max_estimated_triangles" in decision.warnings[0]


def test_strict_resource_mode_rejects_instead_of_downsampling() -> None:
    from topoforge.exceptions import ConfigurationError
    from topoforge.models import ResourceBudgetMode

    with pytest.raises(ConfigurationError, match="strict resource budget rejected"):
        resolve_sampling_decision(
            (500, 600),
            ground_width_m=18_000.0,
            ground_depth_m=15_000.0,
            config=_config(
                sampling_mode=SamplingMode.SOURCE_PRESERVING,
                max_grid_cells=10_000,
                resource_budget_mode=ResourceBudgetMode.STRICT,
            ),
        )
