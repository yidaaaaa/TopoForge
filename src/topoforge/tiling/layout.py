"""Deterministic manufacturing tile layout and stable row/column identities."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError

_TILE_SCHEMA_VERSION = "topoforge-tile-layout-v1"
_TILE_ID_WIDTH = 4


class TileLayoutConfig(BaseModel):
    """Inputs that deterministically partition one processed terrain grid."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_grid_shape: tuple[int, int]
    model_width_mm: float = Field(gt=0)
    model_depth_mm: float = Field(gt=0)
    maximum_tile_width_mm: float = Field(gt=0)
    maximum_tile_depth_mm: float = Field(gt=0)
    overlap_cells: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        """Require a two-dimensional sample grid with at least one cell per axis."""
        rows, columns = self.source_grid_shape
        if rows < 2 or columns < 2:
            raise ValueError("source_grid_shape must contain at least 2 x 2 samples")
        return self


class GridWindow(BaseModel):
    """Half-open row/column window in either grid cells or grid samples."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row_start: int = Field(ge=0)
    row_stop: int = Field(gt=0)
    column_start: int = Field(ge=0)
    column_stop: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        """Reject empty or inverted half-open windows."""
        if self.row_stop <= self.row_start or self.column_stop <= self.column_start:
            raise ValueError("grid window stops must exceed starts")
        return self


class PhysicalBoundsMm(BaseModel):
    """Core tile bounds in the overall +X East/+Y North model frame."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x_min: float = Field(ge=0)
    y_min: float = Field(ge=0)
    x_max: float = Field(gt=0)
    y_max: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        """Reject empty physical bounds."""
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("physical bounds maxima must exceed minima")
        return self


class TerrainTile(BaseModel):
    """One stable terrain tile and its core/overlap sampling windows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_id: str
    tile_key: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    core_cell_window: GridWindow
    core_sample_window: GridWindow
    sampling_window: GridWindow
    physical_bounds_mm: PhysicalBoundsMm
    north_neighbor: str | None = None
    south_neighbor: str | None = None
    west_neighbor: str | None = None
    east_neighbor: str | None = None
    touches_north_edge: bool
    touches_south_edge: bool
    touches_west_edge: bool
    touches_east_edge: bool


class TileLayout(BaseModel):
    """Versioned deterministic layout contract for later tile artifacts and assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_SCHEMA_VERSION
    layout_id: str
    source_grid_shape: tuple[int, int]
    model_size_mm: tuple[float, float]
    maximum_tile_size_mm: tuple[float, float]
    overlap_cells: int = Field(ge=0)
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    row_origin: str = "north"
    column_origin: str = "west"
    east_axis: str = "+X = East"
    north_axis: str = "+Y = North"
    core_partition: str = "half-open cell windows; adjacent core sample windows share one seam"
    sampling_overlap: str = "sampling_window adds overlap_cells around each core cell window"
    tiles: list[TerrainTile]


def _canonical_float(value: float) -> str:
    return format(value, ".12g")


