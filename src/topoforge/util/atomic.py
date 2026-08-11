"""Crash-resistant same-directory atomic file publication."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """Write bytes through a random exclusive temporary and atomically replace the target."""
    destination = Path(os.path.abspath(path.expanduser()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as cleanup_error:
                error.add_note(
                    f"failed to close temporary output descriptor for {destination}: "
                    f"{cleanup_error}"
                )
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                error.add_note(f"failed to remove temporary output {temporary}: {cleanup_error}")
        raise
    return destination
