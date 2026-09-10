from __future__ import annotations

import io
from email.message import Message
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from topoforge.web import api, reference_tiles
from topoforge.web.models import WebAppConfig


@pytest.mark.parametrize("coordinates", [(-1, 0, 0), (15, 0, 0), (2, 4, 0), (2, 0, -1)])
def test_reference_tile_rejects_out_of_range_without_network(
    coordinates: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid tile must not cause a network request")

    monkeypatch.setattr(reference_tiles, "urlopen", unexpected)
    with pytest.raises(ValueError, match="range"):
        reference_tiles.fetch_reference_tile(*coordinates)


@pytest.mark.parametrize(
    ("content_type", "payload", "expected_error"),
    [
        ("application/vnd.mapbox-vector-tile", b"tile", None),
        ("text/html", b"proxy error page", "content type"),
        ("application/octet-stream", b"12345", "download limit"),
    ],
)
def test_reference_fetch_checks_size_type_and_request_identity(
    content_type: str,
    payload: bytes,
    expected_error: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reply(io.BytesIO):
        headers: Message

    def open_reply(request: Request, timeout: int) -> Reply:
        assert request.full_url == "https://vector.openstreetmap.org/shortbread_v1/2/1/3.mvt"
        assert "TopoForge/" in str(request.get_header("User-agent"))
        assert timeout == 20
        reply = Reply(payload)
        reply.headers = Message()
        reply.headers["Content-Type"] = content_type
        return reply

    monkeypatch.setattr(reference_tiles, "urlopen", open_reply)
    monkeypatch.setattr(reference_tiles, "MAX_TILE_BYTES", 4)
    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            reference_tiles.fetch_reference_tile(2, 1, 3)
    else:
        assert reference_tiles.fetch_reference_tile(2, 1, 3) == payload


def test_reference_route_is_same_origin_cacheable_and_handles_failure(
    web_config: WebAppConfig,
    web_static_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    def fetch(z: int, x: int, y: int) -> bytes:
        calls.append((z, x, y))
        return b"tile"

    monkeypatch.setattr(api, "fetch_reference_tile", fetch)
    with TestClient(
        api.create_app(web_config, static_dir=web_static_dir), base_url="http://localhost"
    ) as client:
        response = client.get("/api/v1/reference/tiles/2/1/3.mvt")
        assert response.status_code == 200
        assert response.content == b"tile"
        assert response.headers["cache-control"] == "public, max-age=604800"
        assert response.headers["content-type"] == "application/vnd.mapbox-vector-tile"
        assert client.get("/api/v1/reference/tiles/2/4/3.mvt").status_code == 404
        assert calls == [(2, 1, 3)]

        def unavailable(*args: int) -> bytes:
            raise OSError("simulated network failure")

        monkeypatch.setattr(api, "fetch_reference_tile", unavailable)
        cached = client.get("/api/v1/reference/tiles/2/1/3.mvt?cache_only=true")
        assert cached.status_code == 200
        assert cached.content == b"tile"
        assert cached.headers["x-topoforge-cache"] == "offline"
        assert cached.headers["cache-control"] == "no-store"
        missing = client.get("/api/v1/reference/tiles/2/1/2.mvt?cache_only=true")
        assert missing.status_code == 409
        assert "not cached" in missing.json()["detail"]
        assert missing.headers["cache-control"] == "no-store"
        response = client.get("/api/v1/reference/tiles/2/1/2.mvt")
        assert response.status_code == 503
        assert "network or proxy" in response.json()["detail"]
        assert "cache-control" not in response.headers


def test_cache_survives_restart_and_offline_reads_expired_tiles_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cache" / "shortbread-v1.sqlite3"
    cache = reference_tiles.ReferenceTileCache(path)
    calls = []

    def fetch(z: int, x: int, y: int) -> bytes:
        calls.append((z, x, y))
        return b"first" if len(calls) == 1 else b"updated"

    assert cache.get(2, 1, 3, cache_only=False, fetch=fetch)[0:2] == (b"first", "miss")
    reopened = reference_tiles.ReferenceTileCache(path)
    assert reopened.get(2, 1, 3, cache_only=False, fetch=fetch)[0:2] == (b"first", "hit")
    now = reference_tiles.time.time()
    monkeypatch.setattr(reference_tiles.time, "time", lambda: now + 8 * 86400)
    assert reopened.get(2, 1, 3, cache_only=True, fetch=fetch) == (b"first", "offline", 0)
    with pytest.raises(reference_tiles.ReferenceTileUnavailable):
        reopened.get(2, 1, 2, cache_only=True, fetch=fetch)
    assert len(calls) == 1
    assert reopened.get(2, 1, 3, cache_only=False, fetch=fetch)[0:2] == (b"updated", "miss")
    assert len(calls) == 2


@pytest.mark.parametrize(("max_bytes", "max_tiles"), [(8, 9), (64, 2)])
def test_cache_bounds_storage_and_evicts_least_recently_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_bytes: int, max_tiles: int
) -> None:
    monkeypatch.setattr(reference_tiles, "MAX_TILE_BYTES", 4)
    cache = reference_tiles.ReferenceTileCache(
        tmp_path / "tiles.sqlite3", max_bytes=max_bytes, max_tiles=max_tiles
    )
    for x in (0, 1):
        cache.get(2, x, 0, cache_only=False, fetch=lambda *_: b"tile")
    cache.get(2, 0, 0, cache_only=True)  # Keep this one recently used.
    cache.get(2, 2, 0, cache_only=False, fetch=lambda *_: b"tile")
    with pytest.raises(reference_tiles.ReferenceTileUnavailable):
        cache.get(2, 1, 0, cache_only=True)
    assert cache.get(2, 0, 0, cache_only=True)[0] == b"tile"
    assert cache.get(2, 2, 0, cache_only=True)[0] == b"tile"


def test_corrupt_cache_and_failed_download_do_not_become_offline_tiles(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    cache = reference_tiles.ReferenceTileCache(tmp_path / "tiles.sqlite3")
    cache.get(2, 1, 0, cache_only=False, fetch=lambda *_: b"valid")
    with closing(sqlite3.connect(cache.path)) as db, db:
        db.execute("UPDATE tiles SET payload=?", (b"damaged",))
    with pytest.raises(reference_tiles.ReferenceTileUnavailable):
        cache.get(2, 1, 0, cache_only=True)

    def failed(*_: int) -> bytes:
        raise OSError("offline")

    with pytest.raises(OSError):
        cache.get(2, 1, 0, cache_only=False, fetch=failed)
    with pytest.raises(reference_tiles.ReferenceTileUnavailable):
        cache.get(2, 1, 0, cache_only=True, fetch=failed)


def test_concurrent_tile_requests_share_one_download(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    cache = reference_tiles.ReferenceTileCache(tmp_path / "tiles.sqlite3")
    started, finish = Event(), Event()
    calls = []

    def fetch(*_: int) -> bytes:
        calls.append(1)
        started.set()
        assert finish.wait(timeout=5)
        return b"tile"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get, 2, 1, 0, cache_only=False, fetch=fetch)
        assert started.wait(timeout=5)
        second = pool.submit(cache.get, 2, 1, 0, cache_only=False, fetch=fetch)
        try:
            with pytest.raises(reference_tiles.ReferenceTileUnavailable):
                cache.get(2, 1, 0, cache_only=True, fetch=fetch)
        finally:
            finish.set()
        assert first.result()[0] == second.result()[0] == b"tile"
    assert calls == [1]
