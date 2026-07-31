from pathlib import Path

import pytest

from topoforge.exceptions import ConfigurationError
from topoforge.tiling import (
    TileLayoutConfig,
    canonical_tile_layout_bytes,
    plan_tile_layout,
    read_tile_layout,
    write_tile_layout,
)


def _config(**updates: object) -> TileLayoutConfig:
    values: dict[str, object] = {
        "source_grid_shape": (11, 16),
        "model_width_mm": 300.0,
        "model_depth_mm": 200.0,
        "maximum_tile_width_mm": 120.0,
        "maximum_tile_depth_mm": 120.0,
        "overlap_cells": 1,
    }
    values.update(updates)
    return TileLayoutConfig.model_validate(values)


def test_layout_ids_and_row_column_mapping_are_stable() -> None:
    first = plan_tile_layout(_config())
    second = plan_tile_layout(_config())

    assert first == second
    assert first.layout_id == second.layout_id
    assert first.tile_grid_shape == (2, 3)
    assert first.tile_count == 6
    assert [tile.tile_id for tile in first.tiles] == [
        "tile-r0000-c0000",
        "tile-r0000-c0001",
        "tile-r0000-c0002",
        "tile-r0001-c0000",
        "tile-r0001-c0001",
        "tile-r0001-c0002",
    ]
    assert canonical_tile_layout_bytes(first) == canonical_tile_layout_bytes(second)


def test_row_zero_is_north_and_column_zero_is_west_in_model_coordinates() -> None:
    layout = plan_tile_layout(_config())
    north_west = layout.tiles[0]
    north_east = layout.tiles[2]
    south_west = layout.tiles[3]

    assert layout.row_origin == "north"
    assert layout.column_origin == "west"
    assert north_west.physical_bounds_mm.y_max == pytest.approx(200.0)
    assert south_west.physical_bounds_mm.y_min == pytest.approx(0.0)
    assert north_west.physical_bounds_mm.x_min == pytest.approx(0.0)
    assert north_east.physical_bounds_mm.x_max == pytest.approx(300.0)
    assert north_west.north_neighbor is None
    assert north_west.south_neighbor == "tile-r0001-c0000"
    assert north_west.east_neighbor == "tile-r0000-c0001"
    assert north_east.west_neighbor == "tile-r0000-c0001"


def test_core_cell_windows_partition_without_gaps_and_share_seam_samples() -> None:
    layout = plan_tile_layout(_config())
    first = layout.tiles[0]
    east = layout.tiles[1]
    south = layout.tiles[3]

    assert first.core_cell_window.column_stop == east.core_cell_window.column_start
    assert first.core_sample_window.column_stop - 1 == east.core_sample_window.column_start
    assert first.core_cell_window.row_stop == south.core_cell_window.row_start
    assert first.core_sample_window.row_stop - 1 == south.core_sample_window.row_start

    covered = set()
    for tile in layout.tiles:
        window = tile.core_cell_window
        for row in range(window.row_start, window.row_stop):
            for column in range(window.column_start, window.column_stop):
                key = (row, column)
                assert key not in covered
                covered.add(key)
    assert covered == {(row, column) for row in range(10) for column in range(15)}


def test_overlap_sampling_windows_extend_beyond_core_and_clip_at_outer_edges() -> None:
    layout = plan_tile_layout(_config(overlap_cells=2))
    north_west = layout.tiles[0]
    interior_east = layout.tiles[1]
    south_east = layout.tiles[-1]

    assert north_west.sampling_window.row_start == 0
    assert north_west.sampling_window.column_start == 0
    assert north_west.sampling_window.row_stop > north_west.core_sample_window.row_stop
    assert (
        interior_east.sampling_window.column_start < interior_east.core_sample_window.column_start
    )
    assert interior_east.sampling_window.column_stop > interior_east.core_sample_window.column_stop
    assert south_east.sampling_window.row_stop == 11
    assert south_east.sampling_window.column_stop == 16


def test_uneven_cells_are_distributed_from_north_then_west_deterministically() -> None:
    layout = plan_tile_layout(
        _config(
            source_grid_shape=(12, 17),
            model_width_mm=320.0,
            model_depth_mm=220.0,
            maximum_tile_width_mm=110.0,
            maximum_tile_depth_mm=110.0,
        )
    )
    north_rows = [tile for tile in layout.tiles if tile.row == 0]
    south_rows = [tile for tile in layout.tiles if tile.row == 1]

    assert north_rows[0].core_cell_window.row_stop == 6
    assert south_rows[0].core_cell_window.row_start == 6
    assert [
        tile.core_cell_window.column_stop - tile.core_cell_window.column_start
        for tile in north_rows
    ] == [6, 5, 5]


def test_layout_write_is_canonical_strict_and_refuses_overwrite(tmp_path: Path) -> None:
    layout = plan_tile_layout(_config())
    path = write_tile_layout(layout, tmp_path / "tile-layout.json")

    assert path.read_bytes() == canonical_tile_layout_bytes(layout)
    assert read_tile_layout(path) == layout
    with pytest.raises(ConfigurationError, match="already exists"):
        write_tile_layout(layout, path)


def test_layout_reopen_rejects_tampered_identity(tmp_path: Path) -> None:
    layout = plan_tile_layout(_config())
    path = write_tile_layout(layout, tmp_path / "tile-layout.json")
    payload = path.read_text().replace("tile-r0000-c0000", "tile-r9999-c9999", 1)
    path.write_text(payload)

    with pytest.raises(ConfigurationError, match="deterministic v1 identity"):
        read_tile_layout(path)


def test_layout_rejects_more_tiles_than_available_grid_cells() -> None:
    with pytest.raises(ConfigurationError, match="cannot partition"):
        plan_tile_layout(
            _config(
                source_grid_shape=(3, 3),
                model_width_mm=300.0,
                model_depth_mm=200.0,
                maximum_tile_width_mm=10.0,
                maximum_tile_depth_mm=10.0,
            )
        )
