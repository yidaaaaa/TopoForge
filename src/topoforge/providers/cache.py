"""Content-addressed provider cache with atomic indexes and checksum verification."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.util import sha256_bytes, sha256_file


class CacheStatus(StrEnum):
    """Result of resolving one immutable provider request."""

    HIT = "hit"
    MISS = "miss"
    CORRUPT = "corrupt"


class CacheIdentity(BaseModel):
    """Stable identity of one immutable provider object request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    url: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_url(self) -> CacheIdentity:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("provider cache URLs must use http or https")
        return self

    @property
    def request_key(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256_bytes(payload)


class CacheEntry(BaseModel):
    """Verified request index pointing to an immutable content object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str
    dataset_id: str
    dataset_version: str
    url: str
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size_bytes: int = Field(ge=0)
    object_relpath: str
    etag: str | None = None
    last_modified: str | None = None
    media_type: str | None = None
    response_status: int = Field(ge=100, le=599)
    fetched_at: str
    attempts: int = Field(ge=1)

    def matches(self, identity: CacheIdentity) -> bool:
        return (
            self.request_key == identity.request_key
            and self.provider_id == identity.provider_id
            and self.dataset_id == identity.dataset_id
            and self.dataset_version == identity.dataset_version
            and self.url == identity.url
        )


class CacheLookup(BaseModel):
    """Cache lookup outcome with a reason for miss/corruption."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    status: CacheStatus
    entry: CacheEntry | None = None
    path: Path | None = None
    reason: str


class CacheSummary(BaseModel):
    """Measured cache state for CLI/status evidence."""

    model_config = ConfigDict(extra="forbid")

    root: str
    exists: bool
    request_entries: int = Field(ge=0)
    content_objects: int = Field(ge=0)
    content_bytes: int = Field(ge=0)
    temporary_files: int = Field(ge=0)


class ContentAddressedCache:
    """Store immutable bytes by SHA-256 and request indexes by canonical identity."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.objects_dir = self.root / "objects"
        self.requests_dir = self.root / "requests"
        self.temporary_dir = self.root / "tmp"

    def _ensure_layout(self) -> None:
        for path in (self.objects_dir, self.requests_dir, self.temporary_dir):
            path.mkdir(parents=True, exist_ok=True)

    def request_path(self, identity: CacheIdentity) -> Path:
        key = identity.request_key
        return self.requests_dir / key[:2] / f"{key}.json"

    def object_path(self, sha256: str) -> Path:
        return self.objects_dir / sha256[:2] / sha256

    def temporary_path(self, prefix: str = "download-") -> Path:
        self._ensure_layout()
        descriptor, raw_path = tempfile.mkstemp(prefix=prefix, dir=self.temporary_dir)
        os.close(descriptor)
        return Path(raw_path)

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def lookup(self, identity: CacheIdentity) -> CacheLookup:
        index_path = self.request_path(identity)
        if not index_path.is_file():
            return CacheLookup(status=CacheStatus.MISS, reason="request index is absent")
        try:
            entry = CacheEntry.model_validate_json(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return CacheLookup(
                status=CacheStatus.CORRUPT,
                reason=f"request index is invalid: {type(exc).__name__}",
            )
        if not entry.matches(identity):
            return CacheLookup(status=CacheStatus.CORRUPT, entry=entry, reason="identity mismatch")
        expected_relpath = self.object_path(entry.object_sha256).relative_to(self.root).as_posix()
        if entry.object_relpath != expected_relpath:
            return CacheLookup(
                status=CacheStatus.CORRUPT,
                entry=entry,
                reason="object path is not canonical for its content hash",
            )
        object_path = self.root / entry.object_relpath
        if not object_path.is_file():
            return CacheLookup(
                status=CacheStatus.CORRUPT,
                entry=entry,
                reason="content object is absent",
            )
        if object_path.stat().st_size != entry.object_size_bytes:
            return CacheLookup(
                status=CacheStatus.CORRUPT,
                entry=entry,
                path=object_path,
                reason="content length differs from request index",
            )
        if sha256_file(object_path) != entry.object_sha256:
            return CacheLookup(
                status=CacheStatus.CORRUPT,
                entry=entry,
                path=object_path,
                reason="content SHA-256 differs from request index",
            )
        return CacheLookup(
            status=CacheStatus.HIT,
            entry=entry,
            path=object_path,
            reason="request index and content SHA-256 verified",
        )

    def invalidate(self, identity: CacheIdentity) -> None:
        """Remove only the mutable request index; immutable objects remain deduplicated."""
        self.request_path(identity).unlink(missing_ok=True)

    def store(
        self,
        identity: CacheIdentity,
        source: Path,
        *,
        etag: str | None,
        last_modified: str | None,
        media_type: str | None,
        response_status: int,
        fetched_at: str,
        attempts: int,
    ) -> CacheLookup:
        self._ensure_layout()
        content_sha256 = sha256_file(source)
        content_size = source.stat().st_size
        object_path = self.object_path(content_sha256)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if object_path.exists() and (
            object_path.stat().st_size != content_size or sha256_file(object_path) != content_sha256
        ):
            object_path.unlink()
        if not object_path.exists():
            temporary = self.temporary_path(prefix="publish-")
            try:
                with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                if sha256_file(temporary) != content_sha256:
                    raise OSError("cache publication changed content bytes")
                os.replace(temporary, object_path)
            finally:
                temporary.unlink(missing_ok=True)
        entry = CacheEntry(
            request_key=identity.request_key,
            provider_id=identity.provider_id,
            dataset_id=identity.dataset_id,
            dataset_version=identity.dataset_version,
            url=identity.url,
            object_sha256=content_sha256,
            object_size_bytes=content_size,
            object_relpath=object_path.relative_to(self.root).as_posix(),
            etag=etag,
            last_modified=last_modified,
            media_type=media_type,
            response_status=response_status,
            fetched_at=fetched_at,
            attempts=attempts,
        )
        self._write_json_atomic(self.request_path(identity), entry.model_dump(mode="json"))
        return self.lookup(identity)

    def materialize(self, entry: CacheEntry, destination: Path) -> Path:
        source = self.root / entry.object_relpath
        if not source.is_file() or sha256_file(source) != entry.object_sha256:
            raise OSError("cache object failed verification before materialization")
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(raw_path)
        try:
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != entry.object_sha256:
                raise OSError("materialized cache object failed SHA-256 verification")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def summary(self) -> CacheSummary:
        if not self.root.exists():
            return CacheSummary(
                root=str(self.root),
                exists=False,
                request_entries=0,
                content_objects=0,
                content_bytes=0,
                temporary_files=0,
            )
        request_entries = sum(1 for path in self.requests_dir.rglob("*.json") if path.is_file())
        objects = [path for path in self.objects_dir.rglob("*") if path.is_file()]
        temporary_files = sum(1 for path in self.temporary_dir.rglob("*") if path.is_file())
        return CacheSummary(
            root=str(self.root),
            exists=True,
            request_entries=request_entries,
            content_objects=len(objects),
            content_bytes=sum(path.stat().st_size for path in objects),
            temporary_files=temporary_files,
        )
