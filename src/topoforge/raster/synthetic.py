"""Deterministic analytic DEM fixtures; these never represent real terrain."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio
from rasterio.transform import from_origin


class SyntheticTerrain(StrEnum):
    """Available closed-form elevation surfaces."""

    FLAT = "flat"
    SLOPE = "slope"
    PYRAMID = "pyramid"
    GAUSSIAN_HILL = "gaussian-hill"
    GAUSSIAN_VALLEY = "gaussian-valley"
    SADDLE = "saddle"
    STEP = "step"
    RADIAL_CONE = "radial-cone"
    COASTLINE = "coastline"
    NODATA_HOLE = "nodata-hole"
    MULTIPLE_NODATA_HOLES = "multiple-nodata-holes"


def synthetic_elevations(
    terrain: SyntheticTerrain,
    rows: int = 64,
    columns: int = 80,
) -> npt.NDArray[np.float32]:
    """Evaluate an analytic surface on a normalized rectangular grid."""
    if rows < 2 or columns < 2:
        msg = "Synthetic DEM dimensions must both be at least 2"
        raise ValueError(msg)
    y, x = np.mgrid[-1.0 : 1.0 : complex(rows), -1.0 : 1.0 : complex(columns)]
    if terrain is SyntheticTerrain.FLAT:
        z = np.full_like(x, 125.0)
    elif terrain is SyntheticTerrain.SLOPE:
        z = 100.0 + 80.0 * (x + 1.0) / 2.0 + 25.0 * (1.0 - y) / 2.0
    elif terrain is SyntheticTerrain.PYRAMID:
        z = 100.0 + 220.0 * np.maximum(0.0, 1.0 - np.maximum(np.abs(x), np.abs(y)))
    elif (
        terrain in {SyntheticTerrain.GAUSSIAN_HILL, SyntheticTerrain.NODATA_HOLE}
        or terrain is SyntheticTerrain.MULTIPLE_NODATA_HOLES
    ):
        z = 90.0 + 650.0 * np.exp(-4.2 * (x**2 + 1.25 * y**2))
    elif terrain is SyntheticTerrain.GAUSSIAN_VALLEY:
        z = 420.0 - 260.0 * np.exp(-5.0 * (x**2 + y**2))
    elif terrain is SyntheticTerrain.SADDLE:
        z = 240.0 + 120.0 * (x**2 - y**2)
    elif terrain is SyntheticTerrain.STEP:
        z = np.where(x < 0.0, 100.0, 260.0)
    elif terrain is SyntheticTerrain.RADIAL_CONE:
        z = 100.0 + 300.0 * np.maximum(0.0, 1.0 - np.sqrt(x**2 + y**2))
    elif terrain is SyntheticTerrain.COASTLINE:
        z = 12.0 + 65.0 * x + 18.0 * np.sin(4.0 * y)
    else:  # pragma: no cover - exhaustive enum protection
        msg = f"Unsupported synthetic terrain: {terrain}"
        raise ValueError(msg)

    result = z.astype(np.float32)
    if terrain is SyntheticTerrain.NODATA_HOLE:
        row_mid, col_mid = rows // 2, columns // 2
        result[row_mid - 2 : row_mid + 2, col_mid - 2 : col_mid + 2] = np.nan
    elif terrain is SyntheticTerrain.MULTIPLE_NODATA_HOLES:
        result[rows // 3 : rows // 3 + 2, columns // 3 : columns // 3 + 2] = np.nan
        result[2 * rows // 3 : 2 * rows // 3 + 2, 2 * columns // 3 : 2 * columns // 3 + 2] = np.nan
    return result


def create_synthetic_geotiff(
    path: Path,
    terrain: SyntheticTerrain = SyntheticTerrain.GAUSSIAN_HILL,
    rows: int = 64,
    columns: int = 80,
    pixel_size_m: float = 10.0,
    crs: str = "EPSG:32648",
) -> Path:
    """Write a deterministic metric GeoTIFF fixture and return its path."""
    if pixel_size_m <= 0:
        msg = "pixel_size_m must be positive"
        raise ValueError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)
    elevations = synthetic_elevations(terrain, rows, columns)
    transform = from_origin(500_000.0, 3_300_000.0, pixel_size_m, pixel_size_m)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=columns,
        height=rows,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=np.nan,
        compress="deflate",
        predictor=3,
    ) as dataset:
        dataset.write(elevations, 1)
        dataset.update_tags(
            TOPOFORGE_FIXTURE="true",
            TERRAIN_TYPE="dtm",
            VERTICAL_CRS="synthetic-local-metre",
            VERTICAL_DATUM="synthetic-local-zero",
            LICENSE="Apache-2.0 synthetic fixture",
        )
    return path
