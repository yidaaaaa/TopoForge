from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.validation import validate_mesh


@st.composite
def finite_heightfields(draw: st.DrawFn) -> np.ndarray:
    rows = draw(st.integers(min_value=2, max_value=8))
    columns = draw(st.integers(min_value=2, max_value=8))
    values = draw(
        st.lists(
            st.floats(
                min_value=-500.0,
                max_value=9_000.0,
                allow_nan=False,
                allow_infinity=False,
                width=32,
            ),
            min_size=rows * columns,
            max_size=rows * columns,
        )
    )
    return np.asarray(values, dtype=np.float64).reshape(rows, columns)


@given(
    heights=finite_heightfields(),
    width_mm=st.floats(min_value=10.0, max_value=250.0, allow_nan=False, allow_infinity=False),
    depth_mm=st.floats(min_value=10.0, max_value=250.0, allow_nan=False, allow_infinity=False),
    base_mm=st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=60, deadline=None)
def test_any_small_finite_rectangular_dem_builds_closed_positive_solid(
    heights: np.ndarray,
    width_mm: float,
    depth_mm: float,
    base_mm: float,
) -> None:
    top_z_mm = base_mm + heights - float(np.min(heights))
    mesh = build_rectangular_terrain_mesh(
        top_z_mm,
        width_mm=width_mm,
        depth_mm=depth_mm,
        base_thickness_mm=base_mm,
    )
    report = validate_mesh(
        mesh,
        expected_dimensions_mm=(
            float(width_mm),
            float(depth_mm),
            float(base_mm + np.ptp(heights)),
        ),
    )
    assert report.watertight
    assert report.winding_consistent
    assert report.manifold
    assert report.positive_volume
    assert report.connected_components == 1
    assert report.degenerate_faces == 0
    assert report.duplicate_faces == 0
    assert report.flat_bottom
    assert report.minimum_base_thickness_mm is not None
    assert abs(report.minimum_base_thickness_mm - base_mm) <= 1e-9
