"""Elevation raster ingestion, processing, AOI normalization, and fixtures."""

from topoforge.raster.aoi import normalize_area_of_interest, select_local_metric_crs
from topoforge.raster.processing import (
    ProcessedRaster,
    largest_true_rectangle,
    process_local_raster,
)
from topoforge.raster.sampling import (
    SamplingDecision,
    resolve_sampling_decision,
    triangle_count_for_shape,
)
from topoforge.raster.synthetic import SyntheticTerrain, create_synthetic_geotiff

__all__ = [
    "ProcessedRaster",
    "SamplingDecision",
    "SyntheticTerrain",
    "create_synthetic_geotiff",
    "largest_true_rectangle",
    "normalize_area_of_interest",
    "process_local_raster",
    "resolve_sampling_decision",
    "select_local_metric_crs",
    "triangle_count_for_shape",
]
