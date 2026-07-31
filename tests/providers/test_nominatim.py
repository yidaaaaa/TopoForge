from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from typing import Any

import pytest

from topoforge.geocoding import (
    NominatimConfig,
    NominatimGeocoder,
    PlaceCandidateSelectionError,
    place_candidate_aoi_input,
    select_place_candidate,
)
from topoforge.providers import CachingHttpClient, ContentAddressedCache, HttpTransportConfig
from topoforge.raster import normalize_area_of_interest


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.status = 200
        self._stream = BytesIO(payload)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(payload))
        self.headers["Content-Type"] = "application/json"
        self.headers["ETag"] = '"geocode-fixture"'

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def close(self) -> None:
        pass


def candidate(
    place_id: int,
    display_name: str,
    *,
    south: float,
    north: float,
    west: float,
    east: float,
) -> dict[str, Any]:
    return {
        "place_id": place_id,
        "display_name": display_name,
        "lat": str((south + north) / 2),
        "lon": str((west + east) / 2),
        "boundingbox": [str(south), str(north), str(west), str(east)],
        "category": "natural",
        "type": "peak",
        "importance": 0.75,
        "osm_type": "node",
        "osm_id": 1000 + place_id,
    }


def geocoder(
    tmp_path: Any,
    payload: object,
    *,
    base_url: str = "https://nominatim.openstreetmap.org",
    interval: float = 1.0,
) -> tuple[NominatimGeocoder, list[str]]:
    calls: list[str] = []

    def opener(request: object, _timeout: float) -> FakeResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return FakeResponse(json.dumps(payload).encode())

    client = CachingHttpClient(
        ContentAddressedCache(tmp_path / "cache"),
        HttpTransportConfig(
            timeout_seconds=1,
            max_attempts=1,
            backoff_base_seconds=0,
            max_backoff_seconds=0,
            min_request_interval_seconds=interval,
            max_download_bytes=100_000,
        ),
        open_url=opener,  # type: ignore[arg-type]
    )
    return (
        NominatimGeocoder(
            client,
            NominatimConfig(base_url=base_url),
        ),
        calls,
    )


def test_search_returns_ambiguous_candidates_without_silent_selection(tmp_path: Any) -> None:
    instance, calls = geocoder(
        tmp_path,
        [
            candidate(11, "Peak, Region A", south=29.1, north=29.2, west=101.1, east=101.2),
            candidate(22, "Peak, Region B", south=30.1, north=30.2, west=102.1, east=102.2),
        ],
    )

    result = instance.search(
        "  Peak   Name ",
        limit=5,
        country_codes=["CN", "cn"],
        accept_language="en",
    )

    assert result.query == "Peak Name"
    assert result.candidate_status == "ambiguous"
    assert [item.candidate_id for item in result.candidates] == ["11", "22"]
    assert len(calls) == 1
    assert "countrycodes=cn" in result.request_url
    assert "accept-language=en" in result.request_url
    with pytest.raises(PlaceCandidateSelectionError, match="ambiguous") as caught:
        select_place_candidate(result)
    assert caught.value.result == result
    assert select_place_candidate(result, candidate_id="22").display_name == "Peak, Region B"


def test_unique_candidate_resolves_to_recorded_place_aoi(tmp_path: Any) -> None:
    instance, _ = geocoder(
        tmp_path,
        [candidate(42, "Unique Peak", south=-3.2, north=-3.1, west=-60.2, east=-60.1)],
    )

    result = instance.search("Unique Peak")
    selected = select_place_candidate(result)
    request = place_candidate_aoi_input(result, selected)
    normalized = normalize_area_of_interest(request)

    assert result.candidate_status == "unique"
    assert normalized.kind == "place"
    assert normalized.bounds_wgs84 == (-60.2, -3.2, -60.1, -3.1)
    assert normalized.user_input == {
        "place_query": "Unique Peak",
        "place_candidate_id": "42",
        "place_display_name": "Unique Peak",
        "resolved_place_bbox_wgs84": [-60.2, -3.2, -60.1, -3.1],
    }


def test_no_candidates_and_unknown_id_return_the_candidate_record(tmp_path: Any) -> None:
    empty, _ = geocoder(tmp_path / "empty", [])
    empty_result = empty.search("Missing")
    with pytest.raises(PlaceCandidateSelectionError, match="no candidates"):
        select_place_candidate(empty_result)

    one, _ = geocoder(
        tmp_path / "one",
        [candidate(1, "Found", south=10, north=11, west=20, east=21)],
    )
    one_result = one.search("Found")
    with pytest.raises(PlaceCandidateSelectionError, match="not present") as caught:
        select_place_candidate(one_result, candidate_id="999")
    assert caught.value.result.candidates[0].candidate_id == "1"


def test_repeated_search_uses_content_addressed_cache(tmp_path: Any) -> None:
    instance, calls = geocoder(
        tmp_path,
        [candidate(7, "Cached", south=1, north=2, west=3, east=4)],
    )

    first = instance.search("Cached")
    second = instance.search("Cached")

    assert len(calls) == 1
    assert first.cache_status == "miss"
    assert second.cache_status == "hit"
    assert first.response_sha256 == second.response_sha256
    assert first.candidates == second.candidates


def test_public_endpoint_enforces_one_request_per_second_policy(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match=r"1\.0"):
        geocoder(tmp_path, [], interval=0.2)

    private, _ = geocoder(
        tmp_path / "private",
        [],
        base_url="https://geocoder.fixture.test",
        interval=0.0,
    )
    assert private.search("No result").candidate_status == "none"