def _layout_id(config: TileLayoutConfig, tile_grid_shape: tuple[int, int]) -> str:
    payload = {
        "schema_version": _TILE_SCHEMA_VERSION,
        "source_grid_shape": list(config.source_grid_shape),
        "model_width_mm": _canonical_float(config.model_width_mm),
        "model_depth_mm": _canonical_float(config.model_depth_mm),
        "maximum_tile_width_mm": _canonical_float(config.maximum_tile_width_mm),
        "maximum_tile_depth_mm": _canonical_float(config.maximum_tile_depth_mm),
        "overlap_cells": config.overlap_cells,
        "tile_grid_shape": list(tile_grid_shape),
        "row_origin": "north",
        "column_origin": "west",
        "east_axis": "+X = East",
        "north_axis": "+Y = North",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"layout-{hashlib.sha256(canonical).hexdigest()[:20]}"


def _tile_id(row: int, column: int) -> str:
    if row >= 10**_TILE_ID_WIDTH or column >= 10**_TILE_ID_WIDTH:
        raise ConfigurationError("tile row/column count exceeds the v1 four-digit tile id range")
    return f"tile-r{row:0{_TILE_ID_WIDTH}d}-c{column:0{_TILE_ID_WIDTH}d}"


def _partition_cells(cell_count: int, part_count: int) -> list[tuple[int, int]]:
    if part_count > cell_count:
        raise ConfigurationError(
            f"cannot partition {cell_count} grid cells into {part_count} non-empty tiles; "
            "increase maximum tile size or use a higher-resolution processed grid"
        )
    base, remainder = divmod(cell_count, part_count)
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(part_count):
        stop = start + base + (1 if index < remainder else 0)
        ranges.append((start, stop))
        start = stop
    return ranges


def _sample_window(
    core_row_start: int,
    core_row_stop: int,
    core_column_start: int,
    core_column_stop: int,
    *,
    total_rows: int,
    total_columns: int,
    overlap_cells: int,
) -> GridWindow:
    return GridWindow(
        row_start=max(0, core_row_start - overlap_cells),
        row_stop=min(total_rows, core_row_stop + overlap_cells + 1),
        column_start=max(0, core_column_start - overlap_cells),
        column_stop=min(total_columns, core_column_stop + overlap_cells + 1),
    )


def plan_tile_layout(config: TileLayoutConfig) -> TileLayout:
    """Partition processed grid cells into stable north-to-south/west-to-east tiles."""
    total_rows, total_columns = config.source_grid_shape
    row_cells = total_rows - 1
    column_cells = total_columns - 1
    tile_rows = max(1, math.ceil(config.model_depth_mm / config.maximum_tile_depth_mm))
    tile_columns = max(1, math.ceil(config.model_width_mm / config.maximum_tile_width_mm))
    row_ranges = _partition_cells(row_cells, tile_rows)
    column_ranges = _partition_cells(column_cells, tile_columns)
    grid_shape = (tile_rows, tile_columns)
    layout_id = _layout_id(config, grid_shape)
    tiles: list[TerrainTile] = []

    for row, (row_start, row_stop) in enumerate(row_ranges):
        for column, (column_start, column_stop) in enumerate(column_ranges):
            identifier = _tile_id(row, column)
            x_min = config.model_width_mm * column_start / column_cells
            x_max = config.model_width_mm * column_stop / column_cells
            y_max = config.model_depth_mm * (1.0 - row_start / row_cells)
            y_min = config.model_depth_mm * (1.0 - row_stop / row_cells)
            tiles.append(
                TerrainTile(
                    tile_id=identifier,
                    tile_key=f"{layout_id}/{identifier}",
                    row=row,
                    column=column,
                    core_cell_window=GridWindow(
                        row_start=row_start,
                        row_stop=row_stop,
                        column_start=column_start,
                        column_stop=column_stop,
                    ),
                    core_sample_window=GridWindow(
                        row_start=row_start,
                        row_stop=row_stop + 1,
                        column_start=column_start,
                        column_stop=column_stop + 1,
                    ),
                    sampling_window=_sample_window(
                        row_start,
                        row_stop,
                        column_start,
                        column_stop,
                        total_rows=total_rows,
                        total_columns=total_columns,
                        overlap_cells=config.overlap_cells,
                    ),
                    physical_bounds_mm=PhysicalBoundsMm(
                        x_min=x_min,
                        y_min=y_min,
                        x_max=x_max,
                        y_max=y_max,
                    ),
                    north_neighbor=_tile_id(row - 1, column) if row > 0 else None,
                    south_neighbor=_tile_id(row + 1, column) if row + 1 < tile_rows else None,
                    west_neighbor=_tile_id(row, column - 1) if column > 0 else None,
                    east_neighbor=(
                        _tile_id(row, column + 1) if column + 1 < tile_columns else None
                    ),
                    touches_north_edge=row == 0,
                    touches_south_edge=row + 1 == tile_rows,
                    touches_west_edge=column == 0,
                    touches_east_edge=column + 1 == tile_columns,
                )
            )

    return TileLayout(
        layout_id=layout_id,
        source_grid_shape=config.source_grid_shape,
        model_size_mm=(config.model_width_mm, config.model_depth_mm),
        maximum_tile_size_mm=(
            config.maximum_tile_width_mm,
            config.maximum_tile_depth_mm,
        ),
        overlap_cells=config.overlap_cells,
        tile_grid_shape=grid_shape,
        tile_count=len(tiles),
        tiles=tiles,
    )


def canonical_tile_layout_bytes(layout: TileLayout) -> bytes:
    """Serialize a tile layout with stable key ordering and separators."""
    return (
        json.dumps(
            layout.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def read_tile_layout(path: Path) -> TileLayout:
    """Strictly reopen a layout and verify its deterministic identity."""
    layout = TileLayout.model_validate_json(path.read_text(encoding="utf-8"))
    config = TileLayoutConfig(
        source_grid_shape=layout.source_grid_shape,
        model_width_mm=layout.model_size_mm[0],
        model_depth_mm=layout.model_size_mm[1],
        maximum_tile_width_mm=layout.maximum_tile_size_mm[0],
        maximum_tile_depth_mm=layout.maximum_tile_size_mm[1],
        overlap_cells=layout.overlap_cells,
    )
    expected = plan_tile_layout(config)
    if layout != expected:
        raise ConfigurationError("tile layout content does not match its deterministic v1 identity")
    return layout


def write_tile_layout(layout: TileLayout, path: Path) -> Path:
    """Atomically publish and strictly reopen a new deterministic tile-layout JSON."""
    destination = path.expanduser().resolve()
    if destination.exists():
        raise ConfigurationError(f"tile layout destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(canonical_tile_layout_bytes(layout))
        handle.flush()
    try:
        temporary.replace(destination)
        reopened = read_tile_layout(destination)
        if canonical_tile_layout_bytes(reopened) != destination.read_bytes():
            raise ConfigurationError("reopened tile layout bytes are not canonical")
    except BaseException:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return destination
