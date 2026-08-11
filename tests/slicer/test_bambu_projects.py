"""Bambu project version and macOS isolation regressions."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import shutil
import stat
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest
import rasterio

import topoforge.validation.bambu_projects as bambu_projects_module
from topoforge.engine import build_local_terrain
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import (
    TileLayoutConfig,
    extract_tile_set,
    generate_print_tile_set,
    generate_tile_mesh_set,
    plan_tile_layout,
    slice_print_tile_set,
    write_tile_layout,
)
from topoforge.validation.bambu_projects import (
    archive_evidence,
    frozen_source_bambu_version,
    generate_bambu_project_evidence,
    isolated_environment,
    probe_bambu_studio,
    release_gate,
    verify_bambu_project_evidence,
    write_canonical,
)
from topoforge.validation.slicers import (
    BambuStudioAdapter,
    CommandExecution,
    SlicerProfile,
)


class _ProbeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, cwd
        self.calls.append((tuple(command), env))
        return CommandExecution(0, "BambuStudio-02.07.01.62:", "", 0.01)


def test_darwin_bambu_environment_uses_private_macos_user_directories(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    environment = isolated_environment(
        runtime,
        platform_name="darwin",
        environ={
            "PATH": "/usr/bin",
            "APPIMAGE_EXTRACT_AND_RUN": "1",
            "XDG_CONFIG_HOME": "/real/config",
            "XDG_CACHE_HOME": "/real/cache",
            "XDG_RUNTIME_DIR": "/real/runtime",
        },
    )

    home = runtime / "home"
    assert environment["HOME"] == str(home)
    assert environment["CFFIXED_USER_HOME"] == str(home)
    assert environment["TMPDIR"] == str(runtime / "tmp")
    assert "APPIMAGE_EXTRACT_AND_RUN" not in environment
    assert not any(key.startswith("XDG_") for key in environment)
    assert (home / "Library" / "Application Support").is_dir()
    assert (home / "Library" / "Preferences").is_dir()
    assert (home / "Library" / "Caches").is_dir()


def test_project_gcode_version_must_match_frozen_source_slice(tmp_path: Path) -> None:
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("; BambuStudio 03.00.00.01\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match the frozen"):
        release_gate(gcode, expected_version="02.07.01.62", stdout="", stderr="")


def test_frozen_source_version_requires_available_bambu_probe() -> None:
    manifest: dict[str, Any] = {
        "slicer": {
            "name": "BambuStudio",
            "version": "02.07.01.62",
            "status": "available",
        }
    }
    assert frozen_source_bambu_version(manifest) == "02.07.01.62"

    manifest["slicer"]["version"] = None
    with pytest.raises(RuntimeError, match="must freeze"):
        frozen_source_bambu_version(manifest)


def test_probe_record_is_version_parsed_and_hash_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _ProbeRunner()
    monkeypatch.setattr(bambu_projects_module, "run_command", runner)
    executable = tmp_path / "BambuStudio"
    executable.write_bytes(b"test executable identity\n")
    executable.chmod(0o755)

    report = probe_bambu_studio(
        executable,
        runtime=tmp_path / "probe",
        timeout_seconds=5,
        evidence_root=tmp_path / "evidence",
    )

    assert report["version"] == "02.07.01.62"
    assert report["process_exit_code"] == 0
    assert len(report["stdout_sha256"]) == 64
    assert len(report["stderr_sha256"]) == 64
    assert report["stdout_path"] == "bambu-studio-probe.stdout.log"
    assert report["stderr_path"] == "bambu-studio-probe.stderr.log"
    assert (tmp_path / "evidence" / report["stdout_path"]).read_text() == (
        "BambuStudio-02.07.01.62:"
    )
    assert runner.calls[0][0] == (str(executable), "--help")
    assert runner.calls[0][1] is not None


_MODEL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources><object id="1" type="model"><mesh>
    <vertices>
      <vertex x="0" y="0" z="0"/>
      <vertex x="1" y="0" z="0"/>
      <vertex x="0" y="2" z="3"/>
    </vertices>
    <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
  </mesh></object></resources>
  <build><item objectid="1"/></build>
</model>
"""
_RELATIONSHIPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""


def _write_project_archive(
    path: Path,
    gcode: bytes,
    *,
    extra_members: Sequence[tuple[str | ZipInfo, bytes]] = (),
    compression: int = ZIP_DEFLATED,
    relationships: bytes = _RELATIONSHIPS,
    model_xml: bytes = _MODEL_XML,
) -> None:
    digest = hashlib.md5(gcode, usedforsecurity=False).hexdigest().upper().encode("ascii")
    members: list[tuple[str | ZipInfo, bytes]] = [
        ("[Content_Types].xml", b"<Types/>"),
        ("_rels/.rels", relationships),
        ("3D/3dmodel.model", model_xml),
        ("Metadata/plate_1.gcode", gcode),
        ("Metadata/plate_1.gcode.md5", digest),
        ("Metadata/project_settings.config", b"{}"),
        *extra_members,
    ]
    with ZipFile(path, "w", compression=compression) as package:
        for name, payload in members:
            package.writestr(name, payload)


def _central_directory_infos() -> list[ZipInfo]:
    infos: list[ZipInfo] = []
    for name in sorted(bambu_projects_module._PROJECT_REQUIRED_MEMBERS):
        info = ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        info.compress_type = ZIP_STORED
        info.file_size = 1
        info.compress_size = 1
        infos.append(info)
    return infos


class _CentralDirectory:
    def __init__(self, infos: list[ZipInfo]) -> None:
        self._infos = infos

    def infolist(self) -> list[ZipInfo]:
        return self._infos


def test_archive_evidence_streams_and_measures_the_project_model(tmp_path: Path) -> None:
    gcode = b"; BambuStudio 02.07.01.62\n"
    primary = tmp_path / "primary.gcode"
    project = tmp_path / "project.3mf"
    primary.write_bytes(gcode)
    _write_project_archive(project, gcode)

    evidence = archive_evidence(project, primary)

    assert evidence["archive_test_passed"] is True
    assert evidence["embedded_gcode_md5_verified"] is True
    assert evidence["embedded_gcode_matches_primary"] is True
    assert evidence["project_model_dimensions_mm"] == [1.0, 2.0, 3.0]
    assert evidence["project_model_triangle_count"] == 1


def test_archive_rejects_duplicate_raw_member_names(tmp_path: Path) -> None:
    gcode = b"; BambuStudio 02.07.01.62\n"
    primary = tmp_path / "primary.gcode"
    project = tmp_path / "duplicate.3mf"
    primary.write_bytes(gcode)
    with pytest.warns(UserWarning, match="Duplicate name"):
        _write_project_archive(
            project,
            gcode,
            extra_members=(("Metadata/plate_1.gcode", gcode),),
        )

    with pytest.raises(RuntimeError, match="duplicate member"):
        archive_evidence(project, primary)


@pytest.mark.parametrize(
    "member_name",
    [
        "metadata/plate_1.gcode",
        "../escape",
        "Metadata\\\\plate_1.gcode",
        "Metadata/cafe\u0301.config",
        "Metadata/NUL.txt",
        "Metadata/./hidden.config",
        "Metadata/trailing//",
    ],
)
def test_archive_rejects_aliasing_or_unsafe_member_names(tmp_path: Path, member_name: str) -> None:
    gcode = b"; BambuStudio 02.07.01.62\n"
    primary = tmp_path / "primary.gcode"
    project = tmp_path / "unsafe.3mf"
    primary.write_bytes(gcode)
    _write_project_archive(project, gcode, extra_members=((member_name, b"x"),))

    with pytest.raises(RuntimeError, match=r"collision|unsafe|canonical|safe relative"):
        archive_evidence(project, primary)


