"""Bambu project version and macOS isolation regressions."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import stat
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
    manifest = {
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


def test_project_model_measurement_rejects_out_of_range_triangle_indices() -> None:
    invalid = _MODEL_XML.replace(b'v3="2"', b'v3="99"')

    with pytest.raises(RuntimeError, match="triangle indices are invalid"):
        bambu_projects_module._project_model_measurement(invalid)


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
