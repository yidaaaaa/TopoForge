"""Deterministic WGS84 AOI normalization and local-raster window selection."""

from __future__ import annotations

import math
from typing import Any

from pyproj import CRS, Geod
from shapely.geometry import MultiPolygon, box, mapping
from shapely.geometry.base import BaseGeometry

from topoforge.models import AreaOfInterest, AreaOfInterestInput

_GEOD = Geod(ellps="WGS84")


def _validated_longitude(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < -180.0 or converted > 180.0:
        raise ValueError(f"{name} must be finite and between -180 and 180 degrees")
    return converted


def _validated_latitude(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < -90.0 or converted > 90.0:
        raise ValueError(f"{name} must be finite and between -90 and 90 degrees")
    return converted


def _geometry_for_bounds(
    west: float, south: float, east: float, north: float
) -> tuple[BaseGeometry, bool]:
    crosses = west > east
    if crosses:
        return MultiPolygon((box(west, south, 180.0, north), box(-180.0, south, east, north))), True
    return box(west, south, east, north), False


def _central_longitude(west: float, east: float, crosses: bool) -> float:
    unwrapped_east = east + 360.0 if crosses else east
    center = (west + unwrapped_east) / 2.0
    return ((center + 180.0) % 360.0) - 180.0


def _utm_zone(longitude: float) -> int:
    return min(60, max(1, int((longitude + 180.0) // 6.0) + 1))


def select_local_metric_crs(
    bounds_wgs84: tuple[float, float, float, float], *, crosses_antimeridian: bool
) -> CRS:
    """Select UTM only for one-zone mid-latitude AOIs; otherwise use local AEQD."""
    west, south, east, north = bounds_wgs84
    center_lon = _central_longitude(west, east, crosses_antimeridian)
    center_lat = (south + north) / 2.0
    same_utm_zone = not crosses_antimeridian and _utm_zone(west) == _utm_zone(
        math.nextafter(east, west)
    )
    if -80.0 <= center_lat <= 84.0 and same_utm_zone:
        epsg = (32600 if center_lat >= 0 else 32700) + _utm_zone(center_lon)
        return CRS.from_epsg(epsg)
    return CRS.from_proj4(
        f"+proj=aeqd +lat_0={center_lat:.10f} +lon_0={center_lon:.10f} "
        "+datum=WGS84 +units=m +no_defs"
    )


def _geometry_area_m2(geometry: BaseGeometry) -> float:
    area_m2, _ = _GEOD.geometry_area_perimeter(geometry)
    return abs(float(area_m2))


def _minimal_longitude_bounds(longitudes: list[float]) -> tuple[float, float, bool]:
    normalized = sorted((longitude + 360.0) % 360.0 for longitude in longitudes)
    gaps = [
        ((normalized[(index + 1) % len(normalized)] - normalized[index]) % 360.0, index)
        for index in range(len(normalized))
    ]
    _, gap_index = max(gaps)
    start = normalized[(gap_index + 1) % len(normalized)]
    end = normalized[gap_index]
    west = ((start + 180.0) % 360.0) - 180.0
    east = ((end + 180.0) % 360.0) - 180.0
    return west, east, west > east


def normalize_area_of_interest(request: AreaOfInterestInput) -> AreaOfInterest:
    """Normalize a bbox or center-radius request into explicit WGS84 bounds/geometry."""
    if request.bbox_wgs84 is not None or request.resolved_place_bbox_wgs84 is not None:
        raw_bounds = (
            request.bbox_wgs84
            if request.bbox_wgs84 is not None
            else request.resolved_place_bbox_wgs84
        )
        assert raw_bounds is not None
        west, south, east, north = raw_bounds
        west = _validated_longitude(west, "bbox west")
        east = _validated_longitude(east, "bbox east")
        south = _validated_latitude(south, "bbox south")
        north = _validated_latitude(north, "bbox north")
        if west == east:
            raise ValueError("bbox west and east must differ")
        if south >= north:
            raise ValueError("bbox south must be below bbox north")
        geometry, crosses = _geometry_for_bounds(west, south, east, north)
        bounds = (west, south, east, north)
        if request.bbox_wgs84 is not None:
            kind = "bbox"
            user_input: dict[str, object] = {"bbox_wgs84": list(request.bbox_wgs84)}
            method = "validated WGS84 bbox; antimeridian split when west > east"
        else:
            kind = "place"
            assert request.resolved_place_bbox_wgs84 is not None
            user_input = {
                "place_query": str(request.place_query),
                "place_candidate_id": str(request.place_candidate_id),
                "place_display_name": str(request.place_display_name),
                "resolved_place_bbox_wgs84": list(request.resolved_place_bbox_wgs84),
            }
            method = (
                "explicit Nominatim-compatible candidate bbox; antimeridian split when west > east"
            )
    else:
        assert request.center_wgs84 is not None and request.radius_m is not None
        longitude = _validated_longitude(request.center_wgs84[0], "center longitude")
        latitude = _validated_latitude(request.center_wgs84[1], "center latitude")
        radius_m = float(request.radius_m)
        bearings = tuple(float(value) for value in range(0, 360, 5))
        points = [_GEOD.fwd(longitude, latitude, bearing, radius_m)[:2] for bearing in bearings]
        west, east, crosses = _minimal_longitude_bounds([point[0] for point in points])
        south = max(-90.0, min(point[1] for point in points))
        north = min(90.0, max(point[1] for point in points))
        geometry, crosses = _geometry_for_bounds(west, south, east, north)
        bounds = (west, south, east, north)
        kind = "center-radius"
        user_input = {
            "center_wgs84": [longitude, latitude],
            "radius_m": radius_m,
        }
        method = "geodesic 5-degree bearing envelope normalized to explicit WGS84 pixel-crop bounds"
    target_crs = select_local_metric_crs(bounds, crosses_antimeridian=crosses)
    normalized_geometry = dict(mapping(geometry))
    return AreaOfInterest(
        kind=kind,
        user_input=user_input,
        normalized_geometry_geojson=normalized_geometry,
        bounds_wgs84=bounds,
        target_local_crs=target_crs.to_string(),
        crosses_antimeridian=crosses,
        area_m2=_geometry_area_m2(geometry),
        normalization_method=method,
    )


def aoi_provenance(aoi: AreaOfInterest) -> dict[str, Any]:
    """Return the stable JSON representation used by raster/build reports."""
    return aoi.model_dump(mode="json")
