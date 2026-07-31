"""Bounded HTTP transport with cache, timeout, retry, backoff, and rate limiting."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from topoforge.exceptions import ProviderFetchError
from topoforge.providers.cache import (
    CacheEntry,
    CacheIdentity,
    CacheStatus,
    ContentAddressedCache,
)

_RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpTransportConfig(BaseModel):
    """Operational HTTP limits; every network operation is bounded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=4, ge=1, le=10)
    backoff_base_seconds: float = Field(default=0.5, ge=0)
    max_backoff_seconds: float = Field(default=8.0, ge=0)
    min_request_interval_seconds: float = Field(default=0.2, ge=0)
    max_download_bytes: int = Field(default=2_000_000_000, gt=0)
    chunk_size_bytes: int = Field(default=1024 * 1024, ge=4096)
    user_agent: str = "TopoForge/0.2 (+https://github.com/topoforge/topoforge)"


class NetworkAttempt(BaseModel):
    """One literal network attempt for provenance and retry tests."""

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    status: str
    status_code: int | None = None
    error: str | None = None
    retry_delay_seconds: float = Field(default=0.0, ge=0)


class DownloadResult(BaseModel):
    """Verified cached bytes plus complete acquisition attempt evidence."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    path: Path
    cache_entry: CacheEntry
    cache_status: CacheStatus
    cache_lookup_reason: str
    attempts: list[NetworkAttempt]


class _Response(Protocol):
    headers: Any
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


OpenUrl = Callable[[Request, float], _Response]


def _open_url(request: Request, timeout_seconds: float) -> _Response:
    return cast(_Response, urlopen(request, timeout=timeout_seconds))


def _header(headers: Any, name: str) -> str | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    return str(value) if value is not None else None


class CachingHttpClient:
    """Download immutable provider assets once and verify every subsequent use."""

    def __init__(
        self,
        cache: ContentAddressedCache,
        config: HttpTransportConfig | None = None,
        *,
        open_url: OpenUrl = _open_url,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache = cache
        self.config = config or HttpTransportConfig()
        self._open_url = open_url
        self._sleep = sleep
        self._monotonic = monotonic
        self._rate_lock = threading.Lock()
        self._last_request_started: float | None = None

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            now = self._monotonic()
            if self._last_request_started is not None:
                remaining = self.config.min_request_interval_seconds - (
                    now - self._last_request_started
                )
                if remaining > 0:
                    self._sleep(remaining)
                    now = self._monotonic()
            self._last_request_started = now

    def _retry_delay(self, attempt: int, error: Exception) -> float:
        if isinstance(error, HTTPError):
            retry_after = _header(error.headers, "Retry-After")
            if retry_after is not None:
                try:
                    return min(self.config.max_backoff_seconds, max(0.0, float(retry_after)))
                except ValueError:
                    pass
        delay = self.config.backoff_base_seconds * (2 ** (attempt - 1))
        return min(self.config.max_backoff_seconds, delay)

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, HTTPError):
            return error.code in _RETRYABLE_HTTP_STATUS
        return isinstance(error, (URLError, TimeoutError, socket.timeout, OSError))

    def download(self, identity: CacheIdentity) -> DownloadResult:
        lookup = self.cache.lookup(identity)
        if lookup.status is CacheStatus.HIT:
            assert lookup.entry is not None and lookup.path is not None
            return DownloadResult(
                path=lookup.path,
                cache_entry=lookup.entry,
                cache_status=CacheStatus.HIT,
                cache_lookup_reason=lookup.reason,
                attempts=[],
            )
        initial_status = lookup.status
        initial_reason = lookup.reason
        if lookup.status is CacheStatus.CORRUPT:
            self.cache.invalidate(identity)

        attempts: list[NetworkAttempt] = []
        for attempt_number in range(1, self.config.max_attempts + 1):
            response: _Response | None = None
            temporary: Path | None = None
            try:
                self._wait_for_rate_limit()
                request = Request(
                    identity.url,
                    headers={"User-Agent": self.config.user_agent, "Accept-Encoding": "identity"},
                    method="GET",
                )
                response = self._open_url(request, self.config.timeout_seconds)
                status_code = int(getattr(response, "status", 200))
                if status_code < 200 or status_code >= 300:
                    raise HTTPError(
                        identity.url, status_code, "unexpected response", response.headers, None
                    )
                content_length_text = _header(response.headers, "Content-Length")
                expected_length = int(content_length_text) if content_length_text else None
                if expected_length is not None and expected_length > self.config.max_download_bytes:
                    raise ProviderFetchError(
                        f"Content-Length {expected_length} exceeds max_download_bytes="
                        f"{self.config.max_download_bytes}"
                    )
                temporary = self.cache.temporary_path()
                received = 0
                with temporary.open("wb") as stream:
                    while chunk := response.read(self.config.chunk_size_bytes):
                        received += len(chunk)
                        if received > self.config.max_download_bytes:
                            raise ProviderFetchError(
                                "download exceeded max_download_bytes="
                                f"{self.config.max_download_bytes}"
                            )
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected_length is not None and received != expected_length:
                    raise OSError(
                        f"received {received} bytes but Content-Length declared {expected_length}"
                    )
                stored = self.cache.store(
                    identity,
                    temporary,
                    etag=_header(response.headers, "ETag"),
                    last_modified=_header(response.headers, "Last-Modified"),
                    media_type=_header(response.headers, "Content-Type"),
                    response_status=status_code,
                    fetched_at=datetime.now(UTC).isoformat(),
                    attempts=attempt_number,
                )
                if (
                    stored.status is not CacheStatus.HIT
                    or stored.entry is None
                    or stored.path is None
                ):
                    raise OSError(f"published cache entry did not verify: {stored.reason}")
                attempts.append(
                    NetworkAttempt(
                        attempt=attempt_number, status="succeeded", status_code=status_code
                    )
                )
                return DownloadResult(
                    path=stored.path,
                    cache_entry=stored.entry,
                    cache_status=initial_status,
                    cache_lookup_reason=initial_reason,
                    attempts=attempts,
                )
            except ProviderFetchError:
                raise
            except Exception as exc:
                retryable = self._retryable(exc)
                last_attempt = attempt_number >= self.config.max_attempts
                delay = (
                    self._retry_delay(attempt_number, exc)
                    if retryable and not last_attempt
                    else 0.0
                )
                attempts.append(
                    NetworkAttempt(
                        attempt=attempt_number,
                        status="retrying" if delay > 0 else "failed",
                        status_code=exc.code if isinstance(exc, HTTPError) else None,
                        error=f"{type(exc).__name__}: {exc}",
                        retry_delay_seconds=delay,
                    )
                )
                if not retryable or last_attempt:
                    detail = "; ".join(item.error or item.status for item in attempts)
                    raise ProviderFetchError(
                        f"download failed after {attempt_number} attempt(s) for "
                        f"{identity.url}: {detail}"
                    ) from exc
                if delay > 0:
                    self._sleep(delay)
            finally:
                if response is not None:
                    response.close()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        raise AssertionError("bounded retry loop terminated without a result")
