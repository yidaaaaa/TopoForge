"""Export, reopen, and verify per-tile Bambu Studio project 3MF evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, ValidationError

from topoforge.exporters.three_mf import inspect_3mf
from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate
from topoforge.validation.slicers import SliceStatus, parse_gcode_generator, parse_gcode_metrics
from topoforge.validation.slicers.bambu import parse_bambu_studio_version
from topoforge.validation.slicers.base import CommandExecution, run_command

SCHEMA_VERSION = "topoforge-bambu-tile-project-assembly-v1"
TILE_SCHEMA_VERSION = "topoforge-bambu-tile-project-v1"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_PROJECT_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_PROJECT_MAX_MEMBERS = 20_000
_PROJECT_MAX_MEMBER_BYTES = 1024 * 1024 * 1024
_PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_PROJECT_MAX_COMPRESSION_RATIO = 1000.0
_PROJECT_MAX_RELATIONSHIP_BYTES = 8 * 1024 * 1024
_PROJECT_MAX_MODEL_XML_BYTES = 64 * 1024 * 1024
_PROJECT_MAX_GCODE_TEXT_BYTES = 256 * 1024 * 1024
_PROJECT_MAX_MEMBER_NAME_BYTES = 1024
_PROJECT_ALLOWED_FLAG_BITS = 0x080E
_PROJECT_REQUIRED_MEMBERS = frozenset(
    {
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "Metadata/plate_1.gcode",
        "Metadata/plate_1.gcode.md5",
        "Metadata/project_settings.config",
    }
)
_PROJECT_FILE_ROLES = frozenset(
    {
        "bambu_project_3mf",
        "primary_gcode",
        "reopen_gcode",
        "build_result",
        "reopen_result",
        "build_stdout",
        "build_stderr",
        "reopen_stdout",
        "reopen_stderr",
    }
)
_ROOT_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "layout_id",
        "source_print_manifest_sha256",
        "source_slice_manifest_sha256",
        "bambu_studio_path",
        "bambu_studio_sha256",
        "bambu_studio_version",
        "bambu_studio_probe",
        "printer_profile_id",
        "profile_files",
        "tile_grid_shape",
        "tile_count",
        "all_projects_reopened",
        "all_release_gates_passed",
        "claim_boundary",
        "required_checks_passed",
        "tiles",
    }
)
_ROOT_TILE_FIELDS = frozenset(
    {
        "tile_id",
        "row",
        "column",
        "source_print_tile_manifest_sha256",
        "source_slice_report_sha256",
        "validation_path",
        "validation_sha256",
        "files",
        "sha256",
        "required_checks_passed",
    }
)
_VALIDATION_FIELDS = frozenset(
    {
        "schema_version",
        "tile_id",
        "source_print_local_3mf_path",
        "source_print_local_3mf_sha256",
        "source_slice_report_sha256",
        "source_dimensions_mm",
        "source_triangle_count",
        "build_execution",
        "reopen_execution",
        "build_result",
        "reopen_result",
        "build_object",
        "reopen_object",
        "dimensions_match",
        "triangle_counts_match",
        "project_archive",
        "primary_metrics",
        "reopen_metrics",
        "primary_release_gate",
        "reopen_release_gate",
        "expected_bambu_studio_version",
        "primary_bambu_studio_version",
        "reopen_bambu_studio_version",
        "bambu_studio_versions_match",
        "external_profiles_loaded_on_reopen",
        "required_checks_passed",
    }
)


@dataclass(frozen=True, slots=True)
class _EvidenceArgs:
    print_set: Path
    slice_set: Path
    bambu_studio: Path
    output: Path
    timeout: float


@dataclass(frozen=True, slots=True)
class _SourceTileEvidence:
    tile_id: str
    row: int
    column: int
    print_record: Any
    print_artifact: Any
    slice_record: Any
    slice_report: Any
    source_3mf: Path
    source_3mf_inspection: Any
    source_slice_gcode: Path


@dataclass(frozen=True, slots=True)
class _SourceEvidence:
    print_manifest: Any
    slice_manifest: Any
    expected_version: str
    profiles: tuple[tuple[Any, Path], ...]
    tiles: tuple[_SourceTileEvidence, ...]


class BambuProjectEvidenceResult(BaseModel):
    """Published Bambu project evidence paths and strict verification summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    manifest_path: Path
    verification: dict[str, Any]


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        information = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable: {path}: {exc}") from exc
    if not stat.S_ISREG(information.st_mode):
        raise RuntimeError(f"{label} must be a regular non-link file: {path}")
    return information