def test_archive_rejects_nonregular_and_unsupported_members(tmp_path: Path) -> None:
    gcode = b"; BambuStudio 02.07.01.62\n"
    primary = tmp_path / "primary.gcode"
    primary.write_bytes(gcode)
    link = ZipInfo("Metadata/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    linked_project = tmp_path / "linked.3mf"
    _write_project_archive(linked_project, gcode, extra_members=((link, b"target"),))

    with pytest.raises(RuntimeError, match="not a regular"):
        archive_evidence(linked_project, primary)

    compressed_project = tmp_path / "unsupported.3mf"
    _write_project_archive(compressed_project, gcode, compression=ZIP_BZIP2)
    with pytest.raises(RuntimeError, match="unsupported compression"):
        archive_evidence(compressed_project, primary)


def test_archive_rejects_external_relationships(tmp_path: Path) -> None:
    gcode = b"; BambuStudio 02.07.01.62\n"
    primary = tmp_path / "primary.gcode"
    project = tmp_path / "external.3mf"
    primary.write_bytes(gcode)
    _write_project_archive(
        project,
        gcode,
        relationships=(b'<Relationships><Relationship TargetMode="External"/></Relationships>'),
    )

    with pytest.raises(RuntimeError, match="external relationship"):
        archive_evidence(project, primary)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("encrypted", "encrypted member"),
        ("member_count", "member count"),
        ("member_size", "byte limit"),
        ("total_size", "total uncompressed"),
        ("compression_ratio", "compression ratio"),
        ("raw_nul", "raw name"),
        ("unsupported_flags", "unsupported flags"),
    ],
)
def test_central_directory_limits_fail_before_archive_reads(
    case: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos = _central_directory_infos()
    if case == "encrypted":
        infos[0].flag_bits |= 0x1
    elif case == "member_count":
        monkeypatch.setattr(bambu_projects_module, "_PROJECT_MAX_MEMBERS", len(infos) - 1)
    elif case == "member_size":
        monkeypatch.setattr(bambu_projects_module, "_PROJECT_MAX_MEMBER_BYTES", 0)
    elif case == "total_size":
        monkeypatch.setattr(
            bambu_projects_module,
            "_PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES",
            len(infos) - 1,
        )
    elif case == "compression_ratio":
        infos[0].file_size = 3
        monkeypatch.setattr(bambu_projects_module, "_PROJECT_MAX_COMPRESSION_RATIO", 2.0)
    elif case == "raw_nul":
        infos[0].orig_filename = f"{infos[0].filename}\x00hidden"
    elif case == "unsupported_flags":
        infos[0].flag_bits |= 0x20
    else:
        raise AssertionError(case)

    with pytest.raises(RuntimeError, match=message):
        bambu_projects_module._validated_project_members(
            cast(Any, _CentralDirectory(infos)),
            project=tmp_path / "unread.3mf",
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("classic_count", "member count"),
        ("classic_size", "central directory size"),
        ("actual_count", "entry count does not match"),
        ("zip64_count", "member count"),
        ("concatenated_directory_gap", "does not end immediately"),
        ("zip64_legacy_offset", "ZIP64 central-directory offset disagrees"),
    ],
)
def test_zip_metadata_limits_fail_before_zipfile_construction(
    case: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "metadata-only.3mf"
    primary = tmp_path / "primary.gcode"
    primary.write_bytes(b"; BambuStudio 02.07.01.62\n")
    excessive_entries = bambu_projects_module._PROJECT_MAX_MEMBERS + 1
    if case == "classic_count":
        payload = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            excessive_entries,
            excessive_entries,
            0,
            0,
            0,
        )
    elif case == "classic_size":
        payload = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            0,
            0,
            bambu_projects_module._PROJECT_MAX_CENTRAL_DIRECTORY_BYTES + 1,
            0,
            0,
        )
    elif case == "actual_count":
        central_directory = (b"PK\x01\x02" + b"\x00" * 42) * 2
        payload = central_directory + struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            len(central_directory),
            0,
            0,
        )
    elif case == "zip64_count":
        zip64 = struct.pack(
            "<4sQHHIIQQQQ",
            b"PK\x06\x06",
            44,
            45,
            45,
            0,
            0,
            excessive_entries,
            excessive_entries,
            0,
            0,
        )
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
        eocd = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        payload = zip64 + locator + eocd
    elif case == "concatenated_directory_gap":
        central_directory = b"PK\x01\x02" + b"\x00" * 42
        payload = (
            central_directory
            + b"untrusted concatenation gap"
            + central_directory
            + struct.pack(
                "<4sHHHHIIH",
                b"PK\x05\x06",
                0,
                0,
                1,
                1,
                len(central_directory),
                0,
                0,
            )
        )
    elif case == "zip64_legacy_offset":
        central_directory = b"PK\x01\x02" + b"\x00" * 42
        zip64 = struct.pack(
            "<4sQHHIIQQQQ",
            b"PK\x06\x06",
            44,
            45,
            45,
            0,
            0,
            1,
            1,
            len(central_directory),
            0,
        )
        locator = struct.pack(
            "<4sIQI",
            b"PK\x06\x07",
            0,
            len(central_directory),
            1,
        )
        eocd = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            len(central_directory),
            1,
            0,
        )
        payload = central_directory + zip64 + locator + eocd
    else:
        raise AssertionError(case)
    project.write_bytes(payload)

    def forbidden_zipfile(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ZipFile must not be constructed for oversized metadata")

    monkeypatch.setattr(bambu_projects_module, "ZipFile", forbidden_zipfile)

    with pytest.raises(RuntimeError, match=message):
        archive_evidence(project, primary)


def test_canonical_writer_does_not_follow_the_legacy_fixed_temp_symlink(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "manifest.json"
    victim = tmp_path / "victim.txt"
    legacy_temporary = tmp_path / ".manifest.json.tmp"
    victim.write_text("preserve me", encoding="utf-8")
    try:
        legacy_temporary.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    write_canonical(destination, {"safe": True})

    assert victim.read_text(encoding="utf-8") == "preserve me"
    assert legacy_temporary.is_symlink()
    assert destination.read_bytes() == b'{"safe":true}\n'


def test_sha256_reads_the_pinned_file_after_final_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    detached = tmp_path / "source.original.bin"
    replacement = tmp_path / "replacement.bin"
    original = b"trusted evidence"
    source.write_bytes(original)
    replacement.write_bytes(b"attacker replacement")
    real_open = bambu_projects_module.os.open
    swapped = False

    def racing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and dir_fd is not None and path == source.name:
            swapped = True
            source.rename(detached)
            replacement.rename(source)
        return descriptor

    monkeypatch.setattr(bambu_projects_module.os, "open", racing_open)

    assert bambu_projects_module.sha256(source) == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == b"attacker replacement"


def test_regular_evidence_rejects_preexisting_hard_links(tmp_path: Path) -> None:
    source = tmp_path / "evidence.bin"
    alias = tmp_path / "evidence-alias.bin"
    source.write_bytes(b"sensitive-token")
    try:
        os.link(source, alias)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.raises(RuntimeError, match="exactly one hard link"):
        bambu_projects_module.sha256(source)


def test_pinned_evidence_detects_a_hard_link_created_after_open(tmp_path: Path) -> None:
    source = tmp_path / "evidence.bin"
    alias = tmp_path / "opened-after-hardlink.bin"
    source.write_bytes(b"sensitive-token")

    with bambu_projects_module._open_pinned_regular_file(
        source,
        label="test evidence",
    ) as pinned:
        try:
            os.link(source, alias)
        except OSError as exc:
            pytest.skip(f"hard links are unavailable: {exc}")
        with pytest.raises(RuntimeError, match="changed while it was being read"):
            bambu_projects_module._require_pinned_unchanged(
                pinned,
                label="test evidence",
            )


def test_snapshot_copy_uses_the_already_pinned_file_after_path_swap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated.3mf"
    detached = tmp_path / "generated.original.3mf"
    replacement = tmp_path / "replacement.3mf"
    destination = tmp_path / "snapshot" / "generated.3mf"
    original = b"trusted generated bytes"
    source.write_bytes(original)
    replacement.write_bytes(b"attacker replacement")

    with bambu_projects_module._open_pinned_regular_file(
        source,
        label="generated evidence",
    ) as pinned:
        source.rename(detached)
        replacement.rename(source)
        digest, size = bambu_projects_module._copy_pinned_regular_file_snapshot(
            pinned,
            destination,
            label="generated evidence",
            maximum_bytes=1024,
        )

    assert destination.read_bytes() == original
    assert digest == hashlib.sha256(original).hexdigest()
    assert size == len(original)


def test_generated_json_parse_and_copy_share_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "result.json"
    destination = tmp_path / "evidence" / "result.json"
    original = b'{"return_code":0,"trusted":true}\n'
    source.write_bytes(original)
    real_write = bambu_projects_module._write_atomic_bytes

    def racing_write(path: Path, payload: bytes) -> Path:
        if path == destination:
            source.write_bytes(b'{"return_code":0,"attacker":true}\n')
        return real_write(path, payload)

    monkeypatch.setattr(bambu_projects_module, "_write_atomic_bytes", racing_write)

    value = bambu_projects_module._snapshot_generated_json(
        source,
        destination,
        label="generated result",
    )

    assert value == {"return_code": 0, "trusted": True}
    assert destination.read_bytes() == original
    assert b"attacker" in source.read_bytes()


def test_json_snapshot_fails_closed_after_parent_path_swap_without_reading_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "evidence"
    detached_parent = tmp_path / "evidence.original"
    parent.mkdir()
    source = parent / "manifest.json"
    source.write_bytes(b'{"trusted":true}\n')
    real_open = bambu_projects_module.os.open
    swapped = False

    def racing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and dir_fd is not None and path == source.name:
            swapped = True
            parent.rename(detached_parent)
            parent.mkdir()
            source.write_bytes(b'{"attacker":true}\n')
        return descriptor

    monkeypatch.setattr(bambu_projects_module.os, "open", racing_open)
    value, snapshot = bambu_projects_module._load_canonical_json(source)

    assert value == {"trusted": True}
    assert snapshot.sha256 == hashlib.sha256(b'{"trusted":true}\n').hexdigest()
    assert source.read_bytes() == b'{"attacker":true}\n'


def test_gcode_reader_keeps_the_pinned_handle_after_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "model.gcode"
    detached = tmp_path / "model.original.gcode"
    replacement = tmp_path / "replacement.gcode"
    source.write_text("; BambuStudio 02.07.01.62\nG1 X1 Y1\n", encoding="utf-8")
    replacement.write_text("; untrusted replacement\n", encoding="utf-8")
    real_open = bambu_projects_module.os.open
    swapped = False

    def racing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and dir_fd is not None and path == source.name:
            swapped = True
            source.rename(detached)
            source.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(bambu_projects_module.os, "open", racing_open)

    assert bambu_projects_module.gcode_bambu_version(source) == "02.07.01.62"
    assert source.is_symlink()


def test_archive_evidence_uses_pinned_zip_and_primary_handles_after_path_swaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary.gcode"
    project = tmp_path / "project.3mf"
    primary_payload = b"; BambuStudio 02.07.01.62\nG1 X1 Y1\n"
    primary.write_bytes(primary_payload)
    _write_project_archive(project, primary_payload)
    project_detached = tmp_path / "project.original.3mf"
    primary_detached = tmp_path / "primary.original.gcode"
    project_replacement = tmp_path / "project.replacement"
    primary_replacement = tmp_path / "primary.replacement"
    project_replacement.write_bytes(b"not a ZIP")
    primary_replacement.write_bytes(b"attacker G-code")
    real_open = bambu_projects_module.os.open
    swapped: set[str] = set()

    def racing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None and path == project.name and "project" not in swapped:
            swapped.add("project")
            project.rename(project_detached)
            project_replacement.rename(project)
        elif dir_fd is not None and path == primary.name and "primary" not in swapped:
            swapped.add("primary")
            primary.rename(primary_detached)
            primary_replacement.rename(primary)
        return descriptor

    monkeypatch.setattr(bambu_projects_module.os, "open", racing_open)
    evidence = archive_evidence(project, primary)

    assert evidence["archive_test_passed"] is True
    assert evidence["embedded_gcode_matches_primary"] is True
    assert swapped == {"project", "primary"}


def test_gcode_snapshot_hashes_full_capacity_but_retains_only_semantic_comments(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.gcode"
    header = b"; BambuStudio 02.07.01.62\n; total layer number: 2\n"
    commands = b"G1 X1 Y1 E1\n" * 200_000
    payload = header + commands
    path.write_bytes(payload)

    snapshot = bambu_projects_module._read_gcode_snapshot(path, label="test G-code")

    assert snapshot.size == len(payload)
    assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
    assert snapshot.text == header.decode("utf-8")
    assert len(snapshot.text.encode("utf-8")) < 1024


def test_gcode_snapshot_fails_before_decoding_an_oversized_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "oversized.gcode"
    path.write_bytes(b"; BambuStudio 02.07.01.62\n")
    monkeypatch.setattr(bambu_projects_module, "_PROJECT_MAX_GCODE_TEXT_BYTES", 8)

    with pytest.raises(RuntimeError, match="outside the supported"):
        bambu_projects_module._read_gcode_snapshot(path, label="test G-code")


def test_windows_fallback_opens_regular_files_without_opening_the_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "windows-evidence.bin"
    source.write_bytes(b"portable")
    real_open = bambu_projects_module.os.open
    opened_paths: list[Any] = []

    def recording_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        opened_paths.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        bambu_projects_module,
        "_descriptor_relative_supported",
        lambda: False,
    )
    monkeypatch.setattr(bambu_projects_module.os, "open", recording_open)

    assert bambu_projects_module.sha256(source) == hashlib.sha256(b"portable").hexdigest()
    assert opened_paths == [source.resolve()]


def test_windows_fallback_writes_and_snapshots_without_directory_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.3mf"
    source.write_bytes(b"portable snapshot")
    snapshot = tmp_path / "snapshots" / "source.3mf"
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(
        bambu_projects_module,
        "_descriptor_relative_supported",
        lambda: False,
    )

    digest, size = bambu_projects_module._copy_regular_file_snapshot(
        source,
        snapshot,
        label="Windows source",
        maximum_bytes=1024,
    )
    write_canonical(manifest, {"portable": True})

    assert snapshot.read_bytes() == b"portable snapshot"
    assert size == len(b"portable snapshot")
    assert digest == hashlib.sha256(b"portable snapshot").hexdigest()
    assert manifest.read_bytes() == b'{"portable":true}\n'


def test_canonical_writer_replaces_destination_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "manifest.json"
    victim = tmp_path / "victim.json"
    victim.write_text("preserve me", encoding="utf-8")
    try:
        destination.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    write_canonical(destination, {"safe": True})

    assert not destination.is_symlink()
    assert destination.read_bytes() == b'{"safe":true}\n'
    assert victim.read_text(encoding="utf-8") == "preserve me"


def test_canonical_writer_anchors_replace_when_parent_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "evidence"
    detached_parent = tmp_path / "evidence.original"
    parent.mkdir()
    destination = parent / "manifest.json"
    real_replace = bambu_projects_module.os.replace
    swapped = False

    def racing_replace(
        source: Any,
        target: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(detached_parent)
            parent.mkdir()
            destination.write_text("attacker file", encoding="utf-8")
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(bambu_projects_module.os, "replace", racing_replace)

    with pytest.raises(RuntimeError, match="parent changed during publication"):
        write_canonical(destination, {"safe": True})

    assert destination.read_text(encoding="utf-8") == "attacker file"
    assert (detached_parent / destination.name).read_bytes() == b'{"safe":true}\n'


@pytest.mark.parametrize("operation", ["copy", "atomic_write"])
@pytest.mark.parametrize("force_fallback", [False, True])
def test_nested_symlink_destination_does_not_create_victim_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    force_fallback: bool,
) -> None:
    source = tmp_path / "source.3mf"
    source.write_bytes(b"trusted")
    victim_parent = tmp_path / "victim"
    victim_parent.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    linked = anchor / "linked"
    try:
        linked.symlink_to(victim_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    destination = linked / "missing" / "nested" / "snapshot.3mf"
    if force_fallback:
        monkeypatch.setattr(
            bambu_projects_module,
            "_descriptor_relative_supported",
            lambda: False,
        )

    with pytest.raises(RuntimeError, match=r"symbolic link|reparse|without following"):
        if operation == "copy":
            bambu_projects_module._copy_regular_file_snapshot(
                source,
                destination,
                label="test source",
                maximum_bytes=1024,
            )
        else:
            bambu_projects_module._write_atomic_bytes(destination, b"trusted")

    assert list(victim_parent.iterdir()) == []


_SEMANTIC_GCODE = """; HEADER_BLOCK_START
; BambuStudio 02.07.01.62
; model printing time: 6h 42m 13s; total estimated time: 6h 49m 15s
; total layer number: 224
; total filament length [mm] : 69624.45
; total filament volume [cm^3] : 167466.43
; total filament weight [g] : 211.01
; filament_density: 1.26
; filament_diameter: 1.75
; HEADER_BLOCK_END
; printer_model = Bambu Lab P2S
; printer_settings_id = Bambu Lab P2S 0.4 nozzle
; printer_variant = 0.4
; nozzle_diameter = 0.4
; printable_area = 0x0,256x0,256x256,0x256
; printable_height = 256
; print_settings_id = 0.20mm Standard @BBL P2S
; filament_settings_id = "Bambu PLA Basic @BBL P2S"
; filament_vendor = "Bambu Lab"
; filament_type = PLA
; filament_flow_ratio = 0.98
; filament_max_volumetric_speed = 21
; layer_height = 0.2
; initial_layer_print_height = 0.2
; wall_loops = 2
; top_shell_layers = 5
; bottom_shell_layers = 3
; sparse_infill_density = 15%
; sparse_infill_pattern = grid
; enable_support = 0
; support_type = tree(auto)
; brim_type = auto_brim
; brim_width = 5
; curr_bed_type = Textured PEI Plate
; textured_plate_temp = 55
; nozzle_temperature = 220
"""


def _semantic_model_xml(dimensions_mm: Sequence[float], triangle_count: int) -> bytes:
    width, depth, height = dimensions_mm
    triangles = "".join('<triangle v1="0" v2="1" v3="2"/>' for _ in range(triangle_count))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources><object id="1" type="model"><mesh><vertices>
    <vertex x="0" y="0" z="0"/>
    <vertex x="{width!r}" y="0" z="0"/>
    <vertex x="0" y="{depth!r}" z="{height!r}"/>
  </vertices><triangles>{triangles}</triangles></mesh></object></resources>
  <build><item objectid="1"/></build>
</model>
""".encode()


def _semantic_result(dimensions_mm: Sequence[float], triangle_count: int) -> dict[str, Any]:
    return {
        "error_string": "Success.",
        "return_code": 0,
        "sliced_plates": [
            {
                "warning_message": "",
                "objects": [
                    {
                        "name": "terrain",
                        "triangle_count": triangle_count,
                        "bbox": {
                            "width": dimensions_mm[0],
                            "depth": dimensions_mm[1],
                            "height": dimensions_mm[2],
                            "x": 0.0,
                            "y": 0.0,
                            "z": 0.0,
                        },
                    }
                ],
            }
        ],
    }


class _SemanticBambuRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, env, cwd
        normalized = tuple(command)
        self.calls.append(normalized)
        if "--help" in normalized:
            return CommandExecution(0, "BambuStudio-02.07.01.62:", "", 0.01)
        output_dir = Path(normalized[normalized.index("--outputdir") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        input_path = Path(normalized[-1])
        if input_path.name == "model.bambu-p2s.3mf":
            with ZipFile(input_path, "r") as package:
                measurement = bambu_projects_module._project_model_measurement(
                    package.read("3D/3dmodel.model")
                )
            dimensions = measurement["dimensions_mm"]
            triangle_count = measurement["triangle_count"]
        else:
            inspection = inspect_3mf(input_path)
            dimensions = list(inspection.dimensions_mm)
            triangle_count = inspection.triangle_count
        if "--export-3mf" in normalized:
            project_name = normalized[normalized.index("--export-3mf") + 1]
            _write_project_archive(
                output_dir / project_name,
                _SEMANTIC_GCODE.encode("utf-8"),
                model_xml=_semantic_model_xml(dimensions, triangle_count),
            )
        output_dir.joinpath("plate_1.gcode").write_text(
            _SEMANTIC_GCODE,
            encoding="utf-8",
        )
        output_dir.joinpath("result.json").write_text(
            json.dumps(_semantic_result(dimensions, triangle_count)),
            encoding="utf-8",
        )
        return CommandExecution(0, "official slice succeeded", "", 0.2)


@dataclass(frozen=True, slots=True)
class _SemanticEvidence:
    print_set: Path
    slice_set: Path
    project_set: Path
    executable: Path


def _semantic_print_set(root: Path) -> tuple[Path, Path, Path, Path]:
    source = create_synthetic_geotiff(
        root / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=8,
        columns=12,
        pixel_size_m=20.0,
    )
    bundle = root / "bundle"
    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=bundle,
            model_width_mm=60.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=20_000,
        )
    )
    with rasterio.open(result.artifacts["processed_dem"]) as dataset:
        shape = dataset.shape
    dimensions = result.validation["dimensions_mm"]
    layout = plan_tile_layout(
        TileLayoutConfig(
            source_grid_shape=shape,
            model_width_mm=float(dimensions[0]),
            model_depth_mm=float(dimensions[1]),
            maximum_tile_width_mm=35.0,
            maximum_tile_depth_mm=100.0,
            overlap_cells=1,
        )
    )
    layout_path = write_tile_layout(layout, root / "tile-layout.json")
    tile_set = extract_tile_set(bundle, layout_path, root / "tile-set").output_dir
    mesh_set = generate_tile_mesh_set(tile_set, bundle, root / "mesh-set").output_dir
    print_set = generate_print_tile_set(mesh_set, tile_set, bundle, root / "print-set").output_dir
    return bundle, tile_set, mesh_set, print_set


@pytest.fixture(scope="module")
def semantic_evidence(tmp_path_factory: pytest.TempPathFactory) -> _SemanticEvidence:
    root = tmp_path_factory.mktemp("bambu-semantic-evidence")
    bundle, tile_set, mesh_set, print_set = _semantic_print_set(root)
    executable = root / "BambuStudio"
    executable.write_text("fake official executable\n", encoding="utf-8")
    executable.chmod(0o755)
    machine = root / "machine.json"
    process = root / "process.json"
    filament = root / "filament.json"
    machine.write_text('{"machine":"P2S"}\n', encoding="utf-8")
    process.write_text('{"process":"standard"}\n', encoding="utf-8")
    filament.write_text('{"filament":"PLA"}\n', encoding="utf-8")
    runner = _SemanticBambuRunner()
    slice_result = slice_print_tile_set(
        print_set,
        mesh_set,
        tile_set,
        bundle,
        root / "slice-set",
        adapter=BambuStudioAdapter(executable, runner=runner),
        profile=SlicerProfile(
            name="P2S release profiles",
            settings=(machine, process),
            filaments=(filament,),
        ),
        timeout_seconds=30.0,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bambu_projects_module, "run_command", runner)
        project_result = generate_bambu_project_evidence(
            print_set,
            slice_result.output_dir,
            executable,
            root / "project-set",
            timeout_seconds=30.0,
        )
    return _SemanticEvidence(
        print_set=print_set,
        slice_set=slice_result.output_dir,
        project_set=project_result.output_dir,
        executable=executable.resolve(),
    )


def _verify_semantic_evidence(fixture: _SemanticEvidence, project_set: Path) -> dict[str, Any]:
    return verify_bambu_project_evidence(
        project_set,
        print_set_dir=fixture.print_set,
        slice_set_dir=fixture.slice_set,
        bambu_studio=fixture.executable,
    )


def test_semantic_evidence_baseline_reopens_independently(
    semantic_evidence: _SemanticEvidence,
) -> None:
    verification = _verify_semantic_evidence(semantic_evidence, semantic_evidence.project_set)

    assert verification["status"] == "verified"
    assert verification["tile_count"] == 2
    assert verification["required_checks_passed"] is True


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "extra", "reordered"],
)
def test_semantic_verifier_rejects_resealed_tile_set_changes(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    mutation: str,
) -> None:
    project_set = tmp_path / "project-set"
    shutil.copytree(semantic_evidence.project_set, project_set)
    manifest_path = project_set / "bambu-tile-project-manifest.json"
    manifest = bambu_projects_module.load_json(manifest_path)
    records = manifest["tiles"]
    assert isinstance(records, list) and len(records) == 2
    if mutation == "missing":
        records = records[:-1]
    elif mutation == "duplicate":
        records = [records[0], copy.deepcopy(records[0])]
    elif mutation == "extra":
        extra = copy.deepcopy(records[0])
        extra["tile_id"] = "tile-r9999-c9999"
        records = [*records, extra]
    elif mutation == "reordered":
        records = list(reversed(records))
    else:
        raise AssertionError(mutation)
    manifest["tiles"] = records
    manifest["tile_count"] = len(records)
    write_canonical(manifest_path, manifest)

    with pytest.raises(
        RuntimeError,
        match=r"root identities|missing, duplicate, extra, or reordered",
    ):
        _verify_semantic_evidence(semantic_evidence, project_set)


def test_semantic_verifier_rejects_resealed_profile_substitution(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
) -> None:
    project_set = tmp_path / "project-set"
    shutil.copytree(semantic_evidence.project_set, project_set)
    manifest_path = project_set / "bambu-tile-project-manifest.json"
    manifest = bambu_projects_module.load_json(manifest_path)
    profile_record = manifest["profile_files"][0]
    profile_path = project_set / profile_record["path"]
    profile_path.write_text('{"substituted":true}\n', encoding="utf-8")
    profile_record["sha256"] = bambu_projects_module.sha256(profile_path)
    write_canonical(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="profile identity differs"):
        _verify_semantic_evidence(semantic_evidence, project_set)


def test_semantic_verifier_rejects_fully_resealed_dimensions_and_triangles(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
) -> None:
    project_set = tmp_path / "project-set"
    shutil.copytree(semantic_evidence.project_set, project_set)
    manifest_path = project_set / "bambu-tile-project-manifest.json"
    manifest = bambu_projects_module.load_json(manifest_path)
    record = manifest["tiles"][0]
    validation_path = project_set / record["validation_path"]
    validation = bambu_projects_module.load_json(validation_path)
    fake_dimensions = [value + 1.0 for value in validation["source_dimensions_mm"]]
    fake_triangles = validation["source_triangle_count"] + 1
    build_result = _semantic_result(fake_dimensions, fake_triangles)
    reopen_result = _semantic_result(fake_dimensions, fake_triangles)
    build_result_path = project_set / record["files"]["build_result"]
    reopen_result_path = project_set / record["files"]["reopen_result"]
    write_canonical(build_result_path, build_result)
    write_canonical(reopen_result_path, reopen_result)
    project_path = project_set / record["files"]["bambu_project_3mf"]
    primary_path = project_set / record["files"]["primary_gcode"]
    _write_project_archive(
        project_path,
        primary_path.read_bytes(),
        model_xml=_semantic_model_xml(fake_dimensions, fake_triangles),
    )
    validation["source_dimensions_mm"] = fake_dimensions
    validation["source_triangle_count"] = fake_triangles
    validation["build_result"] = build_result
    validation["reopen_result"] = reopen_result
    validation["build_object"] = bambu_projects_module.object_measurement(build_result)
    validation["reopen_object"] = bambu_projects_module.object_measurement(reopen_result)
    validation["project_archive"] = archive_evidence(project_path, primary_path)
    validation["dimensions_match"] = True
    validation["triangle_counts_match"] = True
    validation["required_checks_passed"] = True
    write_canonical(validation_path, validation)
    record["validation_sha256"] = bambu_projects_module.sha256(validation_path)
    for role, relative in record["files"].items():
        record["sha256"][role] = bambu_projects_module.sha256(project_set / relative)
    write_canonical(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="semantic validation changed"):
        _verify_semantic_evidence(semantic_evidence, project_set)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "extra", "reordered"],
)
def test_build_rejects_malformed_source_slice_tile_sets_before_execution(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    slice_set = tmp_path / "slice-set"
    shutil.copytree(semantic_evidence.slice_set, slice_set)
    manifest_path = slice_set / "tile-slice-manifest.json"
    manifest = bambu_projects_module.load_json(manifest_path)
    records = manifest["tiles"]
    assert isinstance(records, list) and len(records) == 2
    if mutation == "missing":
        records = records[:-1]
    elif mutation == "duplicate":
        records = [records[0], copy.deepcopy(records[0])]
    elif mutation == "extra":
        records = [*records, copy.deepcopy(records[0])]
    elif mutation == "reordered":
        records = list(reversed(records))
    else:
        raise AssertionError(mutation)
    manifest["tiles"] = records
    write_canonical(manifest_path, manifest)
    calls: list[tuple[str, ...]] = []

    def forbidden_runner(
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, env, cwd
        calls.append(tuple(command))
        raise AssertionError("Bambu must not execute for an invalid source tile set")

    monkeypatch.setattr(bambu_projects_module, "run_command", forbidden_runner)
    with pytest.raises(RuntimeError, match="does not match PrintTileSliceManifest"):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            slice_set,
            semantic_evidence.executable,
            tmp_path / "should-not-exist",
            timeout_seconds=5.0,
        )
    assert calls == []


def test_build_rejects_unsafe_resealed_source_tile_id_before_execution(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    print_set = tmp_path / "print-set"
    slice_set = tmp_path / "slice-set"
    shutil.copytree(semantic_evidence.print_set, print_set)
    shutil.copytree(semantic_evidence.slice_set, slice_set)
    print_manifest_path = print_set / "print-tile-assembly-manifest.json"
    slice_manifest_path = slice_set / "tile-slice-manifest.json"
    print_manifest = bambu_projects_module.load_json(print_manifest_path)
    slice_manifest = bambu_projects_module.load_json(slice_manifest_path)
    print_manifest["tiles"][0]["tile_id"] = "../../escape"
    slice_manifest["tiles"][0]["tile_id"] = "../../escape"
    write_canonical(print_manifest_path, print_manifest)
    slice_manifest["source_print_tile_assembly_sha256"] = bambu_projects_module.sha256(
        print_manifest_path
    )
    write_canonical(slice_manifest_path, slice_manifest)
    calls: list[tuple[str, ...]] = []

    def forbidden_runner(
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, env, cwd
        calls.append(tuple(command))
        raise AssertionError("Bambu must not execute for an unsafe source tile id")

    monkeypatch.setattr(bambu_projects_module, "run_command", forbidden_runner)
    with pytest.raises(RuntimeError, match="id/row/column/path binding changed"):
        generate_bambu_project_evidence(
            print_set,
            slice_set,
            semantic_evidence.executable,
            tmp_path / "should-not-exist",
            timeout_seconds=5.0,
        )
    assert calls == []


def test_project_model_measurement_uses_only_the_transformed_build_graph() -> None:
    unused_object = b"""<object id="2" type="model"><mesh>
    <vertices><vertex x="0" y="0" z="0"/><vertex x="999" y="0" z="0"/>
    <vertex x="0" y="999" z="999"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
    </mesh></object>"""
    with_unused = _MODEL_XML.replace(
        b"</mesh></object></resources>",
        b"</mesh></object>" + unused_object + b"</resources>",
    )
    transformed = _MODEL_XML.replace(
        b'<item objectid="1"/>',
        b'<item objectid="1" transform="2 0 0 0 3 0 0 0 4 10 20 30"/>',
    )

    unused_measurement = bambu_projects_module._project_model_measurement(with_unused)
    transformed_measurement = bambu_projects_module._project_model_measurement(transformed)

    assert unused_measurement == {
        "dimensions_mm": [1.0, 2.0, 3.0],
        "triangle_count": 1,
    }
    assert transformed_measurement == {
        "dimensions_mm": [2.0, 6.0, 12.0],
        "triangle_count": 1,
    }


def test_project_model_measurement_streams_without_full_tree_fromstring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_fromstring(_payload: Any) -> Any:
        raise AssertionError("model measurement must stream XML")

    monkeypatch.setattr(bambu_projects_module.ET, "fromstring", forbidden_fromstring)

    measurement = bambu_projects_module._project_model_measurement(
        _semantic_model_xml((30.0, 20.0, 4.0), 2)
    )

    assert measurement["dimensions_mm"] == [30.0, 20.0, 4.0]
    assert measurement["triangle_count"] == 2


def _shared_component_dag_model(depth: int) -> bytes:
    objects = [
        """<object id="1" type="model"><mesh><vertices>
        <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>
        <vertex x="0" y="1" z="1"/></vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
        </mesh></object>"""
    ]
    for object_id in range(2, depth + 2):
        referenced_id = object_id - 1
        objects.append(
            f'<object id="{object_id}" type="model"><components>'
            f'<component objectid="{referenced_id}"/>'
            f'<component objectid="{referenced_id}"/>'
            "</components></object>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{''.join(objects)}</resources>"
        f'<build><item objectid="{depth + 1}"/></build></model>'
    ).encode()


@pytest.mark.parametrize(
    ("constant", "limit", "message"),
    [
        ("_PROJECT_MAX_EXPANDED_INSTANCES", 1_000, "object instance limit"),
        ("_PROJECT_MAX_EXPANDED_VERTICES", 1_000, "vertex limit"),
        ("_PROJECT_MAX_EXPANDED_TRIANGLES", 1_000, "triangle limit"),
        ("_PROJECT_MAX_COMPONENT_DEPTH", 4, "depth limit"),
    ],
)
def test_project_model_measurement_preflights_shared_dag_expansion(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    message: str,
) -> None:
    monkeypatch.setattr(bambu_projects_module, constant, limit)

    with pytest.raises(RuntimeError, match=message):
        bambu_projects_module._project_model_measurement(_shared_component_dag_model(12))


def test_project_model_measurement_rejects_out_of_range_triangle_indices() -> None:
    invalid = _MODEL_XML.replace(b'v3="2"', b'v3="99"')

    with pytest.raises(RuntimeError, match="triangle indices are invalid"):
        bambu_projects_module._project_model_measurement(invalid)


def test_atomic_publication_never_replaces_an_existing_empty_directory(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "stage"
    output = tmp_path / "output"
    staging.mkdir()
    (staging / "manifest.json").write_text("trusted", encoding="utf-8")
    output.mkdir()
    output_identity = (output.stat().st_dev, output.stat().st_ino)

    with pytest.raises(RuntimeError, match="destination already exists"):
        bambu_projects_module._publish_directory_no_replace(staging, output)

    assert (output.stat().st_dev, output.stat().st_ino) == output_identity
    assert list(output.iterdir()) == []
    assert staging.is_dir()


def test_semantic_verification_accepts_relocated_identical_executable(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
) -> None:
    relocated = tmp_path / "relocated-BambuStudio"
    shutil.copyfile(semantic_evidence.executable, relocated)
    relocated.chmod(0o755)

    verification = verify_bambu_project_evidence(
        semantic_evidence.project_set,
        print_set_dir=semantic_evidence.print_set,
        slice_set_dir=semantic_evidence.slice_set,
        bambu_studio=relocated,
    )

    assert verification["required_checks_passed"] is True


@pytest.mark.parametrize(
    "mutation",
    ["extra_flag", "output_directory", "same_basename_model", "same_basename_profile"],
)
def test_semantic_verifier_rejects_resealed_execution_command_changes(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    mutation: str,
) -> None:
    project_set = tmp_path / "project-set"
    shutil.copytree(semantic_evidence.project_set, project_set)
    manifest_path = project_set / "bambu-tile-project-manifest.json"
    manifest = bambu_projects_module.load_json(manifest_path)
    record = manifest["tiles"][0]
    validation_path = project_set / record["validation_path"]
    validation = bambu_projects_module.load_json(validation_path)
    command = validation["build_execution"]["command"]
    if mutation == "extra_flag":
        command.insert(-1, "--unknown-flag")
    elif mutation == "output_directory":
        command[command.index("--outputdir") + 1] = str(tmp_path / "unbound")
    elif mutation == "same_basename_model":
        command[-1] = str(tmp_path / "foreign" / Path(command[-1]).name)
    elif mutation == "same_basename_profile":
        profiles = command[command.index("--load-settings") + 1].split(";")
        profiles[0] = str(tmp_path / "foreign" / Path(profiles[0]).name)
        command[command.index("--load-settings") + 1] = ";".join(profiles)
    else:
        raise AssertionError(mutation)
    write_canonical(validation_path, validation)
    record["validation_sha256"] = bambu_projects_module.sha256(validation_path)
    write_canonical(manifest_path, manifest)

    with pytest.raises(
        RuntimeError,
        match=r"exact normative grammar|output directory|input profiles or model",
    ):
        _verify_semantic_evidence(semantic_evidence, project_set)


@pytest.mark.parametrize("mutation", ["connector_hash", "report_schema"])
def test_build_rejects_resealed_source_identity_changes_before_execution(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    slice_set = tmp_path / "slice-set"
    shutil.copytree(semantic_evidence.slice_set, slice_set)
    manifest_path = slice_set / "tile-slice-manifest.json"
    manifest = bambu_projects_module.load_json(manifest_path)
    if mutation == "connector_hash":
        manifest["source_connector_plan_sha256"] = "0" * 64
        expected = "source print/slice/Bambu identities"
    elif mutation == "report_schema":
        record = manifest["tiles"][0]
        report_path = slice_set / record["report_path"]
        report = bambu_projects_module.load_json(report_path)
        report["schema_version"] = "resealed-future-schema"
        write_canonical(report_path, report)
        record["report_sha256"] = bambu_projects_module.sha256(report_path)
        expected = "source slice report identity mismatch"
    else:
        raise AssertionError(mutation)
    write_canonical(manifest_path, manifest)
    calls: list[tuple[str, ...]] = []

    def forbidden_runner(
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, env, cwd
        calls.append(tuple(command))
        raise AssertionError("Bambu must not execute for invalid source identity")

    monkeypatch.setattr(bambu_projects_module, "run_command", forbidden_runner)
    with pytest.raises(RuntimeError, match=expected):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            slice_set,
            semantic_evidence.executable,
            tmp_path / "should-not-exist",
            timeout_seconds=5.0,
        )
    assert calls == []


def test_build_rejects_symlinked_source_manifest_before_execution(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slice_set = tmp_path / "slice-set"
    shutil.copytree(semantic_evidence.slice_set, slice_set)
    manifest_path = slice_set / "tile-slice-manifest.json"
    outside = tmp_path / "outside-slice-manifest.json"
    manifest_path.replace(outside)
    try:
        manifest_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    calls: list[tuple[str, ...]] = []

    def forbidden_runner(
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, env, cwd
        calls.append(tuple(command))
        raise AssertionError("Bambu must not execute for a symlinked source manifest")

    monkeypatch.setattr(bambu_projects_module, "run_command", forbidden_runner)
    with pytest.raises(RuntimeError, match="regular non-link file"):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            slice_set,
            semantic_evidence.executable,
            tmp_path / "should-not-exist",
            timeout_seconds=5.0,
        )
    assert calls == []


def test_build_rejects_symlinked_executable_entry_path(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "linked-BambuStudio"
    try:
        executable.symlink_to(semantic_evidence.executable)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    runner = _SemanticBambuRunner()
    monkeypatch.setattr(bambu_projects_module, "run_command", runner)

    with pytest.raises(RuntimeError, match=r"regular non-link|symbolic link|reparse point"):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            semantic_evidence.slice_set,
            executable,
            tmp_path / "should-not-exist",
            timeout_seconds=5.0,
        )

    assert runner.calls == []


def test_build_rejects_symlinked_print_root_entry_path(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    print_root = tmp_path / "linked-print-set"
    try:
        print_root.symlink_to(semantic_evidence.print_set, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    runner = _SemanticBambuRunner()
    monkeypatch.setattr(bambu_projects_module, "run_command", runner)
    output = tmp_path / "should-not-exist"

    with pytest.raises(RuntimeError, match=r"without following links|symbolic link|reparse point"):
        generate_bambu_project_evidence(
            print_root,
            semantic_evidence.slice_set,
            semantic_evidence.executable,
            output,
            timeout_seconds=5.0,
        )

    assert runner.calls == []
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.topoforge-stage-*")) == []


def test_source_manifest_swap_is_not_reopened_before_external_execution(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slice_set = tmp_path / "slice-set"
    shutil.copytree(semantic_evidence.slice_set, slice_set)
    manifest_path = slice_set / "tile-slice-manifest.json"
    detached_manifest = slice_set / "tile-slice-manifest.original.json"
    parent_information = manifest_path.parent.stat()
    real_open = bambu_projects_module.os.open
    swapped = False
    runner = _SemanticBambuRunner()

    def racing_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            not swapped
            and dir_fd is not None
            and path == manifest_path.name
            and (
                bambu_projects_module.os.fstat(dir_fd).st_dev,
                bambu_projects_module.os.fstat(dir_fd).st_ino,
            )
            == (parent_information.st_dev, parent_information.st_ino)
        ):
            swapped = True
            manifest_path.rename(detached_manifest)
            manifest_path.write_text("attacker replacement", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(bambu_projects_module.os, "open", racing_open)
    monkeypatch.setattr(bambu_projects_module, "run_command", runner)

    with pytest.raises(RuntimeError, match="JSON is unreadable"):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            slice_set,
            semantic_evidence.executable,
            tmp_path / "should-not-publish",
            timeout_seconds=30.0,
        )

    assert swapped is True
    assert any("--export-3mf" in command for command in runner.calls)


def test_verifier_rejects_symlinked_output_manifest(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
) -> None:
    project_set = tmp_path / "project-set"
    shutil.copytree(semantic_evidence.project_set, project_set)
    manifest_path = project_set / "bambu-tile-project-manifest.json"
    outside = tmp_path / "outside-project-manifest.json"
    manifest_path.replace(outside)
    try:
        manifest_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(RuntimeError, match="regular non-link file"):
        _verify_semantic_evidence(semantic_evidence, project_set)


def test_verifier_rejects_symlinked_output_root_entry_path(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "linked-project-set"
    try:
        project_root.symlink_to(semantic_evidence.project_set, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(RuntimeError, match=r"without following links|symbolic link|reparse point"):
        _verify_semantic_evidence(semantic_evidence, project_root)


def test_generation_race_cannot_replace_an_empty_destination_and_cleans_stage(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "raced-project-set"
    runner = _SemanticBambuRunner()
    real_publish = bambu_projects_module._publish_directory_no_replace
    raced_identity: tuple[int, int] | None = None

    def racing_publish(staging: Path, destination: Path) -> None:
        nonlocal raced_identity
        destination.mkdir()
        information = destination.stat()
        raced_identity = (information.st_dev, information.st_ino)
        real_publish(staging, destination)

    monkeypatch.setattr(bambu_projects_module, "run_command", runner)
    monkeypatch.setattr(
        bambu_projects_module,
        "_publish_directory_no_replace",
        racing_publish,
    )

    with pytest.raises(RuntimeError, match="destination already exists"):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            semantic_evidence.slice_set,
            semantic_evidence.executable,
            output,
            timeout_seconds=30.0,
        )

    information = output.stat()
    assert raced_identity == (information.st_dev, information.st_ino)
    assert list(output.iterdir()) == []
    assert list(tmp_path.glob(f".{output.name}.topoforge-stage-*")) == []


def test_generation_reconciles_a_late_publication_postcheck_error_as_committed(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "reconciled-project-set"
    runner = _SemanticBambuRunner()
    real_native_publish = bambu_projects_module._native_publish_no_replace
    real_stat = bambu_projects_module.os.stat
    native_completed = False
    postcheck_failed = False

    def tracking_native_publish(
        staging: Path,
        destination: Path,
        *,
        parent_descriptor: int | None,
    ) -> None:
        nonlocal native_completed
        real_native_publish(
            staging,
            destination,
            parent_descriptor=parent_descriptor,
        )
        native_completed = True

    def faulting_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal postcheck_failed
        dir_fd = kwargs.get("dir_fd")
        names_output = (
            os.fspath(path) == output.name
            if dir_fd is not None
            else Path(os.fspath(path)) == output
        )
        if native_completed and not postcheck_failed and names_output:
            postcheck_failed = True
            raise OSError(errno.EIO, "injected late output stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(bambu_projects_module, "run_command", runner)
    monkeypatch.setattr(
        bambu_projects_module,
        "_native_publish_no_replace",
        tracking_native_publish,
    )
    monkeypatch.setattr(bambu_projects_module.os, "stat", faulting_stat)

    result = generate_bambu_project_evidence(
        semantic_evidence.print_set,
        semantic_evidence.slice_set,
        semantic_evidence.executable,
        output,
        timeout_seconds=30.0,
    )

    assert postcheck_failed is True
    assert result.output_dir == output.resolve()
    assert result.verification["required_checks_passed"] is True
    assert list(tmp_path.glob(f".{output.name}.topoforge-stage-*")) == []


@pytest.mark.skipif(
    not bambu_projects_module._descriptor_relative_supported(),
    reason="directory fsync publication branch is unavailable",
)
def test_generation_reports_retained_output_after_publication_fsync_error(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "durability-uncertain-project-set"
    runner = _SemanticBambuRunner()
    real_native_publish = bambu_projects_module._native_publish_no_replace
    real_fsync = bambu_projects_module.os.fsync
    native_completed = False
    fsync_failed = False

    def tracking_native_publish(
        staging: Path,
        destination: Path,
        *,
        parent_descriptor: int | None,
    ) -> None:
        nonlocal native_completed
        real_native_publish(
            staging,
            destination,
            parent_descriptor=parent_descriptor,
        )
        native_completed = True

    def faulting_fsync(descriptor: int) -> None:
        nonlocal fsync_failed
        if native_completed and not fsync_failed:
            fsync_failed = True
            raise OSError(errno.EIO, "injected publication directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(bambu_projects_module, "run_command", runner)
    monkeypatch.setattr(
        bambu_projects_module,
        "_native_publish_no_replace",
        tracking_native_publish,
    )
    monkeypatch.setattr(bambu_projects_module.os, "fsync", faulting_fsync)

    with pytest.raises(RuntimeError) as captured:
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            semantic_evidence.slice_set,
            semantic_evidence.executable,
            output,
            timeout_seconds=30.0,
        )

    message = str(captured.value)
    assert fsync_failed is True
    assert "publication committed" in message
    assert "durability could not be confirmed" in message
    assert "output is retained" in message
    assert f"output={output}" in message
    assert "staging=" in message
    assert "[missing]" in message
    assert output.is_dir()
    assert _verify_semantic_evidence(semantic_evidence, output)["required_checks_passed"] is True
    assert list(tmp_path.glob(f".{output.name}.topoforge-stage-*")) == []


def test_atomic_publication_reports_both_paths_when_late_state_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "unreadable-stage"
    output = tmp_path / "unreadable-output"
    staging.mkdir()
    real_native_publish = bambu_projects_module._native_publish_no_replace
    real_stat = bambu_projects_module.os.stat
    native_completed = False

    def tracking_native_publish(
        source: Path,
        destination: Path,
        *,
        parent_descriptor: int | None,
    ) -> None:
        nonlocal native_completed
        real_native_publish(
            source,
            destination,
            parent_descriptor=parent_descriptor,
        )
        native_completed = True

    def faulting_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        dir_fd = kwargs.get("dir_fd")
        name = os.fspath(path)
        names_target = (
            name in {staging.name, output.name}
            if dir_fd is not None
            else Path(name) in {staging, output}
        )
        if native_completed and names_target:
            raise OSError(errno.EIO, "injected unreadable publication state")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        bambu_projects_module,
        "_native_publish_no_replace",
        tracking_native_publish,
    )
    monkeypatch.setattr(bambu_projects_module.os, "stat", faulting_stat)

    with pytest.raises(RuntimeError) as captured:
        bambu_projects_module._publish_directory_no_replace(staging, output)

    message = str(captured.value)
    assert "publication state is uncertain" in message
    assert f"output={output}" in message
    assert f"staging={staging}" in message
    assert stat.S_ISDIR(real_stat(output, follow_symlinks=False).st_mode)


@pytest.mark.skipif(os.name == "nt", reason="Windows holds the locked executable against rename")
@pytest.mark.parametrize("mutation_stage", ["probe", "build", "reopen"])
def test_generation_rejects_executable_replacement_at_every_execution_boundary(
    semantic_evidence: _SemanticEvidence,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_stage: str,
) -> None:
    executable = tmp_path / "BambuStudio"
    shutil.copyfile(semantic_evidence.executable, executable)
    executable.chmod(0o755)
    detached = tmp_path / "BambuStudio.detached"
    delegate = _SemanticBambuRunner()
    replaced = False

    def replacing_runner(
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        nonlocal replaced
        result = delegate(
            command,
            timeout_seconds=timeout_seconds,
            env=env,
            cwd=cwd,
        )
        normalized = tuple(command)
        matches = (
            (mutation_stage == "probe" and "--help" in normalized)
            or (mutation_stage == "build" and "--export-3mf" in normalized)
            or (
                mutation_stage == "reopen"
                and "--export-3mf" not in normalized
                and Path(normalized[-1]).name == "model.bambu-p2s.3mf"
            )
        )
        if matches and not replaced:
            replaced = True
            executable.rename(detached)
            shutil.copyfile(detached, executable)
            executable.chmod(0o755)
        return result

    monkeypatch.setattr(bambu_projects_module, "run_command", replacing_runner)
    output = tmp_path / f"replaced-{mutation_stage}"

    with pytest.raises(RuntimeError, match=r"executable.*(?:changed|identity)"):
        generate_bambu_project_evidence(
            semantic_evidence.print_set,
            semantic_evidence.slice_set,
            executable,
            output,
            timeout_seconds=30.0,
        )

    assert replaced is True
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.topoforge-stage-*")) == []
