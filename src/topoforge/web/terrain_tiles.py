"""Mapzen Terrarium elevation tiles for a visual hillshade reference only."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from io import BytesIO
from urllib.request import Request, urlopen

from PIL import Image

from topoforge import __version__
from topoforge.web.reference_tiles import ReferenceTileDownload

TERRAIN_BASE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium"
TERRAIN_ATTRIBUTION_URL = "https://github.com/tilezen/joerd/blob/master/docs/attribution.md"
MAX_TERRAIN_TILE_BYTES = 512 * 1024
_download_slots = threading.BoundedSemaphore(4)


def fetch_terrain_tile(z: int, x: int, y: int) -> ReferenceTileDownload:
    """Fetch and verify one fixed-host RGB DEM tile, preserving bytes and source metadata."""
    if not 0 <= z <= 14 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise ValueError("Terrain tile coordinates are outside the supported range")
    url = f"{TERRAIN_BASE_URL}/{z}/{x}/{y}.png"
    request = Request(
        url,
        headers={
            "User-Agent": f"TopoForge/{__version__} (+https://github.com/yidaaaaa/TopoForge)",
            "Accept": "image/png",
        },
    )
    with _download_slots, urlopen(request, timeout=20) as response:
        if response.headers.get_content_type() != "image/png":
            raise ValueError("Terrain provider returned an unexpected content type")
        payload = response.read(MAX_TERRAIN_TILE_BYTES + 1)
        metadata = {
            "provider": "Mapzen Terrain Tiles / AWS Open Data",
            "dataset_version": "mutable elevation-tiles-prod Terrarium tiles",
            "dataset_type": "mixed elevation reference; not manufacturing input",
            "url": url,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "crs": "EPSG:3857",
            "encoding": "Terrarium RGB, elevation_m = R*256 + G + B/256 - 32768",
            "vertical_datum": "unknown",
            "native_resolution": "varies by source; tile pixel spacing is not native resolution",
            "acquisition_period": "unknown; Last-Modified is tile publication, not acquisition",
            "license_and_attribution": TERRAIN_ATTRIBUTION_URL,
            "nodata_and_interpolation": "publisher processed; fractions unknown",
        }
        for header in ("ETag", "Last-Modified", "x-amz-version-id", "x-amz-meta-x-imagery-sources"):
            value = response.headers.get(header)
            if value is not None:
                if len(value) > 8192:
                    raise ValueError("Terrain provider metadata exceeds the size limit")
                metadata[header.lower()] = value
    if len(payload) > MAX_TERRAIN_TILE_BYTES:
        raise ValueError("Terrain tile exceeds the download limit")
    # Reject HTML/proxy errors, unexpected dimensions, palette/color conversion and bad PNG CRCs.
    # Return original encoded RGB bytes: color correction would change the elevations.
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG" or image.size != (256, 256) or image.mode != "RGB":
                raise ValueError("Terrain tile must be an unmodified 256 x 256 RGB PNG")
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Terrain tile failed PNG verification; retry the provider") from exc
    return ReferenceTileDownload(payload, metadata)
