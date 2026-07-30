"""Elevation raster ingestion, processing, and deterministic fixtures."""

from topoforge.raster.processing import ProcessedRaster, process_local_raster
from topoforge.raster.synthetic import SyntheticTerrain, create_synthetic_geotiff

__all__ = [
    "ProcessedRaster",
    "SyntheticTerrain",
    "create_synthetic_geotiff",
    "process_local_raster",
]
