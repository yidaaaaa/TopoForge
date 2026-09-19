from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from contextlib import closing
from email.message import Message
from pathlib import Path
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from topoforge.web import api, terrain_tiles
from topoforge.web.models import WebAppConfig
from topoforge.web.reference_tiles import ReferenceTileCache, ReferenceTileDownload


def png(*, size: tuple[int, int] = (256, 256), mode: str = "RGB") -> bytes:
    output = io.BytesIO()
    Image.new(mode, size).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("coordinates", [(-1, 0, 0), (15, 0, 0), (2, 4, 0), (2, 0, -1)])
def test_invalid_coordinates_do_not_fetch(
    coordinates: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terrain_tiles, "urlopen", lambda *_args, **_kwargs: pytest.fail("network"))
    with pytest.raises(ValueError, match="range"):
        terrain_tiles.fetch_terrain_tile(*coordinates)


@pytest.mark.parametrize(
    ("payload", "mime", "limit", "failure"),
    [
        (png(), "image/png", 512 * 1024, None),
        (b"proxy error", "text/html", 512 * 1024, "content type"),
        (png(size=(512, 512)), "image/png", 512 * 1024, "256 x 256"),
        (png(mode="P"), "image/png", 512 * 1024, "RGB"),
        (png()[:-10], "image/png", 512 * 1024, "PNG verification"),
        (png(), "image/png", 12, "download limit"),
    ],
)
def test_terrain_download_preserves_encoded_bytes_and_rejects_bad_images(
    payload: bytes, mime: str, limit: int, failure: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Reply(io.BytesIO):
        headers: Message

    def reply(request: Request, timeout: int) -> Reply:
        assert (
            request.full_url == "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/2/1/3.png"
        )
        assert timeout == 20
        assert "TopoForge/" in str(request.get_header("User-agent"))
        response = Reply(payload)
        response.headers = Message()
        response.headers["Content-Type"] = mime
        response.headers["ETag"] = '"fixture"'
        response.headers["x-amz-meta-x-imagery-sources"] = "srtm/fixture.tif"
        return response

    monkeypatch.setattr(terrain_tiles, "urlopen", reply)
    monkeypatch.setattr(terrain_tiles, "MAX_TERRAIN_TILE_BYTES", limit)
    if failure:
        with pytest.raises(ValueError, match=failure):
            terrain_tiles.fetch_terrain_tile(2, 1, 3)
    else:
        result = terrain_tiles.fetch_terrain_tile(2, 1, 3)
        assert result.payload == payload
        assert result.provenance["etag"] == '"fixture"'
        assert result.provenance["x-amz-meta-x-imagery-sources"] == "srtm/fixture.tif"
        assert result.provenance["vertical_datum"] == "unknown"


def test_terrain_and_vector_caches_are_separate_persistent_and_strictly_offline(
    web_config: WebAppConfig, web_static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = png()
    calls: list[tuple[int, int, int]] = []

    def fetch(z: int, x: int, y: int) -> ReferenceTileDownload:
        calls.append((z, x, y))
        return ReferenceTileDownload(
            payload, {"provider": "synthetic test fixture", "url": "fixture"}
        )

    monkeypatch.setattr(api, "fetch_terrain_tile", fetch)
    monkeypatch.setattr(api, "fetch_reference_tile", lambda *_: b"vector fixture")
    path = "/api/v1/reference/terrain/2/1/3.png"
    with TestClient(
        api.create_app(web_config, static_dir=web_static_dir), base_url="http://localhost"
    ) as client:
        assert client.get(path + "?cache_only=true").status_code == 409
        assert calls == []
        response = client.get(path)
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-topoforge-cache"] == "miss"
        assert client.get("/api/v1/reference/tiles/2/1/3.mvt").content == b"vector fixture"
        assert client.get("/api/v1/reference/terrain/2/4/0.png").status_code == 404
    cache_path = web_config.state_dir / "reference-map" / "mapzen-terrarium-v1.sqlite3"
    with closing(sqlite3.connect(cache_path)) as db:
        row = db.execute("SELECT metadata FROM tile_provenance").fetchone()
        assert json.loads(row[0])["provider"] == "synthetic test fixture"
        assert (
            db.execute("SELECT sha256 FROM tiles").fetchone()[0]
            == hashlib.sha256(payload).hexdigest()
        )

    def forbidden(*_: int) -> bytes:
        pytest.fail("Offline terrain must not contact the provider")

    monkeypatch.setattr(api, "fetch_terrain_tile", forbidden)
    with TestClient(
        api.create_app(web_config, static_dir=web_static_dir), base_url="http://localhost"
    ) as client:
        response = client.get(path + "?cache_only=true")
        assert response.content == payload
        assert response.headers["x-topoforge-cache"] == "offline"
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/reference/terrain/2/0/0.png?cache_only=true").status_code == 409
    assert calls == [(2, 1, 3)]


def test_invalid_provider_payload_is_not_cached(
    web_config: WebAppConfig, web_static_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*_: int) -> bytes:
        raise ValueError("invalid PNG")

    monkeypatch.setattr(api, "fetch_terrain_tile", failed)
    with TestClient(
        api.create_app(web_config, static_dir=web_static_dir), base_url="http://localhost"
    ) as client:
        response = client.get("/api/v1/reference/terrain/2/1/3.png")
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/reference/terrain/2/1/3.png?cache_only=true").status_code == 409


def test_additional_provenance_preserves_old_vector_cache_and_evicts_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE tiles (z INTEGER, x INTEGER, y INTEGER, payload BLOB NOT NULL, "
            "sha256 TEXT NOT NULL, fetched_at REAL NOT NULL, accessed_at INTEGER NOT NULL, "
            "PRIMARY KEY (z,x,y))"
        )
        db.execute(
            "INSERT INTO tiles VALUES (2,1,3,?,?,?,?)",
            (b"old", hashlib.sha256(b"old").hexdigest(), time.time(), time.time_ns()),
        )
    cache = ReferenceTileCache(path, max_tiles=1)
    assert cache.get(2, 1, 3, cache_only=True)[0] == b"old"
    for x in (0, 2):
        cache.get(
            2,
            x,
            0,
            cache_only=False,
            fetch=lambda _z, tile_x, _y: ReferenceTileDownload(b"new", {"id": str(tile_x)}),
        )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM tile_provenance").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT metadata FROM tile_provenance").fetchone()[0]) == {
            "id": "2"
        }
        # The original seven-column table remains readable/writable by older versions.
        assert len(db.execute("PRAGMA table_info(tiles)").fetchall()) == 7
