"""Content identities for the local files used by one elevation GeoTIFF."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import rasterio
from rasterio.errors import RasterioError

from topoforge.exceptions import ConfigurationError
from topoforge.platforms import stat_result_is_link_like
from topoforge.util import sha256_file


def raster_dependency_records(path: Path) -> dict[str, dict[str, int | str]]:
    """Hash declared sibling sidecars without replacing the main raster's identity.

    Names are retained so that a relocated backup can restore GDAL's association
    between the TIFF and its masks, georeferencing, metadata and overviews.
    Non-local or non-GeoTIFF dependency graphs require a self-contained export.
    """
    source = path.expanduser().resolve()
    try:
        with rasterio.open(source) as dataset:
            names = dataset.files
            driver = dataset.driver
    except (OSError, RasterioError) as exc:
        raise ConfigurationError(
            f"cannot inventory raster inputs: {source}; provide a readable local DEM"
        ) from exc
    result: dict[str, dict[str, int | str]] = {}
    for name in sorted(set(names)):
        dependency = Path(os.path.abspath(name))
        if dependency == source:
            continue
        if driver != "GTiff" or dependency.parent != source.parent:
            raise ConfigurationError(
                f"raster dependency is not a GeoTIFF sibling: {name}; "
                "export a self-contained GeoTIFF before building"
            )
        try:
            metadata = dependency.lstat()
        except OSError as exc:
            raise ConfigurationError(
                f"raster dependency is unavailable: {dependency}; restore the complete dataset"
            ) from exc
        if (
            stat_result_is_link_like(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ConfigurationError(
                f"raster dependency must be an ordinary single-link file: {dependency}; "
                "export a self-contained GeoTIFF before building"
            )
        result[dependency.name] = {
            "size_bytes": metadata.st_size,
            "sha256": sha256_file(dependency),
        }
    return result
