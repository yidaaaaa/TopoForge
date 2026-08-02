#!/usr/bin/env python3
"""Verify TopoForge source/wheel archives and an installed CLI smoke build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_member(name: str, prefix: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"archive member is not a safe relative path: {name}")
    if not name.startswith(prefix):
        raise ValueError(f"archive member is outside expected prefix {prefix}: {name}")
    return name[len(prefix) :]


def _single_archive(directory: Path, suffix: str) -> Path:
    matches = sorted(directory.glob(f"*{suffix}"))
    if len(matches) != 1:
        raise ValueError(f"expected one *{suffix} archive in {directory}, found {len(matches)}")
    return matches[0].resolve()


def inspect_sdist(path: Path, version: str) -> dict[str, Any]:
    """Verify the source archive has an intentional, bounded content set."""
    prefix = f"topoforge-{version}/"
    forbidden_roots = {
        ".agent",
        ".hypothesis",
        "artifacts",
        "build",
        "cache",
        "dist",
        "downloads",
        "outputs",
    }
    required_files = {
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "DATA_LICENSES.md",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "benchmarks/baseline.json",
        "pyproject.toml",
        "reference_regions/catalog.yaml",
        "scripts/rollback-topoforge-0.10.0.sh",
        "scripts/run_benchmarks.py",
        "scripts/verify_reference_regions.py",
        "scripts/verify_phase11_lifecycle.py",
        "scripts/verify_release.py",
        "src/topoforge/__init__.py",
        "src/topoforge/web/static/asset-manifest.json",
        "src/topoforge/web/static/index.html",
        "tests/release/test_phase8_contracts.py",
        "tests/web/test_api.py",
        "tests/web/test_jobs.py",
        "tests/web/test_map_tiles.py",
        "uv.lock",
        "web/package-lock.json",
        "web/package.json",
        "web/src/App.tsx",
        "web/src/api.ts",
        "web/src/types.ts",
        "web/src/components/ResultsPanel.tsx",
        "web/src/components/AssemblyPanel.test.tsx",
        "web/src/components/AssemblyPanel.tsx",
        "web/src/components/MapPanel.tsx",
        "web/src/components/MapPanel.test.ts",
        "web/src/components/TerrainPreview.test.ts",
        "web/src/components/TerrainPreview.tsx",
        "web/tests/workspace.spec.ts",
    }
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if any(member.issym() or member.islnk() for member in members):
            raise ValueError("sdist must not contain symbolic or hard links")
        relative = {
            _safe_relative_member(member.name, prefix)
            for member in members
            if member.name != prefix.rstrip("/")
        }
    files = {name for name in relative if name and not name.endswith("/")}
    missing = sorted(required_files - files)
    if missing:
        raise ValueError(f"sdist is missing required files: {missing}")
    forbidden = sorted(
        name
        for name in files
        if PurePosixPath(name).parts[0] in forbidden_roots
        or any(
            part in {"node_modules", "playwright-report", "test-results"}
            for part in PurePosixPath(name).parts
        )
        or "__pycache__" in PurePosixPath(name).parts
        or name.endswith((".pyc", ".pyo"))
    )
    if forbidden:
        raise ValueError(f"sdist contains forbidden generated/private files: {forbidden[:20]}")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "member_count": len(files),
        "forbidden_member_count": 0,
        "required_files_present": sorted(required_files),
    }


def inspect_wheel(path: Path, version: str) -> dict[str, Any]:
    """Verify wheel metadata, licenses, entry point, and package boundaries."""
    dist_info = f"topoforge-{version}.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    entry_points_name = f"{dist_info}/entry_points.txt"
    required_licenses = {
        f"{dist_info}/licenses/DATA_LICENSES.md",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/licenses/THIRD_PARTY_NOTICES.md",
    }
    required_web_files = {
        "topoforge/web/static/asset-manifest.json",
        "topoforge/web/static/index.html",
    }
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for name in names:
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"wheel member is not a safe relative path: {name}")
            if "__pycache__" in member.parts or name.endswith((".pyc", ".pyo")):
                raise ValueError(f"wheel contains generated Python cache: {name}")
        missing_licenses = sorted(required_licenses - names)
        if missing_licenses:
            raise ValueError(f"wheel is missing required license files: {missing_licenses}")
        missing_web = sorted(required_web_files - names)
        if missing_web:
            raise ValueError(f"wheel is missing required Web files: {missing_web}")
        web_manifest = json.loads(
            archive.read("topoforge/web/static/asset-manifest.json").decode("utf-8")
        )
        if web_manifest.get("schema_version") != "topoforge-web-assets-v1":
            raise ValueError("wheel Web asset manifest schema is invalid")
        web_assets = web_manifest.get("assets")
        web_hashes = web_manifest.get("sha256")
        web_sizes = web_manifest.get("sizes")
        if not isinstance(web_assets, list) or not web_assets:
            raise ValueError("wheel Web asset manifest has no assets")
        if not isinstance(web_hashes, dict) or not isinstance(web_sizes, dict):
            raise ValueError("wheel Web asset manifest has no checksum or size map")
        for raw in web_assets:
            if not isinstance(raw, str):
                raise ValueError("wheel Web asset manifest contains a non-string path")
            member = f"topoforge/web/static/{raw}"
            if member not in names:
                raise ValueError(f"wheel Web asset is missing: {raw}")
            payload = archive.read(member)
            if hashlib.sha256(payload).hexdigest() != web_hashes.get(raw):
                raise ValueError(f"wheel Web asset checksum changed: {raw}")
            if len(payload) != web_sizes.get(raw):
                raise ValueError(f"wheel Web asset byte count changed: {raw}")
        if web_manifest.get("languages") != ["zh-CN", "en"]:
            raise ValueError("wheel Web languages are incomplete")
        if web_manifest.get("frameworks") != ["React", "MapLibre", "Three.js"]:
            raise ValueError("wheel Web framework manifest is incomplete")
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        entry_points = archive.read(entry_points_name).decode("utf-8")
    expected_metadata = {
        "Name": "topoforge",
        "Version": version,
        "Requires-Python": "<3.13,>=3.12",
        "License-Expression": "Apache-2.0",
    }
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            raise ValueError(
                f"wheel metadata {key} is {metadata.get(key)!r}, expected {expected!r}"
            )
    if "topoforge = topoforge.cli.app:app" not in entry_points:
        raise ValueError("wheel console entry point is missing or incorrect")
    runtime_roots = {PurePosixPath(name).parts[0] for name in names if name}
    unexpected = sorted(root for root in runtime_roots if root not in {"topoforge", dist_info})
    if unexpected:
        raise ValueError(f"wheel contains unexpected top-level paths: {unexpected}")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "member_count": len(names),
        "metadata": expected_metadata,
        "license_files": sorted(required_licenses),
        "entry_point": "topoforge = topoforge.cli.app:app",
        "web": {
            "asset_count": len(web_assets),
            "languages": web_manifest["languages"],
            "frameworks": web_manifest["frameworks"],
            "required_checks_passed": True,
        },
    }


def _run_command(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    record = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        raise RuntimeError(json.dumps(record, indent=2))
    return record


def installed_smoke(
    wheel: Path,
    *,
    version: str,
    repository_root: Path,
    wheelhouse: Path | None,
) -> dict[str, Any]:
    """Install the wheel in a fresh venv and run outside the source checkout."""
    with tempfile.TemporaryDirectory(prefix="topoforge-release-") as raw_temp:
        root = Path(raw_temp).resolve()
        environment_dir = root / "venv"
        work_dir = root / "work"
        work_dir.mkdir()
        commands: list[dict[str, Any]] = []
        commands.append(
            _run_command(["uv", "venv", "--python", "3.12", str(environment_dir)], cwd=root)
        )
        python = environment_dir / "bin" / "python"
        cli = environment_dir / "bin" / "topoforge"
        install_command = ["uv", "pip", "install", "--python", str(python)]
        if wheelhouse is not None:
            install_command.extend(["--offline", "--find-links", str(wheelhouse.resolve())])
        install_command.append(str(wheel))
        commands.append(_run_command(install_command, cwd=root))
        smoke_env = os.environ.copy()
        smoke_env["PYTHONNOUSERSITE"] = "1"
        smoke_env["PYTHONPATH"] = ""
        import_record = _run_command(
            [
                str(python),
                "-c",
                (
                    "import json, pathlib, topoforge; "
                    "print(json.dumps({'version': topoforge.__version__, "
                    "'origin': str(pathlib.Path(topoforge.__file__).resolve())}))"
                ),
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(import_record)
        imported = json.loads(import_record["stdout"])
        origin = Path(imported["origin"]).resolve()
        if imported["version"] != version:
            raise ValueError(f"installed version is {imported['version']}, expected {version}")
        if origin.is_relative_to(repository_root.resolve()):
            raise ValueError(f"installed import leaked into repository checkout: {origin}")
        doctor = _run_command([str(cli), "doctor"], cwd=work_dir, env=smoke_env)
        commands.append(doctor)
        doctor_payload = json.loads(doctor["stdout"])
        if doctor_payload.get("topoforge") != version:
            raise ValueError("installed doctor command did not report the release version")
        web_check = _run_command(
            [
                str(cli),
                "web",
                "--check",
                "--state-dir",
                str(root / "web-state"),
                "--workspace-root",
                str(root / "web-workspaces"),
                "--input-root",
                str(work_dir),
                "--no-open",
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(web_check)
        web_payload = json.loads(web_check["stdout"])
        if web_payload.get("required_checks_passed") is not True:
            raise ValueError("installed Web application check did not pass")
        if web_payload.get("assets", {}).get("languages") != ["zh-CN", "en"]:
            raise ValueError("installed Web application languages are incomplete")
        raster = work_dir / "smoke.tif"
        synthetic = _run_command(
            [
                str(cli),
                "synthetic",
                "--output",
                str(raster),
                "--rows",
                "16",
                "--columns",
                "20",
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(synthetic)
        output = work_dir / "bundle"
        build = _run_command(
            [
                str(cli),
                "build",
                "--dem",
                str(raster),
                "--output",
                str(output),
                "--size-mm",
                "40",
                "0",
                "--base-mm",
                "2",
                "--max-height-mm",
                "20",
                "--sampling-mode",
                "source-preserving",
                "--resource-budget-mode",
                "strict",
                "--max-grid-cells",
                "10000",
                "--max-estimated-triangles",
                "50000",
            ],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(build)
        build_payload = json.loads(build["stdout"])
        if build_payload.get("required_checks_passed") is not True:
            raise ValueError("installed CLI smoke build did not pass required checks")
        inspection = _run_command(
            [str(cli), "inspect", str(output / "model.3mf")],
            cwd=work_dir,
            env=smoke_env,
        )
        commands.append(inspection)
        inspection_payload = json.loads(inspection["stdout"])
        if inspection_payload.get("strict_warning_count") != 0:
            raise ValueError("installed CLI strict 3MF inspection reported warnings")
        return {
            "isolated_environment": True,
            "repository_import_leakage": False,
            "installed_version": version,
            "installed_origin": str(origin),
            "doctor": doctor_payload,
            "web": web_payload,
            "build_required_checks_passed": True,
            "three_mf_warning_count": 0,
            "commands": commands,
        }


def verify_release(
    primary_dir: Path,
    *,
    repeat_dir: Path | None,
    version: str,
    install: bool,
    repository_root: Path,
    wheelhouse: Path | None,
) -> dict[str, Any]:
    """Verify release archives, reproducibility, and optional installation."""
    sdist = _single_archive(primary_dir, ".tar.gz")
    wheel = _single_archive(primary_dir, ".whl")
    report: dict[str, Any] = {
        "schema_version": 1,
        "topoforge_version": version,
        "sdist": inspect_sdist(sdist, version),
        "wheel": inspect_wheel(wheel, version),
        "reproducible_archives": None,
        "installed_smoke": None,
        "required_checks_passed": False,
    }
    if repeat_dir is not None:
        repeated_sdist = _single_archive(repeat_dir, ".tar.gz")
        repeated_wheel = _single_archive(repeat_dir, ".whl")
        comparisons = {
            "sdist": sha256_file(sdist) == sha256_file(repeated_sdist),
            "wheel": sha256_file(wheel) == sha256_file(repeated_wheel),
        }
        if not all(comparisons.values()):
            raise ValueError(f"release archives are not byte reproducible: {comparisons}")
        report["reproducible_archives"] = comparisons
    if install:
        report["installed_smoke"] = installed_smoke(
            wheel,
            version=version,
            repository_root=repository_root,
            wheelhouse=wheelhouse,
        )
    report["required_checks_passed"] = True
    return report


def main() -> int:
    """Run the command-line release verifier."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-dir", type=Path, required=True)
    parser.add_argument("--repeat-dir", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    report = verify_release(
        args.primary_dir.resolve(),
        repeat_dir=args.repeat_dir.resolve() if args.repeat_dir else None,
        version=args.version,
        install=args.install,
        repository_root=repository_root,
        wheelhouse=args.wheelhouse,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
