from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from topoforge.exceptions import ProviderCacheMissError
from topoforge.providers import CachingHttpClient, ContentAddressedCache, HttpTransportConfig
from topoforge.web.api import create_app
from topoforge.web.models import WebAppConfig
from topoforge.web.place_search import (
    PlaceSearchBusyError,
    PlaceSearchRequest,
    PublicSearchDisabledError,
    WebPlaceSearch,
)


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
) -> dict[str, object]:
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


def search_service(state: Path, payload: object) -> tuple[WebPlaceSearch, list[Request]]:
    service = WebPlaceSearch(state)
    calls: list[Request] = []

    def open_url(request: Request, timeout: float) -> FakeResponse:
        assert timeout == 10
        calls.append(request)
        return FakeResponse(json.dumps(payload).encode())

    service.geocoder.client = CachingHttpClient(
        ContentAddressedCache(state / "place-search"),
        HttpTransportConfig(
            timeout_seconds=10,
            max_attempts=1,
            min_request_interval_seconds=1.1,
            max_download_bytes=256 * 1024,
            user_agent="TopoForge/test-place-search",
        ),
        open_url=open_url,
    )
    return service, calls


def places() -> list[dict[str, object]]:
    return [
        candidate(1, "西湖, 杭州市", south=30.20, north=30.29, west=120.10, east=120.16),
        candidate(2, "西湖, 另一个城市", south=23.10, north=23.15, west=114.30, east=114.38),
    ]


def test_no_network_without_deliberate_choice_or_cached_query(tmp_path: Path) -> None:
    service, calls = search_service(tmp_path, places())
    with pytest.raises(PublicSearchDisabledError):
        service.search(PlaceSearchRequest(query="西湖", cache_only=False))
    with pytest.raises(ProviderCacheMissError):
        service.search(PlaceSearchRequest(query="西湖"))
    assert calls == []


def test_cache_survives_restart_and_keeps_ambiguous_candidates(tmp_path: Path) -> None:
    service, calls = search_service(tmp_path, places())
    result = service.search(
        PlaceSearchRequest(query="  杭州   西湖  ", cache_only=False, allow_public_service=True)
    )
    assert len(result.candidates) == 2
    assert result.query == "杭州 西湖"
    assert result.candidates[0].bounding_box_wgs84 == (120.1, 30.2, 120.16, 30.29)
    assert "accept-language=zh-CN" in calls[0].full_url
    assert "polygon_geojson=0" in calls[0].full_url
    assert calls[0].get_header("User-agent") == "TopoForge/test-place-search"
    restarted, forbidden_calls = search_service(tmp_path, [])
    cached = restarted.search(PlaceSearchRequest(query="杭州 西湖"))
    assert cached.cache_status == "hit"
    assert cached.candidates == result.candidates
    assert forbidden_calls == []
    with pytest.raises(ProviderCacheMissError):
        restarted.search(PlaceSearchRequest(query="杭州 西湖", language="en"))
    assert forbidden_calls == []


def test_busy_search_does_not_queue_additional_requests(tmp_path: Path) -> None:
    service, calls = search_service(tmp_path, places())
    with service._lock, pytest.raises(PlaceSearchBusyError):
        service.search(
            PlaceSearchRequest(query="西湖", cache_only=False, allow_public_service=True)
        )
    assert calls == []


def test_corrupt_offline_cache_never_fetches_replacement(tmp_path: Path) -> None:
    service, calls = search_service(tmp_path, places())
    service.search(PlaceSearchRequest(query="西湖", cache_only=False, allow_public_service=True))
    objects = list((tmp_path / "place-search" / "objects").glob("*/*"))
    assert len(objects) == 1
    objects[0].write_bytes(b"corrupt")
    with pytest.raises(ProviderCacheMissError):
        service.search(PlaceSearchRequest(query="西湖"))
    assert len(calls) == 1


def test_http_adapter_exposes_only_candidates_and_actionable_status(
    web_config: WebAppConfig, web_static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOPOFORGE_GEOCODER_URL", raising=False)
    app = create_app(web_config, static_dir=web_static_dir, allow_testserver_host=True)
    service = app.state.place_search
    fake, calls = search_service(web_config.state_dir, places())
    service.geocoder = fake.geocoder
    with TestClient(app) as client:
        config = client.get("/api/v1/places/config")
        assert config.status_code == 200
        assert config.json()["is_public"] is True
        assert calls == []
        assert client.post("/api/v1/places/search", json={"query": "西湖"}).status_code == 409
        assert (
            client.post(
                "/api/v1/places/search", json={"query": "西湖", "cache_only": False}
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/places/search",
            json={"query": "西湖", "cache_only": False, "allow_public_service": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["candidates"]) == 2
        assert "request_url" not in data and "attempts" not in data
        assert response.headers["cache-control"] == "no-store"
        for query in ("", "  ", "x" * 201, "line\nbreak"):
            assert client.post("/api/v1/places/search", json={"query": query}).status_code == 422
        assert len(calls) == 1
        with service._lock:
            assert client.post("/api/v1/places/search", json={"query": "西湖"}).status_code == 429
        blocked = client.post(
            "/api/v1/places/search",
            json={"query": "西湖", "cache_only": False, "allow_public_service": True},
            headers={"Origin": "https://example.com"},
        )
        assert blocked.status_code == 403
        assert len(calls) == 1


@pytest.mark.parametrize("payload", [{"error": "unavailable"}, places() * 6, [{"lat": "bad"}]])
def test_bad_upstream_results_are_reported_without_false_locations(
    web_config: WebAppConfig, web_static_dir: Path, payload: object
) -> None:
    app = create_app(web_config, static_dir=web_static_dir, allow_testserver_host=True)
    fake, _ = search_service(web_config.state_dir, payload)
    app.state.place_search.geocoder = fake.geocoder
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/places/search",
            json={"query": "fixture", "cache_only": False, "allow_public_service": True},
        )
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "search-unavailable"


def test_custom_endpoint_is_server_configured_and_not_a_browser_proxy(
    web_config: WebAppConfig, web_static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOPOFORGE_GEOCODER_URL", "https://geocoder.example.test/local")
    app = create_app(web_config, static_dir=web_static_dir, allow_testserver_host=True)
    with TestClient(app) as client:
        assert client.get("/api/v1/places/config").json()["is_public"] is False
        response = client.post(
            "/api/v1/places/search",
            json={"query": "fixture", "endpoint": "http://untrusted.example"},
        )
        assert response.status_code == 422
    assert app.state.place_search.endpoint == "https://geocoder.example.test/local"


def test_service_uses_a_shared_rate_limiter_and_small_transport_bounds(tmp_path: Path) -> None:
    service = WebPlaceSearch(tmp_path)
    config = service.geocoder.client.config
    assert config.min_request_interval_seconds >= 1
    assert config.max_attempts == 1
    assert config.timeout_seconds <= 10
    assert config.max_download_bytes <= 256 * 1024
    assert "TopoForge/" in config.user_agent
    assert "github.com/yidaaaaa/TopoForge" in config.user_agent
