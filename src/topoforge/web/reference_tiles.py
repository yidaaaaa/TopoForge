"""Bounded, on-demand reference tiles through the local runtime's network settings."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from topoforge import __version__

CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TILE_BYTES = 8 * 1024 * 1024


def fetch_reference_tile(z: int, x: int, y: int) -> bytes:
    """Fetch one visible Shortbread tile; retain TLS and environment proxy settings."""
    if not 0 <= z <= 14 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise ValueError("Reference tile coordinates are outside the supported range")
    request = Request(
        f"https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt",
        headers={
            "User-Agent": f"TopoForge/{__version__} (+https://github.com/yidaaaaa/TopoForge/issues)",
            "Accept": "application/vnd.mapbox-vector-tile",
        },
    )
    with urlopen(request, timeout=20) as response:
        content_type = response.headers.get_content_type()
        if content_type not in {
            "application/vnd.mapbox-vector-tile",
            "application/x-protobuf",
            "application/octet-stream",
        }:
            raise ValueError("Reference provider returned an unexpected content type")
        data = response.read(MAX_TILE_BYTES + 1)
    if len(data) > MAX_TILE_BYTES:
        raise ValueError("Reference tile exceeds the download limit")
    return data


@dataclass(frozen=True)
class ReferenceTileDownload:
    """Downloaded reference bytes and available provider metadata for local provenance."""

    payload: bytes
    provenance: dict[str, str]


class ReferenceTileUnavailable(LookupError):
    """The requested tile is absent from the local cache."""


class ReferenceTileCache:
    """Persist viewed tiles with bounded LRU storage and a network-free read mode."""

    def __init__(
        self, path: Path, *, max_bytes: int = 128 * 1024 * 1024, max_tiles: int = 4096
    ) -> None:
        if max_bytes < MAX_TILE_BYTES or max_tiles < 1:
            raise ValueError("Cache limits must allow at least one maximum-size tile")
        self.path = path
        self.max_bytes = max_bytes
        self.max_tiles = max_tiles
        self._locks = [threading.Lock() for _ in range(32)]
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA auto_vacuum=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS tiles ("
                "z INTEGER, x INTEGER, y INTEGER, payload BLOB NOT NULL, "
                "sha256 TEXT NOT NULL, fetched_at REAL NOT NULL, accessed_at INTEGER NOT NULL, "
                "PRIMARY KEY (z, x, y))"
            )

            db.execute(
                "CREATE TABLE IF NOT EXISTS tile_provenance ("
                "z INTEGER, x INTEGER, y INTEGER, metadata TEXT NOT NULL, "
                "PRIMARY KEY (z, x, y))"
            )

    def get(
        self,
        z: int,
        x: int,
        y: int,
        *,
        cache_only: bool,
        fetch: Callable[[int, int, int], bytes | ReferenceTileDownload] = fetch_reference_tile,
    ) -> tuple[bytes, str, int]:
        """Return payload, cache outcome and remaining freshness; never fetch in cache-only mode."""
        if not 0 <= z <= 14 or not 0 <= x < 2**z or not 0 <= y < 2**z:
            raise ValueError("Reference tile coordinates are outside the supported range")
        cached = self._read(z, x, y)
        if cached is not None and (cache_only or cached[1] > 0):
            return cached[0], "offline" if cache_only else "hit", cached[1]
        if cache_only:
            raise ReferenceTileUnavailable(
                "Reference tile is not cached; view this area online first"
            )
        # Coalesce identical concurrent requests without blocking cache-only reads on network IO.
        with self._locks[hash((z, x, y)) % len(self._locks)]:
            cached = self._read(z, x, y)
            if cached is not None and cached[1] > 0:
                return cached[0], "hit", cached[1]
            result = fetch(z, x, y)
            payload = result.payload if isinstance(result, ReferenceTileDownload) else result
            provenance = result.provenance if isinstance(result, ReferenceTileDownload) else None
            if len(payload) > MAX_TILE_BYTES:
                raise ValueError("Reference tile exceeds the download limit")
            self._store(z, x, y, payload, provenance)
            return payload, "miss", CACHE_TTL_SECONDS

    def _read(self, z: int, x: int, y: int) -> tuple[bytes, int] | None:
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT payload, sha256, fetched_at FROM tiles WHERE z=? AND x=? AND y=?",
                (z, x, y),
            ).fetchone()
            if row is None:
                return None
            payload = bytes(row[0])
            if len(payload) > MAX_TILE_BYTES or hashlib.sha256(payload).hexdigest() != row[1]:
                db.execute("DELETE FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y))
                db.execute("DELETE FROM tile_provenance WHERE z=? AND x=? AND y=?", (z, x, y))
                return None
            db.execute(
                "UPDATE tiles SET accessed_at=? WHERE z=? AND x=? AND y=?",
                (time.time_ns(), z, x, y),
            )
            remaining = max(0, math.ceil(float(row[2]) + CACHE_TTL_SECONDS - time.time()))
            return payload, remaining

    def _store(
        self, z: int, x: int, y: int, payload: bytes, provenance: dict[str, str] | None = None
    ) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y))
            count, size = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM tiles"
            ).fetchone()
            while count >= self.max_tiles or size + len(payload) > self.max_bytes:
                oldest = db.execute(
                    "SELECT z, x, y, length(payload) FROM tiles ORDER BY accessed_at LIMIT 1"
                ).fetchone()
                db.execute("DELETE FROM tiles WHERE z=? AND x=? AND y=?", oldest[:3])
                count -= 1
                size -= oldest[3]
            db.execute(
                "INSERT INTO tiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    z,
                    x,
                    y,
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    time.time(),
                    time.time_ns(),
                ),
            )
            db.execute("DELETE FROM tile_provenance WHERE z=? AND x=? AND y=?", (z, x, y))
            if provenance is not None:
                db.execute(
                    "INSERT INTO tile_provenance VALUES (?, ?, ?, ?)",
                    (z, x, y, json.dumps(provenance, sort_keys=True)),
                )
            db.execute(
                "DELETE FROM tile_provenance WHERE NOT EXISTS ("
                "SELECT 1 FROM tiles WHERE tiles.z=tile_provenance.z "
                "AND tiles.x=tile_provenance.x AND tiles.y=tile_provenance.y)"
            )
