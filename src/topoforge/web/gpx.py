"""Bounded local GPX map previews using the manufacturing engine's parser."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from topoforge.exceptions import ConfigurationError
from topoforge.overlays import OverlayFormat, OverlayKind
from topoforge.overlays.sources import parse_gpx_bytes
from topoforge.web.jobs import LocalJobManager
from topoforge.web.security import read_owned_regular_bytes, real_directory_tree_identity

MAX_GPX_BYTES = 8 * 1024 * 1024
MAX_GPX_POINTS = 50_000
MAX_GPX_SEGMENTS = 1_000
MAX_GPX_ELEMENTS = 150_000


class GpxPreviewRequest(BaseModel):
    """A selectable local GPX file or one exact input retained by a copied project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    reuse_source_job_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class GpxLineGeometry(BaseModel):
    """One unchanged WGS84 track segment; elevations are not map coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["LineString"] = "LineString"
    coordinates: tuple[tuple[float, float], ...] = Field(min_length=2)


class GpxPreviewProperties(BaseModel):
    """Measured input counts, without inferred licensing or terrain heights."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: str
    point_count: int = Field(ge=2)
    source_elevation_sample_count: int = Field(ge=0)
    source_elevation_min_m: float | None
    source_elevation_max_m: float | None


class GpxPreviewFeature(BaseModel):
    """A GeoJSON feature retaining the engine's segment identity and coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["Feature"] = "Feature"
    geometry: GpxLineGeometry
    properties: GpxPreviewProperties


class GpxPreviewCollection(BaseModel):
    """Complete track geometry; preview import never silently downsamples points."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: tuple[GpxPreviewFeature, ...] = Field(min_length=1)


class GpxPreviewResponse(BaseModel):
    """Checksum-bound GPX preview and measured WGS84 extent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["topoforge-gpx-preview-v1"] = "topoforge-gpx-preview-v1"
    path: str
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=MAX_GPX_BYTES)
    source_crs: Literal["EPSG:4326"] = "EPSG:4326"
    bounds_wgs84: tuple[float, float, float, float]
    segment_count: int = Field(ge=1, le=MAX_GPX_SEGMENTS)
    point_count: int = Field(ge=2, le=MAX_GPX_POINTS)
    geojson: GpxPreviewCollection


def _is_retained_gpx(
    manager: LocalJobManager,
    request: GpxPreviewRequest,
    path: Path,
) -> bool:
    if request.reuse_source_job_id is None:
        return False
    try:
        _, original = manager.read_request_for_reuse(request.reuse_source_job_id)
    except KeyError as exc:
        raise ConfigurationError(
            "original project is no longer available; "
            "select an existing project or a local GPX file"
        ) from exc
    overlay = original.launch.overlay
    return overlay is not None and any(
        source.kind is OverlayKind.GPX
        and source.format is OverlayFormat.GPX
        and source.path is not None
        and Path(os.path.abspath(source.path.expanduser())) == path
        for source in overlay.sources
    )


def preview_gpx(manager: LocalJobManager, request: GpxPreviewRequest) -> GpxPreviewResponse:
    """Read an exposed local GPX, enforcing byte/XML/geometry budgets before preview.

    This adapter has no network or job side effects. Metadata records the source
    bytes only; users must supply their actual license and attribution separately.
    """
    path = Path(os.path.abspath(request.path.expanduser()))
    if path.suffix.lower() != ".gpx":
        raise ConfigurationError("route preview requires a .gpx file; choose a GPX track")
    if not _is_retained_gpx(manager, request, path):
        listing = manager.list_files(path.parent)
        if not any(
            entry.kind == "file" and entry.selectable and Path(entry.path) == path
            for entry in listing.entries
        ):
            raise ConfigurationError(
                "GPX path is not an exposed selectable input file; choose a file in an input root"
            )
    try:
        parent_identity = real_directory_tree_identity(path.parent, context="GPX route parent")
        payload = read_owned_regular_bytes(
            path,
            root=path.parent,
            root_identity=parent_identity,
            context="GPX route",
            max_bytes=MAX_GPX_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"{exc}; choose a readable local GPX within 8 MiB") from exc
    parsed = parse_gpx_bytes(
        payload,
        source_name=str(path),
        max_points=MAX_GPX_POINTS,
        max_segments=MAX_GPX_SEGMENTS,
        max_elements=MAX_GPX_ELEMENTS,
    )
    features: list[GpxPreviewFeature] = []
    west, south, east, north = 180.0, 90.0, -180.0, -90.0
    point_count = 0
    for feature in parsed:
        coordinates = tuple((float(x), float(y)) for x, y in feature.geometry.coords)
        if not any(coordinate != coordinates[0] for coordinate in coordinates[1:]):
            raise ConfigurationError(
                "GPX contains a zero-length segment; remove repeated-only segments and reimport"
            )
        feature_west, feature_south, feature_east, feature_north = feature.geometry.bounds
        west, south = min(west, feature_west), min(south, feature_south)
        east, north = max(east, feature_east), max(north, feature_north)
        if east - west >= 180:
            raise ConfigurationError(
                "GPX crosses the antimeridian or spans 180 degrees; split it into local tracks"
            )
        point_count += len(coordinates)
        properties = GpxPreviewProperties(
            segment_id=feature.feature_id,
            **feature.properties,
        )
        features.append(
            GpxPreviewFeature(
                geometry=GpxLineGeometry(coordinates=coordinates),
                properties=properties,
            )
        )
    return GpxPreviewResponse(
        path=str(path),
        filename=path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        bounds_wgs84=(west, south, east, north),
        segment_count=len(features),
        point_count=point_count,
        geojson=GpxPreviewCollection(features=tuple(features)),
    )
