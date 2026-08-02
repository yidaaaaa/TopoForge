from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.run_benchmarks import terrain_triangle_count
from scripts.verify_reference_regions import verify_reference_catalog
from scripts.verify_release import inspect_sdist, inspect_wheel

VERSION = "0.8.1"


def _required_sdist_files() -> set[str]:
    return {
        ".github/workflows/ci.yml",
        "DATA_LICENSES.md",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "benchmarks/baseline.json",
        "pyproject.toml",
        "reference_regions/catalog.yaml",
        "scripts/rollback-topoforge-0.8.1.sh",
        "scripts/run_benchmarks.py",
        "scripts/verify_reference_regions.py",
        "scripts/verify_release.py",
        "src/topoforge/__init__.py",
        "src/topoforge/web/static/asset-manifest.json",
        "src/topoforge/web/static/index.html",
        "tests/release/test_phase8_contracts.py",
        "tests/web/test_api.py",
        "uv.lock",
        "web/package-lock.json",
        "web/package.json",
        "web/src/App.tsx",
        "web/src/components/MapPanel.tsx",
        "web/src/components/TerrainPreview.test.ts",
        "web/src/components/TerrainPreview.tsx",
        "web/tests/workspace.spec.ts",
    }


def _write_sdist(path: Path, *, forbidden: str | None = None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(_required_sdist_files() | ({forbidden} if forbidden else set())):
            payload = b"fixture\n"
            info = tarfile.TarInfo(f"topoforge-{VERSION}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _write_wheel(path: Path) -> None:
    dist_info = f"topoforge-{VERSION}.dist-info"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: topoforge\n"
        f"Version: {VERSION}\n"
        "Requires-Python: <3.13,>=3.12\n"
        "License-Expression: Apache-2.0\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("topoforge/__init__.py", "")
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\ntopoforge = topoforge.cli.app:app\n",
        )
        archive.writestr(f"{dist_info}/licenses/LICENSE", "Apache-2.0")
        archive.writestr(f"{dist_info}/licenses/DATA_LICENSES.md", "dataset terms")
        archive.writestr(f"{dist_info}/licenses/THIRD_PARTY_NOTICES.md", "notices")
        index = b"<!doctype html><title>TopoForge</title>\n"
        manifest = {
            "schema_version": "topoforge-web-assets-v1",
            "assets": ["index.html"],
            "languages": ["zh-CN", "en"],
            "frameworks": ["React", "MapLibre", "Three.js"],
            "sha256": {"index.html": hashlib.sha256(index).hexdigest()},
            "sizes": {"index.html": len(index)},
        }
        archive.writestr("topoforge/web/static/index.html", index)
        archive.writestr(
            "topoforge/web/static/asset-manifest.json",
            json.dumps(manifest, sort_keys=True) + "\n",
        )


def test_release_archive_contracts_reject_private_generated_content(tmp_path: Path) -> None:
    clean = tmp_path / f"topoforge-{VERSION}.tar.gz"
    _write_sdist(clean)
    assert inspect_sdist(clean, VERSION)["forbidden_member_count"] == 0

    forbidden = tmp_path / f"topoforge-{VERSION}-bad.tar.gz"
    _write_sdist(forbidden, forbidden=".agent/STATE.md")
    with pytest.raises(ValueError, match="forbidden"):
        inspect_sdist(forbidden, VERSION)


def test_wheel_metadata_and_license_contract(tmp_path: Path) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-py3-none-any.whl"
    _write_wheel(wheel)
    report = inspect_wheel(wheel, VERSION)
    assert report["metadata"]["License-Expression"] == "Apache-2.0"
    assert len(report["license_files"]) == 3
    assert report["web"]["languages"] == ["zh-CN", "en"]


def test_reference_catalog_normalizes_without_retained_data() -> None:
    root = Path(__file__).parents[2]
    report = verify_reference_catalog(
        root / "reference_regions" / "catalog.yaml",
        repository_root=root,
        definitions_only=True,
    )
    assert report["required_checks_passed"] is True
    assert report["network_attempts"] == 0
    assert report["region_count"] == 7
    assert report["retained_evidence_count"] == 0


@pytest.mark.parametrize(
    ("rows", "columns", "triangles"),
    [(64, 80, 20476), (128, 160, 81916), (256, 320, 327676)],
)
def test_benchmark_triangle_contract(rows: int, columns: int, triangles: int) -> None:
    assert terrain_triangle_count(rows, columns) == triangles
