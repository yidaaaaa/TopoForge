"""Nominatim-compatible place search with explicit candidate selection."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlencode, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from topoforge.exceptions import ProviderFetchError
from topoforge.models import AreaOfInterestInput
from topoforge.providers.cache import CacheIdentity
from topoforge.providers.transport import CachingHttpClient


class NominatimConfig(BaseModel):
    """Endpoint and public-service usage-policy limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = "https://nominatim.openstreetmap.org"
    maximum_candidates: int = Field(default=10, ge=1, le=10)
    enforce_public_usage_policy: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Nominatim base_url must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class PlaceCandidate(BaseModel):
    """One candidate returned by a Nominatim-compatible search endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    display_name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    bounding_box_wgs84: tuple[float, float, float, float]
    category: str | None = None
    feature_type: str | None = None
    importance: float | None = None
    osm_type: str | None = None
    osm_id: str | None = None

    @field_validator("bounding_box_wgs84")
    @classmethod
    def validate_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        west, south, east, north = value
        if not all(math.isfinite(item) for item in value):
            raise ValueError("candidate bounding box values must be finite")
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("candidate bounding box longitudes must be within WGS84 bounds")
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("candidate bounding box latitudes must be within WGS84 bounds")
        if south >= north or west == east:
            raise ValueError("candidate bounding box must have non-zero width and height")
        return value


class PlaceSearchResult(BaseModel):
    """Stable candidate list plus cache/network and usage-policy evidence."""

    model_config = ConfigDict(extra="forbid")

    query: str
    candidates: list[PlaceCandidate]
    candidate_status: str
    request_url: str
    cache_status: str
    cache_lookup_reason: str
    response_sha256: str
    response_bytes: int
    etag: str | None = None
    last_modified: str | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    attribution: str
    usage_policy: list[str]


class PlaceCandidateSelectionError(ValueError):
    """A place query requires an explicit candidate decision."""

    def __init__(self, message: str, result: PlaceSearchResult) -> None:
        super().__init__(message)
        self.result = result


def _candidate_from_payload(item: object) -> PlaceCandidate:
    if not isinstance(item, dict):
        raise ProviderFetchError("Nominatim response candidate is not a JSON object")
    raw_bbox = item.get("boundingbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        raise ProviderFetchError("Nominatim candidate is missing a four-value boundingbox")
    try:
        south, north, west, east = (float(value) for value in raw_bbox)
        latitude = float(item["lat"])
        longitude = float(item["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderFetchError("Nominatim candidate coordinates are invalid") from exc
    candidate_id = item.get("place_id")
    display_name = item.get("display_name")
    if candidate_id is None or not isinstance(display_name, str) or not display_name.strip():
        raise ProviderFetchError("Nominatim candidate is missing place_id or display_name")
    raw_importance = item.get("importance")
    try:
        importance = float(raw_importance) if raw_importance is not None else None
    except (TypeError, ValueError):
        importance = None
    return PlaceCandidate(
        candidate_id=str(candidate_id),
        display_name=display_name,
        latitude=latitude,
        longitude=longitude,
        bounding_box_wgs84=(west, south, east, north),
        category=str(item["category"]) if item.get("category") is not None else None,
        feature_type=str(item["type"]) if item.get("type") is not None else None,
        importance=importance,
        osm_type=str(item["osm_type"]) if item.get("osm_type") is not None else None,
        osm_id=str(item["osm_id"]) if item.get("osm_id") is not None else None,
    )


class NominatimGeocoder:
    """Cache and parse bounded Nominatim-compatible candidate searches."""

    provider_id = "nominatim"

    def __init__(
        self,
        client: CachingHttpClient,
        config: NominatimConfig | None = None,
    ) -> None:
        self.client = client
        self.config = config or NominatimConfig()
        public_host = urlparse(self.config.base_url).hostname == "nominatim.openstreetmap.org"
        if (
            self.config.enforce_public_usage_policy
            and public_host
            and self.client.config.min_request_interval_seconds < 1.0
        ):
            raise ValueError("public Nominatim usage requires min_request_interval_seconds >= 1.0")

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        country_codes: list[str] | None = None,
        accept_language: str | None = None,
    ) -> PlaceSearchResult:
        """Return candidates; ambiguity is retained rather than silently resolved."""
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise ValueError("place query must contain non-whitespace characters")
        resolved_limit = limit or self.config.maximum_candidates
        if resolved_limit < 1 or resolved_limit > self.config.maximum_candidates:
            raise ValueError(
                f"candidate limit must be between 1 and {self.config.maximum_candidates}"
            )
        normalized_countries = sorted(
            {value.strip().lower() for value in (country_codes or []) if value.strip()}
        )
        parameters: list[tuple[str, str]] = [
            ("q", normalized_query),
            ("format", "jsonv2"),
            ("limit", str(resolved_limit)),
            ("addressdetails", "1"),
            ("dedupe", "1"),
            ("polygon_geojson", "0"),
        ]
        if normalized_countries:
            parameters.append(("countrycodes", ",".join(normalized_countries)))
        if accept_language is not None and accept_language.strip():
            parameters.append(("accept-language", accept_language.strip()))
        url = f"{self.config.base_url}/search?{urlencode(parameters)}"
        result = self.client.download(
            CacheIdentity(
                provider_id=self.provider_id,
                dataset_id="nominatim-search",
                dataset_version="jsonv2",
                url=url,
            )
        )
        try:
            payload = json.loads(result.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderFetchError("Nominatim response is not valid UTF-8 JSON") from exc
        if not isinstance(payload, list):
            raise ProviderFetchError("Nominatim search response root is not a JSON list")
        candidates = [_candidate_from_payload(item) for item in payload]
        status = "none" if not candidates else "unique" if len(candidates) == 1 else "ambiguous"
        return PlaceSearchResult(
            query=normalized_query,
            candidates=candidates,
            candidate_status=status,
            request_url=url,
            cache_status=result.cache_status.value,
            cache_lookup_reason=result.cache_lookup_reason,
            response_sha256=result.cache_entry.object_sha256,
            response_bytes=result.cache_entry.object_size_bytes,
            etag=result.cache_entry.etag,
            last_modified=result.cache_entry.last_modified,
            attempts=[item.model_dump(mode="json") for item in result.attempts],
            attribution="Geocoding © OpenStreetMap contributors",
            usage_policy=[
                "candidate search only; no client-side autocomplete",
                "queries are cached by canonical request URL",
                "public endpoint requests are limited to at most one per second",
                "ambiguous results require an explicit candidate id",
            ],
        )


def select_place_candidate(
    result: PlaceSearchResult,
    *,
    candidate_id: str | None = None,
) -> PlaceCandidate:
    """Select a unique result or require an explicit id for ambiguous candidates."""
    if not result.candidates:
        raise PlaceCandidateSelectionError(
            f"place query {result.query!r} returned no candidates",
            result,
        )
    if candidate_id is not None:
        matches = [item for item in result.candidates if item.candidate_id == candidate_id]
        if len(matches) != 1:
            raise PlaceCandidateSelectionError(
                f"candidate id {candidate_id!r} is not present in the returned candidates",
                result,
            )
        return matches[0]
    if len(result.candidates) != 1:
        raise PlaceCandidateSelectionError(
            f"place query {result.query!r} is ambiguous; choose a candidate id",
            result,
        )
    return result.candidates[0]


def place_candidate_aoi_input(
    result: PlaceSearchResult,
    candidate: PlaceCandidate,
) -> AreaOfInterestInput:
    """Create a network-free resolved place AOI that preserves the candidate decision."""
    return AreaOfInterestInput(
        place_query=result.query,
        place_candidate_id=candidate.candidate_id,
        place_display_name=candidate.display_name,
        resolved_place_bbox_wgs84=candidate.bounding_box_wgs84,
    )
