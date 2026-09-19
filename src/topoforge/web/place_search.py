"""Local Web adapter for explicit, cached Nominatim-compatible place searches."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from topoforge import __version__
from topoforge.exceptions import ConfigurationError
from topoforge.geocoding import NominatimConfig, NominatimGeocoder, PlaceCandidate
from topoforge.providers import CachingHttpClient, ContentAddressedCache, HttpTransportConfig

PUBLIC_GEOCODER = "https://nominatim.openstreetmap.org"
PUBLIC_POLICY = "https://operations.osmfoundation.org/policies/nominatim/"


class PlaceSearchRequest(BaseModel):
    """One submitted query; online/public service use must be explicit."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    language: Literal["zh-CN", "en"] = "zh-CN"
    cache_only: bool = True
    allow_public_service: bool = False

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """Reject empty/control text and canonicalize whitespace before caching."""
        if any(ord(char) < 32 for char in value):
            raise ValueError("Use a single-line place name or address")
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a place name or address")
        return value


class PlaceSearchResponse(BaseModel):
    """Bounded candidate coordinates and attribution, without transport internals."""

    query: str
    candidates: list[PlaceCandidate]
    cache_status: str
    attribution: str
    endpoint: str


class PlaceSearchBusyError(RuntimeError):
    """A second search must wait for the current explicit search to finish."""


class PublicSearchDisabledError(RuntimeError):
    """The user has not selected the public service for online searches."""


class WebPlaceSearch:
    """Reuse the core geocoder and a persistent cache with one shared rate limiter."""

    def __init__(self, state_dir: Path, *, endpoint: str = PUBLIC_GEOCODER) -> None:
        config = NominatimConfig(base_url=endpoint)
        parsed = urlparse(config.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError("Use a geocoder URL without credentials, query or fragment")
        self.endpoint = config.base_url
        self.is_public = (parsed.hostname or "").lower().rstrip(
            "."
        ) == "nominatim.openstreetmap.org"
        if self.is_public and (parsed.scheme != "https" or parsed.port not in (None, 443)):
            raise ConfigurationError("Use HTTPS for the public Nominatim service")
        self._lock = threading.Lock()
        self.geocoder = NominatimGeocoder(
            CachingHttpClient(
                ContentAddressedCache(state_dir / "place-search"),
                HttpTransportConfig(
                    timeout_seconds=10,
                    max_attempts=1,
                    min_request_interval_seconds=1.1,
                    max_download_bytes=256 * 1024,
                    chunk_size_bytes=16 * 1024,
                    user_agent=(
                        f"TopoForge/{__version__} local-place-search "
                        "(+https://github.com/yidaaaaa/TopoForge)"
                    ),
                ),
            ),
            config,
        )

    def search(self, request: PlaceSearchRequest) -> PlaceSearchResponse:
        """Serve one deliberate search, retaining ambiguity and cache-only semantics."""
        if not request.cache_only and self.is_public and not request.allow_public_service:
            raise PublicSearchDisabledError(
                "Select the public search service before searching online"
            )
        if not self._lock.acquire(blocking=False):
            raise PlaceSearchBusyError("A place search is running; try again after it finishes")
        try:
            result = self.geocoder.search(
                request.query,
                accept_language=request.language,
                cache_only=request.cache_only,
            )
            return PlaceSearchResponse(
                query=result.query,
                candidates=result.candidates,
                cache_status=result.cache_status,
                attribution=result.attribution,
                endpoint=self.endpoint,
            )
        finally:
            self._lock.release()
