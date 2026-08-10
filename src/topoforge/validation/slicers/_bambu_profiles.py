"""Strict resolution and provenance for official Bambu Studio presets."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from topoforge.util import sha256_bytes, sha256_file

ProfileKind = Literal["machine", "process", "filament"]

DEFAULT_MACHINE_PROFILE = "Bambu Lab P2S 0.4 nozzle"
DEFAULT_PROCESS_PROFILE = "0.20mm Standard @BBL P2S"
DEFAULT_FILAMENT_PROFILE = "Bambu PLA Basic @BBL P2S"
PROFILE_BUNDLE_SCHEMA_VERSION = "topoforge-bambu-profile-bundle-v1"

_PROFILE_KINDS: tuple[ProfileKind, ...] = ("machine", "process", "filament")
_MAXIMUM_PROFILE_FILES = 10_000
_MAXIMUM_PROFILE_BYTES = 4 * 1024 * 1024
_MAXIMUM_DEPENDENCY_DEPTH = 128


@dataclass(frozen=True, slots=True)
class BambuPreset:
    """One indexed official preset or dependency fragment."""

    kind: ProfileKind
    name: str
    path: Path
    relative_path: str
    payload: dict[str, Any]
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ResolvedBambuPreset:
    """One flattened preset plus every source record used to build it."""

    kind: ProfileKind
    name: str
    payload: dict[str, Any]
    sources: tuple[BambuPreset, ...]


@dataclass(frozen=True, slots=True)
class PreparedBambuProfiles:
    """Verified content-addressed machine, process, and filament bundle."""

    bundle_dir: Path
    manifest_path: Path
    manifest_sha256: str
    machine_profile: Path
    process_profile: Path
    filament_profile: Path


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Bambu preset cannot be inspected: {path}") from exc
    if size > _MAXIMUM_PROFILE_BYTES:
        raise ValueError(f"Bambu preset exceeds the 4 MiB safety bound: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Bambu preset is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Bambu preset JSON root must be an object: {path}")
    return value


def load_profile_index(profiles_root: Path) -> dict[str, BambuPreset]:
    """Strictly index named presets below an official ``profiles/BBL`` tree."""
    root = profiles_root.expanduser().resolve()
    index: dict[str, BambuPreset] = {}
    seen_files = 0
    for kind in _PROFILE_KINDS:
        directory = root / kind
        if not directory.is_dir():
            raise ValueError(f"Bambu profiles root is missing its {kind!r} directory: {root}")
        for path in sorted(directory.rglob("*.json")):
            seen_files += 1
            if seen_files > _MAXIMUM_PROFILE_FILES:
                raise ValueError("Bambu profile tree exceeds the 10,000-file safety bound")
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Bambu profile tree contains an unsafe JSON entry: {path}")
            resolved_path = path.resolve()
            if root not in resolved_path.parents:
                raise ValueError(f"Bambu profile path escapes the official root: {path}")
            payload = _read_json_object(resolved_path)
            raw_name = payload.get("name")
            if raw_name is None:
                # Bambu ships a few JSON lookup tables beside named filament presets.
                continue
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"Bambu preset has an invalid name: {resolved_path}")
            name = raw_name.strip()
            relative_path = resolved_path.relative_to(root).as_posix()
            preset = BambuPreset(
                kind=kind,
                name=name,
                path=resolved_path,
                relative_path=relative_path,
                payload=payload,
                sha256=sha256_file(resolved_path),
                size_bytes=resolved_path.stat().st_size,
            )
            prior = index.get(name)
            if prior is not None and prior.path != preset.path:
                raise ValueError(
                    f"duplicate Bambu preset name {name!r}: {prior.path} and {preset.path}"
                )
            index[name] = preset
    if not index:
        raise ValueError(f"Bambu profiles root contains no named JSON presets: {root}")
    return index


def resolve_profile(
    name: str,
    index: dict[str, BambuPreset],
    *,
    expected_kind: ProfileKind | None = None,
) -> ResolvedBambuPreset:
    """Flatten parent and include dependencies in Bambu's documented order."""
    sources: dict[str, BambuPreset] = {}

    def visit(current: str, stack: tuple[str, ...]) -> dict[str, Any]:
        if current in stack:
            raise ValueError("Bambu preset dependency cycle: " + " -> ".join((*stack, current)))
        if len(stack) >= _MAXIMUM_DEPENDENCY_DEPTH:
            raise ValueError("Bambu preset dependency depth exceeds the 128-level safety bound")
        try:
            preset = index[current]
        except KeyError as exc:
            raise ValueError(f"Bambu preset dependency was not found: {current!r}") from exc
        sources.setdefault(current, preset)
        merged: dict[str, Any] = {}
        parent = preset.payload.get("inherits")
        if parent is not None and parent != "":
            if not isinstance(parent, str):
                raise ValueError(f"Bambu preset {current!r} has an invalid inherits value")
            merged.update(visit(parent, (*stack, current)))
        includes = preset.payload.get("include", [])
        if isinstance(includes, str):
            includes = [includes]
        if not isinstance(includes, list) or any(
            not isinstance(include, str) or not include for include in includes
        ):
            raise ValueError(f"Bambu preset {current!r} has an invalid include list")
        for include in includes:
            merged.update(visit(include, (*stack, current)))
        merged.update(preset.payload)
        return merged

    payload = visit(name, ())
    selected = index.get(name)
    if selected is None:
        raise ValueError(f"Bambu preset was not found: {name!r}")
    if expected_kind is not None and selected.kind != expected_kind:
        raise ValueError(f"Bambu preset {name!r} is {selected.kind!r}, expected {expected_kind!r}")
    return ResolvedBambuPreset(
        kind=selected.kind,
        name=name,
        payload=payload,
        sources=tuple(sources.values()),
    )


