"""Streaming SHA-256 helpers used by provenance and verification."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(value: bytes) -> str:
    """Return the hexadecimal SHA-256 digest of bytes."""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
