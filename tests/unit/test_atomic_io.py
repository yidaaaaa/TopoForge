from __future__ import annotations

import os
from pathlib import Path

import pytest

import topoforge.util.atomic as atomic_module
from topoforge.util import atomic_write_bytes


def test_atomic_write_bytes_uses_random_temporary_and_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    replacements: list[tuple[Path, Path]] = []
    real_fsync = atomic_module.os.fsync
    real_replace = atomic_module.os.replace

    def record_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    def record_replace(source: str | Path, target: str | Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(atomic_module.os, "fsync", record_fsync)
    monkeypatch.setattr(atomic_module.os, "replace", record_replace)
    destination = tmp_path / "record.json"

    assert atomic_write_bytes(destination, b"stable\n") == destination

    assert destination.read_bytes() == b"stable\n"
    assert len(fsync_calls) == (1 if os.name == "nt" else 2)
    assert len(replacements) == 1
    temporary, replaced_destination = replacements[0]
    assert replaced_destination == destination
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(".record.json.")
    assert temporary.name.endswith(".tmp")
    assert temporary.name != ".record.json.tmp"
    assert not temporary.exists()


def test_atomic_write_bytes_failure_preserves_target_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "record.json"
    destination.write_bytes(b"original\n")

    def fail_replace(_source: str | Path, _target: str | Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        atomic_write_bytes(destination, b"replacement\n")

    assert destination.read_bytes() == b"original\n"
    assert not list(tmp_path.glob(".record.json.*.tmp"))