def flattened_profile_bytes(profile: ResolvedBambuPreset) -> bytes:
    """Return canonical dependency-free JSON accepted by Bambu Studio CLI."""
    payload = dict(profile.payload)
    payload.pop("inherits", None)
    payload.pop("include", None)
    return _canonical_bytes(payload)


def _source_records(profile: ResolvedBambuPreset) -> list[dict[str, Any]]:
    return [
        {
            "kind": source.kind,
            "name": source.name,
            "path": source.relative_path,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
        }
        for source in profile.sources
    ]


def _verify_existing_bundle(
    bundle_dir: Path,
    manifest: dict[str, Any],
    profile_payloads: dict[str, bytes],
) -> PreparedBambuProfiles:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.read_bytes() != _canonical_bytes(manifest):
        raise ValueError(f"cached Bambu profile manifest changed: {manifest_path}")
    profile_paths: dict[str, Path] = {}
    for kind, payload in profile_payloads.items():
        path = bundle_dir / f"{kind}.json"
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"cached Bambu resolved profile changed: {path}")
        profile_paths[kind] = path
    return PreparedBambuProfiles(
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        manifest_sha256=sha256_file(manifest_path),
        machine_profile=profile_paths["machine"],
        process_profile=profile_paths["process"],
        filament_profile=profile_paths["filament"],
    )


def prepare_bambu_profiles(
    profiles_root: Path,
    output_root: Path,
    *,
    machine: str = DEFAULT_MACHINE_PROFILE,
    process: str = DEFAULT_PROCESS_PROFILE,
    filament: str = DEFAULT_FILAMENT_PROFILE,
    executable: Path | None = None,
    executable_version: str | None = None,
) -> PreparedBambuProfiles:
    """Resolve and atomically cache an exact P2S profile bundle with provenance."""
    source_root = profiles_root.expanduser().resolve()
    index = load_profile_index(source_root)
    resolved = {
        "machine": resolve_profile(machine, index, expected_kind="machine"),
        "process": resolve_profile(process, index, expected_kind="process"),
        "filament": resolve_profile(filament, index, expected_kind="filament"),
    }
    profile_payloads = {
        kind: flattened_profile_bytes(profile) for kind, profile in resolved.items()
    }
    executable_record: dict[str, Any] | None = None
    if executable is not None:
        resolved_executable = executable.expanduser().resolve()
        if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
            raise ValueError(f"Bambu Studio executable is not a usable file: {resolved_executable}")
        executable_record = {
            "path": str(resolved_executable),
            "sha256": sha256_file(resolved_executable),
            "size_bytes": resolved_executable.stat().st_size,
            "version": executable_version,
        }

    profiles_manifest: dict[str, dict[str, Any]] = {}
    for kind, profile in resolved.items():
        payload = profile_payloads[kind]
        profiles_manifest[kind] = {
            "name": profile.name,
            "resolved_path": f"{kind}.json",
            "resolved_sha256": sha256_bytes(payload),
            "resolved_size_bytes": len(payload),
            "sources": _source_records(profile),
        }
    identity = {
        "schema_version": PROFILE_BUNDLE_SCHEMA_VERSION,
        "source_root": str(source_root),
        "executable": executable_record,
        "profiles": profiles_manifest,
    }
    bundle_id = sha256_bytes(_canonical_bytes(identity))
    manifest = {
        **identity,
        "bundle_id": bundle_id,
        "required_checks_passed": True,
    }

    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    bundle_dir = destination_root / bundle_id
    if bundle_dir.exists():
        if not bundle_dir.is_dir():
            raise ValueError(f"Bambu profile cache path is not a directory: {bundle_dir}")
        return _verify_existing_bundle(bundle_dir, manifest, profile_payloads)

    temporary = Path(tempfile.mkdtemp(prefix=".topoforge-bambu-profiles-", dir=destination_root))
    try:
        for kind, payload in profile_payloads.items():
            (temporary / f"{kind}.json").write_bytes(payload)
        (temporary / "manifest.json").write_bytes(_canonical_bytes(manifest))
        try:
            temporary.rename(bundle_dir)
        except OSError:
            if not bundle_dir.is_dir():
                raise
        return _verify_existing_bundle(bundle_dir, manifest, profile_payloads)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
