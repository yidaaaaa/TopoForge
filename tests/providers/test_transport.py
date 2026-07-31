from __future__ import annotations

from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import URLError

import pytest

from topoforge.exceptions import ProviderFetchError
from topoforge.providers.cache import CacheIdentity, CacheStatus, ContentAddressedCache
from topoforge.providers.transport import CachingHttpClient, HttpTransportConfig


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.status = status
        self._stream = BytesIO(payload)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(payload))
        self.headers["Content-Type"] = "image/tiff"
        self.headers["ETag"] = '"fixture"'
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def close(self) -> None:
        self.closed = True


def request() -> CacheIdentity:
    return CacheIdentity(
        provider_id="fixture",
        dataset_id="fixture-dem",
        dataset_version="2026",
        url="https://example.test/dem.tif",
    )


def config(**updates: object) -> HttpTransportConfig:
    values: dict[str, object] = {
        "timeout_seconds": 3,
        "max_attempts": 3,
        "backoff_base_seconds": 0.25,
        "max_backoff_seconds": 1,
        "min_request_interval_seconds": 0,
        "max_download_bytes": 1024,
        "chunk_size_bytes": 4096,
    }
    values.update(updates)
    return HttpTransportConfig.model_validate(values)


def test_transport_retries_then_uses_verified_cache(tmp_path: Path) -> None:
    calls: list[float] = []
    sleeps: list[float] = []

    def opener(_request: object, timeout: float) -> FakeResponse:
        calls.append(timeout)
        if len(calls) == 1:
            raise URLError("fixture timeout")
        return FakeResponse(b"valid raster fixture")

    client = CachingHttpClient(
        ContentAddressedCache(tmp_path / "cache"),
        config(),
        open_url=opener,  # type: ignore[arg-type]
        sleep=sleeps.append,
    )
    first = client.download(request())
    second = client.download(request())

    assert first.cache_status is CacheStatus.MISS
    assert [item.status for item in first.attempts] == ["retrying", "succeeded"]
    assert sleeps == [0.25]
    assert calls == [3.0, 3.0]
    assert first.path.read_bytes() == b"valid raster fixture"
    assert second.cache_status is CacheStatus.HIT
    assert second.attempts == []
    assert calls == [3.0, 3.0]


def test_transport_recovers_corrupt_cache_by_refetching(tmp_path: Path) -> None:
    payloads = [b"same bytes", b"same bytes"]

    def opener(_request: object, _timeout: float) -> FakeResponse:
        return FakeResponse(payloads.pop(0))

    client = CachingHttpClient(
        ContentAddressedCache(tmp_path / "cache"),
        config(),
        open_url=opener,  # type: ignore[arg-type]
    )
    first = client.download(request())
    first.path.write_bytes(b"bad")
    recovered = client.download(request())

    assert recovered.cache_status is CacheStatus.CORRUPT
    assert recovered.path.read_bytes() == b"same bytes"


def test_transport_enforces_download_limit(tmp_path: Path) -> None:
    client = CachingHttpClient(
        ContentAddressedCache(tmp_path / "cache"),
        config(max_download_bytes=4),
        open_url=lambda _request, _timeout: FakeResponse(b"too large"),  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderFetchError, match="Content-Length"):
        client.download(request())


def test_transport_enforces_minimum_request_interval(tmp_path: Path) -> None:
    now = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    client = CachingHttpClient(
        ContentAddressedCache(tmp_path / "cache"),
        config(min_request_interval_seconds=1),
        open_url=lambda _request, _timeout: FakeResponse(b"fixture"),  # type: ignore[arg-type]
        sleep=sleep,
        monotonic=monotonic,
    )
    client.download(request())
    client.download(request().model_copy(update={"url": "https://example.test/second.tif"}))

    assert sleeps == [1.0]
