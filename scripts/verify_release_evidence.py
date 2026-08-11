#!/usr/bin/env python3
"""Fail closed on Windows evidence before publishing any TopoForge 0.11.x release."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

GATE_SCHEMA_VERSION = "topoforge-windows-release-gate-v1"
MANIFEST_SCHEMA_VERSION = "topoforge-windows-release-evidence-v1"
WINDOWS_TARGETS = frozenset({"windows-10-22h2-x64", "windows-11-x64"})
TARGET_ARGUMENTS = {
    "windows-10-22h2-x64": "win10-22h2",
    "windows-11-x64": "win11",
}
VERIFIER_PATHS = {
    "builder": "scripts/build_windows_portable.py",
    "portable": "scripts/verify_windows_portable.py",
    "system": "scripts/verify_windows_system.py",
    "bambu": "scripts/verify_windows_bambu.py",
    "helper": "scripts/windows_acceptance.py",
}
SAFE_ARTIFACT_COMPONENT = re.compile(r"^[A-Za-z0-9._/-]+$")
ARTIFACT_ZIP_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024
ARTIFACT_ZIP_MEMBER_MAX_BYTES = 256 * 1024 * 1024
ARTIFACT_ZIP_EXPANDED_MAX_BYTES = 512 * 1024 * 1024
ARTIFACT_ZIP_MEMBER_COUNT_MAX = 128
WINDOWS_RESERVED_ARTIFACT_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
GITHUB_PUBLISHED_AT_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
VERSION_PATTERN = re.compile(r"^0[.]11[.]([0-9]+)")
THUMBPRINT_PATTERN = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
CONFIG_PATH = PurePosixPath("packaging/windows-x64-runtime.json")
BUILD_CONSTRAINTS_PATH = PurePosixPath("packaging/build-constraints.txt")
BAMBU_IDENTITY_POLICY_PATH = PurePosixPath("packaging/bambu-studio-windows-identity-policy.json")
BAMBU_IDENTITY_POLICY_SCHEMA_VERSION = "topoforge-bambu-windows-identity-policy-v1"
PLATFORM_CORE_VERIFIER_PATH = PurePosixPath("scripts/verify_platform_core.py")
CROSS_PLATFORM_SCHEMA_VERSION = "topoforge-cross-platform-comparison-v1"
ROLLBACK_SCHEMA_VERSION = "topoforge-rollback-runtime-verification-v4"
PORTABLE_REPORT_SCHEMA_VERSION = "topoforge-windows-portable-verification-v2"
SYSTEM_REPORT_SCHEMA_VERSION = "topoforge-windows-system-verification-v2"
BAMBU_REPORT_SCHEMA_VERSION = "topoforge-windows-bambu-verification-v2"
PROFILE_BUNDLE_SCHEMA_VERSION = "topoforge-bambu-profile-bundle-v1"
PUBLIC_EVIDENCE_SCHEMA_VERSION = "topoforge-windows-public-evidence-v1"
PUBLIC_PRIVATE_FIELDS = frozenset(
    {
        "command",
        "commands",
        "cwd",
        "repository_root",
        "stderr",
        "stdout",
    }
)
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
CORE_ROLES = ("model.3mf", "model.stl", "preview.glb")
WINDOWS_CANDIDATE_ARTIFACT_NAME = "topoforge-windows-x64-portable-candidate"
CANONICAL_LINUX_CI_ARTIFACT_NAME = "topoforge-linux-x86_64-python-3.12-core-evidence"
CANONICAL_LINUX_CI_RELATIVE_PATH = PurePosixPath("ci-linux-x86_64-python-3.12-core.json")
CLEAN_EVIDENCE_WORKFLOW_NAME = "windows-clean-release-evidence"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CLEAN_EVIDENCE_WORKFLOW_PATH = ".github/workflows/windows-clean-release-evidence.yml"
CLEAN_EVIDENCE_ARTIFACT_NAMES = {
    target: f"topoforge-{target}-clean-release-evidence" for target in WINDOWS_TARGETS
}
ROLLBACK_PRODUCER_PATH = PurePosixPath("scripts/verify_release_rollback.py")
ROLLBACK_RUNTIME_RELATIVE_PATH = PurePosixPath("rollback-verification-runtime.json")
PUBLIC_REPORT_KEYS = {
    "schema_version",
    "archive",
    "topoforge_version",
    "target",
    "runtime",
    "contents",
    "project_wheel",
    "provenance",
    "launchers",
    "cross_host_inspection_passed",
    "reproducibility",
    "execution",
    "public_evidence_projection",
    "required_checks_passed",
}
PUBLIC_EXECUTION_KEYS = {
    "expected_target",
    "windows_target",
    "hosted_server",
    "evidence_scope",
    "platform",
    "candidate_binding",
    "extraction_path",
    "archive_sha256",
    "archive_sha256_verified_before_after_and_at_completion",
    "path_contains_spaces",
    "path_contains_non_ascii",
    "cli_launcher",
    "web_launcher_installation_check",
    "real_http_web",
    "windows_process_containment",
    "core",
    "system",
    "bambu",
    "claim_boundary",
    "required_checks_passed",
}
SECRET_LIKE_PATTERN = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/=-]{6,}|"
    r"\b(?:api[ _-]?key|password|secret|token)\s*[:=]\s*\S+|"
    r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{16,}|"
    r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}|"
    r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])|"
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----)"
)
WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:/|\\)")
UNC_PATH_PATTERN = re.compile(r"(?<![:/])(?://|\\\\)[^/\\\s]+(?:/|\\)")
URI_PATTERN = re.compile(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
PUBLIC_PATH_PREFIXES = (
    "C:/TopoForge Public Evidence/",
    "C:/Program Files/Bambu Studio/",
)
PUBLIC_CLAIM_BOUNDARY = "clean support requires matching Win10 and Win11 reports"
PUBLIC_REDACTED_ROOT_LABELS = frozenset(
    {
        "candidate_archive",
        "repository",
        "work_root",
        "bambu_studio_install_root",
        "bambu_profiles_root",
        "user_profile",
        "local_app_data",
        "roaming_app_data",
        "environment_temp",
        "environment_tmp",
    }
)
PUBLIC_ROOT_PATH_KEYS = frozenset(
    {
        "artifact_path",
        "binding_path",
        "extraction_path",
        "expected_executable_sibling_path",
        "path",
        "root",
    }
)
PUBLIC_ROOT_PLACEHOLDER_PATTERN = re.compile(r"C:/TopoForge Public Evidence/([a-z][a-z0-9_]*)")


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _sha256(value: Any, label: str) -> str:
    digest = _non_empty_string(value, label)
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _artifact_digest(value: Any, label: str) -> str:
    digest = _non_empty_string(value, label)
    if ARTIFACT_DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be a sha256:-prefixed lowercase artifact digest")
    return digest


def _github_published_at(value: Any, label: str) -> str:
    published_at = _non_empty_string(value, label)
    if GITHUB_PUBLISHED_AT_PATTERN.fullmatch(published_at) is None:
        raise ValueError(f"{label} must be a canonical GitHub UTC timestamp")
    try:
        datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid GitHub UTC timestamp") from exc
    return published_at


def _commit(value: Any, label: str) -> str:
    commit = _non_empty_string(value, label)
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise ValueError(f"{label} must be a full lowercase Git commit")
    return commit


def _safe_relative_path(value: Any, label: str) -> PurePosixPath:
    raw = _non_empty_string(value, label)
    if "\x00" in raw or "\\" in raw or SAFE_ARTIFACT_COMPONENT.fullmatch(raw) is None:
        raise ValueError(f"{label} is not a canonical safe relative path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or "." in path.parts
        or ".." in path.parts
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ValueError(f"{label} is not a canonical safe relative path")
    return path


def _artifact_member_path(name: str, *, is_directory: bool) -> PurePosixPath:
    raw = name[:-1] if is_directory and name.endswith("/") else name
    path = _safe_relative_path(raw, "artifact ZIP member")
    for part in path.parts:
        reserved_stem = part.split(".", 1)[0].casefold()
        if part.endswith(".") or reserved_stem in WINDOWS_RESERVED_ARTIFACT_NAMES:
            raise ValueError(f"artifact ZIP member has an unsafe Windows component: {name!r}")
    return path


def _artifact_collision_key(path: PurePosixPath) -> str:
    return "/".join(part.casefold() for part in path.parts)


def _register_artifact_path(
    path: PurePosixPath,
    *,
    is_directory: bool,
    explicit_paths: dict[str, str],
    path_kinds: dict[str, tuple[str, str]],
) -> None:
    rendered = path.as_posix()
    key = _artifact_collision_key(path)
    previous_explicit = explicit_paths.get(key)
    if previous_explicit is not None:
        if previous_explicit == rendered:
            raise ValueError(f"artifact ZIP contains a duplicate member: {rendered}")
        raise ValueError(
            f"artifact ZIP members collide across platforms: {previous_explicit!r} and {rendered!r}"
        )
    explicit_paths[key] = rendered
    for length in range(1, len(path.parts) + 1):
        prefix_path = PurePosixPath(*path.parts[:length])
        prefix = prefix_path.as_posix()
        prefix_key = _artifact_collision_key(prefix_path)
        kind = "directory" if length < len(path.parts) or is_directory else "file"
        previous = path_kinds.get(prefix_key)
        if previous is None:
            path_kinds[prefix_key] = (prefix, kind)
            continue
        previous_path, previous_kind = previous
        if previous_path != prefix:
            raise ValueError(
                f"artifact ZIP paths collide across platforms: {previous_path!r} and {prefix!r}"
            )
        if previous_kind != kind:
            raise ValueError(f"artifact ZIP path is both a file and directory: {prefix!r}")


def _consume_artifact_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    received = 0
    with archive.open(info) as source:
        while True:
            chunk = source.read(min(1024 * 1024, info.file_size - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > info.file_size or received > ARTIFACT_ZIP_MEMBER_MAX_BYTES:
                raise ValueError(f"artifact ZIP member exceeds its declared size: {info.filename}")
    if received != info.file_size:
        raise ValueError(f"artifact ZIP member byte count changed: {info.filename}")


def _expected_artifact_members(
    required_members: Iterable[str],
    allowed_members: Iterable[str],
) -> tuple[set[str], set[str], set[str]]:
    required: set[str] = set()
    allowed: set[str] = set()
    explicit_paths: dict[str, str] = {}
    path_kinds: dict[str, tuple[str, str]] = {}
    for raw, destination in (
        *((value, required) for value in required_members),
        *((value, allowed) for value in allowed_members),
    ):
        path = _artifact_member_path(raw, is_directory=False)
        _register_artifact_path(
            path,
            is_directory=False,
            explicit_paths=explicit_paths,
            path_kinds=path_kinds,
        )
        destination.add(path.as_posix())
    permitted = required | allowed
    if not permitted:
        raise ValueError("artifact extraction requires at least one permitted member")
    parent_directories = {
        parent.as_posix()
        for raw in permitted
        for parent in PurePosixPath(raw).parents
        if parent != PurePosixPath(".")
    }
    return required, permitted, parent_directories


def _validate_artifact_archive(
    archive: zipfile.ZipFile,
    *,
    required_members: set[str],
    permitted_members: set[str],
    permitted_directories: set[str],
) -> tuple[dict[str, zipfile.ZipInfo], int]:
    infos = archive.infolist()
    if not infos:
        raise ValueError("artifact ZIP is empty")
    if len(infos) > ARTIFACT_ZIP_MEMBER_COUNT_MAX:
        raise ValueError("artifact ZIP exceeds its member-count bound")
    if archive.comment:
        raise ValueError("artifact ZIP comments are forbidden")
    files: dict[str, zipfile.ZipInfo] = {}
    explicit_paths: dict[str, str] = {}
    path_kinds: dict[str, tuple[str, str]] = {}
    expanded_bytes = 0
    for info in infos:
        is_directory = info.is_dir()
        path = _artifact_member_path(info.filename, is_directory=is_directory)
        rendered = path.as_posix()
        _register_artifact_path(
            path,
            is_directory=is_directory,
            explicit_paths=explicit_paths,
            path_kinds=path_kinds,
        )
        mode_type = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
        allowed_modes = {0, stat.S_IFDIR} if is_directory else {0, stat.S_IFREG}
        if mode_type not in allowed_modes:
            raise ValueError(f"artifact ZIP contains a link or special member: {info.filename}")
        if info.flag_bits & 0x1:
            raise ValueError(f"artifact ZIP contains an encrypted member: {info.filename}")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError(f"artifact ZIP uses unsupported compression: {info.filename}")
        if is_directory:
            if rendered not in permitted_directories or info.file_size != 0:
                raise ValueError(f"artifact ZIP contains an unexpected directory: {info.filename}")
            continue
        if rendered not in permitted_members:
            raise ValueError(f"artifact ZIP contains an unexpected file: {info.filename}")
        if info.file_size > ARTIFACT_ZIP_MEMBER_MAX_BYTES:
            raise ValueError(f"artifact ZIP member exceeds its size bound: {info.filename}")
        expanded_bytes += info.file_size
        if expanded_bytes > ARTIFACT_ZIP_EXPANDED_MAX_BYTES:
            raise ValueError("artifact ZIP exceeds its total expansion bound")
        _consume_artifact_member(archive, info)
        files[rendered] = info
    missing = sorted(required_members - set(files))
    if missing:
        raise ValueError(f"artifact ZIP is missing required members: {missing}")
    if not files:
        raise ValueError("artifact ZIP contains no permitted regular files")
    return files, expanded_bytes


def extract_exact_artifact(
    archive_path: Path,
    destination: Path,
    *,
    required_members: Iterable[str],
    allowed_members: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate a bounded artifact ZIP completely, then atomically extract exact files."""
    metadata = archive_path.lstat()
    if (
        archive_path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > ARTIFACT_ZIP_ARCHIVE_MAX_BYTES
    ):
        raise ValueError("artifact ZIP must be a bounded real single-link file")
    if destination.exists() or destination.is_symlink():
        raise ValueError("artifact extraction destination must not already exist")
    destination_parent = destination.parent.resolve()
    if not destination_parent.is_dir():
        raise ValueError("artifact extraction destination parent must already exist")
    destination_path = destination_parent / destination.name
    required, permitted, directories = _expected_artifact_members(
        required_members,
        allowed_members,
    )
    with archive_path.open("rb") as source_handle:
        opened = os.fstat(source_handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ):
            raise ValueError("artifact ZIP changed while it was opened")
        with zipfile.ZipFile(source_handle) as archive:
            files, expanded_bytes = _validate_artifact_archive(
                archive,
                required_members=required,
                permitted_members=permitted,
                permitted_directories=directories,
            )
            validated = os.fstat(source_handle.fileno())
            if (validated.st_size, validated.st_mtime_ns) != (
                metadata.st_size,
                metadata.st_mtime_ns,
            ):
                raise ValueError("artifact ZIP changed during validation")
            staging = Path(tempfile.mkdtemp(prefix=".topoforge-artifact-", dir=destination_parent))
            try:
                for member_name in sorted(files):
                    info = files[member_name]
                    output = staging.joinpath(*PurePosixPath(member_name).parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    received = 0
                    with archive.open(info) as member_source, output.open("xb") as member_output:
                        while True:
                            chunk = member_source.read(
                                min(1024 * 1024, info.file_size - received + 1)
                            )
                            if not chunk:
                                break
                            received += len(chunk)
                            if (
                                received > info.file_size
                                or received > ARTIFACT_ZIP_MEMBER_MAX_BYTES
                            ):
                                raise ValueError(
                                    f"artifact ZIP member changed during extraction: {member_name}"
                                )
                            member_output.write(chunk)
                    if received != info.file_size:
                        raise ValueError(
                            "artifact ZIP member byte count changed during extraction: "
                            f"{member_name}"
                        )
                extracted = os.fstat(source_handle.fileno())
                if (extracted.st_size, extracted.st_mtime_ns) != (
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ):
                    raise ValueError("artifact ZIP changed during extraction")
                staging.replace(destination_path)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    return {
        "archive": str(archive_path),
        "destination": str(destination_path),
        "archive_bytes": metadata.st_size,
        "expanded_bytes": expanded_bytes,
        "files": sorted(files),
        "required_members": sorted(required),
        "permitted_members": sorted(permitted),
        "required_checks_passed": True,
    }


def _safe_profile_relative_path(value: Any, label: str) -> PurePosixPath:
    raw = _non_empty_string(value, label)
    path = PurePosixPath(raw)
    if (
        "\x00" in raw
        or "\\" in raw
        or path.is_absolute()
        or path.as_posix() != raw
        or "." in path.parts
        or ".." in path.parts
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ValueError(f"{label} is not a canonical safe profile-relative path")
    return path


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields are {sorted(actual)}, expected exactly {sorted(expected)}"
        )


def _validate_public_clean_evidence(report: dict[str, Any], *, label: str) -> None:
    projection = _json_object(
        report.get("public_evidence_projection"),
        f"{label}.public_evidence_projection",
    )
    _exact_keys(
        projection,
        {
            "schema_version",
            "private_report_sha256",
            "removed_fields",
            "redacted_root_labels",
            "required_checks_passed",
        },
        f"{label}.public_evidence_projection",
    )
    removed = projection.get("removed_fields")
    labels = projection.get("redacted_root_labels")
    if (
        projection.get("schema_version") != PUBLIC_EVIDENCE_SCHEMA_VERSION
        or projection.get("required_checks_passed") is not True
        or not isinstance(removed, list)
        or removed != sorted(set(removed))
        or not set(removed) <= PUBLIC_PRIVATE_FIELDS
        or "commands" not in removed
        or not isinstance(labels, list)
        or labels != sorted(set(labels))
        or not labels
        or any(
            not isinstance(item, str) or item not in PUBLIC_REDACTED_ROOT_LABELS for item in labels
        )
    ):
        raise ValueError(f"{label} is not a valid public evidence projection")
    _sha256(
        projection.get("private_report_sha256"),
        f"{label}.public_evidence_projection.private_report_sha256",
    )

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() in PUBLIC_PRIVATE_FIELDS:
                    raise ValueError(f"{label} retains private field {path}.{key}")
                inspect(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                inspect(item, f"{path}[{index}]")
        elif isinstance(value, str):
            normalized = value.replace("\\", "/").casefold()
            if SECRET_LIKE_PATTERN.search(value):
                raise ValueError(f"{label} retains secret-like text at {path}")
            if UNC_PATH_PATTERN.search(value) or any(
                marker in normalized for marker in ("/users/", "/home/", "/root/")
            ):
                raise ValueError(f"{label} retains a private machine path at {path}")
            for match in WINDOWS_ABSOLUTE_PATH_PATTERN.finditer(value):
                normalized_path = value[match.start() :].replace("\\", "/")
                if not normalized_path.startswith(PUBLIC_PATH_PREFIXES):
                    raise ValueError(f"{label} retains an unknown absolute path at {path}")
            for match in URI_PATTERN.finditer(value):
                parsed = urlsplit(match.group(0))
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                    raise ValueError(f"{label} retains a non-loopback URI at {path}")

    inspect(report, label)
    _exact_keys(report, PUBLIC_REPORT_KEYS, label)
    execution = _json_object(report.get("execution"), f"{label}.execution")
    _exact_keys(execution, PUBLIC_EXECUTION_KEYS, f"{label}.execution")


def _validate_public_projection_pair(
    private_report: dict[str, Any],
    public_report: dict[str, Any],
    *,
    private_bytes: bytes,
    label: str,
) -> None:
    """Require the downloaded public report to be a faithful private-report projection."""
    projection = _json_object(
        public_report.get("public_evidence_projection"),
        f"{label}.public_evidence_projection",
    )
    if projection.get("private_report_sha256") != _sha256_bytes(private_bytes):
        raise ValueError(f"{label} private report SHA-256 differs from its public projection")
    labels = projection.get("redacted_root_labels")
    if (
        not isinstance(labels, list)
        or labels != sorted(set(labels))
        or not labels
        or any(
            not isinstance(item, str) or item not in PUBLIC_REDACTED_ROOT_LABELS for item in labels
        )
    ):
        raise ValueError(f"{label} public projection labels are invalid")
    claimed_labels = set(labels)
    removed: set[str] = set()
    root_candidates: dict[str, set[str]] = {}

    def collect_roots(
        private: Any,
        public: Any,
        path: str,
        *,
        field_key: str | None = None,
    ) -> None:
        if isinstance(private, dict):
            if not isinstance(public, dict):
                raise ValueError(f"{label} projection changed type at {path}")
            expected_keys: set[str] = set()
            for item_key in private:
                if item_key.casefold() in PUBLIC_PRIVATE_FIELDS:
                    removed.add(item_key.casefold())
                else:
                    expected_keys.add(item_key)
            if path == label:
                expected_keys.add("public_evidence_projection")
            if set(public) != expected_keys:
                raise ValueError(f"{label} public projection changed fields at {path}")
            for item_key, private_item in private.items():
                if item_key.casefold() not in PUBLIC_PRIVATE_FIELDS:
                    collect_roots(
                        private_item,
                        public[item_key],
                        f"{path}.{item_key}",
                        field_key=item_key,
                    )
            return
        if isinstance(private, list):
            if not isinstance(public, list) or len(private) != len(public):
                raise ValueError(f"{label} public projection changed list shape at {path}")
            for index, (private_item, public_item) in enumerate(zip(private, public, strict=True)):
                collect_roots(
                    private_item,
                    public_item,
                    f"{path}[{index}]",
                    field_key=field_key,
                )
            return
        if not isinstance(private, str) or not isinstance(public, str):
            return
        matches = list(PUBLIC_ROOT_PLACEHOLDER_PATTERN.finditer(public.replace("\\", "/")))
        if not matches:
            return
        for match in matches:
            if match.group(1) not in claimed_labels:
                raise ValueError(f"{label} public projection used an undeclared root at {path}")
            following = public.replace("\\", "/")[match.end() : match.end() + 1]
            if following not in {"", "/"}:
                raise ValueError(f"{label} public projection root lacks a path boundary at {path}")
        if field_key is None or field_key.casefold() not in PUBLIC_ROOT_PATH_KEYS:
            return
        if len(matches) != 1 or matches[0].start() != 0:
            raise ValueError(f"{label} public root path is not canonical at {path}")
        public_normalized = public.replace("\\", "/")
        private_normalized = private.replace("\\", "/")
        match = matches[0]
        suffix = public_normalized[match.end() :]
        if suffix and not private_normalized.casefold().endswith(suffix.casefold()):
            raise ValueError(f"{label} public root path suffix changed at {path}")
        private_root = (
            private_normalized[: -len(suffix)] if suffix else private_normalized
        ).rstrip("/")
        if (
            not private_root
            or private_root.casefold().startswith("c:/topoforge public evidence/")
            or (
                WINDOWS_ABSOLUTE_PATH_PATTERN.match(private_root) is None
                and not private_root.startswith("//")
            )
        ):
            raise ValueError(f"{label} private root is not an absolute private path at {path}")
        root_candidates.setdefault(match.group(1), set()).add(private_root)

    collect_roots(private_report, public_report, label)
    exact_roots: dict[str, str] = {}
    for root_label, candidates in root_candidates.items():
        identities = {candidate.casefold() for candidate in candidates}
        if len(identities) != 1:
            raise ValueError(f"{label} private root {root_label} is not unique")
        exact_roots[root_label] = sorted(candidates, key=str.casefold)[0]
    normalized_roots = [value.casefold() for value in exact_roots.values()]
    if len(normalized_roots) != len(set(normalized_roots)):
        raise ValueError(f"{label} private projection roots are not unique")

    def replace_root(value: str, source: str, replacement: str, path: str) -> str:
        result = value
        variants = sorted(
            {source, source.replace("/", "\\")},
            key=len,
            reverse=True,
        )
        for variant in variants:
            start = 0
            while True:
                index = result.casefold().find(variant.casefold(), start)
                if index < 0:
                    break
                end = index + len(variant)
                before_ok = index == 0 or not result[index - 1].isalnum()
                after_ok = end == len(result) or result[end] in "/\\"
                if not before_ok or not after_ok:
                    raise ValueError(f"{label} private root lacks an exact path boundary at {path}")
                result = result[:index] + replacement + result[end:]
                start = index + len(replacement)
        return result

    def project(private: Any, path: str) -> Any:
        if isinstance(private, dict):
            projected_dict: dict[str, Any] = {}
            for key, item in private.items():
                if key.casefold() in PUBLIC_PRIVATE_FIELDS:
                    removed.add(key.casefold())
                    continue
                projected_dict[key] = project(item, f"{path}.{key}")
            return projected_dict
        if isinstance(private, list):
            return [project(item, f"{path}[{index}]") for index, item in enumerate(private)]
        if isinstance(private, str):
            projected_value = private
            for root_label, source in sorted(
                exact_roots.items(), key=lambda item: len(item[1]), reverse=True
            ):
                projected_value = replace_root(
                    projected_value,
                    source,
                    f"C:/TopoForge Public Evidence/{root_label}",
                    path,
                )
            return projected_value
        return private

    expected_public = project(private_report, label)
    if not isinstance(expected_public, dict):
        raise AssertionError("private clean evidence report changed root type")
    expected_public["public_evidence_projection"] = projection
    claimed_removed = projection.get("removed_fields")
    if claimed_removed != sorted(removed):
        raise ValueError(f"{label} public projection removed_fields is not faithful")
    expected_canonical = json.dumps(
        expected_public, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    public_canonical = json.dumps(
        public_report, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if expected_canonical != public_canonical:
        raise ValueError(f"{label} public report is not the canonical exact private projection")


def _validate_file_record(raw: Any, label: str) -> dict[str, Any]:
    record = _json_object(raw, label)
    _non_empty_string(record.get("path"), f"{label}.path")
    _sha256(record.get("sha256"), f"{label}.sha256")
    _positive_int(record.get("size_bytes"), f"{label}.size_bytes")
    return record


def _validate_path_contract(raw: Any, label: str) -> dict[str, Any]:
    contract = _json_object(raw, label)
    root = _non_empty_string(contract.get("root"), f"{label}.root")
    if (
        contract.get("contains_spaces") is not True
        or " " not in root
        or contract.get("contains_non_ascii") is not True
        or not any(ord(character) > 127 for character in root)
        or contract.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{label} did not prove the required spaces/non-ASCII path")
    return contract


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{label} must be a regular non-symlink file: {path}")
    payload = path.read_bytes()
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    return _json_object(decoded, label), payload


def _run_git(
    repository_root: Path,
    arguments: list[str],
    *,
    accepted_codes: set[int] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    accepted = {0} if accepted_codes is None else accepted_codes
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode not in accepted:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git command failed ({arguments}): {message}")
    return completed


def _git_commit(repository_root: Path, revision: str) -> str:
    completed = _run_git(repository_root, ["rev-parse", "--verify", f"{revision}^{{commit}}"])
    return _commit(completed.stdout.decode("ascii").strip(), f"Git revision {revision}")


def _tracked_file_bytes(
    repository_root: Path,
    relative_path: PurePosixPath,
    *,
    release_commit: str,
) -> bytes:
    local_path = repository_root.joinpath(*relative_path.parts)
    if local_path.is_symlink() or not local_path.is_file():
        raise FileNotFoundError(
            f"release evidence must be a tracked regular file: {relative_path.as_posix()}"
        )
    blob = _run_git(
        repository_root,
        ["show", f"{release_commit}:{relative_path.as_posix()}"],
    ).stdout
    local = local_path.read_bytes()
    if local != blob:
        raise ValueError(
            f"release evidence differs from the {release_commit} Git blob: "
            f"{relative_path.as_posix()}"
        )
    return local


def windows_evidence_required(version: str) -> bool:
    """Return whether this version belongs to the fail-closed Windows 0.11.x line."""
    return VERSION_PATTERN.match(version) is not None


def _expected_release_role(version: str) -> str:
    match = VERSION_PATTERN.match(version)
    if match is None:
        raise ValueError(f"Windows release evidence is not defined for version {version!r}")
    return "phase12a-core-web-portable" if int(match.group(1)) == 0 else "phase12b-bambu"


def _candidate_binding(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    portable = _json_object(manifest.get("portable_archive"), "portable_archive")
    _exact_keys(
        portable,
        {
            "filename",
            "sha256",
            "bytes",
            "config_sha256",
            "build_constraints_sha256",
            "verifier_sha256",
        },
        "portable_archive",
    )
    filename = _non_empty_string(portable.get("filename"), "portable_archive.filename")
    if (
        PurePosixPath(filename).name != filename
        or SAFE_ARTIFACT_COMPONENT.fullmatch(filename) is None
    ):
        raise ValueError("portable_archive.filename must be a safe filename")
    raw_verifiers = _json_object(
        portable.get("verifier_sha256"),
        "portable_archive.verifier_sha256",
    )
    _exact_keys(raw_verifiers, set(VERIFIER_PATHS), "portable_archive.verifier_sha256")
    verifier_sha256 = {
        role: _sha256(raw_verifiers.get(role), f"portable_archive.verifier_sha256.{role}")
        for role in VERIFIER_PATHS
    }
    return {
        "filename": filename,
        "sha256": _sha256(portable.get("sha256"), "portable_archive.sha256"),
        "bytes": _positive_int(portable.get("bytes"), "portable_archive.bytes"),
        "config_sha256": _sha256(
            portable.get("config_sha256"),
            "portable_archive.config_sha256",
        ),
        "build_constraints_sha256": _sha256(
            portable.get("build_constraints_sha256"),
            "portable_archive.build_constraints_sha256",
        ),
        "verifier_sha256": verifier_sha256,
    }


def _validate_nested_binding(
    raw: Any,
    *,
    expected: dict[str, Any],
    source_commit: str,
    target_id: str,
    role: str,
    label: str,
) -> None:
    binding = _json_object(raw, label)
    verifier_hashes = _json_object(
        expected.get("verifier_sha256"),
        "portable_archive.verifier_sha256",
    )
    expected_values = {
        "archive_sha256": expected["sha256"],
        "archive_bytes": expected["bytes"],
        "source_commit": source_commit,
        "source_tracked_dirty": False,
        "config_sha256": expected["config_sha256"],
        "build_constraints_sha256": expected["build_constraints_sha256"],
        "verifier_role": role,
        "verifier_sha256": verifier_hashes[role],
        "expected_target": TARGET_ARGUMENTS[target_id],
        "target_id": target_id,
        "required_checks_passed": True,
    }
    for key, value in expected_values.items():
        if binding.get(key) != value:
            raise ValueError(f"{label}.{key} does not match the release manifest")


def _validate_parent_binding(
    raw: Any,
    *,
    expected: dict[str, Any],
    source_commit: str,
    target_id: str,
    label: str,
) -> None:
    binding = _json_object(raw, label)
    expected_values = {
        "archive_sha256": expected["sha256"],
        "source_commit": source_commit,
        "source_tracked_dirty": False,
        "config_sha256": expected["config_sha256"],
        "build_constraints_sha256": expected["build_constraints_sha256"],
        "verifier_sha256": expected["verifier_sha256"],
        "expected_target": target_id,
        "required_checks_passed": True,
    }
    for key, value in expected_values.items():
        if binding.get(key) != value:
            raise ValueError(f"{label}.{key} does not match the release manifest")


def _source_file_bytes(
    repository_root: Path,
    relative_path: PurePosixPath,
    *,
    require_tracked: bool,
    release_commit: str | None,
) -> bytes:
    if require_tracked:
        if release_commit is None:
            raise ValueError("tracked source validation requires a release commit")
        return _tracked_file_bytes(
            repository_root,
            relative_path,
            release_commit=release_commit,
        )
    path = repository_root.joinpath(*relative_path.parts)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"release source file is missing: {relative_path.as_posix()}")
    return path.read_bytes()


def _evidence_json(
    repository_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_sha256: str,
    label: str,
    require_tracked: bool,
    release_commit: str | None,
) -> tuple[dict[str, Any], bytes]:
    if require_tracked:
        payload = _source_file_bytes(
            repository_root,
            relative_path,
            require_tracked=True,
            release_commit=release_commit,
        )
        try:
            value = _json_object(json.loads(payload), label)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    else:
        value, payload = _read_json(repository_root.joinpath(*relative_path.parts), label)
    if _sha256_bytes(payload) != expected_sha256:
        raise ValueError(f"{label} SHA-256 changed")
    return value, payload


def _validate_windows_target(
    raw: Any,
    *,
    target_id: str,
    label: str,
) -> dict[str, Any]:
    target = _json_object(raw, label)
    if target.get("target_id") != target_id:
        raise ValueError(f"{label}.target_id does not match the clean report target")
    if (
        target.get("native_windows_verified") is not True
        or target.get("target_verified") is not True
    ):
        raise ValueError(f"{target_id} OS/build target was not verified")
    if target.get("system") != "Windows":
        raise ValueError(f"{target_id} clean report is not from Windows")
    machine = target.get("machine")
    if not isinstance(machine, str) or machine.casefold() not in {"amd64", "x86_64"}:
        raise ValueError(f"{target_id} clean report is not from Windows x64")
    native_identity = {
        "process_machine_code": 0,
        "process_machine": "UNKNOWN",
        "native_machine_code": 0x8664,
        "native_machine": "AMD64",
        "native_x64_verified": True,
    }
    for key, expected in native_identity.items():
        if target.get(key) != expected:
            raise ValueError(f"{target_id} clean report did not prove native Windows x64 at {key}")
    installation_type = target.get("installation_type")
    if not isinstance(installation_type, str) or installation_type.casefold() != "client":
        raise ValueError(f"{target_id} clean report is not from a Windows client installation")
    product_name = target.get("product_name")
    display_version = target.get("display_version")
    build = target.get("current_build_number")
    ubr = target.get("ubr")
    if (
        not isinstance(product_name, str)
        or not isinstance(display_version, str)
        or not isinstance(build, int)
        or isinstance(build, bool)
        or not isinstance(ubr, int)
        or isinstance(ubr, bool)
        or ubr < 0
    ):
        raise ValueError(f"{target_id} Windows registry identity is incomplete")
    product = product_name.casefold()
    if target_id == "windows-10-22h2-x64":
        matches = (
            "windows 10" in product
            and "windows 11" not in product
            and "server" not in product
            and display_version.casefold() == "22h2"
            and build == 19045
        )
    else:
        # Windows 11 can retain a compatibility ProductName of "Windows 10 ...".
        matches = (
            ("windows 10" in product or "windows 11" in product)
            and "server" not in product
            and build >= 22000
        )
    if not matches:
        raise ValueError(f"{target_id} Windows product/version/build identity does not match")
    return target


def _validate_nested_report(
    raw: Any,
    *,
    expected: dict[str, Any],
    source_commit: str,
    target_id: str,
    role: str,
    parent_target: dict[str, Any],
) -> dict[str, Any]:
    label = f"{target_id}.execution.{role}"
    report = _json_object(raw, label)
    expected_schema = {
        "system": SYSTEM_REPORT_SCHEMA_VERSION,
        "bambu": BAMBU_REPORT_SCHEMA_VERSION,
    }.get(role)
    if expected_schema is None or report.get("schema_version") != expected_schema:
        raise ValueError(f"{label} schema is unsupported")
    _validate_path_contract(report.get("path_contract"), f"{label}.path_contract")
    if report.get("required_checks_passed") is not True:
        raise ValueError(f"{target_id} {role} acceptance did not pass")
    if report.get("expected_target") != target_id:
        raise ValueError(f"{label}.expected_target changed")
    nested_target = _validate_windows_target(
        report.get("windows_target"),
        target_id=target_id,
        label=f"{label}.windows_target",
    )
    identity_fields = {
        "target_id",
        "product_name",
        "display_version",
        "current_build_number",
        "ubr",
        "installation_type",
        "system",
        "machine",
        "process_machine_code",
        "process_machine",
        "native_machine_code",
        "native_machine",
        "native_x64_verified",
        "native_windows_verified",
        "target_verified",
    }
    changed = sorted(
        field for field in identity_fields if nested_target.get(field) != parent_target.get(field)
    )
    if changed:
        raise ValueError(f"{label}.windows_target differs from the parent at {changed}")
    _validate_nested_binding(
        report.get("candidate_binding"),
        expected=expected,
        source_commit=source_commit,
        target_id=target_id,
        role=role,
        label=f"{label}.candidate_binding",
    )
    return report


def _normalize_thumbprint(value: Any, label: str) -> str:
    raw = _non_empty_string(value, label)
    normalized = re.sub(r"[\s:-]", "", raw).upper()
    if THUMBPRINT_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{label} must be one SHA-1 or SHA-256 certificate thumbprint")
    return normalized


def _bambu_identity(raw: Any, *, required: bool) -> dict[str, Any] | None:
    if not required:
        if raw is not None:
            raise ValueError("phase12a bambu_studio_identity must be null")
        return None
    identity = _json_object(raw, "bambu_studio_identity")
    _exact_keys(
        identity,
        {
            "publisher_subject",
            "certificate_thumbprint",
            "executable_sha256",
            "version",
            "profile_content_identity_sha256",
            "resolved_profile_sha256",
            "source_records_sha256",
            "source_root_identity_sha256",
        },
        "bambu_studio_identity",
    )
    raw_profiles = _json_object(
        identity.get("resolved_profile_sha256"),
        "bambu_studio_identity.resolved_profile_sha256",
    )
    profile_roles = {"machine", "process", "filament"}
    _exact_keys(raw_profiles, profile_roles, "bambu_studio_identity.resolved_profile_sha256")
    return {
        "publisher_subject": _non_empty_string(
            identity.get("publisher_subject"), "bambu_studio_identity.publisher_subject"
        ),
        "certificate_thumbprint": _normalize_thumbprint(
            identity.get("certificate_thumbprint"),
            "bambu_studio_identity.certificate_thumbprint",
        ),
        "executable_sha256": _sha256(
            identity.get("executable_sha256"), "bambu_studio_identity.executable_sha256"
        ),
        "version": _non_empty_string(identity.get("version"), "bambu_studio_identity.version"),
        "profile_content_identity_sha256": _sha256(
            identity.get("profile_content_identity_sha256"),
            "bambu_studio_identity.profile_content_identity_sha256",
        ),
        "resolved_profile_sha256": {
            role: _sha256(
                raw_profiles.get(role),
                f"bambu_studio_identity.resolved_profile_sha256.{role}",
            )
            for role in sorted(profile_roles)
        },
        "source_records_sha256": _sha256(
            identity.get("source_records_sha256"),
            "bambu_studio_identity.source_records_sha256",
        ),
        "source_root_identity_sha256": _sha256(
            identity.get("source_root_identity_sha256"),
            "bambu_studio_identity.source_root_identity_sha256",
        ),
    }


def _validate_bambu_identity_policy(
    *,
    root: Path,
    expected_identity: dict[str, Any],
    source_commit: str,
    approval_commit: str,
    require_tracked: bool,
    release_commit: str | None,
) -> dict[str, Any]:
    policy_bytes = _source_file_bytes(
        root,
        BAMBU_IDENTITY_POLICY_PATH,
        require_tracked=require_tracked,
        release_commit=release_commit,
    )
    if require_tracked:
        resolved_approval = _git_commit(root, approval_commit)
        if resolved_approval != approval_commit or approval_commit == source_commit:
            raise ValueError("Bambu identity policy approval must be a prior full commit")
        ancestry = _run_git(
            root,
            ["merge-base", "--is-ancestor", approval_commit, source_commit],
            accepted_codes={0, 1},
        )
        if ancestry.returncode != 0:
            raise ValueError("Bambu identity policy approval is not an ancestor of source")
        source_policy = _run_git(
            root,
            ["show", f"{source_commit}:{BAMBU_IDENTITY_POLICY_PATH.as_posix()}"],
        ).stdout
        if source_policy != policy_bytes:
            raise ValueError(
                "Bambu identity policy differs from the portable candidate source commit"
            )
        approved_policy = _run_git(
            root,
            ["show", f"{approval_commit}:{BAMBU_IDENTITY_POLICY_PATH.as_posix()}"],
        ).stdout
        if approved_policy != policy_bytes:
            raise ValueError(
                "Bambu identity policy blob differs from its independent prior approval"
            )
    try:
        policy = _json_object(json.loads(policy_bytes), "Bambu identity policy")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Bambu identity policy is not valid UTF-8 JSON") from exc
    _exact_keys(
        policy,
        {
            "schema_version",
            "policy_status",
            "allowed_identities",
            "note",
            "required_checks_passed",
        },
        "Bambu identity policy",
    )
    allowed_raw = policy.get("allowed_identities")
    if (
        policy.get("schema_version") != BAMBU_IDENTITY_POLICY_SCHEMA_VERSION
        or policy.get("policy_status") != "frozen"
        or policy.get("required_checks_passed") is not True
        or not isinstance(allowed_raw, list)
        or not allowed_raw
        or not isinstance(policy.get("note"), str)
        or not policy["note"]
    ):
        raise ValueError(
            "Bambu identity policy is not independently frozen for release; keep Phase 12B "
            "publication blocked"
        )
    allowed: list[dict[str, Any]] = []
    for index, raw_identity in enumerate(allowed_raw):
        identity = _bambu_identity(raw_identity, required=True)
        if identity is None:
            raise AssertionError(f"Bambu policy identity {index} was unexpectedly null")
        if identity in allowed:
            raise ValueError("Bambu identity policy contains duplicate identities")
        allowed.append(identity)
    if expected_identity not in allowed:
        raise ValueError(
            "Bambu release identity is not allowed by the candidate source policy; do not "
            "describe the operator-selected binary as official"
        )
    return {
        "path": BAMBU_IDENTITY_POLICY_PATH.as_posix(),
        "sha256": _sha256_bytes(policy_bytes),
        "approval_commit": approval_commit,
        "policy_status": "frozen",
        "allowed_identity_count": len(allowed),
        "required_checks_passed": True,
    }


def _validate_bambu_report_identity(
    report: dict[str, Any],
    *,
    target_id: str,
    expected: dict[str, Any],
) -> None:
    studio = _json_object(report.get("bambu_studio"), f"{target_id}.bambu_studio")
    if studio.get("required_checks_passed") is not True:
        raise ValueError(f"{target_id} Bambu Studio gate did not pass")
    executable = _json_object(studio.get("executable"), f"{target_id}.bambu_studio.executable")
    if executable.get("sha256") != expected["executable_sha256"]:
        raise ValueError(f"{target_id} Bambu executable SHA-256 differs from the manifest")
    executable_size_bytes = _positive_int(
        executable.get("size_bytes"), f"{target_id}.bambu_studio.executable.size_bytes"
    )
    executable_path = _non_empty_string(
        executable.get("path"), f"{target_id}.bambu_studio.executable.path"
    )
    probe = _json_object(studio.get("probe"), f"{target_id}.bambu_studio.probe")
    if probe.get("version") != expected["version"]:
        raise ValueError(f"{target_id} Bambu Studio version differs from the manifest")
    signature = _json_object(studio.get("authenticode"), f"{target_id}.bambu_studio.authenticode")
    expected_subjects = signature.get("expected_publisher_subjects")
    expected_thumbprints = signature.get("expected_certificate_thumbprints")
    actual_thumbprint = _normalize_thumbprint(
        signature.get("certificate_thumbprint"),
        f"{target_id}.bambu_studio.authenticode.certificate_thumbprint",
    )
    checks = {
        "status": "Valid",
        "executable_sha256": expected["executable_sha256"],
        "publisher_subject": expected["publisher_subject"],
        "certificate_thumbprint": expected["certificate_thumbprint"],
        "publisher_subject_matched": True,
        "certificate_thumbprint_matched": True,
        "operator_identity_frozen": True,
        "required_checks_passed": True,
    }
    for key, value in checks.items():
        actual = actual_thumbprint if key == "certificate_thumbprint" else signature.get(key)
        if actual != value:
            raise ValueError(f"{target_id} Bambu signer identity changed at {key}")
    if expected_subjects != [expected["publisher_subject"]]:
        raise ValueError(f"{target_id} Bambu expected publisher is not the manifest singleton")
    normalized_expected = (
        [
            _normalize_thumbprint(item, f"{target_id}.expected thumbprint")
            for item in expected_thumbprints
        ]
        if isinstance(expected_thumbprints, list)
        else None
    )
    if normalized_expected != [expected["certificate_thumbprint"]]:
        raise ValueError(f"{target_id} Bambu expected thumbprint is not the manifest singleton")
    _non_empty_string(
        signature.get("status_message"),
        f"{target_id}.bambu_studio.authenticode.status_message",
    )
    for field in ("certificate_not_before", "certificate_not_after"):
        _non_empty_string(signature.get(field), f"{target_id}.bambu_studio.authenticode.{field}")

    binding_label = f"{target_id}.bambu_studio.profiles_root_binding"
    profile_binding = _json_object(studio.get("profiles_root_binding"), binding_label)
    _exact_keys(
        profile_binding,
        {
            "path",
            "selection_mode",
            "expected_executable_sibling_path",
            "relative_to_executable",
            "is_executable_sibling",
            "override_requested",
            "override_authorized_by_frozen_hashes",
            "profile_identity_frozen",
            "profile_manifest_sha256",
            "profile_content_identity_sha256",
            "expected_profile_content_identity_sha256",
            "profile_content_identity_sha256_matched",
            "resolved_profiles",
            "expected_resolved_profile_sha256",
            "source_records",
            "source_records_sha256",
            "source_root_identity_sha256",
            "required_checks_passed",
        },
        binding_label,
    )
    profile_root = _non_empty_string(profile_binding.get("path"), f"{binding_label}.path")
    expected_sibling = _non_empty_string(
        profile_binding.get("expected_executable_sibling_path"),
        f"{binding_label}.expected_executable_sibling_path",
    )
    derived_sibling = PureWindowsPath(executable_path).parent.joinpath(
        "resources", "profiles", "BBL"
    )
    if (
        studio.get("profiles_root") != profile_root
        or PureWindowsPath(profile_root) != derived_sibling
        or PureWindowsPath(expected_sibling) != derived_sibling
        or profile_binding.get("relative_to_executable") != "resources/profiles/BBL"
        or profile_binding.get("is_executable_sibling") is not True
    ):
        raise ValueError(f"{target_id} Bambu profiles root is not the signed executable sibling")
    selection_mode = profile_binding.get("selection_mode")
    if selection_mode not in {
        "executable-sibling-discovery",
        "explicit-cli-override",
        "environment-override",
    }:
        raise ValueError(f"{binding_label}.selection_mode is invalid")
    override_requested = selection_mode != "executable-sibling-discovery"
    expected_override_authorization = True if override_requested else None
    root_checks = {
        "override_requested": override_requested,
        "override_authorized_by_frozen_hashes": expected_override_authorization,
        "profile_identity_frozen": True,
        "profile_content_identity_sha256": expected["profile_content_identity_sha256"],
        "expected_profile_content_identity_sha256": expected["profile_content_identity_sha256"],
        "profile_content_identity_sha256_matched": True,
        "expected_resolved_profile_sha256": expected["resolved_profile_sha256"],
        "source_records_sha256": expected["source_records_sha256"],
        "source_root_identity_sha256": expected["source_root_identity_sha256"],
        "required_checks_passed": True,
    }
    for key, value in root_checks.items():
        if profile_binding.get(key) != value:
            raise ValueError(f"{binding_label}.{key} differs from the manifest identity")

    profile_manifest_sha256 = _sha256(
        profile_binding.get("profile_manifest_sha256"),
        f"{binding_label}.profile_manifest_sha256",
    )

    resolved = _json_object(
        profile_binding.get("resolved_profiles"), f"{binding_label}.resolved_profiles"
    )
    source_records = _json_object(
        profile_binding.get("source_records"), f"{binding_label}.source_records"
    )
    roles = {"machine", "process", "filament"}
    _exact_keys(resolved, roles, f"{binding_label}.resolved_profiles")
    _exact_keys(source_records, roles, f"{binding_label}.source_records")
    profile_content_profiles: dict[str, dict[str, Any]] = {}
    for role in sorted(roles):
        record_label = f"{binding_label}.resolved_profiles.{role}"
        record = _json_object(resolved.get(role), record_label)
        _exact_keys(
            record,
            {
                "path",
                "sha256",
                "size_bytes",
                "name",
                "expected_sha256",
                "sha256_matched",
                "source_count",
            },
            record_label,
        )
        role_sha = expected["resolved_profile_sha256"][role]
        if (
            record.get("sha256") != role_sha
            or record.get("expected_sha256") != role_sha
            or record.get("sha256_matched") is not True
        ):
            raise ValueError(f"{target_id} resolved {role} profile differs from the manifest")
        _non_empty_string(record.get("path"), f"{record_label}.path")
        profile_name = _non_empty_string(record.get("name"), f"{record_label}.name")
        profile_size_bytes = _positive_int(record.get("size_bytes"), f"{record_label}.size_bytes")
        source_count = _positive_int(record.get("source_count"), f"{record_label}.source_count")
        raw_sources = source_records.get(role)
        if not isinstance(raw_sources, list) or len(raw_sources) != source_count:
            raise ValueError(f"{binding_label}.source_records.{role} count changed")
        for index, raw_source in enumerate(raw_sources):
            source_label = f"{binding_label}.source_records.{role}[{index}]"
            source = _json_object(raw_source, source_label)
            _exact_keys(source, {"kind", "name", "path", "sha256", "size_bytes"}, source_label)
            if source.get("kind") not in roles:
                raise ValueError(f"{source_label}.kind is invalid")
            source_path = _safe_profile_relative_path(source.get("path"), f"{source_label}.path")
            if not source_path.parts or source_path.parts[0] != source.get("kind"):
                raise ValueError(f"{source_label}.path is outside its source kind")
            _non_empty_string(source.get("name"), f"{source_label}.name")
            _sha256(source.get("sha256"), f"{source_label}.sha256")
            _positive_int(source.get("size_bytes"), f"{source_label}.size_bytes")
        profile_content_profiles[role] = {
            "name": profile_name,
            "resolved_path": f"{role}.json",
            "resolved_sha256": role_sha,
            "resolved_size_bytes": profile_size_bytes,
            "sources": raw_sources,
        }
    profile_content_identity = {
        "schema_version": PROFILE_BUNDLE_SCHEMA_VERSION,
        "executable": {
            "sha256": expected["executable_sha256"],
            "size_bytes": executable_size_bytes,
            "version": expected["version"],
        },
        "profiles": profile_content_profiles,
    }
    if (
        _canonical_json_sha256(profile_content_identity)
        != expected["profile_content_identity_sha256"]
    ):
        raise ValueError(f"{target_id} Bambu profile content identity changed")
    if _canonical_json_sha256(source_records) != expected["source_records_sha256"]:
        raise ValueError(f"{target_id} Bambu source records canonical SHA-256 changed")
    source_root_identity = {
        "relative_to_executable": "resources/profiles/BBL",
        "is_executable_sibling": True,
        "profile_content_identity_sha256": expected["profile_content_identity_sha256"],
        "source_records_sha256": expected["source_records_sha256"],
    }
    if _canonical_json_sha256(source_root_identity) != expected["source_root_identity_sha256"]:
        raise ValueError(f"{target_id} Bambu source-root identity SHA-256 changed")

    bundle_label = f"{target_id}.profile_bundle"
    bundle = _json_object(report.get("profile_bundle"), bundle_label)
    _exact_keys(
        bundle,
        {
            "manifest",
            "profile_content_identity_sha256",
            "expected_profile_content_identity_sha256",
            "profile_content_identity_sha256_matched",
            "machine",
            "process",
            "filament",
            "source_records_sha256",
            "profile_identity_frozen",
            "required_checks_passed",
        },
        bundle_label,
    )
    manifest_record = _json_object(bundle.get("manifest"), f"{bundle_label}.manifest")
    _exact_keys(
        manifest_record,
        {"path", "sha256", "size_bytes"},
        f"{bundle_label}.manifest",
    )
    if manifest_record.get("sha256") != profile_manifest_sha256:
        raise ValueError(f"{target_id} profile bundle manifest identity changed")
    _non_empty_string(manifest_record.get("path"), f"{bundle_label}.manifest.path")
    _positive_int(manifest_record.get("size_bytes"), f"{bundle_label}.manifest.size_bytes")
    for role in sorted(roles):
        if bundle.get(role) != resolved[role]:
            raise ValueError(f"{target_id} profile bundle {role} projection changed")
    if (
        bundle.get("profile_content_identity_sha256") != expected["profile_content_identity_sha256"]
        or bundle.get("expected_profile_content_identity_sha256")
        != expected["profile_content_identity_sha256"]
        or bundle.get("profile_content_identity_sha256_matched") is not True
        or bundle.get("source_records_sha256") != expected["source_records_sha256"]
        or bundle.get("profile_identity_frozen") is not True
        or bundle.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{target_id} profile bundle frozen identity did not pass")


def _validate_windows_containment(
    system_report: dict[str, Any],
    *,
    target_id: str,
    source_commit: str,
    binding: dict[str, Any],
) -> None:
    label = f"{target_id}.execution.system.windows_process_containment"
    containment = _json_object(system_report.get("windows_process_containment"), label)
    expected_root = {
        "platform": "Windows",
        "executed": True,
        "containment_entrypoint": "topoforge.web.processes.enable_current_process_containment",
        "job_object_kill_on_close_verified": True,
        "production_cancellation_verified": True,
        "required_checks_passed": True,
    }
    for key, expected in expected_root.items():
        if containment.get(key) != expected:
            raise ValueError(f"{label}.{key} did not prove native Job Object containment")
    _sha256(containment.get("probe_code_sha256"), f"{label}.probe_code_sha256")
    nested_binding = _json_object(
        system_report.get("candidate_binding"),
        f"{target_id}.execution.system.candidate_binding",
    )
    binding_sha256 = _sha256(
        nested_binding.get("binding_sha256"),
        f"{target_id}.execution.system.candidate_binding.binding_sha256",
    )
    source_binding = _json_object(containment.get("source_binding"), f"{label}.source_binding")
    expected_source = {
        "candidate_bound": True,
        "candidate_binding_sha256": binding_sha256,
        "source_commit": source_commit,
        "system_verifier_sha256": binding["verifier_sha256"]["system"],
        "system_verifier_matches_candidate": True,
        "required_checks_passed": True,
    }
    for key, expected in expected_source.items():
        if source_binding.get(key) != expected:
            raise ValueError(f"{label}.source_binding.{key} changed")
    modes = (
        (
            "leader_exit",
            "leader-exit",
            {
                "leader_exit_code": 0,
                "leader_alive_after_exit": False,
                "child_alive_after_exit": False,
                "kill_on_job_close_verified": True,
            },
        ),
        (
            "cancellation",
            "cancel",
            {
                "leader_alive_after_cancel": False,
                "child_alive_after_cancel": False,
                "production_termination_adapter_exercised": True,
            },
        ),
    )
    for field, mode_name, expected_values in modes:
        mode = _json_object(containment.get(field), f"{label}.{field}")
        for integer_field in ("leader_pid", "leader_process_group_id", "child_pid"):
            value = mode.get(integer_field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{label}.{field}.{integer_field} is invalid")
        if (
            mode["leader_pid"] != mode["leader_process_group_id"]
            or mode["leader_pid"] == mode["child_pid"]
        ):
            raise ValueError(f"{label}.{field} process IDs are invalid")
        for identity_field in ("leader_process_identity", "child_process_identity"):
            identity = mode.get(identity_field)
            if not isinstance(identity, str) or not identity.startswith("windows:"):
                raise ValueError(f"{label}.{field}.{identity_field} is invalid")
        if (
            mode.get("mode") != mode_name
            or mode.get("containment_enabled") is not True
            or mode.get("required_checks_passed") is not True
        ):
            raise ValueError(f"{label}.{field} did not pass")
        for key, expected in expected_values.items():
            if mode.get(key) != expected:
                raise ValueError(f"{label}.{field}.{key} changed")
        exit_code = mode.get("leader_exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(f"{label}.{field}.leader_exit_code is invalid")


def _validate_three_mf_lifecycle(raw: Any, *, label: str) -> list[int | float]:
    report = _json_object(raw, label)
    if report.get("strict_warning_count") != 0:
        raise ValueError(f"{label} strict warnings changed")
    if report.get("unit") != "millimeter":
        raise ValueError(f"{label} unit changed")
    for field, expected in (("object_count", 1), ("build_item_count", 1)):
        observed = report.get(field)
        if not isinstance(observed, int) or isinstance(observed, bool) or observed != expected:
            raise ValueError(f"{label} {field} is invalid")
    for field in ("vertex_count", "triangle_count"):
        observed = report.get(field)
        if not isinstance(observed, int) or isinstance(observed, bool) or observed <= 0:
            raise ValueError(f"{label} {field} is invalid")
    if report.get("lib3mf_version") != [2, 5, 0]:
        raise ValueError(f"{label} lib3mf version changed")
    dimensions = report.get("dimensions_mm")
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 3
        or any(
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in dimensions
        )
    ):
        raise ValueError(f"{label} dimensions are invalid")
    return dimensions


def _validate_windows_core_runtime(raw: Any, *, target_id: str, version: str) -> dict[str, Any]:
    label = f"{target_id}.execution.core"
    core = _json_object(raw, label)
    platform_record = _json_object(core.get("platform"), f"{label}.platform")
    machine = platform_record.get("machine")
    python_version = platform_record.get("python")
    python_executable = _non_empty_string(
        platform_record.get("python_executable"), f"{label}.platform.python_executable"
    )
    if (
        platform_record.get("system") != "Windows"
        or not isinstance(machine, str)
        or machine.casefold() not in {"amd64", "x86_64"}
        or not isinstance(python_version, str)
        or re.match(r"^3[.]12(?:[.]|$)", python_version) is None
        or not PureWindowsPath(python_executable).is_absolute()
        or PureWindowsPath(python_executable).name.casefold() != "python.exe"
    ):
        raise ValueError(f"{label} is not native Windows x64 on Python 3.12")
    _validate_path_contract(core.get("path_contract"), f"{label}.path_contract")
    doctor = _json_object(core.get("doctor"), f"{label}.doctor")
    if doctor.get("topoforge") != version or doctor.get("python") != python_version:
        raise ValueError(f"{label}.doctor version contract changed")
    synthetic = _json_object(core.get("synthetic"), f"{label}.synthetic")
    _non_empty_string(synthetic.get("path"), f"{label}.synthetic.path")
    _sha256(synthetic.get("sha256"), f"{label}.synthetic.sha256")
    if synthetic.get("terrain") != "saddle":
        raise ValueError(f"{label}.synthetic terrain changed")
    web = _json_object(core.get("web"), f"{label}.web")
    assets = _json_object(web.get("assets"), f"{label}.web.assets")
    if (
        web.get("status") != "ok"
        or web.get("loopback_only") is not True
        or web.get("required_checks_passed") is not True
        or assets.get("languages") != ["zh-CN", "en"]
        or assets.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{label}.web installation contract changed")
    _positive_int(assets.get("asset_count"), f"{label}.web.assets.asset_count")
    return core


def _validate_real_http_identity(real_http: dict[str, Any], *, target_id: str) -> None:
    label = f"{target_id}.execution.system.real_http_web"
    shutdown = _json_object(real_http.get("shutdown"), f"{label}.shutdown")
    port = _positive_int(shutdown.get("port"), f"{label}.shutdown.port")
    base_url = _non_empty_string(real_http.get("base_url"), f"{label}.base_url")
    try:
        parsed = urlsplit(base_url)
        observed_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label}.base_url is invalid") from exc
    expected_base_url = f"http://127.0.0.1:{port}"
    if (
        base_url != expected_base_url
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or observed_port != port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label}.base_url is not the measured loopback endpoint")
    health = _json_object(real_http.get("health"), f"{label}.health")
    if health.get("status") != "ok":
        raise ValueError(f"{label}.health did not report ok")
    launcher = _json_object(real_http.get("launcher"), f"{label}.launcher")
    launcher_path = _non_empty_string(launcher.get("path"), f"{label}.launcher.path")
    _sha256(launcher.get("sha256"), f"{label}.launcher.sha256")
    if (
        launcher.get("kind") != "candidate-batch-launcher"
        or launcher.get("launcher_no_open") is not True
        or launcher.get("containment") != "kill-on-close-job-wrapper"
        or launcher.get("contained_process_tree") is not True
        or PureWindowsPath(launcher_path).name.casefold() != "topoforge-web.cmd"
    ):
        raise ValueError(f"{label}.launcher is not the candidate batch launcher")
    browser = _json_object(real_http.get("browser"), f"{label}.browser")
    if (
        browser.get("url") != f"{base_url}/"
        or browser.get("launcher_no_open_is_not_browser_evidence") is not True
    ):
        raise ValueError(f"{label}.browser URL/launcher semantics changed")
    dispatch = _json_object(browser.get("dispatch"), f"{label}.browser.dispatch")
    confirmed_load = _json_object(browser.get("confirmed_load"), f"{label}.browser.confirmed_load")
    callback_origin = _non_empty_string(
        confirmed_load.get("callback_origin"),
        f"{label}.browser.confirmed_load.callback_origin",
    )
    try:
        callback = urlsplit(callback_origin)
        callback_port = callback.port
    except ValueError as exc:
        raise ValueError(f"{label}.browser callback origin is invalid") from exc
    timeout_seconds = confirmed_load.get("callback_timeout_seconds")
    elapsed_seconds = confirmed_load.get("elapsed_seconds")
    if (
        dispatch.get("attempted") is not True
        or dispatch.get("accepted") is not True
        or dispatch.get("required_checks_passed") is not True
        or confirmed_load.get("required") is not True
        or confirmed_load.get("confirmed") is not True
        or confirmed_load.get("one_time_nonce") is not True
        or SHA256_PATTERN.fullmatch(str(confirmed_load.get("nonce_sha256"))) is None
        or confirmed_load.get("request_method") != "GET"
        or confirmed_load.get("request_path") != "/__topoforge_browser_loaded__"
        or confirmed_load.get("remote_address") != "127.0.0.1"
        or confirmed_load.get("redirect_target") != browser.get("url")
        or callback.scheme != "http"
        or callback.hostname != "127.0.0.1"
        or callback_port is None
        or callback.path
        or callback.query
        or callback.fragment
        or not isinstance(timeout_seconds, int | float)
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or not isinstance(elapsed_seconds, int | float)
        or isinstance(elapsed_seconds, bool)
        or not math.isfinite(elapsed_seconds)
        or elapsed_seconds < 0
        or elapsed_seconds > timeout_seconds
        or confirmed_load.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{label}.browser load was not confirmed by the one-time callback")


def _validate_bambu_workflow(
    report: dict[str, Any], *, target_id: str, expected_version: str
) -> None:
    prefix = f"{target_id}.execution.bambu"
    workflow = _json_object(report.get("workflow"), f"{prefix}.workflow")
    completed_stages = workflow.get("completed_stages")
    if (
        workflow.get("state") != "completed"
        or workflow.get("final_stage") != "project"
        or not isinstance(completed_stages, list)
        or not {"connect", "slice", "project"} <= set(completed_stages)
        or workflow.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{prefix}.workflow did not complete connect/slice/project")
    _non_empty_string(workflow.get("workflow_id"), f"{prefix}.workflow.workflow_id")
    for field in ("manifest", "status", "summary", "report", "source"):
        _validate_file_record(workflow.get(field), f"{prefix}.workflow.{field}")

    official_slice = _json_object(report.get("official_slice"), f"{prefix}.official_slice")
    slice_count = _positive_int(
        official_slice.get("tile_count"), f"{prefix}.official_slice.tile_count"
    )
    _validate_file_record(official_slice.get("manifest"), f"{prefix}.official_slice.manifest")
    if (
        official_slice.get("release_role") != "official-p2s-release"
        or official_slice.get("official_p2s_release_gate_passed") is not True
        or official_slice.get("all_parameter_checks_passed") is not True
        or official_slice.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{prefix}.official_slice release gate did not pass")

    project = _json_object(report.get("official_project"), f"{prefix}.official_project")
    project_count = _positive_int(
        project.get("tile_count"), f"{prefix}.official_project.tile_count"
    )
    _validate_file_record(project.get("manifest"), f"{prefix}.official_project.manifest")
    verification = _json_object(
        project.get("verification"), f"{prefix}.official_project.verification"
    )
    tiles = project.get("tiles")
    if (
        project_count != slice_count
        or not isinstance(tiles, list)
        or len(tiles) != project_count
        or verification.get("tile_count") != project_count
        or project.get("bambu_studio_version") != expected_version
        or project.get("all_projects_reopened") is not True
        or project.get("all_release_gates_passed") is not True
        or project.get("external_profiles_loaded_on_reopen") is not False
        or project.get("required_checks_passed") is not True
        or verification.get("status") != "verified"
        or verification.get("all_projects_reopened") is not True
        or verification.get("all_release_gates_passed") is not True
        or verification.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{prefix}.official_project verification contract did not pass")
    tile_ids: set[str] = set()
    for index, raw_tile in enumerate(tiles):
        label = f"{prefix}.official_project.tiles[{index}]"
        tile = _json_object(raw_tile, label)
        tile_id = _non_empty_string(tile.get("tile_id"), f"{label}.tile_id")
        if tile_id in tile_ids:
            raise ValueError(f"{prefix}.official_project tile IDs are not unique")
        tile_ids.add(tile_id)
        _validate_file_record(tile.get("validation"), f"{label}.validation")
        if (
            tile.get("external_profiles_loaded_on_reopen") is not False
            or tile.get("required_checks_passed") is not True
        ):
            raise ValueError(f"{label} validation contract did not pass")


def _validate_system_lifecycle(system: dict[str, Any], *, target_id: str) -> None:
    prefix = f"{target_id}.execution.system"
    real_http = _json_object(system.get("real_http_web"), f"{prefix}.real_http_web")
    job = _json_object(real_http.get("job"), f"{prefix}.real_http_web.job")
    download = _json_object(real_http.get("download"), f"{prefix}.real_http_web.download")
    shutdown = _json_object(real_http.get("shutdown"), f"{prefix}.real_http_web.shutdown")
    root = _json_object(real_http.get("root"), f"{prefix}.real_http_web.root")
    model_sha = _sha256(job.get("model_3mf_sha256"), f"{prefix}.real_http_web.job.model_3mf_sha256")
    download_dimensions = _validate_three_mf_lifecycle(
        download.get("three_mf"), label=f"{prefix}.real_http_web.download.three_mf"
    )
    expected_stages = job.get("expected_stages")
    ready_stages = job.get("ready_stages")
    if (
        real_http.get("required_checks_passed") is not True
        or root.get("status") != 200
        or root.get("packaged_application_served") is not True
        or job.get("state") != "completed"
        or job.get("required_checks_passed") is not True
        or not isinstance(expected_stages, list)
        or not expected_stages
        or ready_stages != expected_stages
        or download.get("sha256") != model_sha
        or download.get("required_checks_passed") is not True
        or shutdown.get("method") != "identity-bound-process-tree"
        or shutdown.get("port_closed") is not True
        or shutdown.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{prefix}.real_http_web synthetic job/download/shutdown did not pass")
    _positive_int(root.get("bytes"), f"{prefix}.real_http_web.root.bytes")
    _positive_int(download.get("bytes"), f"{prefix}.real_http_web.download.bytes")
    _positive_int(shutdown.get("port"), f"{prefix}.real_http_web.shutdown.port")

    completed = _json_object(system.get("completed_job"), f"{prefix}.completed_job")
    completed_expected = completed.get("expected_stages")
    completed_ready = completed.get("ready_stages")
    completed_sha = _sha256(
        completed.get("artifact_sha256"), f"{prefix}.completed_job.artifact_sha256"
    )
    completed_dimensions = _validate_three_mf_lifecycle(
        completed.get("three_mf"), label=f"{prefix}.completed_job.three_mf"
    )
    if (
        completed.get("exit_code") != 0
        or not isinstance(completed_expected, list)
        or not completed_expected
        or completed_ready != completed_expected
        or completed_dimensions != download_dimensions
        or completed.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{prefix}.completed_job did not pass")
    _positive_int(completed.get("event_count"), f"{prefix}.completed_job.event_count")

    restart = _json_object(system.get("restart_recovery"), f"{prefix}.restart_recovery")
    if any(
        restart.get(key) != expected
        for key, expected in {
            "state": "completed",
            "summary_reopened": True,
            "artifact_reopened": True,
            "required_checks_passed": True,
        }.items()
    ):
        raise ValueError(f"{prefix}.restart_recovery did not pass")

    backup = _json_object(system.get("backup_restore"), f"{prefix}.backup_restore")
    _sha256(backup.get("archive_sha256"), f"{prefix}.backup_restore.archive_sha256")
    _positive_int(backup.get("archive_size_bytes"), f"{prefix}.backup_restore.archive_size_bytes")
    _positive_int(backup.get("file_count"), f"{prefix}.backup_restore.file_count")
    restored_sha = _sha256(
        backup.get("restored_artifact_sha256"),
        f"{prefix}.backup_restore.restored_artifact_sha256",
    )
    restored_dimensions = _validate_three_mf_lifecycle(
        backup.get("restored_three_mf"), label=f"{prefix}.backup_restore.restored_three_mf"
    )
    if (
        backup.get("required_checks_passed") is not True
        or restored_sha != completed_sha
        or restored_dimensions != completed_dimensions
    ):
        raise ValueError(f"{prefix}.backup_restore did not preserve the completed artifact")

    process = _json_object(system.get("process_lifecycle"), f"{prefix}.process_lifecycle")
    _positive_int(process.get("pid"), f"{prefix}.process_lifecycle.pid")
    worker_options = _json_object(
        process.get("worker_options"), f"{prefix}.process_lifecycle.worker_options"
    )
    creation_flags = worker_options.get("creationflags")
    event_keys = process.get("event_keys")
    required_events = {"job.queued", "job.started", "job.cancelling", "job.cancelled"}
    if (
        not isinstance(creation_flags, int)
        or isinstance(creation_flags, bool)
        or creation_flags != WINDOWS_CREATE_NEW_PROCESS_GROUP
        or process.get("recovered_state") != "running"
        or process.get("cancelling_state") != "cancelling"
        or process.get("terminal_state") != "cancelled"
        or process.get("process_alive_after_cancel") is not False
        or not isinstance(event_keys, list)
        or not required_events <= set(event_keys)
        or process.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{prefix}.process_lifecycle did not prove production cancellation")


def _validate_clean_report(
    report: dict[str, Any],
    *,
    version: str,
    source_commit: str,
    target_id: str,
    binding: dict[str, Any],
    bambu_identity: dict[str, str] | None,
) -> dict[str, Any]:
    if report.get("schema_version") != PORTABLE_REPORT_SCHEMA_VERSION:
        raise ValueError(f"{target_id} clean report schema is unsupported")
    if report.get("required_checks_passed") is not True:
        raise ValueError(f"{target_id} clean report did not pass its required checks")
    if report.get("topoforge_version") != version:
        raise ValueError(f"{target_id} clean report version does not match {version}")
    archive = _json_object(report.get("archive"), f"{target_id}.archive")
    if archive.get("sha256") != binding["sha256"] or archive.get("bytes") != binding["bytes"]:
        raise ValueError(f"{target_id} clean report used a different portable archive")
    provenance = _json_object(report.get("provenance"), f"{target_id}.provenance")
    expected_provenance = {
        "source_commit": source_commit,
        "source_dirty": False,
        "source_tracked_dirty": False,
        "config_sha256": binding["config_sha256"],
        "build_constraints_sha256": binding["build_constraints_sha256"],
        "verifier_sha256": binding["verifier_sha256"],
    }
    for key, value in expected_provenance.items():
        if provenance.get(key) != value:
            raise ValueError(f"{target_id} clean report provenance field {key} changed")
    execution = _json_object(report.get("execution"), f"{target_id}.execution")
    extraction_path = _non_empty_string(
        execution.get("extraction_path"), f"{target_id}.execution.extraction_path"
    )
    cli_launcher = _json_object(
        execution.get("cli_launcher"), f"{target_id}.execution.cli_launcher"
    )
    web_check = _json_object(
        execution.get("web_launcher_installation_check"),
        f"{target_id}.execution.web_launcher_installation_check",
    )
    if (
        execution.get("evidence_scope") != "clean-client-target"
        or execution.get("archive_sha256") != binding["sha256"]
        or execution.get("archive_sha256_verified_before_after_and_at_completion") is not True
        or execution.get("path_contains_spaces") is not True
        or " " not in extraction_path
        or execution.get("path_contains_non_ascii") is not True
        or not any(ord(character) > 127 for character in extraction_path)
        or cli_launcher.get("topoforge") != version
        or not isinstance(cli_launcher.get("python"), str)
        or re.match(r"^3[.]12(?:[.]|$)", cli_launcher["python"]) is None
        or web_check.get("status") != "ok"
        or web_check.get("loopback_only") is not True
        or web_check.get("required_checks_passed") is not True
        or execution.get("claim_boundary") != PUBLIC_CLAIM_BOUNDARY
    ):
        raise ValueError(f"{target_id} clean portable execution contract changed")
    if execution.get("required_checks_passed") is not True:
        raise ValueError(f"{target_id} native execution did not pass")
    if execution.get("hosted_server") is not False:
        raise ValueError(f"{target_id} clean report is incorrectly marked as hosted Server")
    if execution.get("expected_target") != target_id:
        raise ValueError(f"{target_id} clean report expected_target changed")
    windows_target = _validate_windows_target(
        execution.get("windows_target"),
        target_id=target_id,
        label=f"{target_id}.execution.windows_target",
    )
    _validate_parent_binding(
        execution.get("candidate_binding"),
        expected=binding,
        source_commit=source_commit,
        target_id=target_id,
        label=f"{target_id}.execution.candidate_binding",
    )
    _validate_windows_core_runtime(execution.get("core"), target_id=target_id, version=version)
    system = _validate_nested_report(
        execution.get("system"),
        expected=binding,
        source_commit=source_commit,
        target_id=target_id,
        role="system",
        parent_target=windows_target,
    )
    _validate_windows_containment(
        system,
        target_id=target_id,
        source_commit=source_commit,
        binding=binding,
    )
    _validate_system_lifecycle(system, target_id=target_id)
    _validate_real_http_identity(
        _json_object(
            system.get("real_http_web"),
            f"{target_id}.execution.system.real_http_web",
        ),
        target_id=target_id,
    )
    real_http = _json_object(
        system.get("real_http_web"), f"{target_id}.execution.system.real_http_web"
    )
    browser = _json_object(
        real_http.get("browser"), f"{target_id}.execution.system.real_http_web.browser"
    )
    if (
        real_http.get("required_checks_passed") is not True
        or browser.get("mode") != "require"
        or browser.get("attempted") is not True
        or browser.get("opened") is not True
        or browser.get("required_checks_passed") is not True
    ):
        raise ValueError(f"{target_id} clean default-browser evidence did not pass")
    bambu = execution.get("bambu")
    if bambu_identity is not None:
        bambu_report = _validate_nested_report(
            bambu,
            expected=binding,
            source_commit=source_commit,
            target_id=target_id,
            role="bambu",
            parent_target=windows_target,
        )
        _validate_bambu_report_identity(
            bambu_report,
            target_id=target_id,
            expected=bambu_identity,
        )
        _validate_bambu_workflow(
            bambu_report,
            target_id=target_id,
            expected_version=bambu_identity["version"],
        )
        project = _json_object(
            bambu_report.get("official_project"), f"{target_id}.execution.bambu.official_project"
        )
        if (
            project.get("all_projects_reopened") is not True
            or project.get("all_release_gates_passed") is not True
            or project.get("external_profiles_loaded_on_reopen") is not False
            or project.get("required_checks_passed") is not True
        ):
            raise ValueError(f"{target_id} official Bambu reopen/reslice gate did not pass")
    elif bambu is not None:
        raise ValueError("phase12a clean reports must not claim optional Bambu acceptance")
    _validate_public_clean_evidence(report, label=target_id)
    return execution


def _validate_hosted_windows_record(raw: Any) -> None:
    target = _json_object(raw, "hosted.execution.windows_target")
    if target.get("system") != "Windows" or target.get("native_windows_verified") is not True:
        raise ValueError("hosted evidence is not from native Windows")
    if target.get("target_verified") is not False:
        raise ValueError("hosted evidence must not claim a clean Windows client target")
    product_name = target.get("product_name")
    installation_type = target.get("installation_type")
    if (
        not isinstance(product_name, str)
        or "server" not in product_name.casefold()
        or not isinstance(installation_type, str)
        or "server" not in installation_type.casefold()
    ):
        raise ValueError("hosted-server evidence is not from Windows Server")
    expected = {
        "process_machine_code": 0,
        "process_machine": "UNKNOWN",
        "native_machine_code": 0x8664,
        "native_machine": "AMD64",
        "native_x64_verified": True,
    }
    for key, value in expected.items():
        if target.get(key) != value:
            raise ValueError(f"hosted evidence did not prove native x64 at {key}")
    if "target_id" in target or "expected_target" in target:
        raise ValueError("hosted evidence must not identify a clean Windows target")


def _validate_hosted_report(
    report: dict[str, Any],
    *,
    version: str,
    source_commit: str,
    binding: dict[str, Any],
) -> None:
    if report.get("schema_version") != PORTABLE_REPORT_SCHEMA_VERSION:
        raise ValueError("hosted portable report schema is unsupported")
    if report.get("required_checks_passed") is not True:
        raise ValueError("hosted portable verification did not pass")
    if report.get("topoforge_version") != version:
        raise ValueError("hosted portable verification version changed")
    archive = _json_object(report.get("archive"), "hosted.archive")
    if archive.get("sha256") != binding["sha256"] or archive.get("bytes") != binding["bytes"]:
        raise ValueError("hosted portable verification archive identity changed")
    provenance = _json_object(report.get("provenance"), "hosted.provenance")
    expected_provenance = {
        "source_commit": source_commit,
        "source_dirty": False,
        "source_tracked_dirty": False,
        "config_sha256": binding["config_sha256"],
        "build_constraints_sha256": binding["build_constraints_sha256"],
        "verifier_sha256": binding["verifier_sha256"],
    }
    for key, value in expected_provenance.items():
        if provenance.get(key) != value:
            raise ValueError(f"hosted portable provenance field {key} changed")
    reproducibility = _json_object(report.get("reproducibility"), "hosted.reproducibility")
    if (
        reproducibility.get("byte_reproducible") is not True
        or reproducibility.get("primary_sha256") != binding["sha256"]
        or reproducibility.get("repeat_sha256") != binding["sha256"]
    ):
        raise ValueError("hosted portable verification did not prove byte reproducibility")
    execution = _json_object(report.get("execution"), "hosted.execution")
    if execution.get("required_checks_passed") is not True:
        raise ValueError("hosted portable execution did not pass")
    if execution.get("hosted_server") is not True or execution.get("expected_target") is not None:
        raise ValueError("hosted portable execution was not explicitly run in hosted-server mode")
    _validate_hosted_windows_record(execution.get("windows_target"))
    system = _json_object(execution.get("system"), "hosted.execution.system")
    if system.get("schema_version") != SYSTEM_REPORT_SCHEMA_VERSION:
        raise ValueError("hosted system report schema is unsupported")
    browser = _json_object(
        _json_object(system.get("real_http_web"), "hosted.execution.system.real_http_web").get(
            "browser"
        ),
        "hosted.execution.system.real_http_web.browser",
    )
    if browser.get("mode") != "skip":
        raise ValueError("hosted Server evidence must not claim default-browser support")

    dispatch = _json_object(browser.get("dispatch"), "hosted browser.dispatch")
    confirmed_load = _json_object(browser.get("confirmed_load"), "hosted browser.confirmed_load")
    if (
        dispatch.get("attempted") is not False
        or dispatch.get("accepted") is not None
        or dispatch.get("required_checks_passed") is not True
        or confirmed_load.get("required") is not False
        or confirmed_load.get("confirmed") is not None
        or confirmed_load.get("required_checks_passed") is not True
    ):
        raise ValueError("hosted Server browser skip evidence changed")


def _geometry_bool_signature(raw: Any, label: str) -> dict[str, bool]:
    geometry = _json_object(raw, label)
    fields = ("watertight", "winding_consistent", "manifold", "positive_volume")
    result: dict[str, bool] = {}
    for field in fields:
        if geometry.get(field) is not True:
            raise ValueError(f"{label}.{field} did not pass")
        result[field] = True
    return result


def _manufacturing_signature(
    raw: Any, *, label: str, require_linux: bool = False
) -> dict[str, Any]:
    core = _json_object(raw, label)
    if core.get("schema_version") != "topoforge-platform-core-verification-v1":
        raise ValueError(f"{label} schema is unsupported")
    if core.get("required_checks_passed") is not True:
        raise ValueError(f"{label} required checks did not pass")
    platform_record = _json_object(core.get("platform"), f"{label}.platform")
    if require_linux:
        machine = platform_record.get("machine")
        python_version = platform_record.get("python")
        if (
            platform_record.get("system") != "Linux"
            or not isinstance(machine, str)
            or machine.casefold() not in {"amd64", "x86_64"}
            or not isinstance(python_version, str)
            or re.match(r"^3[.]12(?:[.]|$)", python_version) is None
        ):
            raise ValueError("canonical Linux core report is not Linux x86_64 on Python 3.12")
    builds = _json_object(core.get("builds"), f"{label}.builds")
    build_fields = (
        "dimensions_mm",
        "volume_mm3",
        "triangle_count",
        "connected_components",
        "degenerate_faces",
        "duplicate_faces",
        "bottom_planarity_error_mm",
        "orientation",
    )
    build_signature = {field: builds.get(field) for field in build_fields}
    dimensions = build_signature["dimensions_mm"]
    if (
        not isinstance(dimensions, list)
        or len(dimensions) != 3
        or any(
            not isinstance(item, int | float)
            or isinstance(item, bool)
            or not math.isfinite(item)
            or item <= 0
            for item in dimensions
        )
    ):
        raise ValueError(f"{label}.builds.dimensions_mm is invalid")
    volume = build_signature["volume_mm3"]
    if (
        not isinstance(volume, int | float)
        or isinstance(volume, bool)
        or not math.isfinite(volume)
        or volume <= 0
    ):
        raise ValueError(f"{label}.builds.volume_mm3 is invalid")
    for field, expected in {
        "connected_components": 1,
        "degenerate_faces": 0,
        "duplicate_faces": 0,
    }.items():
        observed = build_signature[field]
        if not isinstance(observed, int) or isinstance(observed, bool) or observed != expected:
            raise ValueError(f"{label}.builds.{field} is {observed!r}, expected integer {expected}")
    triangle_count = build_signature["triangle_count"]
    if (
        not isinstance(triangle_count, int)
        or isinstance(triangle_count, bool)
        or triangle_count <= 0
    ):
        raise ValueError(f"{label}.builds.triangle_count is invalid")
    bottom_error = build_signature["bottom_planarity_error_mm"]
    if (
        not isinstance(bottom_error, int | float)
        or isinstance(bottom_error, bool)
        or not math.isfinite(bottom_error)
        or not 0 <= bottom_error <= 0.01
    ):
        raise ValueError(f"{label}.builds.bottom_planarity_error_mm is invalid")
    orientation = _json_object(build_signature["orientation"], f"{label}.builds.orientation")
    expected_orientation = {
        "east_axis": "+X = East",
        "north_axis": "+Y = North",
        "up_axis": "+Z = Up",
        "north_edge": "y=model_depth_mm",
    }
    if any(orientation.get(key) != value for key, value in expected_orientation.items()):
        raise ValueError(f"{label}.builds.orientation changed")
    artifacts = _json_object(core.get("artifacts"), f"{label}.artifacts")
    first = _json_object(artifacts.get("first_sha256"), f"{label}.artifacts.first_sha256")
    repeat = _json_object(artifacts.get("repeat_sha256"), f"{label}.artifacts.repeat_sha256")
    deterministic = _json_object(artifacts.get("deterministic"), f"{label}.artifacts.deterministic")
    if (
        set(first) != set(CORE_ROLES)
        or set(repeat) != set(CORE_ROLES)
        or set(deterministic) != set(CORE_ROLES)
    ):
        raise ValueError(f"{label} manufacturing artifact role set changed")
    for role in CORE_ROLES:
        if _sha256(first.get(role), f"{label}.artifacts.first_sha256.{role}") != repeat.get(role):
            raise ValueError(f"{label} {role} first/repeat SHA-256 changed")
        if deterministic.get(role) is not True:
            raise ValueError(f"{label} {role} determinism did not pass")
    strict = _json_object(core.get("strict_reopen"), f"{label}.strict_reopen")
    three_mf = _json_object(strict.get("three_mf"), f"{label}.strict_reopen.three_mf")
    if three_mf.get("strict_warning_count") != 0 or three_mf.get("dimensions_mm") != dimensions:
        raise ValueError(f"{label} strict 3MF reopen contract changed")
    return {
        "builds": {**build_signature, "orientation": expected_orientation},
        "artifact_roles": list(CORE_ROLES),
        "deterministic": {role: True for role in CORE_ROLES},
        "strict_reopen": {
            "three_mf": {"strict_warning_count": 0, "dimensions_mm": dimensions},
            "stl": _geometry_bool_signature(strict.get("stl"), f"{label}.strict_reopen.stl"),
            "glb": _geometry_bool_signature(strict.get("glb"), f"{label}.strict_reopen.glb"),
        },
    }


def _canonical_json_sha256(value: Any) -> str:
    return _sha256_bytes(
        (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
    )


def _cross_platform_evidence(
    raw: Any,
    *,
    root: Path,
    version: str,
    source_commit: str,
    binding: dict[str, Any],
    clean_reports: dict[str, dict[str, Any]],
    clean_report_hashes: dict[str, str],
    evidence_prefix: PurePosixPath,
    require_tracked: bool,
    release_commit: str | None,
    artifact_root: Path | None,
    metadata_only: bool,
) -> tuple[dict[str, Any], set[str]]:
    cross = _json_object(raw, "cross_platform")
    _exact_keys(
        cross,
        {
            "linux_report_path",
            "linux_report_sha256",
            "linux_source_commit",
            "linux_verifier_sha256",
            "linux_ci_artifact_id",
            "linux_ci_artifact_name",
            "linux_ci_artifact_digest",
            "linux_ci_relative_path",
            "comparison_report_path",
            "comparison_report_sha256",
        },
        "cross_platform",
    )
    linux_path = _safe_relative_path(
        cross.get("linux_report_path"), "cross_platform.linux_report_path"
    )
    comparison_path = _safe_relative_path(
        cross.get("comparison_report_path"), "cross_platform.comparison_report_path"
    )
    if linux_path.parent != evidence_prefix or comparison_path.parent != evidence_prefix:
        raise ValueError(
            "cross-platform reports must be directly below the release evidence directory"
        )
    linux_sha = _sha256(cross.get("linux_report_sha256"), "cross_platform.linux_report_sha256")
    comparison_sha = _sha256(
        cross.get("comparison_report_sha256"), "cross_platform.comparison_report_sha256"
    )
    if (
        _commit(cross.get("linux_source_commit"), "cross_platform.linux_source_commit")
        != source_commit
    ):
        raise ValueError("canonical Linux report source commit differs from the candidate")
    verifier_sha = _sha256(
        cross.get("linux_verifier_sha256"), "cross_platform.linux_verifier_sha256"
    )
    platform_verifier_bytes = _source_file_bytes(
        root,
        PLATFORM_CORE_VERIFIER_PATH,
        require_tracked=require_tracked,
        release_commit=release_commit,
    )
    if _sha256_bytes(platform_verifier_bytes) != verifier_sha:
        raise ValueError(
            "canonical Linux platform-core verifier SHA-256 differs from release source"
        )
    artifact_name = _non_empty_string(
        cross.get("linux_ci_artifact_name"), "cross_platform.linux_ci_artifact_name"
    )
    if artifact_name != CANONICAL_LINUX_CI_ARTIFACT_NAME:
        raise ValueError(
            "cross_platform.linux_ci_artifact_name does not name the canonical Python 3.12 artifact"
        )
    artifact_id = _positive_int(
        cross.get("linux_ci_artifact_id"), "cross_platform.linux_ci_artifact_id"
    )
    artifact_digest = _artifact_digest(
        cross.get("linux_ci_artifact_digest"),
        "cross_platform.linux_ci_artifact_digest",
    )
    linux_ci_relative = _safe_relative_path(
        cross.get("linux_ci_relative_path"), "cross_platform.linux_ci_relative_path"
    )
    if linux_ci_relative != CANONICAL_LINUX_CI_RELATIVE_PATH:
        raise ValueError(
            "cross_platform.linux_ci_relative_path does not name the canonical Python 3.12 report"
        )
    linux_report, linux_bytes = _evidence_json(
        root,
        linux_path,
        expected_sha256=linux_sha,
        label="canonical Linux core report",
        require_tracked=require_tracked,
        release_commit=release_commit,
    )
    if not metadata_only:
        if artifact_root is None:
            raise ValueError(
                "full cross-platform verification requires the downloaded Linux CI artifact"
            )
        downloaded = artifact_root.resolve().joinpath(*linux_ci_relative.parts)
        if not downloaded.resolve().is_relative_to(artifact_root.resolve()):
            raise ValueError("canonical Linux CI report escaped the artifact root")
        if downloaded.is_symlink() or not downloaded.is_file():
            raise FileNotFoundError(f"canonical Linux CI report is missing: {downloaded}")
        if downloaded.read_bytes() != linux_bytes:
            raise ValueError("downloaded canonical Linux CI report differs from the tracked report")
    signatures = {
        "linux-x86_64": _manufacturing_signature(
            linux_report, label="linux-x86_64", require_linux=True
        ),
        "windows-10-22h2-x64": _manufacturing_signature(
            _json_object(
                clean_reports["windows-10-22h2-x64"].get("execution"), "Win10 execution"
            ).get("core"),
            label="windows-10-22h2-x64",
        ),
        "windows-11-x64": _manufacturing_signature(
            _json_object(clean_reports["windows-11-x64"].get("execution"), "Win11 execution").get(
                "core"
            ),
            label="windows-11-x64",
        ),
    }
    baseline = signatures["linux-x86_64"]
    changed = [
        platform_id for platform_id, signature in signatures.items() if signature != baseline
    ]
    if changed:
        raise ValueError(f"Linux/Win10/Win11 manufacturing signatures differ: {changed}")
    signature_sha = _canonical_json_sha256(baseline)
    comparison, _ = _evidence_json(
        root,
        comparison_path,
        expected_sha256=comparison_sha,
        label="cross-platform comparison report",
        require_tracked=require_tracked,
        release_commit=release_commit,
    )
    _exact_keys(
        comparison,
        {
            "schema_version",
            "topoforge_version",
            "source_commit",
            "archive_sha256",
            "input_report_sha256",
            "platform_ids",
            "manufacturing_signature_sha256",
            "required_checks_passed",
        },
        "cross-platform comparison report",
    )
    expected_inputs = {
        "linux-x86_64": linux_sha,
        **clean_report_hashes,
    }
    expected_values = {
        "schema_version": CROSS_PLATFORM_SCHEMA_VERSION,
        "topoforge_version": version,
        "source_commit": source_commit,
        "archive_sha256": binding["sha256"],
        "input_report_sha256": expected_inputs,
        "platform_ids": ["linux-x86_64", "windows-10-22h2-x64", "windows-11-x64"],
        "manufacturing_signature_sha256": signature_sha,
        "required_checks_passed": True,
    }
    for key, expected in expected_values.items():
        if comparison.get(key) != expected:
            raise ValueError(f"cross-platform comparison report changed at {key}")
    return (
        {
            "linux_report_sha256": linux_sha,
            "linux_ci_artifact_id": artifact_id,
            "linux_ci_artifact_name": artifact_name,
            "linux_ci_artifact_digest": artifact_digest,
            "linux_ci_relative_path": linux_ci_relative.as_posix(),
            "comparison_report_sha256": comparison_sha,
            "manufacturing_signature_sha256": signature_sha,
            "required_checks_passed": True,
        },
        {linux_path.as_posix(), comparison_path.as_posix()},
    )


def _rollback_previous_version(version: str) -> str:
    match = re.fullmatch(r"0[.]11[.]([0-9]+)", version)
    if match is None:
        raise ValueError(f"rollback evidence does not support version {version}")
    patch = int(match.group(1))
    return "0.10.3" if patch == 0 else f"0.11.{patch - 1}"


def _validate_rollback_script(
    script_bytes: bytes,
    *,
    version: str,
    previous_version: str,
) -> None:
    if __package__:
        from scripts.verify_release_rollback import canonical_rollback_script
    else:
        from verify_release_rollback import canonical_rollback_script

    expected = canonical_rollback_script(version, previous_version)
    if script_bytes != expected:
        raise ValueError("rollback script differs from the exact generated canonical script")


def _rollback_evidence(
    raw: Any,
    *,
    root: Path,
    version: str,
    source_commit: str,
    require_tracked: bool,
    release_commit: str | None,
    artifact_root: Path | None,
    metadata_only: bool,
) -> tuple[dict[str, Any], set[str]]:
    rollback = _json_object(raw, "rollback")
    _exact_keys(
        rollback,
        {
            "script_path",
            "script_sha256",
            "producer_path",
            "producer_sha256",
            "runtime_report_relative_path",
            "current_wheel",
            "previous_release",
        },
        "rollback",
    )
    expected_script_path = PurePosixPath(f"scripts/rollback-topoforge-{version}.sh")
    script_path = _safe_relative_path(rollback.get("script_path"), "rollback.script_path")
    if script_path != expected_script_path:
        raise ValueError(f"rollback script must be {expected_script_path.as_posix()}")
    script_sha = _sha256(rollback.get("script_sha256"), "rollback.script_sha256")
    script_bytes = _source_file_bytes(
        root,
        script_path,
        require_tracked=require_tracked,
        release_commit=release_commit,
    )
    if _sha256_bytes(script_bytes) != script_sha:
        raise ValueError("rollback script SHA-256 differs from release source")
    previous_version = _rollback_previous_version(version)
    previous_tag = f"v{previous_version}"
    current_wheel = _json_object(rollback.get("current_wheel"), "rollback.current_wheel")
    _exact_keys(current_wheel, {"filename", "sha256"}, "rollback.current_wheel")
    expected_current_wheel_filename = f"topoforge-{version}-py3-none-any.whl"
    if current_wheel.get("filename") != expected_current_wheel_filename:
        raise ValueError(f"rollback current wheel must be {expected_current_wheel_filename}")
    current_wheel_sha256 = _sha256(current_wheel.get("sha256"), "rollback.current_wheel.sha256")
    previous_release = _json_object(rollback.get("previous_release"), "rollback.previous_release")
    _exact_keys(
        previous_release,
        {
            "release_tag",
            "release_id",
            "published_at",
            "wheel_filename",
            "wheel_asset_id",
            "wheel_sha256",
            "checksums_filename",
            "checksums_asset_id",
            "checksums_sha256",
        },
        "rollback.previous_release",
    )
    expected_previous_wheel_filename = f"topoforge-{previous_version}-py3-none-any.whl"
    if (
        previous_release.get("release_tag") != previous_tag
        or previous_release.get("wheel_filename") != expected_previous_wheel_filename
        or previous_release.get("checksums_filename") != "SHA256SUMS"
    ):
        raise ValueError("rollback previous release descriptor changed")
    previous_release_id = _positive_int(
        previous_release.get("release_id"), "rollback.previous_release.release_id"
    )
    previous_release_published_at = _github_published_at(
        previous_release.get("published_at"), "rollback.previous_release.published_at"
    )
    previous_wheel_asset_id = _positive_int(
        previous_release.get("wheel_asset_id"),
        "rollback.previous_release.wheel_asset_id",
    )
    previous_checksums_asset_id = _positive_int(
        previous_release.get("checksums_asset_id"),
        "rollback.previous_release.checksums_asset_id",
    )
    if previous_wheel_asset_id == previous_checksums_asset_id:
        raise ValueError("rollback previous release asset IDs must differ")
    previous_wheel_sha256 = _sha256(
        previous_release.get("wheel_sha256"), "rollback.previous_release.wheel_sha256"
    )
    previous_checksums_sha256 = _sha256(
        previous_release.get("checksums_sha256"),
        "rollback.previous_release.checksums_sha256",
    )
    if current_wheel_sha256 == previous_wheel_sha256:
        raise ValueError("rollback current and previous wheel hashes must differ")
    _validate_rollback_script(script_bytes, version=version, previous_version=previous_version)
    producer_path = _safe_relative_path(rollback.get("producer_path"), "rollback.producer_path")
    if producer_path != ROLLBACK_PRODUCER_PATH:
        raise ValueError(f"rollback producer must be {ROLLBACK_PRODUCER_PATH.as_posix()}")
    producer_sha = _sha256(rollback.get("producer_sha256"), "rollback.producer_sha256")
    producer_bytes = _source_file_bytes(
        root,
        producer_path,
        require_tracked=require_tracked,
        release_commit=release_commit,
    )
    if _sha256_bytes(producer_bytes) != producer_sha:
        raise ValueError("rollback producer SHA-256 differs from release source")
    runtime_relative = _safe_relative_path(
        rollback.get("runtime_report_relative_path"),
        "rollback.runtime_report_relative_path",
    )
    if runtime_relative != ROLLBACK_RUNTIME_RELATIVE_PATH:
        raise ValueError(
            f"rollback runtime report must be {ROLLBACK_RUNTIME_RELATIVE_PATH.as_posix()}"
        )
    base_summary = {
        "script_path": script_path.as_posix(),
        "script_sha256": script_sha,
        "producer_path": producer_path.as_posix(),
        "producer_sha256": producer_sha,
        "runtime_report_relative_path": runtime_relative.as_posix(),
        "previous_version": previous_version,
        "current_wheel": {
            "filename": expected_current_wheel_filename,
            "sha256": current_wheel_sha256,
        },
        "previous_release": {
            "release_tag": previous_tag,
            "release_id": previous_release_id,
            "published_at": previous_release_published_at,
            "wheel_filename": expected_previous_wheel_filename,
            "wheel_asset_id": previous_wheel_asset_id,
            "wheel_sha256": previous_wheel_sha256,
            "checksums_filename": "SHA256SUMS",
            "checksums_asset_id": previous_checksums_asset_id,
            "checksums_sha256": previous_checksums_sha256,
        },
    }
    if metadata_only:
        return ({**base_summary, "runtime_verified": False}, set())
    if artifact_root is None:
        raise ValueError("full rollback verification requires the downloaded artifact root")
    artifact_directory = artifact_root.resolve()
    report_path = artifact_directory.joinpath(*runtime_relative.parts)
    if not report_path.resolve().is_relative_to(artifact_directory):
        raise ValueError("rollback runtime report escaped the artifact root")
    report, report_bytes = _read_json(report_path, "rollback runtime verification report")
    _exact_keys(
        report,
        {
            "schema_version",
            "topoforge_version",
            "source_commit",
            "release_commit",
            "producer_sha256",
            "script_sha256",
            "previous_version",
            "release_artifacts",
            "installed_environment",
            "source_checkout",
            "retained_evidence",
            "required_checks_passed",
        },
        "rollback runtime verification report",
    )
    expected_values = {
        "schema_version": ROLLBACK_SCHEMA_VERSION,
        "topoforge_version": version,
        "source_commit": source_commit,
        "producer_sha256": producer_sha,
        "script_sha256": script_sha,
        "previous_version": previous_version,
        "required_checks_passed": True,
    }
    for key, expected in expected_values.items():
        if report.get(key) != expected:
            raise ValueError(f"rollback runtime verification report changed at {key}")
    runtime_release_commit = _commit(
        report.get("release_commit"), "rollback runtime verification report.release_commit"
    )
    if require_tracked and runtime_release_commit != release_commit:
        raise ValueError("rollback runtime release commit differs from the verified release tag")

    release_artifacts = _json_object(
        report.get("release_artifacts"),
        "rollback runtime verification report.release_artifacts",
    )
    _exact_keys(
        release_artifacts,
        {"current", "previous", "required_checks_passed"},
        "rollback runtime verification report.release_artifacts",
    )
    if release_artifacts.get("required_checks_passed") is not True:
        raise ValueError("rollback release artifacts did not pass")
    artifact_records: dict[str, dict[str, Any]] = {}
    for role, expected_version, expected_filename, expected_sha256, expected_role in (
        (
            "current",
            version,
            expected_current_wheel_filename,
            current_wheel_sha256,
            "formal-current-release-primary-wheel",
        ),
        (
            "previous",
            previous_version,
            expected_previous_wheel_filename,
            previous_wheel_sha256,
            "verified-previous-public-release-wheel",
        ),
    ):
        record = _json_object(release_artifacts.get(role), f"rollback release artifacts.{role}")
        expected_fields = {
            "role",
            "filename",
            "sha256",
            "bytes",
            "metadata_name",
            "metadata_version",
            "console_entry_point",
            "required_checks_passed",
        }
        if role == "previous":
            expected_fields.update(
                {"release_tag", "release_id", "published_at", "wheel_asset_id", "checksums"}
            )
        _exact_keys(record, expected_fields, f"rollback release artifacts.{role}")
        if (
            record.get("role") != expected_role
            or record.get("filename") != expected_filename
            or record.get("sha256") != expected_sha256
            or record.get("metadata_name") != "topoforge"
            or record.get("metadata_version") != expected_version
            or record.get("console_entry_point") != "topoforge = topoforge.cli.app:app"
            or record.get("required_checks_passed") is not True
        ):
            raise ValueError(f"rollback release artifacts.{role} changed")
        _positive_int(record.get("bytes"), f"rollback release artifacts.{role}.bytes")
        if role == "previous":
            if (
                record.get("release_tag") != previous_tag
                or record.get("release_id") != previous_release_id
                or record.get("published_at") != previous_release_published_at
                or record.get("wheel_asset_id") != previous_wheel_asset_id
            ):
                raise ValueError("rollback previous release artifact source identity changed")
            checksums = _json_object(record.get("checksums"), "rollback previous release checksums")
            _exact_keys(
                checksums,
                {"asset_id", "filename", "sha256", "wheel_entry", "required_checks_passed"},
                "rollback previous release checksums",
            )
            if (
                checksums.get("asset_id") != previous_checksums_asset_id
                or checksums.get("filename") != "SHA256SUMS"
                or checksums.get("sha256") != previous_checksums_sha256
                or checksums.get("wheel_entry")
                != f"{previous_wheel_sha256}  {expected_previous_wheel_filename}"
                or checksums.get("required_checks_passed") is not True
            ):
                raise ValueError("rollback previous release checksums changed")
        artifact_records[role] = record

    installed = _json_object(
        report.get("installed_environment"),
        "rollback runtime verification report.installed_environment",
    )
    _exact_keys(
        installed,
        {
            "strategy",
            "current",
            "previous",
            "activation",
            "required_checks_passed",
        },
        "rollback verification report.installed_environment",
    )
    if (
        installed.get("strategy") != "parallel-isolated-environments-atomic-pointer-switch"
        or installed.get("required_checks_passed") is not True
    ):
        raise ValueError("installed rollback evidence strategy changed")
    installation_records: dict[str, dict[str, Any]] = {}
    for role, expected_version in (("current", version), ("previous", previous_version)):
        record = _json_object(installed.get(role), f"installed rollback evidence.{role}")
        _exact_keys(
            record,
            {
                "version",
                "wheel_filename",
                "wheel_sha256",
                "launcher_relative_path",
                "launcher_sha256",
                "doctor_output_sha256",
                "doctor_exit_code",
                "dependency_install_mode",
                "uv_lock_sha256",
                "locked_requirements_sha256",
                "required_checks_passed",
            },
            f"installed rollback evidence.{role}",
        )
        if (
            record.get("version") != expected_version
            or record.get("wheel_filename") != artifact_records[role]["filename"]
            or record.get("wheel_sha256") != artifact_records[role]["sha256"]
            or record.get("launcher_relative_path") != "bin/topoforge"
            or record.get("doctor_exit_code") != 0
            or record.get("dependency_install_mode")
            != "uv-lock-hashed-dependencies-plus-project-wheel-no-deps"
            or record.get("required_checks_passed") is not True
        ):
            raise ValueError(f"installed rollback evidence.{role} did not pass")
        _sha256(record.get("wheel_sha256"), f"installed rollback evidence.{role}.wheel_sha256")
        for digest_field in (
            "launcher_sha256",
            "doctor_output_sha256",
            "uv_lock_sha256",
            "locked_requirements_sha256",
        ):
            _sha256(
                record.get(digest_field),
                f"installed rollback evidence.{role}.{digest_field}",
            )
        installation_records[role] = record
    if (
        installation_records["current"]["wheel_sha256"]
        == installation_records["previous"]["wheel_sha256"]
    ):
        raise ValueError("installed rollback evidence did not distinguish release artifacts")
    activation = _json_object(installed.get("activation"), "installed rollback activation")
    _exact_keys(
        activation,
        {
            "entrypoint",
            "before_target",
            "before_launcher_target",
            "before_launcher_sha256",
            "before_version",
            "before_output_sha256",
            "before_exit_code",
            "after_target",
            "after_launcher_target",
            "after_launcher_sha256",
            "after_version",
            "after_output_sha256",
            "after_exit_code",
            "atomic_pointer_switch",
            "required_checks_passed",
        },
        "installed rollback activation",
    )
    activation_expected = {
        "entrypoint": "active-installation/topoforge",
        "before_target": "current",
        "before_launcher_target": "current-environment/bin/topoforge",
        "before_version": version,
        "before_exit_code": 0,
        "after_target": "previous",
        "after_launcher_target": "previous-environment/bin/topoforge",
        "after_version": previous_version,
        "after_exit_code": 0,
        "atomic_pointer_switch": True,
        "required_checks_passed": True,
    }
    for key, expected in activation_expected.items():
        if activation.get(key) != expected:
            raise ValueError(f"installed rollback activation changed at {key}")
    for field in (
        "before_launcher_sha256",
        "before_output_sha256",
        "after_launcher_sha256",
        "after_output_sha256",
    ):
        _sha256(activation.get(field), f"installed rollback activation.{field}")
    if (
        activation["before_launcher_sha256"] != installation_records["current"]["launcher_sha256"]
        or activation["after_launcher_sha256"]
        != installation_records["previous"]["launcher_sha256"]
        or activation["before_output_sha256"]
        != installation_records["current"]["doctor_output_sha256"]
        or activation["after_output_sha256"]
        != installation_records["previous"]["doctor_output_sha256"]
        or activation["before_output_sha256"] == activation["after_output_sha256"]
    ):
        raise ValueError("installed rollback activation launcher/output hashes are not bound")

    source = _json_object(
        report.get("source_checkout"),
        "rollback runtime verification report.source_checkout",
    )
    _exact_keys(
        source,
        {
            "release_tag",
            "release_commit",
            "previous_tag",
            "previous_commit",
            "script_exit_code",
            "rollback_worktree_commit",
            "rollback_worktree_clean",
            "required_checks_passed",
        },
        "rollback runtime verification report.source_checkout",
    )
    previous_commit = _commit(
        source.get("previous_commit"),
        "rollback verification report.source_checkout.previous_commit",
    )
    source_expected = {
        "release_tag": f"v{version}",
        "release_commit": runtime_release_commit,
        "previous_tag": previous_tag,
        "previous_commit": previous_commit,
        "script_exit_code": 0,
        "rollback_worktree_commit": previous_commit,
        "rollback_worktree_clean": True,
        "required_checks_passed": True,
    }
    for key, expected in source_expected.items():
        if source.get(key) != expected:
            raise ValueError(f"source rollback evidence changed at {key}")
    if require_tracked and _git_commit(root, previous_tag) != previous_commit:
        raise ValueError("source rollback previous commit differs from the tracked previous tag")

    retained = _json_object(
        report.get("retained_evidence"),
        "rollback runtime verification report.retained_evidence",
    )
    _exact_keys(
        retained,
        {"before_rollback", "after_rollback", "required_checks_passed"},
        "rollback runtime verification report.retained_evidence",
    )
    inventories: dict[str, dict[str, Any]] = {}
    for stage in ("before_rollback", "after_rollback"):
        inventory = _json_object(retained.get(stage), f"retained_evidence.{stage}")
        _exact_keys(
            inventory,
            {"file_count", "total_bytes", "manifest_sha256"},
            f"retained_evidence.{stage}",
        )
        inventories[stage] = {
            "file_count": _positive_int(
                inventory.get("file_count"), f"retained_evidence.{stage}.file_count"
            ),
            "total_bytes": _positive_int(
                inventory.get("total_bytes"), f"retained_evidence.{stage}.total_bytes"
            ),
            "manifest_sha256": _sha256(
                inventory.get("manifest_sha256"),
                f"retained_evidence.{stage}.manifest_sha256",
            ),
        }
    if retained.get("required_checks_passed") is not True or (
        inventories["before_rollback"] != inventories["after_rollback"]
    ):
        raise ValueError("retained evidence inventory changed during rollback")
    return (
        {
            **base_summary,
            "runtime_report_sha256": _sha256_bytes(report_bytes),
            "runtime_verified": True,
            "release_commit": runtime_release_commit,
            "previous_commit": previous_commit,
            "retained_manifest_sha256": inventories["before_rollback"]["manifest_sha256"],
            "required_checks_passed": True,
        },
        set(),
    )


def _validate_source_transition(
    repository_root: Path,
    *,
    source_commit: str,
    release_commit: str,
    allowed_paths: set[str],
) -> None:
    ancestry = _run_git(
        repository_root,
        ["merge-base", "--is-ancestor", source_commit, release_commit],
        accepted_codes={0, 1},
    )
    if ancestry.returncode != 0:
        raise ValueError("portable source commit is not an ancestor of the release tag commit")
    changed = {
        item.decode("utf-8", errors="strict")
        for item in _run_git(
            repository_root,
            ["diff", "--name-only", "-z", source_commit, release_commit],
        ).stdout.split(b"\x00")
        if item
    }
    unexpected = sorted(changed - allowed_paths)
    if unexpected:
        raise ValueError(
            "release tag changed source outside tracked release evidence after the portable "
            f"candidate was built: {unexpected}"
        )


def verify_windows_release_evidence(
    *,
    version: str,
    release_tag: str,
    repository_root: Path,
    manifest_path: Path | None,
    artifact_root: Path | None,
    release_commit: str | None,
    require_tracked: bool,
    metadata_only: bool,
) -> dict[str, Any]:
    """Verify tracked clean-system reports and the exact Windows candidate archive."""
    if not windows_evidence_required(version):
        return {
            "schema_version": GATE_SCHEMA_VERSION,
            "topoforge_version": version,
            "release_tag": release_tag,
            "gate_required": False,
            "required_checks_passed": True,
        }
    expected_tag = f"v{version}"
    if release_tag != expected_tag:
        raise ValueError(f"release tag is {release_tag!r}, expected {expected_tag!r}")
    root = repository_root.resolve()
    if not metadata_only and artifact_root is None:
        raise ValueError("full 0.11.x release evidence verification requires --artifact-root")
    expected_manifest = PurePosixPath("release-evidence", version, "windows-release.json")
    if manifest_path is None:
        manifest_file = root.joinpath(*expected_manifest.parts)
    else:
        manifest_file = manifest_path.resolve()
        try:
            relative_manifest = manifest_file.relative_to(root)
        except ValueError as exc:
            raise ValueError("release evidence manifest must be inside the repository") from exc
        if PurePosixPath(relative_manifest.as_posix()) != expected_manifest:
            raise ValueError(f"0.11.x evidence manifest must be {expected_manifest.as_posix()}")
    resolved_release_commit: str | None = None
    if require_tracked:
        resolved_release_commit = _git_commit(root, release_commit or "HEAD")
        tag_commit = _git_commit(root, f"refs/tags/{release_tag}")
        if tag_commit != resolved_release_commit:
            raise ValueError("release tag does not resolve to the verified release commit")
        manifest_bytes = _tracked_file_bytes(
            root,
            expected_manifest,
            release_commit=resolved_release_commit,
        )
        if manifest_file.is_symlink() or manifest_file.read_bytes() != manifest_bytes:
            raise ValueError("release evidence manifest path differs from its tracked blob")
        manifest = _json_object(json.loads(manifest_bytes), "release evidence manifest")
    else:
        manifest, manifest_bytes = _read_json(manifest_file, "release evidence manifest")
    _exact_keys(
        manifest,
        {
            "schema_version",
            "topoforge_version",
            "release_tag",
            "release_role",
            "source_commit",
            "portable_archive",
            "candidate_artifact",
            "clean_system_reports",
            "bambu_studio_identity",
            "bambu_policy_approval_commit",
            "cross_platform",
            "rollback",
            "required_checks_passed",
        },
        "release evidence manifest",
    )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("release evidence manifest schema is unsupported")
    if manifest.get("topoforge_version") != version:
        raise ValueError("release evidence manifest version changed")
    if manifest.get("release_tag") != release_tag:
        raise ValueError("release evidence manifest tag changed")
    release_role = _expected_release_role(version)
    if manifest.get("release_role") != release_role:
        raise ValueError(f"release evidence role must be {release_role!r}")
    if manifest.get("required_checks_passed") is not True:
        raise ValueError("release evidence manifest did not pass")
    source_commit = _commit(manifest.get("source_commit"), "source_commit")
    binding = _candidate_binding(manifest)
    expected_filename = f"topoforge-{version}-windows-x64-portable.zip"
    if binding["filename"] != expected_filename:
        raise ValueError(f"portable archive filename must be {expected_filename}")
    source_bindings = {
        "config_sha256": CONFIG_PATH,
        "build_constraints_sha256": BUILD_CONSTRAINTS_PATH,
    }
    for field, relative_path in source_bindings.items():
        payload = _source_file_bytes(
            root,
            relative_path,
            require_tracked=require_tracked,
            release_commit=resolved_release_commit,
        )
        if _sha256_bytes(payload) != binding[field]:
            raise ValueError(f"release manifest {field} differs from the release source")
    manifest_verifiers = _json_object(
        binding.get("verifier_sha256"), "portable_archive.verifier_sha256"
    )
    for role, raw_path in VERIFIER_PATHS.items():
        relative_path = PurePosixPath(raw_path)
        payload = _source_file_bytes(
            root,
            relative_path,
            require_tracked=require_tracked,
            release_commit=resolved_release_commit,
        )
        if _sha256_bytes(payload) != manifest_verifiers[role]:
            raise ValueError(
                f"release manifest {role} verifier SHA-256 differs from the release source"
            )
    artifact = _json_object(manifest.get("candidate_artifact"), "candidate_artifact")
    _exact_keys(
        artifact,
        {
            "github_actions_run_id",
            "github_actions_run_attempt",
            "github_actions_workflow_id",
            "github_actions_workflow_path",
            "github_actions_event",
            "artifact_id",
            "artifact_name",
            "artifact_digest",
            "archive_relative_path",
            "verification_relative_path",
            "verification_sha256",
        },
        "candidate_artifact",
    )
    run_id = _positive_int(
        artifact.get("github_actions_run_id"), "candidate_artifact.github_actions_run_id"
    )
    run_attempt = _positive_int(
        artifact.get("github_actions_run_attempt"),
        "candidate_artifact.github_actions_run_attempt",
    )
    workflow_id = _positive_int(
        artifact.get("github_actions_workflow_id"),
        "candidate_artifact.github_actions_workflow_id",
    )
    candidate_workflow_path = _non_empty_string(
        artifact.get("github_actions_workflow_path"),
        "candidate_artifact.github_actions_workflow_path",
    )
    candidate_workflow_event = _non_empty_string(
        artifact.get("github_actions_event"), "candidate_artifact.github_actions_event"
    )
    if candidate_workflow_path != CI_WORKFLOW_PATH or candidate_workflow_event != "push":
        raise ValueError("candidate artifact must come from the canonical CI push workflow")
    artifact_id = _positive_int(artifact.get("artifact_id"), "candidate_artifact.artifact_id")
    candidate_artifact_name = _non_empty_string(
        artifact.get("artifact_name"), "candidate_artifact.artifact_name"
    )
    if candidate_artifact_name != WINDOWS_CANDIDATE_ARTIFACT_NAME:
        raise ValueError(
            "candidate_artifact.artifact_name does not name the canonical Windows candidate"
        )
    candidate_artifact_digest = _artifact_digest(
        artifact.get("artifact_digest"), "candidate_artifact.artifact_digest"
    )
    archive_relative = _safe_relative_path(
        artifact.get("archive_relative_path"), "candidate_artifact.archive_relative_path"
    )
    verification_relative = _safe_relative_path(
        artifact.get("verification_relative_path"),
        "candidate_artifact.verification_relative_path",
    )
    verification_sha256 = _sha256(
        artifact.get("verification_sha256"), "candidate_artifact.verification_sha256"
    )
    if archive_relative.name != binding["filename"]:
        raise ValueError("candidate artifact archive path does not end with the bound filename")
    if archive_relative == verification_relative:
        raise ValueError("candidate archive and verification report paths must differ")
    raw_reports = manifest.get("clean_system_reports")
    if not isinstance(raw_reports, list) or len(raw_reports) != 2:
        raise ValueError("clean_system_reports must contain exactly Win10 and Win11")
    report_entries: dict[str, dict[str, Any]] = {}
    evidence_prefix = PurePosixPath("release-evidence", version)
    for raw in raw_reports:
        entry = _json_object(raw, "clean_system_reports entry")
        _exact_keys(
            entry,
            {
                "target_id",
                "report_path",
                "report_sha256",
                "github_actions_run_id",
                "github_actions_run_attempt",
                "github_actions_workflow_id",
                "github_actions_workflow_path",
                "github_actions_event",
                "artifact_id",
                "artifact_name",
                "artifact_digest",
                "private_report_relative_path",
                "private_report_sha256",
                "public_report_relative_path",
                "public_report_sha256",
            },
            "clean report",
        )
        target_id = _non_empty_string(entry.get("target_id"), "clean report target_id")
        if target_id not in WINDOWS_TARGETS:
            raise ValueError(f"unsupported clean report target: {target_id}")
        if target_id in report_entries:
            raise ValueError(f"duplicate clean report target: {target_id}")
        report_path = _safe_relative_path(entry.get("report_path"), "clean report path")
        if report_path.parent != evidence_prefix:
            raise ValueError(f"clean report must be directly below {evidence_prefix.as_posix()}")
        report_sha = _sha256(entry.get("report_sha256"), "clean report SHA-256")
        artifact_name = _non_empty_string(entry.get("artifact_name"), "clean artifact name")
        if artifact_name != CLEAN_EVIDENCE_ARTIFACT_NAMES[target_id]:
            raise ValueError(f"{target_id} clean artifact name is not canonical")
        workflow_path = _non_empty_string(
            entry.get("github_actions_workflow_path"), "clean artifact workflow path"
        )
        workflow_event = _non_empty_string(
            entry.get("github_actions_event"), "clean artifact workflow event"
        )
        if workflow_path != CLEAN_EVIDENCE_WORKFLOW_PATH or workflow_event != "workflow_dispatch":
            raise ValueError(f"{target_id} clean artifact workflow identity is not canonical")
        private_relative = _safe_relative_path(
            entry.get("private_report_relative_path"), "clean private report relative path"
        )
        public_relative = _safe_relative_path(
            entry.get("public_report_relative_path"), "clean public report relative path"
        )
        expected_private = PurePosixPath(target_id, "private-report.json")
        expected_public = PurePosixPath(target_id, "public-report.json")
        if private_relative != expected_private or public_relative != expected_public:
            raise ValueError(f"{target_id} clean artifact report paths are not canonical")
        public_sha = _sha256(
            entry.get("public_report_sha256"), "clean artifact public report SHA-256"
        )
        if public_sha != report_sha:
            raise ValueError(f"{target_id} tracked and artifact public report hashes differ")
        report_entries[target_id] = {
            "report_path": report_path,
            "report_sha256": report_sha,
            "github_actions_run_id": _positive_int(
                entry.get("github_actions_run_id"), "clean artifact GitHub Actions run ID"
            ),
            "github_actions_run_attempt": _positive_int(
                entry.get("github_actions_run_attempt"),
                "clean artifact GitHub Actions run attempt",
            ),
            "github_actions_workflow_id": _positive_int(
                entry.get("github_actions_workflow_id"),
                "clean artifact GitHub Actions workflow ID",
            ),
            "github_actions_workflow_path": workflow_path,
            "github_actions_event": workflow_event,
            "artifact_id": _positive_int(entry.get("artifact_id"), "clean artifact ID"),
            "artifact_name": artifact_name,
            "artifact_digest": _artifact_digest(
                entry.get("artifact_digest"), "clean artifact digest"
            ),
            "private_report_relative_path": private_relative,
            "private_report_sha256": _sha256(
                entry.get("private_report_sha256"), "clean artifact private report SHA-256"
            ),
            "public_report_relative_path": public_relative,
            "public_report_sha256": public_sha,
        }
    if set(report_entries) != WINDOWS_TARGETS:
        raise ValueError("clean reports must cover Windows 10 22H2 x64 and Windows 11 x64")
    bambu_required = release_role == "phase12b-bambu"
    manifest_bambu_identity = _bambu_identity(
        manifest.get("bambu_studio_identity"), required=bambu_required
    )
    raw_approval_commit = manifest.get("bambu_policy_approval_commit")
    if bambu_required:
        bambu_policy_approval_commit = _commit(raw_approval_commit, "bambu_policy_approval_commit")
    else:
        if raw_approval_commit is not None:
            raise ValueError("phase12a must not claim a Bambu policy approval commit")
        bambu_policy_approval_commit = None
    if manifest_bambu_identity is not None:
        if bambu_policy_approval_commit is None:
            raise AssertionError("Phase 12B Bambu approval commit was not resolved")
        bambu_identity_policy = _validate_bambu_identity_policy(
            root=root,
            expected_identity=manifest_bambu_identity,
            source_commit=source_commit,
            approval_commit=bambu_policy_approval_commit,
            require_tracked=require_tracked,
            release_commit=resolved_release_commit,
        )
    else:
        bambu_identity_policy = None
    clean_reports: dict[str, dict[str, Any]] = {}
    clean_hashes: dict[str, str] = {}
    clean_artifact_summaries: dict[str, dict[str, Any]] = {}
    for target_id in sorted(WINDOWS_TARGETS):
        descriptor = report_entries[target_id]
        relative_report = descriptor["report_path"]
        expected_sha256 = descriptor["report_sha256"]
        report, tracked_public_bytes = _evidence_json(
            root,
            relative_report,
            expected_sha256=expected_sha256,
            label=f"{target_id} clean report",
            require_tracked=require_tracked,
            release_commit=resolved_release_commit,
        )
        clean_artifact_summaries[target_id] = {
            "github_actions_run_id": descriptor["github_actions_run_id"],
            "github_actions_run_attempt": descriptor["github_actions_run_attempt"],
            "github_actions_workflow_id": descriptor["github_actions_workflow_id"],
            "github_actions_workflow_path": descriptor["github_actions_workflow_path"],
            "github_actions_event": descriptor["github_actions_event"],
            "artifact_id": descriptor["artifact_id"],
            "artifact_name": descriptor["artifact_name"],
            "artifact_digest": descriptor["artifact_digest"],
            "private_report_relative_path": descriptor["private_report_relative_path"].as_posix(),
            "private_report_sha256": descriptor["private_report_sha256"],
            "public_report_relative_path": descriptor["public_report_relative_path"].as_posix(),
            "public_report_sha256": descriptor["public_report_sha256"],
            "private_public_projection_verified": False,
        }
        if not metadata_only:
            if artifact_root is None:
                raise ValueError("full clean evidence verification requires --artifact-root")
            artifact_directory = artifact_root.resolve()
            private_path = artifact_directory.joinpath(
                *descriptor["private_report_relative_path"].parts
            )
            public_path = artifact_directory.joinpath(
                *descriptor["public_report_relative_path"].parts
            )
            if not private_path.resolve().is_relative_to(artifact_directory) or not (
                public_path.resolve().is_relative_to(artifact_directory)
            ):
                raise ValueError(f"{target_id} clean artifact escaped the artifact root")
            private_report, private_bytes = _read_json(
                private_path, f"{target_id} private clean artifact report"
            )
            public_artifact, public_bytes = _read_json(
                public_path, f"{target_id} public clean artifact report"
            )
            if _sha256_bytes(private_bytes) != descriptor["private_report_sha256"]:
                raise ValueError(f"{target_id} private clean artifact SHA-256 changed")
            if (
                _sha256_bytes(public_bytes) != descriptor["public_report_sha256"]
                or public_bytes != tracked_public_bytes
                or public_artifact != report
            ):
                raise ValueError(
                    f"{target_id} artifact public report differs from the tracked public bytes"
                )
            _validate_public_projection_pair(
                private_report,
                public_artifact,
                private_bytes=private_bytes,
                label=target_id,
            )
            clean_artifact_summaries[target_id]["private_public_projection_verified"] = True
        _validate_clean_report(
            report,
            version=version,
            source_commit=source_commit,
            target_id=target_id,
            binding=binding,
            bambu_identity=manifest_bambu_identity,
        )
        clean_reports[target_id] = report
        clean_hashes[target_id] = expected_sha256
    cross_summary, cross_allowed = _cross_platform_evidence(
        manifest.get("cross_platform"),
        root=root,
        version=version,
        source_commit=source_commit,
        binding=binding,
        clean_reports=clean_reports,
        clean_report_hashes=clean_hashes,
        evidence_prefix=evidence_prefix,
        require_tracked=require_tracked,
        release_commit=resolved_release_commit,
        artifact_root=artifact_root,
        metadata_only=metadata_only,
    )
    rollback_summary, rollback_allowed = _rollback_evidence(
        manifest.get("rollback"),
        root=root,
        version=version,
        source_commit=source_commit,
        require_tracked=require_tracked,
        release_commit=resolved_release_commit,
        artifact_root=artifact_root,
        metadata_only=metadata_only,
    )
    allowed_release_paths = {
        expected_manifest.as_posix(),
        *(entry["report_path"].as_posix() for entry in report_entries.values()),
        *cross_allowed,
        *rollback_allowed,
    }
    if require_tracked:
        if resolved_release_commit is None:
            raise AssertionError("tracked release commit was not resolved")
        _validate_source_transition(
            root,
            source_commit=source_commit,
            release_commit=resolved_release_commit,
            allowed_paths=allowed_release_paths,
        )
    archive_report: dict[str, Any] | None = None
    hosted_report_summary: dict[str, Any] | None = None
    if not metadata_only:
        if artifact_root is None:
            raise ValueError("full 0.11.x release evidence verification requires --artifact-root")
        artifact_directory = artifact_root.resolve()
        archive_path = artifact_directory.joinpath(*archive_relative.parts)
        hosted_path = artifact_directory.joinpath(*verification_relative.parts)
        if not archive_path.resolve().is_relative_to(artifact_directory):
            raise ValueError("candidate archive escaped the downloaded artifact root")
        if not hosted_path.resolve().is_relative_to(artifact_directory):
            raise ValueError("hosted verification escaped the downloaded artifact root")
        if archive_path.is_symlink() or not archive_path.is_file():
            raise FileNotFoundError(f"candidate archive is missing: {archive_path}")
        if hosted_path.is_symlink() or not hosted_path.is_file():
            raise FileNotFoundError(f"hosted portable verification is missing: {hosted_path}")
        if archive_path.stat().st_size != binding["bytes"]:
            raise ValueError("downloaded candidate archive byte count changed")
        if _sha256_file(archive_path) != binding["sha256"]:
            raise ValueError("downloaded candidate archive SHA-256 changed")
        hosted_report, hosted_bytes = _read_json(hosted_path, "hosted portable verification")
        if _sha256_bytes(hosted_bytes) != verification_sha256:
            raise ValueError("hosted portable verification SHA-256 changed")
        _validate_hosted_report(
            hosted_report,
            version=version,
            source_commit=source_commit,
            binding=binding,
        )
        if __package__:
            from scripts.verify_windows_portable import inspect_windows_portable
        else:
            from verify_windows_portable import inspect_windows_portable
        config_path = root.joinpath(*CONFIG_PATH.parts)
        inspection = inspect_windows_portable(
            archive_path,
            config_path=config_path,
            expected_version=version,
        )
        provenance = _json_object(inspection.get("provenance"), "archive provenance")
        expected_archive_provenance = {
            "source_commit": source_commit,
            "source_dirty": False,
            "source_tracked_dirty": False,
            "config_sha256": binding["config_sha256"],
            "build_constraints_sha256": binding["build_constraints_sha256"],
            "verifier_sha256": binding["verifier_sha256"],
        }
        for key, value in expected_archive_provenance.items():
            if provenance.get(key) != value:
                raise ValueError(f"portable archive inspection provenance field {key} changed")
        inspection_source_commit = _commit(
            provenance.get("source_commit"), "portable archive inspection source_commit"
        )
        if inspection_source_commit != source_commit:
            raise ValueError("portable archive inspection source commit differs from the candidate")
        archive_report = {
            "path": archive_relative.as_posix(),
            "sha256": binding["sha256"],
            "bytes": binding["bytes"],
            "inspection_source_commit": inspection_source_commit,
            "strict_cross_host_inspection_passed": True,
        }
        hosted_report_summary = {
            "path": verification_relative.as_posix(),
            "sha256": verification_sha256,
            "byte_reproducible": True,
            "hosted_server": True,
            "required_checks_passed": True,
        }
    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "topoforge_version": version,
        "release_tag": release_tag,
        "release_role": release_role,
        "release_commit": resolved_release_commit,
        "source_commit": source_commit,
        "manifest": {
            "path": expected_manifest.as_posix(),
            "sha256": _sha256_bytes(manifest_bytes),
            "tracked": require_tracked,
        },
        "portable_archive": dict(binding),
        "candidate_artifact": {
            "github_actions_run_id": run_id,
            "github_actions_run_attempt": run_attempt,
            "github_actions_workflow_id": workflow_id,
            "github_actions_workflow_path": candidate_workflow_path,
            "github_actions_event": candidate_workflow_event,
            "artifact_id": artifact_id,
            "artifact_name": candidate_artifact_name,
            "artifact_digest": candidate_artifact_digest,
            "archive_relative_path": archive_relative.as_posix(),
            "verification_relative_path": verification_relative.as_posix(),
            "verification_sha256": verification_sha256,
        },
        "clean_targets": sorted(report_entries),
        "clean_artifacts": clean_artifact_summaries,
        "bambu_studio_identity": manifest_bambu_identity,
        "bambu_studio_identity_policy": bambu_identity_policy,
        "cross_platform": cross_summary,
        "rollback": rollback_summary,
        "archive_verification": archive_report,
        "hosted_verification": hosted_report_summary,
        "gate_required": True,
        "metadata_only": metadata_only,
        "required_checks_passed": True,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_github_output(path: Path, report: dict[str, Any]) -> None:
    artifact = report.get("candidate_artifact")
    lines = [f"required={'true' if report.get('gate_required') else 'false'}"]
    if isinstance(artifact, dict):
        values = {
            "run_id": artifact["github_actions_run_id"],
            "run_attempt": artifact["github_actions_run_attempt"],
            "workflow_id": artifact["github_actions_workflow_id"],
            "workflow_path": artifact["github_actions_workflow_path"],
            "workflow_event": artifact["github_actions_event"],
            "artifact_id": artifact["artifact_id"],
            "artifact_name": artifact["artifact_name"],
            "artifact_digest": artifact["artifact_digest"],
            "archive_relative_path": artifact["archive_relative_path"],
            "verification_relative_path": artifact["verification_relative_path"],
            "archive_filename": report["portable_archive"]["filename"],
            "archive_sha256": report["portable_archive"]["sha256"],
            "source_commit": report["source_commit"],
            "linux_artifact_name": report["cross_platform"]["linux_ci_artifact_name"],
            "linux_artifact_id": report["cross_platform"]["linux_ci_artifact_id"],
            "linux_artifact_digest": report["cross_platform"]["linux_ci_artifact_digest"],
            "linux_relative_path": report["cross_platform"]["linux_ci_relative_path"],
            "rollback_runtime_relative_path": report["rollback"]["runtime_report_relative_path"],
            "current_wheel_filename": report["rollback"]["current_wheel"]["filename"],
            "current_wheel_sha256": report["rollback"]["current_wheel"]["sha256"],
            "previous_release_tag": report["rollback"]["previous_release"]["release_tag"],
            "previous_release_id": report["rollback"]["previous_release"]["release_id"],
            "previous_release_published_at": report["rollback"]["previous_release"]["published_at"],
            "previous_wheel_filename": report["rollback"]["previous_release"]["wheel_filename"],
            "previous_wheel_asset_id": report["rollback"]["previous_release"]["wheel_asset_id"],
            "previous_wheel_sha256": report["rollback"]["previous_release"]["wheel_sha256"],
            "previous_checksums_filename": report["rollback"]["previous_release"][
                "checksums_filename"
            ],
            "previous_checksums_asset_id": report["rollback"]["previous_release"][
                "checksums_asset_id"
            ],
            "previous_checksums_sha256": report["rollback"]["previous_release"]["checksums_sha256"],
        }
        clean_artifacts = report["clean_artifacts"]
        for prefix, target_id in (
            ("win10", "windows-10-22h2-x64"),
            ("win11", "windows-11-x64"),
        ):
            descriptor = clean_artifacts[target_id]
            values.update(
                {
                    f"{prefix}_run_id": descriptor["github_actions_run_id"],
                    f"{prefix}_run_attempt": descriptor["github_actions_run_attempt"],
                    f"{prefix}_workflow_id": descriptor["github_actions_workflow_id"],
                    f"{prefix}_workflow_path": descriptor["github_actions_workflow_path"],
                    f"{prefix}_workflow_event": descriptor["github_actions_event"],
                    f"{prefix}_artifact_id": descriptor["artifact_id"],
                    f"{prefix}_artifact_name": descriptor["artifact_name"],
                    f"{prefix}_artifact_digest": descriptor["artifact_digest"],
                    f"{prefix}_private_relative_path": descriptor["private_report_relative_path"],
                    f"{prefix}_private_sha256": descriptor["private_report_sha256"],
                    f"{prefix}_public_relative_path": descriptor["public_report_relative_path"],
                    f"{prefix}_public_sha256": descriptor["public_report_sha256"],
                }
            )
        lines.extend(f"{key}={value}" for key, value in values.items())
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    """Run the release-evidence gate and retain success or failure diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-artifact", type=Path)
    parser.add_argument("--extract-destination", type=Path)
    parser.add_argument("--extract-member", action="append", default=[])
    parser.add_argument("--allow-extract-member", action="append", default=[])
    parser.add_argument("--version")
    parser.add_argument("--tag")
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--release-commit")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--no-require-tracked", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    extraction_requested = any(
        (
            args.extract_artifact is not None,
            args.extract_destination is not None,
            bool(args.extract_member),
            bool(args.allow_extract_member),
        )
    )
    if extraction_requested:
        if args.extract_artifact is None or args.extract_destination is None:
            parser.error("safe extraction requires --extract-artifact and --extract-destination")
        if args.version is not None or args.tag is not None or args.report is not None:
            parser.error("safe extraction cannot be combined with release-gate arguments")
        extraction = extract_exact_artifact(
            args.extract_artifact,
            args.extract_destination,
            required_members=args.extract_member,
            allowed_members=args.allow_extract_member,
        )
        print(json.dumps(extraction, indent=2, sort_keys=True))
        return 0
    if args.version is None or args.tag is None or args.report is None:
        parser.error("release evidence verification requires --version, --tag, and --report")
    report_path = args.report.resolve()
    try:
        report = verify_windows_release_evidence(
            version=args.version,
            release_tag=args.tag,
            repository_root=args.repository_root,
            manifest_path=args.manifest,
            artifact_root=args.artifact_root,
            release_commit=args.release_commit,
            require_tracked=not args.no_require_tracked,
            metadata_only=args.metadata_only,
        )
    except Exception as exc:
        failure = {
            "schema_version": GATE_SCHEMA_VERSION,
            "topoforge_version": args.version,
            "release_tag": args.tag,
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "required_checks_passed": False,
        }
        _write_report(report_path, failure)
        raise
    _write_report(report_path, report)
    if args.github_output is not None:
        _write_github_output(args.github_output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
