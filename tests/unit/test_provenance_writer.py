from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

import topoforge.provenance.writer as writer_module
from topoforge.provenance import write_json, write_validation_html


def _write_json_fixture(path: Path) -> Path:
    return write_json(path, {"ready": True})


def _write_html_fixture(path: Path) -> Path:
    return write_validation_html(path, {})


def test_write_json_preserves_exact_bytes_and_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    real_fsync = writer_module.os.fsync

    def record_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(writer_module.os, "fsync", record_fsync)
    destination = tmp_path / "report.json"

    assert write_json(destination, {"z": "雪", "a": 1}) == destination

    expected = '{\n  "a": 1,\n  "z": "雪"\n}\n'.replace("\n", os.linesep).encode("utf-8")
    assert destination.read_bytes() == expected
    assert len(fsync_calls) == (1 if os.name == "nt" else 2)


def test_write_validation_html_preserves_exact_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "validation.html"

    assert write_validation_html(destination, {}) == destination

    expected = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TopoForge validation</title>
<style>body { font: 15px system-ui; margin: 2rem; max-width: 1100px; color: #1d252b; }
h1 { margin-bottom: .2rem; }
.status { font-weight: 700; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; border: 1px solid #ccd4d9; padding: .55rem; }
th { width: 32%; background: #f2f5f6; }
code { white-space: pre-wrap; }</style>
</head>
<body>
<h1>TopoForge validation</h1>
<p class="status">Required checks: FAIL</p>
<p>Self-intersection status is literal. An unavailable exhaustive test is not shown as passed.</p>
<table></table>
</body>
</html>
""".replace("\n", os.linesep).encode("utf-8")
    assert destination.read_bytes() == expected


@pytest.mark.parametrize(
    ("filename", "writer"),
    [
        pytest.param("report.json", _write_json_fixture, id="json"),
        pytest.param("validation.html", _write_html_fixture, id="html"),
    ],
)
def test_writers_do_not_follow_legacy_fixed_temp_symlink(
    tmp_path: Path,
    filename: str,
    writer: Callable[[Path], Path],
) -> None:
    destination = tmp_path / filename
    external = tmp_path / "external.txt"
    external.write_bytes(b"preserve")
    legacy_temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        legacy_temporary.symlink_to(external)
    except OSError:
        pytest.skip("host cannot create symlink fixture")

    assert writer(destination) == destination

    assert external.read_bytes() == b"preserve"
    assert legacy_temporary.is_symlink()
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_atomic_writer_failure_preserves_destination_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "report.json"
    destination.write_bytes(b"original")

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(writer_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        write_json(destination, {"replacement": True})

    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))