def sha256(path: Path) -> str:
    _require_regular_file(path, label="SHA-256 input")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    """Return the SHA-256 of a UTF-8 diagnostic stream."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def write_canonical(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _relative_parts(relative: str, *, label: str) -> tuple[str, ...]:
    if not relative or "\x00" in relative or "\\" in relative or relative.startswith("/"):
        raise RuntimeError(f"{label} is not a canonical relative POSIX path: {relative!r}")
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"{label} is not a safe relative path: {relative!r}")
    return parts


def resolve_relative(root: Path, relative: str) -> Path:
    parts = _relative_parts(relative, label="evidence path")
    lexical = root.joinpath(*parts)
    current = lexical
    while current != root:
        if current.is_symlink():
            raise RuntimeError(f"evidence path contains a symbolic link: {relative}")
        current = current.parent
    candidate = lexical.resolve()
    if candidate == root or root not in candidate.parents:
        raise RuntimeError(f"path escapes evidence directory: {relative}")
    return candidate


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(f"JSON contains a duplicate object key: {key}")
        value[key] = item
    return value


def _read_json_value(path: Path) -> Any:
    before = _require_regular_file(path, label="JSON file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"JSON file changed while opening: {path}")
            payload = handle.read(_MAX_JSON_BYTES + 1)
    except OSError as exc:
        raise RuntimeError(f"JSON file is unavailable: {path}: {exc}") from exc
    size = len(payload)
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise RuntimeError(
            f"JSON file size {size} is outside the supported 1..{_MAX_JSON_BYTES} byte range: "
            f"{path}"
        )
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"JSON is unreadable: {path}: {exc}") from exc


def load_json(path: Path) -> dict[str, Any]:
    value = _read_json_value(path)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root is not an object: {path}")
    return value


def _load_canonical_model(path: Path, model: type[BaseModel]) -> BaseModel:
    value = _read_json_value(path)
    try:
        parsed = model.model_validate(value)
    except ValidationError as exc:
        raise RuntimeError(f"JSON does not match {model.__name__}: {path}: {exc}") from exc
    if path.read_bytes() != canonical_bytes(parsed.model_dump(mode="json")):
        raise RuntimeError(f"JSON is not canonical: {path}")
    return parsed


def execution_record(execution: CommandExecution, command: list[str]) -> dict[str, Any]:
    return {
        "command": command,
        "process_exit_code": execution.returncode,
        "duration_seconds": execution.duration_seconds,
    }


def result_passed(value: dict[str, Any]) -> bool:
    plates = value.get("sliced_plates")
    return bool(
        value.get("return_code") == 0
        and value.get("error_string") in {"Success", "Success."}
        and isinstance(plates, list)
        and len(plates) == 1
        and isinstance(plates[0], dict)
        and plates[0].get("warning_message") in {None, ""}
    )


def _read_bounded_text(path: Path, *, label: str) -> str:
    information = _require_regular_file(path, label=label)
    if information.st_size <= 0 or information.st_size > _PROJECT_MAX_GCODE_TEXT_BYTES:
        raise RuntimeError(
            f"{label} size {information.st_size} is outside the supported "
            f"1..{_PROJECT_MAX_GCODE_TEXT_BYTES} byte range: {path}"
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _gcode_bambu_version_text(gcode_text: str, *, path: Path) -> str:
    tokens = re.findall(
        r"^;\s*(?:generated\s+by\s+)?BambuStudio\s+([^\s;]+)",
        gcode_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not tokens:
        raise RuntimeError(f"G-code does not identify Bambu Studio as its generator: {path}")
    versions: set[str] = set()
    for token in tokens:
        parsed = parse_bambu_studio_version(f"BambuStudio-{token}:")
        if parsed is None or parsed != token:
            raise RuntimeError(f"G-code contains a malformed Bambu Studio version: {path}")
        versions.add(parsed)
    if len(versions) != 1:
        raise RuntimeError(f"G-code contains conflicting Bambu Studio versions: {path}")
    generator = parse_gcode_generator(gcode_text)
    version = next(iter(versions))
    if (
        generator is None
        or generator[0].casefold().replace(" ", "") != "bambustudio"
        or generator[1] != version
    ):
        raise RuntimeError(f"G-code Bambu Studio generator identity is ambiguous: {path}")
    return version


def gcode_bambu_version(gcode: Path) -> str:
    """Parse and validate one independently generated Bambu G-code version."""
    return _gcode_bambu_version_text(
        _read_bounded_text(gcode, label="Bambu G-code"),
        path=gcode,
    )


def release_gate(
    gcode: Path,
    *,
    expected_version: str,
    stdout: str,
    stderr: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    gcode_text = _read_bounded_text(gcode, label="Bambu G-code")
    actual_version = _gcode_bambu_version_text(gcode_text, path=gcode)
    if actual_version != expected_version:
        raise RuntimeError(
            f"Bambu Studio G-code version {actual_version!r} does not match the frozen "
            f"source-slice version {expected_version!r}: {gcode}"
        )
    gcode_text = gcode.read_text(encoding="utf-8", errors="replace")
    metrics = parse_gcode_metrics(
        gcode_text,
        diagnostics="\n".join((stdout, stderr)),
    )
    payload = {
        "slicer": {"name": "BambuStudio", "version": actual_version},
        "status": "succeeded",
        "exit_code": 0,
        "gcode_generated": True,
        "metrics": metrics.model_dump(mode="json"),
    }
    gate = evaluate_bambu_p2s_release_gate(payload, printer_profile_id="bambu-p2s-0.4")
    return metrics.model_dump(mode="json"), gate, actual_version


def _windows_archive_alias(name: str) -> str:
    if name.endswith("//"):
        raise RuntimeError(f"Bambu project archive member has an unsafe name: {name!r}")
    canonical_name = name[:-1] if name.endswith("/") else name
    parts = _relative_parts(canonical_name, label="Bambu project archive member")
    aliases: list[str] = []
    for part in parts:
        normalized = unicodedata.normalize("NFC", part)
        if (
            normalized != part
            or normalized.endswith((" ", "."))
            or ":" in normalized
            or any(ord(character) < 32 for character in normalized)
        ):
            raise RuntimeError(f"Bambu project archive member has an unsafe name: {name!r}")
        alias = normalized.casefold().rstrip(" .")
        stem = alias.split(".", 1)[0]
        if (
            not alias
            or stem in {"con", "prn", "aux", "nul"}
            or re.fullmatch(r"(?:com|lpt)[1-9]", stem) is not None
        ):
            raise RuntimeError(f"Bambu project archive member has a Windows-unsafe name: {name!r}")
        aliases.append(alias)
    return "/".join(aliases)


def _archive_member_is_regular(info: ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.is_dir():
        return file_type in {0, stat.S_IFDIR}
    return file_type in {0, stat.S_IFREG}


def _validated_project_members(package: ZipFile, *, project: Path) -> dict[str, ZipInfo]:
    infos = package.infolist()
    if not infos or len(infos) > _PROJECT_MAX_MEMBERS:
        raise RuntimeError(
            f"Bambu project archive member count {len(infos)} is outside the supported "
            f"1..{_PROJECT_MAX_MEMBERS} range: {project}"
        )
    by_name: dict[str, ZipInfo] = {}
    aliases: dict[str, str] = {}
    total_uncompressed = 0
    for info in infos:
        name = info.filename
        original_name = info.orig_filename
        if (
            not name
            or original_name != name
            or "\x00" in original_name
            or len(original_name.encode("utf-8", errors="surrogatepass"))
            > _PROJECT_MAX_MEMBER_NAME_BYTES
        ):
            raise RuntimeError(
                f"Bambu project archive member raw name is invalid: {original_name!r}"
            )
        if name in by_name:
            raise RuntimeError(f"Bambu project archive contains a duplicate member: {name}")
        alias = _windows_archive_alias(name)
        previous = aliases.get(alias)
        if previous is not None:
            raise RuntimeError(
                "Bambu project archive contains a Unicode/case/Windows alias collision: "
                f"{previous!r} and {name!r}"
            )
        aliases[alias] = name
        by_name[name] = info
        if info.flag_bits & 0x1:
            raise RuntimeError(f"Bambu project archive contains an encrypted member: {name}")
        if info.flag_bits & ~_PROJECT_ALLOWED_FLAG_BITS:
            raise RuntimeError(
                f"Bambu project archive member has unsupported flags 0x{info.flag_bits:04x}: {name}"
            )
        if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
            raise RuntimeError(
                f"Bambu project archive uses unsupported compression {info.compress_type}: {name}"
            )
        if not _archive_member_is_regular(info):
            raise RuntimeError(
                f"Bambu project archive member is not a regular file/directory: {name}"
            )
        if info.file_size < 0 or info.file_size > _PROJECT_MAX_MEMBER_BYTES:
            raise RuntimeError(
                f"Bambu project archive member exceeds the {_PROJECT_MAX_MEMBER_BYTES} "
                f"byte limit: {name}"
            )
        total_uncompressed += info.file_size
        if total_uncompressed > _PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise RuntimeError(
                "Bambu project archive exceeds the total uncompressed byte limit: "
                f"{_PROJECT_MAX_TOTAL_UNCOMPRESSED_BYTES}"
            )
        if info.file_size:
            if info.compress_size <= 0:
                raise RuntimeError(
                    f"Bambu project archive member has an invalid compressed size: {name}"
                )
            ratio = info.file_size / info.compress_size
            if ratio > _PROJECT_MAX_COMPRESSION_RATIO:
                raise RuntimeError(
                    f"Bambu project archive member compression ratio {ratio:.3f} exceeds "
                    f"{_PROJECT_MAX_COMPRESSION_RATIO:g}: {name}"
                )
    missing = _PROJECT_REQUIRED_MEMBERS - by_name.keys()
    if missing:
        raise RuntimeError(
            f"Bambu project archive is missing required members: {', '.join(sorted(missing))}"
        )
    if any(by_name[name].is_dir() for name in _PROJECT_REQUIRED_MEMBERS):
        raise RuntimeError("Bambu project archive stores a required member as a directory")
    return by_name


def _reject_external_relationships(payload: bytes, *, name: str) -> None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError(f"Bambu project relationship XML is invalid: {name}: {exc}") from exc
    for relationship in root.iter():
        if relationship.attrib.get("TargetMode", "Internal").casefold() == "external":
            raise RuntimeError(f"Bambu project archive contains an external relationship: {name}")


def _project_model_measurement(payload: bytes) -> dict[str, Any]:
    core_namespace = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    core_prefix = f"{{{core_namespace}}}"

    def children(element: ET.Element, name: str) -> list[ET.Element]:
        return [child for child in element if child.tag == f"{core_prefix}{name}"]

    def one_child(element: ET.Element, name: str) -> ET.Element:
        matches = children(element, name)
        if len(matches) != 1:
            raise RuntimeError(f"Bambu project model must contain one core {name} element")
        return matches[0]

    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

    def transform(value: str | None) -> tuple[float, ...]:
        if value is None:
            return identity
        try:
            parsed = tuple(float(item) for item in value.split())
        except ValueError as exc:
            raise RuntimeError("Bambu project model has an invalid transform") from exc
        if len(parsed) != 12 or not all(math.isfinite(item) for item in parsed):
            raise RuntimeError("Bambu project model has an invalid transform")
        return parsed

    def apply_transform(
        point: tuple[float, float, float], matrix: tuple[float, ...]
    ) -> tuple[float, float, float]:
        x, y, z = point
        return (
            x * matrix[0] + y * matrix[3] + z * matrix[6] + matrix[9],
            x * matrix[1] + y * matrix[4] + z * matrix[7] + matrix[10],
            x * matrix[2] + y * matrix[5] + z * matrix[8] + matrix[11],
        )

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError(f"Bambu project model XML is invalid: {exc}") from exc
    if root.tag != f"{core_prefix}model":
        raise RuntimeError("Bambu project model has no core model root")
    if root.attrib.get("unit", "millimeter").casefold() not in {
        "millimeter",
        "millimetre",
        "mm",
    }:
        raise RuntimeError("Bambu project model must use millimetres")

    resources = one_child(root, "resources")
    build = one_child(root, "build")
    objects: dict[int, dict[str, Any]] = {}
    for object_element in children(resources, "object"):
        try:
            object_id = int(object_element.attrib["id"])
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Bambu project model has an invalid object id") from exc
        if object_id <= 0 or object_id in objects:
            raise RuntimeError("Bambu project model object ids must be unique and positive")
        meshes = children(object_element, "mesh")
        component_groups = children(object_element, "components")
        if (len(meshes), len(component_groups)) not in {(1, 0), (0, 1)}:
            raise RuntimeError("Bambu project object must contain one mesh or components group")
        if meshes:
            vertices_element = one_child(meshes[0], "vertices")
            triangles_element = one_child(meshes[0], "triangles")
            vertices: list[tuple[float, float, float]] = []
            for vertex in children(vertices_element, "vertex"):
                try:
                    coordinates = tuple(float(vertex.attrib[axis]) for axis in ("x", "y", "z"))
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project model has an invalid vertex") from exc
                if len(coordinates) != 3 or not all(math.isfinite(value) for value in coordinates):
                    raise RuntimeError("Bambu project model has a non-finite vertex")
                vertices.append(coordinates)
            triangles = children(triangles_element, "triangle")
            if not vertices or not triangles:
                raise RuntimeError("Bambu project model has an empty triangle mesh")
            for triangle in triangles:
                try:
                    indices = tuple(int(triangle.attrib[axis]) for axis in ("v1", "v2", "v3"))
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project model has an invalid triangle") from exc
                if len(set(indices)) != 3 or min(indices) < 0 or max(indices) >= len(vertices):
                    raise RuntimeError("Bambu project model triangle indices are invalid")
            objects[object_id] = {
                "vertices": tuple(vertices),
                "triangle_count": len(triangles),
                "components": (),
            }
        else:
            components: list[tuple[int, tuple[float, ...]]] = []
            for component in children(component_groups[0], "component"):
                try:
                    referenced_id = int(component.attrib["objectid"])
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("Bambu project component has an invalid object id") from exc
                components.append((referenced_id, transform(component.attrib.get("transform"))))
            if not components:
                raise RuntimeError("Bambu project components group is empty")
            objects[object_id] = {
                "vertices": (),
                "triangle_count": 0,
                "components": tuple(components),
            }

    build_items = children(build, "item")
    if len(build_items) != 1:
        raise RuntimeError("Bambu project model must contain exactly one build item")
    try:
        build_object_id = int(build_items[0].attrib["objectid"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Bambu project build item has an invalid object id") from exc
    build_transform = transform(build_items[0].attrib.get("transform"))
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    triangle_count = 0

    def visit(
        object_id: int,
        transforms: tuple[tuple[float, ...], ...],
        active: frozenset[int],
    ) -> None:
        nonlocal triangle_count
        if object_id in active:
            raise RuntimeError("Bambu project component graph contains a cycle")
        model_object = objects.get(object_id)
        if model_object is None:
            raise RuntimeError("Bambu project references a missing object")
        vertices = model_object["vertices"]
        if vertices:
            for vertex in vertices:
                transformed = vertex
                for matrix in transforms:
                    transformed = apply_transform(transformed, matrix)
                for axis, coordinate in enumerate(transformed):
                    minimum[axis] = min(minimum[axis], coordinate)
                    maximum[axis] = max(maximum[axis], coordinate)
            triangle_count += int(model_object["triangle_count"])
            return
        descendants = active | {object_id}
        for referenced_id, component_transform in model_object["components"]:
            visit(referenced_id, (component_transform, *transforms), descendants)

    visit(build_object_id, (build_transform,), frozenset())
    if triangle_count <= 0 or not all(math.isfinite(value) for value in (*minimum, *maximum)):
        raise RuntimeError("Bambu project build item has no finite triangle mesh")
    return {
        "dimensions_mm": [maximum[axis] - minimum[axis] for axis in range(3)],
        "triangle_count": triangle_count,
    }


def archive_evidence(project: Path, primary_gcode: Path) -> dict[str, Any]:
    project_information = _require_regular_file(project, label="Bambu project archive")
    primary_information = _require_regular_file(primary_gcode, label="primary Bambu G-code")
    if project_information.st_size <= 0 or project_information.st_size > _PROJECT_MAX_ARCHIVE_BYTES:
        raise RuntimeError(
            f"Bambu project archive size {project_information.st_size} is outside the "
            f"supported 1..{_PROJECT_MAX_ARCHIVE_BYTES} byte range: {project}"
        )
    if primary_information.st_size <= 0 or primary_information.st_size > _PROJECT_MAX_MEMBER_BYTES:
        raise RuntimeError(
            f"primary Bambu G-code size {primary_information.st_size} is outside the "
            f"supported 1..{_PROJECT_MAX_MEMBER_BYTES} byte range: {primary_gcode}"
        )

    actual_md5 = hashlib.md5(usedforsecurity=False)
    recorded_md5_bytes: bytes | None = None
    model_xml_bytes: bytes | None = None
    embedded_matches_primary = True
    try:
        with ZipFile(project, "r") as package:
            members = _validated_project_members(package, project=project)
            embedded_info = members["Metadata/plate_1.gcode"]
            if embedded_info.file_size != primary_information.st_size:
                embedded_matches_primary = False
            with primary_gcode.open("rb") as primary:
                for info in package.infolist():
                    if info.is_dir():
                        continue
                    capture_relationship = info.filename.endswith(".rels")
                    capture_md5 = info.filename == "Metadata/plate_1.gcode.md5"
                    capture_model = info.filename == "3D/3dmodel.model"
                    captured = bytearray()
                    count = 0
                    with package.open(info, "r") as member:
                        while True:
                            block = member.read(1024 * 1024)
                            if not block:
                                break
                            count += len(block)
                            if count > info.file_size or count > _PROJECT_MAX_MEMBER_BYTES:
                                raise RuntimeError(
                                    "Bambu project archive member expanded beyond its "
                                    f"validated size: {info.filename}"
                                )
                            if capture_relationship:
                                if count > _PROJECT_MAX_RELATIONSHIP_BYTES:
                                    raise RuntimeError(
                                        "Bambu project relationship member exceeds the "
                                        f"{_PROJECT_MAX_RELATIONSHIP_BYTES} byte limit: "
                                        f"{info.filename}"
                                    )
                                captured.extend(block)
                            elif capture_md5:
                                if count > 128:
                                    raise RuntimeError(
                                        "Bambu project embedded G-code MD5 record is oversized"
                                    )
                                captured.extend(block)
                            elif capture_model:
                                if count > _PROJECT_MAX_MODEL_XML_BYTES:
                                    raise RuntimeError(
                                        "Bambu project model XML exceeds the "
                                        f"{_PROJECT_MAX_MODEL_XML_BYTES} byte limit"
                                    )
                                captured.extend(block)
                            elif info.filename == "Metadata/plate_1.gcode":
                                actual_md5.update(block)
                                if primary.read(len(block)) != block:
                                    embedded_matches_primary = False
                    if count != info.file_size:
                        raise RuntimeError(
                            f"Bambu project archive member size changed while reading: "
                            f"{info.filename}"
                        )
                    if capture_relationship:
                        _reject_external_relationships(bytes(captured), name=info.filename)
                    elif capture_md5:
                        recorded_md5_bytes = bytes(captured)
                    elif capture_model:
                        model_xml_bytes = bytes(captured)
                if primary.read(1):
                    embedded_matches_primary = False
    except (BadZipFile, OSError) as exc:
        raise RuntimeError(f"Bambu project archive is invalid: {project}: {exc}") from exc

    if recorded_md5_bytes is None:
        raise RuntimeError("Bambu project archive has no embedded G-code MD5 record")
    if model_xml_bytes is None:
        raise RuntimeError("Bambu project archive has no model XML")
    try:
        recorded_md5 = recorded_md5_bytes.decode("ascii").strip().upper()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Bambu project embedded G-code MD5 is not ASCII") from exc
    if re.fullmatch(r"[0-9A-F]{32}", recorded_md5) is None:
        raise RuntimeError("Bambu project embedded G-code MD5 is malformed")
    actual_md5_value = actual_md5.hexdigest().upper()
    model_measurement = _project_model_measurement(model_xml_bytes)
    return {
        "archive_test_passed": True,
        "embedded_gcode_md5": recorded_md5,
        "embedded_gcode_md5_actual": actual_md5_value,
        "embedded_gcode_md5_verified": recorded_md5 == actual_md5_value,
        "embedded_gcode_matches_primary": embedded_matches_primary,
        "project_model_dimensions_mm": model_measurement["dimensions_mm"],
        "project_model_triangle_count": model_measurement["triangle_count"],
    }


def profile_paths(slice_root: Path, manifest: dict[str, Any]) -> tuple[list[Path], list[Path]]:
    settings: list[Path] = []
    filaments: list[Path] = []
    for item in manifest.get("profile_files", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError("official slice manifest has an invalid profile record")
        source = resolve_relative(slice_root, item["path"])
        if sha256(source) != item.get("sha256"):
            raise RuntimeError(f"official slice profile checksum mismatch: {source}")
        if item.get("role") == "settings":
            settings.append(source)
        elif item.get("role") == "filament":
            filaments.append(source)
        else:
            raise RuntimeError(f"unknown official slice profile role: {item.get('role')}")
    if len(settings) != 2 or len(filaments) != 1:
        raise RuntimeError("Bambu project export requires machine, process, and filament profiles")
    return settings, filaments


def frozen_source_bambu_version(manifest: Mapping[str, Any]) -> str:
    """Return the source slice version that project evidence must preserve."""
    slicer = manifest.get("slicer")
    if not isinstance(slicer, Mapping):
        raise RuntimeError("official slice manifest has no frozen slicer identity")
    version = slicer.get("version")
    if (
        slicer.get("name") != "BambuStudio"
        or slicer.get("status") != "available"
        or not isinstance(version, str)
        or not version
    ):
        raise RuntimeError("official slice manifest must freeze an available Bambu Studio version")
    return version


def isolated_environment(
    runtime: Path,
    *,
    system: str | None = None,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a platform-specific temporary Bambu Studio user environment."""
    system_name = platform.system() if system is None else system
    active_platform = platform_name
    if active_platform is None:
        if system_name == "Windows":
            active_platform = "win32"
        elif system_name == "Darwin":
            active_platform = "darwin"
        else:
            active_platform = sys.platform
    environment = dict(os.environ if environ is None else environ)
    home = runtime / "home"
    if active_platform == "win32":
        for key in (
            "APPIMAGE_EXTRACT_AND_RUN",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_RUNTIME_DIR",
        ):
            environment.pop(key, None)
        roaming = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        temporary = runtime / "temp"
        for path in (home, roaming, local, temporary):
            path.mkdir(parents=True, exist_ok=True)
        environment.update(
            {
                "APPDATA": str(roaming),
                "HOME": str(home),
                "LOCALAPPDATA": str(local),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "USERPROFILE": str(home),
            }
        )
        drive, tail = os.path.splitdrive(str(home))
        if drive:
            environment.update({"HOMEDRIVE": drive, "HOMEPATH": tail})
        return environment
    if active_platform == "darwin":
        application_support = home / "Library" / "Application Support"
        preferences = home / "Library" / "Preferences"
        caches = home / "Library" / "Caches"
        temporary = runtime / "tmp"
        for path in (home, application_support, preferences, caches, temporary):
            path.mkdir(parents=True, exist_ok=True)
        for key in (
            "APPIMAGE_EXTRACT_AND_RUN",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "HOME": str(home),
                "CFFIXED_USER_HOME": str(home),
                "TMPDIR": str(temporary),
            }
        )
        return environment

    config = home / ".config"
    cache = home / ".cache"
    xdg_runtime = runtime / "xdg-runtime"
    for path in (home, config, cache, xdg_runtime):
        path.mkdir(parents=True, exist_ok=True)
    xdg_runtime.chmod(0o700)
    environment.update(
        {
            "APPIMAGE_EXTRACT_AND_RUN": "1",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_RUNTIME_DIR": str(xdg_runtime),
        }
    )
    return environment


