"""Local GPX and GeoJSON parsing for overlay sources."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.base import BaseGeometry

from topoforge.exceptions import ConfigurationError
from topoforge.overlays.models import OverlayFormat, OverlayKind, OverlaySourceConfig


@dataclass(frozen=True, slots=True)
class ParsedOverlayFeature:
    """One stable parsed feature before CRS transformation and clipping."""

    feature_id: str
    geometry: BaseGeometry
    properties: dict[str, Any]
    label: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _finite_coordinates(geometry: BaseGeometry) -> bool:
    bounds = geometry.bounds
    return len(bounds) == 4 and all(math.isfinite(float(value)) for value in bounds)


def _line_parts(geometry: BaseGeometry) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, Polygon):
        return [
            LineString(geometry.exterior.coords),
            *(LineString(ring.coords) for ring in geometry.interiors),
        ]
    if isinstance(geometry, MultiPolygon):
        parts: list[LineString] = []
        for polygon in geometry.geoms:
            parts.extend(_line_parts(polygon))
        return parts
    if isinstance(geometry, GeometryCollection):
        parts = []
        for child in geometry.geoms:
            parts.extend(_line_parts(child))
        return parts
    return []


def _point_parts(geometry: BaseGeometry) -> list[Point]:
    if isinstance(geometry, Point):
        return [geometry]
    if isinstance(geometry, MultiPoint):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        points: list[Point] = []
        for child in geometry.geoms:
            points.extend(_point_parts(child))
        return points
    return []


def _json_properties(value: object, *, feature_id: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"GeoJSON feature {feature_id} properties must be an object")
    try:
        normalized = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"GeoJSON feature {feature_id} properties are not JSON serializable"
        ) from exc
    if not isinstance(normalized, dict):
        raise AssertionError("normalized GeoJSON properties stopped being an object")
    return normalized


def parse_geojson_source(source: OverlaySourceConfig) -> tuple[ParsedOverlayFeature, ...]:
    """Parse one local GeoJSON source with explicit semantic geometry checks."""
    if source.path is None:
        raise AssertionError("validated GeoJSON source path disappeared")
    path = source.path.expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"overlay GeoJSON is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"overlay GeoJSON root must be an object: {path}")
    embedded_crs = payload.get("crs")
    if embedded_crs is not None:
        if not isinstance(embedded_crs, dict):
            raise ConfigurationError("GeoJSON crs member must be an object when present")
        properties = embedded_crs.get("properties")
        embedded_name = properties.get("name") if isinstance(properties, dict) else None
        if isinstance(embedded_name, str) and embedded_name.upper() != source.source_crs.upper():
            raise ConfigurationError(
                f"GeoJSON embedded CRS {embedded_name} conflicts with configured "
                f"{source.source_crs}"
            )
    if payload.get("type") == "FeatureCollection":
        raw_features = payload.get("features")
        if not isinstance(raw_features, list):
            raise ConfigurationError("GeoJSON FeatureCollection features must be a list")
    elif payload.get("type") == "Feature":
        raw_features = [payload]
    else:
        raw_features = [{"type": "Feature", "geometry": payload, "properties": {}}]

    features: list[ParsedOverlayFeature] = []
    for index, raw in enumerate(raw_features):
        if not isinstance(raw, dict) or raw.get("type") != "Feature":
            raise ConfigurationError(f"GeoJSON feature {index} is not a Feature object")
        feature_id = str(raw.get("id", f"feature-{index:06d}"))
        raw_geometry = raw.get("geometry")
        if not isinstance(raw_geometry, dict):
            raise ConfigurationError(f"GeoJSON feature {feature_id} has no geometry object")
        try:
            geometry = shape(raw_geometry)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"GeoJSON feature {feature_id} geometry is invalid") from exc
        if geometry.is_empty or not _finite_coordinates(geometry):
            raise ConfigurationError(
                f"GeoJSON feature {feature_id} geometry is empty or non-finite"
            )
        properties = _json_properties(raw.get("properties"), feature_id=feature_id)
        if source.kind is OverlayKind.LABEL:
            points = _point_parts(geometry)
            if not points:
                raise ConfigurationError(
                    f"label source feature {feature_id} must contain Point geometry"
                )
            label_value = properties.get(source.label_property)
            if not isinstance(label_value, str) or not label_value.strip():
                raise ConfigurationError(
                    f"label source feature {feature_id} requires non-empty property "
                    f"{source.label_property!r}"
                )
            for part_index, point in enumerate(points):
                features.append(
                    ParsedOverlayFeature(
                        feature_id=f"{feature_id}-p{part_index:04d}",
                        geometry=point,
                        properties=properties,
                        label=label_value.strip(),
                    )
                )
            continue
        lines = _line_parts(geometry)
        if not lines:
            raise ConfigurationError(
                f"{source.kind.value} source feature {feature_id} must contain line or "
                "polygon-boundary geometry"
            )
        for part_index, line in enumerate(lines):
            if len(line.coords) < 2 or line.length <= 0:
                continue
            features.append(
                ParsedOverlayFeature(
                    feature_id=f"{feature_id}-l{part_index:04d}",
                    geometry=line,
                    properties=properties,
                )
            )
    if not features:
        raise ConfigurationError(f"overlay GeoJSON contains no usable features: {path}")
    return tuple(features)


def _gpx_point(element: ET.Element) -> tuple[float, float, float | None]:
    try:
        longitude = float(element.attrib["lon"])
        latitude = float(element.attrib["lat"])
    except (KeyError, ValueError) as exc:
        raise ConfigurationError("GPX point requires finite numeric lat/lon attributes") from exc
    if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
        raise ConfigurationError(f"GPX point is outside WGS84 bounds: {longitude}, {latitude}")
    elevation: float | None = None
    for child in element:
        if _local_name(child.tag) == "ele" and child.text is not None:
            try:
                elevation = float(child.text)
            except ValueError as exc:
                raise ConfigurationError("GPX elevation must be numeric when present") from exc
            break
    if not all(math.isfinite(value) for value in (longitude, latitude)) or (
        elevation is not None and not math.isfinite(elevation)
    ):
        raise ConfigurationError("GPX point contains a non-finite coordinate")
    return longitude, latitude, elevation


def parse_gpx_source(source: OverlaySourceConfig) -> tuple[ParsedOverlayFeature, ...]:
    """Parse GPX tracks and routes without using optional network-aware libraries."""
    if source.path is None:
        raise AssertionError("validated GPX source path disappeared")
    path = source.path.expanduser().resolve()
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ConfigurationError(f"overlay GPX is unreadable: {path}") from exc
    if _local_name(root.tag) != "gpx":
        raise ConfigurationError(f"overlay GPX root element is not <gpx>: {path}")
    features: list[ParsedOverlayFeature] = []
    segment_index = 0
    for element in root.iter():
        local = _local_name(element.tag)
        if local not in {"trkseg", "rte"}:
            continue
        expected_point = "trkpt" if local == "trkseg" else "rtept"
        coordinates: list[tuple[float, float]] = []
        elevations: list[float] = []
        for child in element:
            if _local_name(child.tag) != expected_point:
                continue
            longitude, latitude, elevation = _gpx_point(child)
            coordinates.append((longitude, latitude))
            if elevation is not None:
                elevations.append(elevation)
        if len(coordinates) < 2:
            continue
        features.append(
            ParsedOverlayFeature(
                feature_id=f"gpx-segment-{segment_index:06d}",
                geometry=LineString(coordinates),
                properties={
                    "point_count": len(coordinates),
                    "source_elevation_sample_count": len(elevations),
                    "source_elevation_min_m": min(elevations) if elevations else None,
                    "source_elevation_max_m": max(elevations) if elevations else None,
                },
            )
        )
        segment_index += 1
    if not features:
        raise ConfigurationError(f"overlay GPX contains no track/route with two points: {path}")
    return tuple(features)


def parse_local_source(source: OverlaySourceConfig) -> tuple[ParsedOverlayFeature, ...]:
    """Dispatch one validated local source to its structured parser."""
    if source.format is OverlayFormat.GPX:
        return parse_gpx_source(source)
    if source.format is OverlayFormat.GEOJSON:
        return parse_geojson_source(source)
    raise ValueError("generated contours are derived from the verified processed DEM")