def probe_bambu_studio(
    executable: Path,
    *,
    runtime: Path,
    timeout_seconds: float,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Probe the exact executable in isolation and retain version-bearing hashes."""
    command = [str(executable), "--help"]
    execution = run_command(
        command,
        timeout_seconds=min(timeout_seconds, 30.0),
        env=isolated_environment(runtime),
    )
    combined = "\n".join((execution.stdout, execution.stderr))
    version = parse_bambu_studio_version(combined)
    if execution.returncode != 0 or version is None:
        detail = execution.stderr.strip() or execution.stdout.strip() or "no version banner"
        raise RuntimeError(
            f"Bambu Studio version probe failed with {execution.returncode}: {detail}"
        )
    record: dict[str, Any] = {
        **execution_record(execution, command),
        "version": version,
        "stdout_sha256": sha256_text(execution.stdout),
        "stderr_sha256": sha256_text(execution.stderr),
    }
    if evidence_root is not None:
        evidence_root.mkdir(parents=True, exist_ok=True)
        stdout_path = evidence_root / "bambu-studio-probe.stdout.log"
        stderr_path = evidence_root / "bambu-studio-probe.stderr.log"
        stdout_path.write_text(execution.stdout, encoding="utf-8")
        stderr_path.write_text(execution.stderr, encoding="utf-8")
        record.update(
            {
                "stdout_path": stdout_path.name,
                "stderr_path": stderr_path.name,
                "stdout_sha256": sha256(stdout_path),
                "stderr_sha256": sha256(stderr_path),
            }
        )
    return record


def run_checked(
    command: list[str],
    *,
    runtime: Path,
    timeout_seconds: float,
) -> CommandExecution:
    execution = run_command(
        command,
        timeout_seconds=timeout_seconds,
        env=isolated_environment(runtime),
    )
    if execution.returncode != 0:
        raise RuntimeError(
            f"Bambu Studio exited {execution.returncode}: "
            f"{execution.stderr.strip() or execution.stdout.strip()}"
        )
    return execution


def object_measurement(result: dict[str, Any]) -> dict[str, Any]:
    plates = result.get("sliced_plates")
    if not isinstance(plates, list) or len(plates) != 1 or not isinstance(plates[0], dict):
        raise RuntimeError("Bambu result has no single sliced plate")
    objects = plates[0].get("objects")
    if not isinstance(objects, list) or len(objects) != 1 or not isinstance(objects[0], dict):
        raise RuntimeError("Bambu result has no single terrain object")
    bbox = objects[0].get("bbox")
    if not isinstance(bbox, dict):
        raise RuntimeError("Bambu result object has no bounding box")
    return {
        "name": objects[0].get("name"),
        "triangle_count": objects[0].get("triangle_count"),
        "dimensions_mm": [bbox.get("width"), bbox.get("depth"), bbox.get("height")],
        "position_mm": [bbox.get("x"), bbox.get("y"), bbox.get("z")],
    }


def dimensions_match(first: Sequence[Any], second: Sequence[float]) -> bool:
    try:
        return all(
            abs(float(actual) - expected) <= 0.001
            for actual, expected in zip(first, second, strict=True)
        )
    except (TypeError, ValueError):
        return False


def _expected_tile_id(row: int, column: int) -> str:
    return f"tile-r{row:04d}-c{column:04d}"


def _verified_source_profiles(slice_root: Path, manifest: Any) -> tuple[tuple[Any, Path], ...]:
    records = tuple(manifest.profile_files)
    identities = tuple((record.role, record.index) for record in records)
    expected = (("settings", 0), ("settings", 1), ("filament", 0))
    if identities != expected:
        raise RuntimeError(
            "official slice profiles must be exactly machine/process/filament in stable order"
        )
    paths: set[Path] = set()
    aliases: set[str] = set()
    verified: list[tuple[Any, Path]] = []
    for record in records:
        if PurePosixPath(record.path).parent != PurePosixPath("profiles") or ";" in record.path:
            raise RuntimeError("official slice profile path is not CLI-safe")
        path = resolve_relative(slice_root, record.path)
        alias = unicodedata.normalize("NFC", record.path).casefold()
        if path in paths or alias in aliases:
            raise RuntimeError("official slice profile paths are duplicated or aliased")
        if sha256(path) != record.sha256:
            raise RuntimeError(f"official slice profile checksum mismatch: {path}")
        paths.add(path)
        aliases.add(alias)
        verified.append((record, path))
    profile_root = slice_root / "profiles"
    if (
        not profile_root.is_dir()
        or profile_root.is_symlink()
        or {path.resolve() for path in profile_root.iterdir()} != paths
        or any(not path.is_file() or path.is_symlink() for path in profile_root.iterdir())
    ):
        raise RuntimeError("official slice profile inventory has missing or extra files")
    return tuple(verified)


def _source_print_tile(
    *,
    print_root: Path,
    print_manifest: Any,
    print_record: Any,
) -> tuple[Any, Path, Any]:
    from topoforge.tiling.connectors import PrintTileArtifactManifest

    expected_id = _expected_tile_id(print_record.row, print_record.column)
    expected_directory = f"tiles/{expected_id}"
    expected_manifest_path = f"{expected_directory}/print_tile_manifest.json"
    if (
        print_record.tile_id != expected_id
        or print_record.directory != expected_directory
        or print_record.tile_manifest != expected_manifest_path
    ):
        raise RuntimeError(
            f"source print tile id/row/column/path binding changed: {print_record.tile_id}"
        )
    artifact_path = resolve_relative(print_root, print_record.tile_manifest)
    if sha256(artifact_path) != print_record.tile_manifest_sha256:
        raise RuntimeError(f"source print tile manifest checksum mismatch: {expected_id}")
    artifact_value = _load_canonical_model(artifact_path, PrintTileArtifactManifest)
    if not isinstance(artifact_value, PrintTileArtifactManifest):
        raise AssertionError("unexpected source print tile model")
    artifact = artifact_value
    expected_files = {role: f"{expected_directory}/{name}" for role, name in artifact.files.items()}
    if (
        artifact.schema_version != "topoforge-print-tile-artifact-v1"
        or artifact.tile_id != expected_id
        or artifact.layout_id != print_manifest.layout_id
        or (artifact.row, artifact.column) != (print_record.row, print_record.column)
        or artifact.tile_key != print_record.tile_key
        or artifact.source_tile_mesh_manifest_sha256
        != print_record.source_tile_mesh_manifest_sha256
        or artifact.connector_plan_sha256 != print_manifest.connector_plan_sha256
        or artifact.sha256 != print_record.sha256
        or expected_files != print_record.files
    ):
        raise RuntimeError(f"source print tile artifact identity mismatch: {expected_id}")
    tile_directory = print_root.joinpath(*_relative_parts(expected_directory, label="tile"))
    expected_inventory = {*artifact.files.values(), "print_tile_manifest.json"}
    if (
        not tile_directory.is_dir()
        or tile_directory.is_symlink()
        or {path.name for path in tile_directory.iterdir()} != expected_inventory
        or any(not path.is_file() or path.is_symlink() for path in tile_directory.iterdir())
    ):
        raise RuntimeError(f"source print tile inventory changed: {expected_id}")
    verified_files: dict[str, Path] = {}
    for role, relative in print_record.files.items():
        path = resolve_relative(print_root, relative)
        actual_hash = sha256(path)
        if actual_hash != print_record.sha256[role] or actual_hash != artifact.sha256[role]:
            raise RuntimeError(f"source print artifact checksum mismatch: {expected_id}: {role}")
        verified_files[role] = path
    source_3mf = verified_files["print_local_3mf"]
    inspection = inspect_3mf(source_3mf)
    bounds = print_record.print_local_bounds_mm
    bounds_dimensions = (
        bounds[3] - bounds[0],
        bounds[4] - bounds[1],
        bounds[5] - bounds[2],
    )
    validation = artifact.validation
    if (
        validation.schema_version != "topoforge-print-tile-artifact-v1"
        or validation.tile_id != expected_id
        or validation.male_connector_ids != print_record.male_connector_ids
        or validation.female_connector_ids != print_record.female_connector_ids
        or validation.expected_print_local_bounds_mm != print_record.print_local_bounds_mm
        or validation.global_to_print_local_translation_mm
        != print_record.global_to_print_local_translation_mm
        or inspection.strict_warning_count != 0
        or validation.strict_3mf_warning_count.get("print_local") != 0
        or inspection.triangle_count != print_record.triangle_count
        or inspection.triangle_count != validation.local_geometry.triangle_count
        or inspection.triangle_count != validation.local_format_triangle_counts.get("3mf")
        or not dimensions_match(
            list(inspection.dimensions_mm),
            tuple(validation.local_geometry.dimensions_mm),
        )
        or not dimensions_match(list(inspection.dimensions_mm), bounds_dimensions)
        or validation.required_checks_passed is not True
        or validation.triangle_counts_match is not True
        or validation.bounds_match is not True
        or validation.orientation_consistent is not True
    ):
        raise RuntimeError(
            f"source print-local 3MF measurements do not match source evidence: {expected_id}"
        )
    return artifact, source_3mf, inspection


def _source_slice_tile(
    *,
    slice_root: Path,
    slice_manifest: Any,
    slice_record: Any,
    print_record: Any,
    source_3mf: Path,
    source_inspection: Any,
    expected_version: str,
) -> tuple[Any, Path]:
    from topoforge.tiling.slicing import PrintTileSliceReport

    tile_id = print_record.tile_id
    expected_directory = f"tiles/{tile_id}"
    if (
        slice_record.tile_id != tile_id
        or (slice_record.row, slice_record.column) != (print_record.row, print_record.column)
        or slice_record.directory != expected_directory
        or slice_record.report_path != f"{expected_directory}/slice_report.json"
        or slice_record.gcode_path != f"{expected_directory}/model.gcode"
        or slice_record.source_print_tile_manifest_sha256 != print_record.tile_manifest_sha256
        or slice_record.source_print_local_3mf_sha256 != sha256(source_3mf)
    ):
        raise RuntimeError(f"source print/slice tile binding mismatch: {tile_id}")
    tile_directory = slice_root.joinpath(
        *_relative_parts(expected_directory, label="source slice tile")
    )
    if (
        not tile_directory.is_dir()
        or tile_directory.is_symlink()
        or {path.name for path in tile_directory.iterdir()} != {"slice_report.json", "model.gcode"}
        or any(not path.is_file() or path.is_symlink() for path in tile_directory.iterdir())
    ):
        raise RuntimeError(f"source slice tile inventory changed: {tile_id}")
    report_path = resolve_relative(slice_root, slice_record.report_path)
    source_gcode = resolve_relative(slice_root, slice_record.gcode_path)
    if sha256(report_path) != slice_record.report_sha256:
        raise RuntimeError(f"source slice report checksum mismatch: {tile_id}")
    if sha256(source_gcode) != slice_record.gcode_sha256:
        raise RuntimeError(f"source slice G-code checksum mismatch: {tile_id}")
    report_value = _load_canonical_model(report_path, PrintTileSliceReport)
    if not isinstance(report_value, PrintTileSliceReport):
        raise AssertionError("unexpected source slice report model")
    report = report_value
    source_hash = sha256(source_3mf)
    if (
        report.schema_version != "topoforge-print-tile-slice-v1"
        or report.tile_id != tile_id
        or report.source_print_tile_manifest_sha256 != print_record.tile_manifest_sha256
        or report.source_print_local_3mf_path != print_record.files["print_local_3mf"]
        or report.source_print_local_3mf_sha256 != source_hash
        or report.gcode_path != slice_record.gcode_path
        or report.gcode_sha256 != slice_record.gcode_sha256
        or report.gcode_size_bytes != source_gcode.stat().st_size
        or report.input_strict_3mf_warning_count != source_inspection.strict_warning_count
        or report.slicer_result.input_model != Path(report.source_print_local_3mf_path)
        or report.slicer_result.output_gcode != Path(report.gcode_path)
        or report.slicer_result.profile != slice_manifest.profile_name
        or report.slicer_result.slicer.name != slice_manifest.slicer.name
        or report.slicer_result.slicer.version != slice_manifest.slicer.version
        or report.slicer_result.slicer.status != slice_manifest.slicer.status
    ):
        raise RuntimeError(f"source slice report identity mismatch: {tile_id}")

    gcode_text = _read_bounded_text(source_gcode, label="source slice G-code")
    actual_version = _gcode_bambu_version_text(gcode_text, path=source_gcode)
    reopened_metrics = parse_gcode_metrics(
        gcode_text,
        diagnostics="\n".join((report.slicer_result.stdout, report.slicer_result.stderr)),
    )
    expected_gate = evaluate_bambu_p2s_release_gate(
        report.slicer_result.model_dump(mode="json"),
        printer_profile_id=slice_manifest.printer_profile_id,
    )
    release_passed = expected_gate.get("release_gate_passed") is True
    parameter_checks_passed = expected_gate.get("parameter_checks_passed") is True
    exit_code_zero = report.slicer_result.exit_code == 0
    gcode_generated = bool(
        report.slicer_result.gcode_generated
        and report.slicer_result.gcode_size_bytes == source_gcode.stat().st_size
    )
    layer_count_positive = bool(
        reopened_metrics.layer_count is not None and reopened_metrics.layer_count > 0
    )
    required = bool(
        report.slicer_result.status is SliceStatus.SUCCEEDED
        and exit_code_zero
        and gcode_generated
        and source_inspection.strict_warning_count == 0
        and reopened_metrics == report.slicer_result.metrics
        and layer_count_positive
        and not reopened_metrics.out_of_bed
        and not reopened_metrics.empty_layer_warning
        and not reopened_metrics.floating_region_warning
        and reopened_metrics.support_material is False
        and release_passed
        and parameter_checks_passed
        and actual_version == expected_version
    )
    if (
        reopened_metrics != report.reopened_metrics
        or report.manufacturing_release_gate != expected_gate
        or report.release_role != "official-p2s-release"
        or report.official_p2s_release_gate_passed is not release_passed
        or report.exit_code_zero is not exit_code_zero
        or report.gcode_generated is not gcode_generated
        or report.metrics_reopen_match is not (reopened_metrics == report.slicer_result.metrics)
        or report.layer_count_positive is not layer_count_positive
        or report.out_of_bed is not reopened_metrics.out_of_bed
        or report.empty_layer_warning is not reopened_metrics.empty_layer_warning
        or report.floating_region_warning is not reopened_metrics.floating_region_warning
        or report.support_material is not reopened_metrics.support_material
        or report.required_checks_passed is not required
        or slice_record.layer_count != reopened_metrics.layer_count
        or slice_record.estimated_time_seconds != reopened_metrics.estimated_time_seconds
        or slice_record.filament_used_mm != reopened_metrics.filament_used_mm
        or slice_record.filament_used_cm3 != reopened_metrics.filament_used_cm3
        or slice_record.filament_used_g != reopened_metrics.filament_used_g
        or slice_record.required_checks_passed is not required
        or not required
    ):
        raise RuntimeError(f"source slice metrics/release gate changed: {tile_id}")
    return report, source_gcode


def _optional_total(values: list[float | int | None]) -> float | int | None:
    present = [value for value in values if value is not None]
    return None if not present else sum(present)


def _verify_source_evidence(
    *,
    print_root: Path,
    slice_root: Path,
    executable: Path,
) -> _SourceEvidence:
    from topoforge.tiling.connectors import PrintTileAssemblyManifest
    from topoforge.tiling.slicing import PrintTileSliceManifest

    print_path = print_root / "print-tile-assembly-manifest.json"
    slice_path = slice_root / "tile-slice-manifest.json"
    print_value = _load_canonical_model(print_path, PrintTileAssemblyManifest)
    slice_value = _load_canonical_model(slice_path, PrintTileSliceManifest)
    if not isinstance(print_value, PrintTileAssemblyManifest):
        raise AssertionError("unexpected source print assembly model")
    if not isinstance(slice_value, PrintTileSliceManifest):
        raise AssertionError("unexpected source slice assembly model")
    print_manifest = print_value
    slice_manifest = slice_value
    expected_version = frozen_source_bambu_version(slice_manifest.model_dump(mode="python"))
    executable_hash = sha256(executable)
    if (
        print_manifest.schema_version != "topoforge-print-tile-assembly-v1"
        or slice_manifest.schema_version != "topoforge-print-tile-slice-assembly-v1"
        or slice_manifest.layout_id != print_manifest.layout_id
        or slice_manifest.source_print_tile_assembly_sha256 != sha256(print_path)
        or slice_manifest.source_connector_plan_sha256 != print_manifest.connector_plan_sha256
        or slice_manifest.tile_grid_shape != print_manifest.tile_grid_shape
        or slice_manifest.tile_count != print_manifest.tile_count
        or slice_manifest.printer_profile_id != "bambu-p2s-0.4"
        or slice_manifest.release_role != "official-p2s-release"
        or slice_manifest.slicer.name != "BambuStudio"
        or slice_manifest.slicer.status.value != "available"
        or slice_manifest.slicer.version != expected_version
        or slice_manifest.slicer_executable_sha256 != executable_hash
        or parse_bambu_studio_version(f"BambuStudio-{expected_version}:") != expected_version
    ):
        raise RuntimeError("source print/slice/Bambu identities do not match")
    profiles = _verified_source_profiles(slice_root, slice_manifest)

    print_records = tuple(print_manifest.tiles)
    slice_records = tuple(slice_manifest.tiles)
    print_identity = tuple((record.tile_id, record.row, record.column) for record in print_records)
    slice_identity = tuple((record.tile_id, record.row, record.column) for record in slice_records)
    if print_identity != slice_identity:
        raise RuntimeError("source print and slice manifests have different tile sets/order")

    tiles: list[_SourceTileEvidence] = []
    reports: list[Any] = []
    total_gcode_size = 0
    for print_record, slice_record in zip(print_records, slice_records, strict=True):
        artifact, source_3mf, inspection = _source_print_tile(
            print_root=print_root,
            print_manifest=print_manifest,
            print_record=print_record,
        )
        report, source_gcode = _source_slice_tile(
            slice_root=slice_root,
            slice_manifest=slice_manifest,
            slice_record=slice_record,
            print_record=print_record,
            source_3mf=source_3mf,
            source_inspection=inspection,
            expected_version=expected_version,
        )
        total_gcode_size += source_gcode.stat().st_size
        reports.append(report)
        tiles.append(
            _SourceTileEvidence(
                tile_id=print_record.tile_id,
                row=print_record.row,
                column=print_record.column,
                print_record=print_record,
                print_artifact=artifact,
                slice_record=slice_record,
                slice_report=report,
                source_3mf=source_3mf,
                source_3mf_inspection=inspection,
                source_slice_gcode=source_gcode,
            )
        )

    time_sum = _optional_total([record.estimated_time_seconds for record in slice_records])
    filament_mm_sum = _optional_total([record.filament_used_mm for record in slice_records])
    filament_cm3_sum = _optional_total([record.filament_used_cm3 for record in slice_records])
    filament_g_sum = _optional_total([record.filament_used_g for record in slice_records])
    aggregate_required = bool(
        tiles
        and all(report.required_checks_passed for report in reports)
        and all(
            report.manufacturing_release_gate is not None
            and report.manufacturing_release_gate.get("release_gate_passed") is True
            for report in reports
        )
    )
    parameter_checks = bool(
        tiles
        and all(
            report.manufacturing_release_gate is not None
            and report.manufacturing_release_gate.get("parameter_checks_passed") is True
            for report in reports
        )
    )
    if (
        total_gcode_size != slice_manifest.total_gcode_size_bytes
        or slice_manifest.total_estimated_time_seconds
        != (None if time_sum is None else int(time_sum))
        or slice_manifest.total_filament_used_mm
        != (None if filament_mm_sum is None else float(filament_mm_sum))
        or slice_manifest.total_filament_used_cm3
        != (None if filament_cm3_sum is None else float(filament_cm3_sum))
        or slice_manifest.total_filament_used_g
        != (None if filament_g_sum is None else float(filament_g_sum))
        or slice_manifest.maximum_layer_count != max(record.layer_count for record in slice_records)
        or slice_manifest.official_p2s_release_gate_passed is not aggregate_required
        or slice_manifest.all_parameter_checks_passed is not parameter_checks
        or slice_manifest.all_exit_codes_zero is not True
        or slice_manifest.no_out_of_bed is not True
        or slice_manifest.no_empty_layers is not True
        or slice_manifest.no_floating_regions is not True
        or slice_manifest.no_support_material is not True
        or slice_manifest.required_checks_passed is not aggregate_required
        or not aggregate_required
    ):
        raise RuntimeError("source slice aggregate evidence does not recompute")
    return _SourceEvidence(
        print_manifest=print_manifest,
        slice_manifest=slice_manifest,
        expected_version=expected_version,
        profiles=profiles,
        tiles=tuple(tiles),
    )


def copy_profiles(
    staging: Path,
    profiles: tuple[tuple[Any, Path], ...],
) -> tuple[list[Path], list[Path], list[dict[str, Any]]]:
    destination = staging / "profiles"
    destination.mkdir()
    copied_settings: list[Path] = []
    copied_filaments: list[Path] = []
    records: list[dict[str, Any]] = []
    for record, source in profiles:
        target = copied_settings if record.role == "settings" else copied_filaments
        name = f"{record.role}-{record.index:02d}-{source.name}"
        output = destination / name
        shutil.copyfile(source, output)
        target.append(output)
        records.append(
            {
                "role": record.role,
                "index": record.index,
                "path": f"profiles/{name}",
                "sha256": sha256(output),
            }
        )
    return copied_settings, copied_filaments, records


def _diagnostic_text(path: Path, *, label: str) -> str:
    information = _require_regular_file(path, label=label)
    if information.st_size > _MAX_JSON_BYTES:
        raise RuntimeError(f"{label} exceeds the {_MAX_JSON_BYTES} byte diagnostic limit: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _historical_stage_root(output_directory: str, *, tile_id: str, mode: str) -> Path:
    output = Path(output_directory)
    if (
        not output.is_absolute()
        or output.name != mode
        or output.parent.name != tile_id
        or output.parent.parent.name != ".runtime"
    ):
        raise RuntimeError(f"Bambu {mode} output directory is not stage/tile bound")
    return output.parent.parent.parent


def _verify_execution_record(
    value: Any,
    *,
    mode: str,
    executable_path: str,
    source: _SourceTileEvidence,
    settings_names: tuple[str, ...],
    filament_names: tuple[str, ...],
) -> Path:
    if not isinstance(value, dict) or set(value) != {
        "command",
        "process_exit_code",
        "duration_seconds",
    }:
        raise RuntimeError(f"Bambu {mode} execution record has an invalid field set")
    command = value.get("command")
    duration = value.get("duration_seconds")
    if (
        not isinstance(command, list)
        or any(not isinstance(item, str) or not item for item in command)
        or value.get("process_exit_code") != 0
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0
    ):
        raise RuntimeError(f"Bambu {mode} execution record is invalid")
    if mode == "build":
        if (
            len(command) != 21
            or command[0:4] != [executable_path, "--debug", "2", "--load-settings"]
            or command[5] != "--load-filaments"
            or command[7:13]
            != [
                "--load-defaultfila",
                "--curr-bed-type",
                "Textured PEI Plate",
                "--normative-check",
                "--ensure-on-bed",
                "--arrange",
            ]
            or command[13:19]
            != [
                "1",
                "--slice",
                "0",
                "--export-3mf",
                f"{source.tile_id}.bambu-p2s.3mf",
                "--outputdir",
            ]
        ):
            raise RuntimeError("Bambu build command does not match the exact normative grammar")
        stage = _historical_stage_root(command[19], tile_id=source.tile_id, mode="build")
        expected_settings = tuple(str(stage / "profiles" / name) for name in settings_names)
        expected_filaments = tuple(str(stage / "profiles" / name) for name in filament_names)
        if (
            tuple(command[4].split(";")) != expected_settings
            or tuple(command[6].split(";")) != expected_filaments
            or command[20] != str(source.source_3mf)
        ):
            raise RuntimeError("Bambu build command changed its input profiles or model")
        return stage
    if mode == "reopen":
        if len(command) != 9 or command[0:7] != [
            executable_path,
            "--debug",
            "2",
            "--normative-check",
            "--slice",
            "0",
            "--outputdir",
        ]:
            raise RuntimeError("Bambu reopen command does not match the exact normative grammar")
        stage = _historical_stage_root(command[7], tile_id=source.tile_id, mode="reopen")
        expected_project = stage / "tiles" / source.tile_id / "model.bambu-p2s.3mf"
        if command[8] != str(expected_project):
            raise RuntimeError("Bambu reopen command changed its project input")
        return stage
    raise AssertionError(f"unknown Bambu execution mode: {mode}")


def _verify_project_profiles(
    root: Path,
    reported: Any,
    source_profiles: tuple[tuple[Any, Path], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(reported, list) or len(reported) != len(source_profiles):
        raise RuntimeError("Bambu project profile set does not match source profiles")
    settings: list[str] = []
    filaments: list[str] = []
    expected_paths: set[Path] = set()
    for item, (source_record, _source_path) in zip(reported, source_profiles, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "index", "path", "sha256"}
            or item.get("role") != source_record.role
            or item.get("index") != source_record.index
            or item.get("sha256") != source_record.sha256
            or not isinstance(item.get("path"), str)
        ):
            raise RuntimeError("Bambu project profile identity differs from source profiles")
        path = resolve_relative(root, item["path"])
        if path in expected_paths or sha256(path) != source_record.sha256:
            raise RuntimeError(f"Bambu project profile substitution detected: {path}")
        expected_paths.add(path)
        target = settings if source_record.role == "settings" else filaments
        target.append(path.name)
    profile_root = root / "profiles"
    if (
        not profile_root.is_dir()
        or profile_root.is_symlink()
        or {path.resolve() for path in profile_root.iterdir()} != expected_paths
    ):
        raise RuntimeError("Bambu project profile directory has missing or extra files")
    return tuple(settings), tuple(filaments)


def verify_output(
    root: Path,
    *,
    print_root: Path,
    slice_root: Path,
    executable: Path,
) -> dict[str, Any]:
    source = _verify_source_evidence(
        print_root=print_root,
        slice_root=slice_root,
        executable=executable,
    )
    manifest_path = root / "bambu-tile-project-manifest.json"
    manifest = load_json(manifest_path)
    if manifest_path.read_bytes() != canonical_bytes(manifest):
        raise RuntimeError("Bambu tile project manifest is not canonical")
    executable_path = manifest.get("bambu_studio_path")
    probe = manifest.get("bambu_studio_probe")
    claim_boundary = (
        "official Bambu Studio software export/reopen/reslice evidence; "
        "no physical print or vendor certification claim"
    )
    if (
        set(manifest) != _ROOT_MANIFEST_FIELDS
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("layout_id") != source.print_manifest.layout_id
        or manifest.get("source_print_manifest_sha256")
        != sha256(print_root / "print-tile-assembly-manifest.json")
        or manifest.get("source_slice_manifest_sha256")
        != sha256(slice_root / "tile-slice-manifest.json")
        or manifest.get("bambu_studio_path") != str(executable)
        or manifest.get("bambu_studio_sha256") != sha256(executable)
        or manifest.get("bambu_studio_version") != source.expected_version
        or manifest.get("printer_profile_id") != "bambu-p2s-0.4"
        or manifest.get("tile_grid_shape") != list(source.print_manifest.tile_grid_shape)
        or manifest.get("tile_count") != len(source.tiles)
        or manifest.get("claim_boundary") != claim_boundary
        or not isinstance(executable_path, str)
        or not executable_path
        or not isinstance(probe, dict)
        or set(probe)
        != {
            "command",
            "process_exit_code",
            "duration_seconds",
            "version",
            "stdout_sha256",
            "stderr_sha256",
            "stdout_path",
            "stderr_path",
        }
    ):
        raise RuntimeError("Bambu tile project root identities changed")

    probe_stdout_relative = probe.get("stdout_path")
    probe_stderr_relative = probe.get("stderr_path")
    if not isinstance(probe_stdout_relative, str) or not isinstance(probe_stderr_relative, str):
        raise RuntimeError("Bambu Studio probe logs are not bound to the evidence root")
    probe_stdout = resolve_relative(root, probe_stdout_relative)
    probe_stderr = resolve_relative(root, probe_stderr_relative)
    probe_duration = probe.get("duration_seconds")
    probe_output_version = parse_bambu_studio_version(
        "\n".join(
            (
                _diagnostic_text(probe_stdout, label="Bambu probe stdout"),
                _diagnostic_text(probe_stderr, label="Bambu probe stderr"),
            )
        )
    )
    if (
        probe.get("command") != [executable_path, "--help"]
        or probe.get("process_exit_code") != 0
        or probe.get("version") != source.expected_version
        or isinstance(probe_duration, bool)
        or not isinstance(probe_duration, (int, float))
        or not math.isfinite(float(probe_duration))
        or float(probe_duration) < 0
        or sha256(probe_stdout) != probe.get("stdout_sha256")
        or sha256(probe_stderr) != probe.get("stderr_sha256")
        or probe_output_version != source.expected_version
    ):
        raise RuntimeError("Bambu Studio probe evidence changed")

    settings_names, filament_names = _verify_project_profiles(
        root,
        manifest.get("profile_files"),
        source.profiles,
    )
    records = manifest.get("tiles")
    if not isinstance(records, list):
        raise RuntimeError("Bambu tile project records are not a list")
    reported_identity = tuple(
        (
            record.get("tile_id"),
            record.get("row"),
            record.get("column"),
        )
        for record in records
        if isinstance(record, dict)
    )
    source_identity = tuple((tile.tile_id, tile.row, tile.column) for tile in source.tiles)
    if len(reported_identity) != len(records) or reported_identity != source_identity:
        raise RuntimeError("Bambu project has a missing, duplicate, extra, or reordered tile")
    tiles_root = root / "tiles"
    if (
        not tiles_root.is_dir()
        or tiles_root.is_symlink()
        or {path.name for path in tiles_root.iterdir()} != {tile.tile_id for tile in source.tiles}
        or any(not path.is_dir() or path.is_symlink() for path in tiles_root.iterdir())
    ):
        raise RuntimeError("Bambu project tile directory set differs from source tile set")

    recomputed_tiles: list[bool] = []
    expected_names = {
        "bambu_project_3mf": "model.bambu-p2s.3mf",
        "primary_gcode": "primary.gcode",
        "reopen_gcode": "reopen.gcode",
        "build_result": "build_result.json",
        "reopen_result": "reopen_result.json",
        "build_stdout": "build.stdout.log",
        "build_stderr": "build.stderr.log",
        "reopen_stdout": "reopen.stdout.log",
        "reopen_stderr": "reopen.stderr.log",
    }
    for record, source_tile in zip(records, source.tiles, strict=True):
        if not isinstance(record, dict) or set(record) != _ROOT_TILE_FIELDS:
            raise RuntimeError("Bambu tile project record has an invalid field set")
        tile_id = source_tile.tile_id
        relative_dir = f"tiles/{tile_id}"
        expected_files = {role: f"{relative_dir}/{name}" for role, name in expected_names.items()}
        files = record.get("files")
        hashes = record.get("sha256")
        if (
            record.get("source_print_tile_manifest_sha256")
            != source_tile.print_record.tile_manifest_sha256
            or record.get("source_slice_report_sha256") != source_tile.slice_record.report_sha256
            or record.get("validation_path") != f"{relative_dir}/project_validation.json"
            or record.get("required_checks_passed") is not True
            or not isinstance(files, dict)
            or files != expected_files
            or not isinstance(hashes, dict)
            or set(hashes) != _PROJECT_FILE_ROLES
        ):
            raise RuntimeError(f"Bambu project tile source/role binding changed: {tile_id}")
        validation_path = resolve_relative(root, record["validation_path"])
        validation = load_json(validation_path)
        if (
            set(validation) != _VALIDATION_FIELDS
            or validation_path.read_bytes() != canonical_bytes(validation)
            or sha256(validation_path) != record.get("validation_sha256")
        ):
            raise RuntimeError(f"Bambu tile validation identity changed: {tile_id}")
        resolved_files: dict[str, Path] = {}
        for role in sorted(_PROJECT_FILE_ROLES):
            relative = files[role]
            path = resolve_relative(root, relative)
            if sha256(path) != hashes.get(role):
                raise RuntimeError(f"Bambu tile project checksum mismatch: {tile_id}: {role}")
            resolved_files[role] = path
        tile_directory = root / relative_dir
        expected_inventory = {
            *expected_names.values(),
            "project_validation.json",
        }
        if (
            not tile_directory.is_dir()
            or tile_directory.is_symlink()
            or {path.name for path in tile_directory.iterdir()} != expected_inventory
            or any(not path.is_file() or path.is_symlink() for path in tile_directory.iterdir())
        ):
            raise RuntimeError(f"Bambu project tile file inventory changed: {tile_id}")

        project = resolved_files["bambu_project_3mf"]
        primary = resolved_files["primary_gcode"]
        reopened = resolved_files["reopen_gcode"]
        build_result = load_json(resolved_files["build_result"])
        reopen_result = load_json(resolved_files["reopen_result"])
        if not result_passed(build_result) or not result_passed(reopen_result):
            raise RuntimeError(f"Bambu result.json failed on reopen: {tile_id}")
        build_object = object_measurement(build_result)
        reopen_object = object_measurement(reopen_result)
        source_dimensions = tuple(source_tile.source_3mf_inspection.dimensions_mm)
        source_triangles = int(source_tile.source_3mf_inspection.triangle_count)
        archive = archive_evidence(project, primary)
        dimensions_ok = bool(
            dimensions_match(build_object["dimensions_mm"], source_dimensions)
            and dimensions_match(reopen_object["dimensions_mm"], source_dimensions)
            and dimensions_match(archive["project_model_dimensions_mm"], source_dimensions)
        )
        triangles_ok = bool(
            build_object["triangle_count"] == source_triangles
            and reopen_object["triangle_count"] == source_triangles
            and archive["project_model_triangle_count"] == source_triangles
        )
        build_stdout = _diagnostic_text(resolved_files["build_stdout"], label="Bambu build stdout")
        build_stderr = _diagnostic_text(resolved_files["build_stderr"], label="Bambu build stderr")
        reopen_stdout = _diagnostic_text(
            resolved_files["reopen_stdout"], label="Bambu reopen stdout"
        )
        reopen_stderr = _diagnostic_text(
            resolved_files["reopen_stderr"], label="Bambu reopen stderr"
        )
        build_metrics, build_gate, build_version = release_gate(
            primary,
            expected_version=source.expected_version,
            stdout=build_stdout,
            stderr=build_stderr,
        )
        reopen_metrics, reopen_gate, reopen_version = release_gate(
            reopened,
            expected_version=source.expected_version,
            stdout=reopen_stdout,
            stderr=reopen_stderr,
        )
        build_stage = _verify_execution_record(
            validation.get("build_execution"),
            mode="build",
            executable_path=executable_path,
            source=source_tile,
            settings_names=settings_names,
            filament_names=filament_names,
        )
        reopen_stage = _verify_execution_record(
            validation.get("reopen_execution"),
            mode="reopen",
            executable_path=executable_path,
            source=source_tile,
            settings_names=settings_names,
            filament_names=filament_names,
        )
        if build_stage != reopen_stage:
            raise RuntimeError("Bambu build and reopen commands use different staging roots")
        required = bool(
            archive["archive_test_passed"]
            and archive["embedded_gcode_md5_verified"]
            and archive["embedded_gcode_matches_primary"]
            and build_gate.get("release_gate_passed") is True
            and reopen_gate.get("release_gate_passed") is True
            and build_version == reopen_version == source.expected_version
            and dimensions_ok
            and triangles_ok
        )
        if (
            validation.get("schema_version") != TILE_SCHEMA_VERSION
            or validation.get("tile_id") != tile_id
            or validation.get("source_print_local_3mf_path")
            != source_tile.print_record.files["print_local_3mf"]
            or validation.get("source_print_local_3mf_sha256") != sha256(source_tile.source_3mf)
            or validation.get("source_slice_report_sha256")
            != source_tile.slice_record.report_sha256
            or validation.get("source_dimensions_mm") != list(source_dimensions)
            or validation.get("source_triangle_count") != source_triangles
            or validation.get("build_result") != build_result
            or validation.get("reopen_result") != reopen_result
            or validation.get("build_object") != build_object
            or validation.get("reopen_object") != reopen_object
            or validation.get("dimensions_match") is not dimensions_ok
            or validation.get("triangle_counts_match") is not triangles_ok
            or validation.get("project_archive") != archive
            or validation.get("primary_metrics") != build_metrics
            or validation.get("reopen_metrics") != reopen_metrics
            or validation.get("primary_release_gate") != build_gate
            or validation.get("reopen_release_gate") != reopen_gate
            or validation.get("expected_bambu_studio_version") != source.expected_version
            or validation.get("primary_bambu_studio_version") != build_version
            or validation.get("reopen_bambu_studio_version") != reopen_version
            or validation.get("bambu_studio_versions_match")
            is not (build_version == reopen_version == source.expected_version)
            or validation.get("external_profiles_loaded_on_reopen") is not False
            or validation.get("required_checks_passed") is not required
            or not required
        ):
            raise RuntimeError(f"Bambu project semantic validation changed: {tile_id}")
        recomputed_tiles.append(required)

    aggregate = bool(recomputed_tiles and all(recomputed_tiles))
    if (
        manifest.get("all_projects_reopened") is not aggregate
        or manifest.get("all_release_gates_passed") is not aggregate
        or manifest.get("required_checks_passed") is not aggregate
        or not aggregate
    ):
        raise RuntimeError("Bambu tile project aggregate gate changed")
    return {
        "status": "verified",
        "tile_count": len(source.tiles),
        "all_projects_reopened": True,
        "all_release_gates_passed": True,
        "required_checks_passed": True,
    }


def _build_evidence(args: _EvidenceArgs) -> dict[str, Any]:
    print_root = args.print_set.expanduser().resolve()
    slice_root = args.slice_set.expanduser().resolve()
    executable = args.bambu_studio.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        raise RuntimeError(f"Bambu tile project destination already exists: {output}")
    if not executable.is_file():
        raise RuntimeError(f"Bambu Studio executable does not exist: {executable}")
    source = _verify_source_evidence(
        print_root=print_root,
        slice_root=slice_root,
        executable=executable,
    )
    print_manifest = source.print_manifest
    expected_version = source.expected_version
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.topoforge-stage-", dir=output.parent))
    runtime_root = staging / ".runtime"
    try:
        probe_record = probe_bambu_studio(
            executable,
            runtime=runtime_root / "probe",
            timeout_seconds=args.timeout,
            evidence_root=staging,
        )
        if probe_record["version"] != expected_version:
            raise RuntimeError(
                f"Bambu Studio probe version {probe_record['version']!r} does not match "
                f"the frozen source-slice version {expected_version!r}"
            )
        settings, filaments, profile_records = copy_profiles(staging, source.profiles)
        records: list[dict[str, Any]] = []
        for source_tile in source.tiles:
            tile_id = source_tile.tile_id
            source_record = source_tile.print_record
            source_slice = source_tile.slice_record
            input_relative = source_record.files["print_local_3mf"]
            input_path = source_tile.source_3mf
            input_inspection = source_tile.source_3mf_inspection
            tile_dir = staging / "tiles" / tile_id
            tile_dir.mkdir(parents=True)
            build_runtime = runtime_root / tile_id / "build"
            reopen_runtime = runtime_root / tile_id / "reopen"
            build_runtime.mkdir(parents=True)
            reopen_runtime.mkdir(parents=True)
            project_name = f"{tile_id}.bambu-p2s.3mf"
            build_command = [
                str(executable),
                "--debug",
                "2",
                "--load-settings",
                ";".join(str(path) for path in settings),
                "--load-filaments",
                ";".join(str(path) for path in filaments),
                "--load-defaultfila",
                "--curr-bed-type",
                "Textured PEI Plate",
                "--normative-check",
                "--ensure-on-bed",
                "--arrange",
                "1",
                "--slice",
                "0",
                "--export-3mf",
                project_name,
                "--outputdir",
                str(build_runtime),
                str(input_path),
            ]
            build_execution = run_checked(
                build_command, runtime=build_runtime / "environment", timeout_seconds=args.timeout
            )
            build_result_path = build_runtime / "result.json"
            build_project_path = build_runtime / project_name
            build_gcode_path = build_runtime / "plate_1.gcode"
            build_result = load_json(build_result_path)
            if (
                not result_passed(build_result)
                or not build_project_path.is_file()
                or not build_gcode_path.is_file()
            ):
                raise RuntimeError(f"Bambu project build failed for {tile_id}")
            project_path = tile_dir / "model.bambu-p2s.3mf"
            primary_gcode = tile_dir / "primary.gcode"
            shutil.copyfile(build_project_path, project_path)
            shutil.copyfile(build_gcode_path, primary_gcode)
            shutil.copyfile(build_result_path, tile_dir / "build_result.json")
            (tile_dir / "build.stdout.log").write_text(build_execution.stdout, encoding="utf-8")
            (tile_dir / "build.stderr.log").write_text(build_execution.stderr, encoding="utf-8")
            reopen_command = [
                str(executable),
                "--debug",
                "2",
                "--normative-check",
                "--slice",
                "0",
                "--outputdir",
                str(reopen_runtime),
                str(project_path),
            ]
            reopen_execution = run_checked(
                reopen_command, runtime=reopen_runtime / "environment", timeout_seconds=args.timeout
            )
            reopen_result_path = reopen_runtime / "result.json"
            reopen_gcode_path = reopen_runtime / "plate_1.gcode"
            reopen_result = load_json(reopen_result_path)
            if not result_passed(reopen_result) or not reopen_gcode_path.is_file():
                raise RuntimeError(f"Bambu project reopen failed for {tile_id}")
            reopen_gcode = tile_dir / "reopen.gcode"
            shutil.copyfile(reopen_gcode_path, reopen_gcode)
            shutil.copyfile(reopen_result_path, tile_dir / "reopen_result.json")
            (tile_dir / "reopen.stdout.log").write_text(reopen_execution.stdout, encoding="utf-8")
            (tile_dir / "reopen.stderr.log").write_text(reopen_execution.stderr, encoding="utf-8")
            primary_metrics, primary_gate, primary_version = release_gate(
                primary_gcode,
                expected_version=expected_version,
                stdout=build_execution.stdout,
                stderr=build_execution.stderr,
            )
            reopen_metrics, reopen_gate, reopen_version = release_gate(
                reopen_gcode,
                expected_version=expected_version,
                stdout=reopen_execution.stdout,
                stderr=reopen_execution.stderr,
            )
            build_object = object_measurement(build_result)
            reopen_object = object_measurement(reopen_result)
            archive = archive_evidence(project_path, primary_gcode)
            dimensions_ok = bool(
                dimensions_match(build_object["dimensions_mm"], input_inspection.dimensions_mm)
                and dimensions_match(reopen_object["dimensions_mm"], input_inspection.dimensions_mm)
                and dimensions_match(
                    archive["project_model_dimensions_mm"],
                    input_inspection.dimensions_mm,
                )
            )
            triangles_ok = bool(
                build_object["triangle_count"] == input_inspection.triangle_count
                and reopen_object["triangle_count"] == input_inspection.triangle_count
                and archive["project_model_triangle_count"] == input_inspection.triangle_count
            )
            required = bool(
                build_execution.returncode == 0
                and reopen_execution.returncode == 0
                and result_passed(build_result)
                and result_passed(reopen_result)
                and archive["archive_test_passed"]
                and archive["embedded_gcode_md5_verified"]
                and archive["embedded_gcode_matches_primary"]
                and primary_gate["release_gate_passed"]
                and reopen_gate["release_gate_passed"]
                and primary_version == expected_version
                and reopen_version == expected_version
                and dimensions_ok
                and triangles_ok
            )
            validation = {
                "schema_version": TILE_SCHEMA_VERSION,
                "tile_id": tile_id,
                "source_print_local_3mf_path": input_relative,
                "source_print_local_3mf_sha256": sha256(input_path),
                "source_slice_report_sha256": source_slice.report_sha256,
                "source_dimensions_mm": list(input_inspection.dimensions_mm),
                "source_triangle_count": input_inspection.triangle_count,
                "build_execution": execution_record(build_execution, build_command),
                "reopen_execution": execution_record(reopen_execution, reopen_command),
                "build_result": build_result,
                "reopen_result": reopen_result,
                "build_object": build_object,
                "reopen_object": reopen_object,
                "dimensions_match": dimensions_ok,
                "triangle_counts_match": triangles_ok,
                "project_archive": archive,
                "primary_metrics": primary_metrics,
                "reopen_metrics": reopen_metrics,
                "primary_release_gate": primary_gate,
                "reopen_release_gate": reopen_gate,
                "expected_bambu_studio_version": expected_version,
                "primary_bambu_studio_version": primary_version,
                "reopen_bambu_studio_version": reopen_version,
                "bambu_studio_versions_match": (
                    primary_version == reopen_version == expected_version
                ),
                "external_profiles_loaded_on_reopen": False,
                "required_checks_passed": required,
            }
            if not required:
                raise RuntimeError(f"Bambu project validation failed for {tile_id}")
            validation_path = write_canonical(tile_dir / "project_validation.json", validation)
            role_names = {
                "bambu_project_3mf": "model.bambu-p2s.3mf",
                "primary_gcode": "primary.gcode",
                "reopen_gcode": "reopen.gcode",
                "build_result": "build_result.json",
                "reopen_result": "reopen_result.json",
                "build_stdout": "build.stdout.log",
                "build_stderr": "build.stderr.log",
                "reopen_stdout": "reopen.stdout.log",
                "reopen_stderr": "reopen.stderr.log",
            }
            relative_dir = f"tiles/{tile_id}"
            files = {role: f"{relative_dir}/{name}" for role, name in role_names.items()}
            hashes = {role: sha256(staging / relative) for role, relative in files.items()}
            records.append(
                {
                    "tile_id": tile_id,
                    "row": source_record.row,
                    "column": source_record.column,
                    "source_print_tile_manifest_sha256": source_record.tile_manifest_sha256,
                    "source_slice_report_sha256": source_slice.report_sha256,
                    "validation_path": f"{relative_dir}/project_validation.json",
                    "validation_sha256": sha256(validation_path),
                    "files": files,
                    "sha256": hashes,
                    "required_checks_passed": True,
                }
            )
        shutil.rmtree(runtime_root, ignore_errors=True)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "layout_id": print_manifest.layout_id,
            "source_print_manifest_sha256": sha256(
                print_root / "print-tile-assembly-manifest.json"
            ),
            "source_slice_manifest_sha256": sha256(slice_root / "tile-slice-manifest.json"),
            "bambu_studio_path": str(executable),
            "bambu_studio_sha256": sha256(executable),
            "bambu_studio_version": expected_version,
            "bambu_studio_probe": probe_record,
            "printer_profile_id": "bambu-p2s-0.4",
            "profile_files": profile_records,
            "tile_grid_shape": list(print_manifest.tile_grid_shape),
            "tile_count": len(records),
            "all_projects_reopened": all(record["required_checks_passed"] for record in records),
            "all_release_gates_passed": all(record["required_checks_passed"] for record in records),
            "claim_boundary": (
                "official Bambu Studio software export/reopen/reslice evidence; "
                "no physical print or vendor certification claim"
            ),
            "required_checks_passed": all(record["required_checks_passed"] for record in records),
            "tiles": records,
        }
        write_canonical(staging / "bambu-tile-project-manifest.json", manifest)
        verification = verify_output(
            staging, print_root=print_root, slice_root=slice_root, executable=executable
        )
        staging.replace(output)
        return {
            "status": "published",
            "output": str(output),
            "manifest": str(output / "bambu-tile-project-manifest.json"),
            "verification": verification,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_bambu_project_evidence(
    output_dir: Path,
    *,
    print_set_dir: Path,
    slice_set_dir: Path,
    bambu_studio: Path,
) -> dict[str, Any]:
    """Strictly reopen project archives, G-code, source bindings, and release gates."""
    root = output_dir.expanduser().resolve()
    print_root = print_set_dir.expanduser().resolve()
    slice_root = slice_set_dir.expanduser().resolve()
    executable = bambu_studio.expanduser().resolve()
    if not executable.is_file():
        raise RuntimeError(f"Bambu Studio executable does not exist: {executable}")
    return verify_output(
        root,
        print_root=print_root,
        slice_root=slice_root,
        executable=executable,
    )


def generate_bambu_project_evidence(
    print_set_dir: Path,
    slice_set_dir: Path,
    bambu_studio: Path,
    output_dir: Path,
    *,
    timeout_seconds: float = 1800.0,
) -> BambuProjectEvidenceResult:
    """Export one Bambu project per tile and verify no-profile reopen/reslice evidence."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    published = _build_evidence(
        _EvidenceArgs(
            print_set=print_set_dir,
            slice_set=slice_set_dir,
            bambu_studio=bambu_studio,
            output=output_dir,
            timeout=timeout_seconds,
        )
    )
    output = Path(str(published["output"])).resolve()
    manifest = Path(str(published["manifest"])).resolve()
    verification = published.get("verification")
    if not isinstance(verification, dict):
        raise RuntimeError("Bambu project verification result is not an object")
    return BambuProjectEvidenceResult(
        output_dir=output,
        manifest_path=manifest,
        verification=verification,
    )
