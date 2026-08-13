from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import stat
import tarfile
import tomllib
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import scripts.verify_release as release_verifier
import yaml
from scripts.run_benchmarks import terrain_triangle_count
from scripts.verify_platform_core import _absolute_python_executable
from scripts.verify_reference_regions import verify_reference_catalog
from scripts.verify_release import (
    REQUIRED_SDIST_FILES,
    _venv_executable,
    inspect_sdist,
    inspect_wheel,
)

VERSION = "0.10.3"
RELEASE_ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "astral-sh/setup-uv": "d0d8abe699bfb85fec6de9f7adb5ae17292296ff",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}


def test_release_evidence_workflows_pin_every_action_to_a_full_sha() -> None:
    root = Path(__file__).parents[2]
    workflow_paths = (
        root / ".github" / "workflows" / "ci.yml",
        root / ".github" / "workflows" / "release.yml",
        root / ".github" / "workflows" / "windows-clean-release-evidence.yml",
    )
    for path in workflow_paths:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses is None:
                    continue
                action, separator, revision = uses.partition("@")
                assert separator == "@", uses
                assert action in RELEASE_ACTION_PINS, uses
                assert revision == RELEASE_ACTION_PINS[action], uses


def _required_sdist_files() -> set[str]:
    return set(REQUIRED_SDIST_FILES) | {
        ".github/workflows/macos.yml",
        "docs/macos-support-matrix.json",
        "docs/macos-support.md",
        f"scripts/rollback-topoforge-{VERSION}.sh",
        "scripts/collect_macos_ci_evidence.py",
        "scripts/verify_macos_support_matrix.py",
        "src/topoforge/platform_paths.py",
        "tests/release/test_phase13_macos_contracts.py",
        "tests/release/test_macos_ci_evidence.py",
        "tests/slicer/test_bambu_macos.py",
        "tests/unit/test_platform_paths.py",
    }


def _write_sdist(
    path: Path,
    *,
    forbidden: str | None = None,
    missing: str | None = None,
    extra_members: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
) -> None:
    names = _required_sdist_files() | ({forbidden} if forbidden else set())
    if missing is not None:
        names.remove(missing)
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(names):
            payload = b"fixture\n"
            info = tarfile.TarInfo(f"topoforge-{VERSION}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        for info, payload in extra_members or []:
            archive.addfile(info, None if payload is None else io.BytesIO(payload))


def _record_hash(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _write_wheel(
    path: Path,
    *,
    extra_members: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    entry_points: str = "[console_scripts]\ntopoforge = topoforge.cli.app:app\n",
    wheel_metadata: str = (
        "Wheel-Version: 1.0\n"
        "Generator: topoforge-test-fixture\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ),
    metadata: str | bytes | None = None,
    record_mutator: Callable[[list[list[str]]], None] | None = None,
) -> None:
    dist_info = f"topoforge-{VERSION}.dist-info"
    record_name = f"{dist_info}/RECORD"
    default_metadata = (
        "Metadata-Version: 2.4\n"
        "Name: topoforge\n"
        f"Version: {VERSION}\n"
        "Requires-Python: <3.15,>=3.11\n"
        "License-Expression: Apache-2.0\n"
    )
    metadata_payload = default_metadata if metadata is None else metadata
    index = b"<!doctype html><title>TopoForge</title>\n"
    manifest = {
        "schema_version": "topoforge-web-assets-v1",
        "assets": ["index.html"],
        "languages": ["zh-CN", "en"],
        "frameworks": ["React", "MapLibre", "Three.js"],
        "sha256": {"index.html": hashlib.sha256(index).hexdigest()},
        "sizes": {"index.html": len(index)},
    }
    manifest.update(manifest_overrides or {})
    members: list[tuple[str | zipfile.ZipInfo, bytes | str]] = [
        ("topoforge/__init__.py", ""),
        (f"{dist_info}/METADATA", metadata_payload),
        (f"{dist_info}/entry_points.txt", entry_points),
        (f"{dist_info}/WHEEL", wheel_metadata),
        (f"{dist_info}/licenses/LICENSE", "Apache-2.0"),
        (f"{dist_info}/licenses/DATA_LICENSES.md", "dataset terms"),
        (f"{dist_info}/licenses/THIRD_PARTY_NOTICES.md", "notices"),
        ("topoforge/web/static/index.html", index),
        (
            "topoforge/web/static/asset-manifest.json",
            json.dumps(manifest, sort_keys=True) + "\n",
        ),
        *(extra_members or []),
    ]
    rows: list[list[str]] = []
    for member, payload in members:
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        rows.append([name, _record_hash(payload_bytes), str(len(payload_bytes))])
    rows.append([record_name, "", ""])
    if record_mutator is not None:
        record_mutator(rows)
    record_stream = io.StringIO(newline="")
    csv.writer(record_stream, lineterminator="\n").writerows(rows)
    members.append((record_name, record_stream.getvalue()))

    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in members:
            archive.writestr(member, payload)


@pytest.mark.parametrize(
    "forbidden_member", [".agent/STATE.md", ".codex/settings.json", "AGENTS.md"]
)
def test_release_archive_contracts_reject_private_generated_content(
    tmp_path: Path, forbidden_member: str
) -> None:
    clean = tmp_path / f"topoforge-{VERSION}.tar.gz"
    _write_sdist(clean)
    assert inspect_sdist(clean, VERSION)["forbidden_member_count"] == 0

    forbidden = tmp_path / f"topoforge-{VERSION}-bad.tar.gz"
    _write_sdist(forbidden, forbidden=forbidden_member)
    with pytest.raises(ValueError, match="forbidden"):
        inspect_sdist(forbidden, VERSION)


def test_release_archive_rejects_missing_phase13_contract_file(tmp_path: Path) -> None:
    missing = "docs/macos-support-matrix.json"
    archive = tmp_path / f"topoforge-{VERSION}-missing-phase13.tar.gz"
    _write_sdist(archive, missing=missing)
    with pytest.raises(ValueError, match="missing required files"):
        inspect_sdist(archive, VERSION)


def test_wheel_metadata_and_license_contract(tmp_path: Path) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-py3-none-any.whl"
    _write_wheel(wheel)
    report = inspect_wheel(wheel, VERSION)
    assert report["metadata"]["License-Expression"] == "Apache-2.0"
    assert len(report["license_files"]) == 3
    assert report["web"]["languages"] == ["zh-CN", "en"]
    assert report["record_closed"] is True
    assert report["wheel_metadata"]["Tag"] == "py3-none-any"


@pytest.mark.parametrize(
    "metadata",
    [
        (
            "Metadata-Version: 2.4\nName: topoforge\nName: topoforge\n"
            f"Version: {VERSION}\nRequires-Python: <3.15,>=3.11\n"
            "License-Expression: Apache-2.0\n"
        ),
        (
            "Metadata-Version: 2.4\nName: topoforge\n"
            f"Version: {VERSION}\nVersion: 0.0.0\n"
            "Requires-Python: <3.15,>=3.11\nLicense-Expression: Apache-2.0\n"
        ),
        (
            "Metadata-Version: 2.4\nMetadata-Version: 2.4\nName: topoforge\n"
            f"Version: {VERSION}\nRequires-Python: <3.15,>=3.11\n"
            "License-Expression: Apache-2.0\n"
        ),
        (
            f"Name: topoforge\nVersion: {VERSION}\n"
            "Requires-Python: <3.15,>=3.11\nLicense-Expression: Apache-2.0\n"
        ),
        b"Metadata-Version: 2.4\nName: topo\xffforge\n",
        (
            "Metadata-Version: 2.4\nName: topoforge\nmalformed metadata line\n"
            f"Version: {VERSION}\nRequires-Python: <3.15,>=3.11\n"
            "License-Expression: Apache-2.0\n"
        ),
    ],
)
def test_wheel_rejects_ambiguous_or_malformed_core_metadata(
    tmp_path: Path,
    metadata: str | bytes,
) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-metadata-py3-none-any.whl"
    _write_wheel(wheel, metadata=metadata)

    with pytest.raises(ValueError, match="wheel METADATA"):
        inspect_wheel(wheel, VERSION)


@pytest.mark.parametrize(
    "entry_points",
    [
        "[console_scripts]\ntopoforge-malicious = topoforge.cli.app:app\n",
        ("[console_scripts]\ntopoforge = topoforge.cli.app:app\nother = topoforge.cli.app:app\n"),
        (
            "[console_scripts]\ntopoforge = topoforge.cli.app:app\n"
            "[other]\nvalue = topoforge.cli.app:app\n"
        ),
        (
            "[console_scripts]\n"
            "topoforge = topoforge.cli.app:app\n"
            "topoforge = topoforge.cli.app:app\n"
        ),
    ],
)
def test_wheel_rejects_non_exact_console_entry_points(
    tmp_path: Path,
    entry_points: str,
) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-entry-points-py3-none-any.whl"
    _write_wheel(wheel, entry_points=entry_points)

    with pytest.raises(ValueError, match="console entry point"):
        inspect_wheel(wheel, VERSION)


@pytest.mark.parametrize(
    "wheel_metadata",
    [
        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        (
            "Wheel-Version: 1.0\nGenerator: fixture\n"
            "Root-Is-Purelib: true\nTag: cp312-cp312-win_amd64\n"
        ),
        (
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\nTag: py3-none-any\n"
        ),
        (
            " orphan continuation\nWheel-Version: 1.0\nGenerator: fixture\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        (
            "Wheel-Version: 1.0\nGenerator: fixture\n folded value\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    ],
)
def test_wheel_rejects_non_exact_wheel_metadata(
    tmp_path: Path,
    wheel_metadata: str,
) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-wheel-metadata-py3-none-any.whl"
    _write_wheel(wheel, wheel_metadata=wheel_metadata)

    with pytest.raises(ValueError, match="wheel WHEEL"):
        inspect_wheel(wheel, VERSION)


@pytest.mark.parametrize(
    "mutation",
    ["unrecorded-module", "wrong-hash", "wrong-size", "duplicate", "self-hash", "unsafe-path"],
)
def test_wheel_rejects_incomplete_or_malformed_record(
    tmp_path: Path,
    mutation: str,
) -> None:
    backdoor = "topoforge/backdoor.py"

    def mutate(rows: list[list[str]]) -> None:
        if mutation == "unrecorded-module":
            rows[:] = [row for row in rows if row[0] != backdoor]
        elif mutation == "wrong-hash":
            rows[0][1] = f"sha256={'A' * 43}"
        elif mutation == "wrong-size":
            rows[0][2] = str(int(rows[0][2]) + 1)
        elif mutation == "duplicate":
            rows.insert(1, list(rows[0]))
        elif mutation == "self-hash":
            rows[-1][1:] = [f"sha256={'A' * 43}", "1"]
        else:
            rows[0][0] = f"./{rows[0][0]}"

    wheel = tmp_path / f"topoforge-{VERSION}-record-{mutation}-py3-none-any.whl"
    _write_wheel(
        wheel,
        extra_members=[(backdoor, b"raise RuntimeError('unrecorded')\n")],
        record_mutator=mutate,
    )

    with pytest.raises(ValueError, match="wheel RECORD"):
        inspect_wheel(wheel, VERSION)


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


def test_platform_core_preserves_virtual_environment_interpreter_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "managed-python"
    target.write_bytes(b"python")
    environment = tmp_path / "environment with spaces"
    environment.mkdir()
    executable = environment / "python"
    try:
        executable.symlink_to(target)
    except OSError:
        pytest.skip("host cannot create a test interpreter symlink")

    assert _absolute_python_executable(executable) == executable.absolute()
    assert _absolute_python_executable(executable) != target.absolute()


def test_release_virtual_environment_paths_are_platform_aware(tmp_path: Path) -> None:
    environment = tmp_path / "environment with spaces" / "地形"
    assert _venv_executable(environment, "python", windows=False) == (
        environment / "bin" / "python"
    )
    assert _venv_executable(environment, "topoforge", windows=True) == (
        environment / "Scripts" / "topoforge.exe"
    )


def test_packaged_web_assets_are_checkout_byte_exact() -> None:
    root = Path(__file__).parents[2]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "web/index.html text eol=lf" in attributes
    assert "src/topoforge/web/static/** -text" in attributes


@pytest.mark.parametrize(
    "relative_path",
    (
        "scripts/build_windows_portable.py",
        "scripts/verify_windows_bambu.py",
        "scripts/verify_windows_portable.py",
        "scripts/verify_windows_system.py",
    ),
)
def test_windows_script_console_json_is_ascii_safe(relative_path: str) -> None:
    root = Path(__file__).parents[2]
    source = (root / relative_path).read_text(encoding="utf-8")
    unsafe_lines = [
        line
        for line in source.splitlines()
        if "print(json.dumps" in line and "ensure_ascii=False" in line
    ]
    assert unsafe_lines == []


def test_ci_release_verification_uses_project_version() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "id: package-version" in workflow
    assert "uv version --short" in workflow
    assert "--version ${{ steps.package-version.outputs.version }}" in workflow
    assert "--version 0.8.0" not in workflow


def test_python_community_support_contract() -> None:
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["requires-python"] == ">=3.11,<3.15"
    classifiers = project["project"]["classifiers"]
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {version}" in classifiers
    assert "manifold3d>=3.5,<4" in project["project"]["dependencies"]
    assert project["tool"]["ruff"]["target-version"] == "py311"
    assert project["tool"]["pyright"]["pythonVersion"] == "3.11"


def test_python_compatibility_ci_contract() -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    compatibility = workflow["jobs"]["python-compatibility"]
    steps = json.dumps(compatibility["steps"], sort_keys=True)

    assert compatibility["runs-on"] == "ubuntu-22.04"
    assert compatibility["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.13",
        "3.14",
    ]
    assert "uv lock --check" in steps
    assert "uv sync --locked --all-groups" in steps
    assert "scripts/verify_platform_core.py" in steps
    assert "uv run pytest" in steps
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in steps
    assert "setup-node" not in steps
    assert "playwright" not in steps


def test_windows_core_ci_contract() -> None:
    root = Path(__file__).parents[2]
    workflow_text = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]
    windows = jobs["windows-core"]
    windows_steps = json.dumps(windows["steps"], sort_keys=True)
    windows_system = next(step for step in windows["steps"] if step.get("id") == "windows-system")

    assert jobs["quality"]["runs-on"] == "ubuntu-22.04"
    assert jobs["release"]["needs"] == "quality"
    assert windows["runs-on"] == "windows-2022"
    assert '"architecture": "x64"' in windows_steps
    assert "uv sync --locked --all-groups" in windows_steps
    assert "scripts/verify_platform_core.py" in windows_steps
    assert "Report Windows core acceptance failure" in windows_steps
    assert "::error title=Windows core acceptance" in windows_steps
    assert "ci-windows-x64-core.json" in windows_steps
    assert "scripts/verify_windows_system.py" in windows_steps
    assert windows_system["env"]["TOPOFORGE_CI_TRACEBACK"] == "1"
    assert "Report Windows system acceptance failure" in windows_steps
    assert "::error title=Windows system acceptance" in windows_steps
    assert "$message.Substring($message.Length - 3600)" in windows_steps
    assert "failure() && steps.windows-system.outcome == 'failure'" in windows_steps
    assert "--require-windows" in windows_steps
    assert "ci-windows-x64-system.json" in windows_steps
    assert "uv run pytest -q --tb=short" in windows_steps
    assert "Tee-Object" in windows_steps
    assert "ci-windows-x64-pytest.log" in windows_steps
    assert "Report Windows Python regression failure" in windows_steps
    assert "failure() && steps.windows-pytest.outcome == 'failure'" in windows_steps
    assert "::error title=Windows pytest" in windows_steps
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in windows_steps
    assert "setup-node" not in windows_steps
    assert "playwright" not in windows_steps


def test_windows_bambu_ci_is_explicitly_contract_only() -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["windows-bambu-contracts"]
    steps = json.dumps(job["steps"], sort_keys=True)

    assert job["name"] == "Windows x64 Bambu contracts (no official binary)"
    assert job["needs"] == "windows-core"
    assert job["runs-on"] == "windows-2022"
    assert '"architecture": "x64"' in steps
    assert "tests/slicer/test_bambu_windows.py" in steps
    assert "tests/unit/test_bambu_profiles.py" in steps
    assert "tests/cli/test_windows_bambu.py" in steps
    assert "tests/release/test_windows_bambu_acceptance.py" in steps
    assert "scripts/verify_windows_bambu.py --help" in steps
    assert "--require-windows" not in steps
    assert "upload-artifact" not in steps


def test_windows_installed_release_ci_contract() -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["windows-installed-release"]
    steps = json.dumps(job["steps"], sort_keys=True)

    assert job["needs"] == "windows-core"
    assert job["runs-on"] == "windows-2022"
    assert '"architecture": "x64"' in steps
    assert "uv lock --check" in steps
    assert "uv sync --locked --all-groups" in steps
    installed_build = (
        "uv build --no-sources --build-constraints packaging/build-constraints.txt "
        "--require-hashes --out-dir dist/windows-release"
    )
    assert installed_build in steps
    assert "scripts/verify_release.py" in steps
    assert "--install" in steps
    assert "ci-windows-x64-installed-release.json" in steps
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in steps
    assert "setup-node" not in steps
    assert "playwright" not in steps


def test_playwright_server_startup_is_platform_neutral() -> None:
    root = Path(__file__).parents[2]
    config = (root / "web/playwright.config.ts").read_text(encoding="utf-8")
    scenario = (root / "web/tests/workspace.spec.ts").read_text(encoding="utf-8")

    assert "scripts/run_playwright_server.py" in config
    assert 'cwd: ".."' in config
    assert "/tmp" not in config
    assert "cd .." not in config
    assert "&&" not in config
    assert 'from "node:os"' in scenario
    assert "tmpdir()" in scenario
    assert "playwrightWorkspaceRoot" in scenario
    assert "playwrightInput" in scenario


def test_github_release_workflow_contract() -> None:
    root = Path(__file__).parents[2]
    path = root / ".github/workflows/release.yml"
    workflow_text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)

    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    assert workflow[True]["push"]["branches"] == ["main"]
    assert workflow[True]["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in workflow[True]
    assert "fetch-depth: 0" in workflow_text
    setup_python = f"actions/setup-python@{RELEASE_ACTION_PINS['actions/setup-python']}"
    assert workflow_text.index(setup_python) < workflow_text.index("id: target")
    assert "group: release-publication" in workflow_text
    assert "current_tag=" in workflow_text
    assert 'if [[ "$candidate" == "$current_tag" ]]' in workflow_text
    assert "git tag --merged HEAD --list 'v*' --sort=-v:refname" in workflow_text
    assert "release_contract_supported" in workflow_text
    assert 'git cat-file -e "${commit}:${required_path}"' in workflow_text
    assert workflow_text.count("git grep -Fq -e") == 4
    assert "git show" not in workflow_text[: workflow_text.index("id: version")]
    assert "predates the Phase 12 release contract" in workflow_text
    assert workflow_text.index("release_contract_supported") < workflow_text.index(
        "ref: ${{ steps.target.outputs.tag }}"
    )
    assert "gh release view" in workflow_text
    assert workflow_text.count("publish=false") >= 2
    assert "ref: ${{ steps.target.outputs.tag }}" in workflow_text
    prepare = workflow["jobs"]["prepare"]
    release = workflow["jobs"]["release"]
    assert prepare["permissions"] == {"actions": "read", "contents": "read"}
    assert release["permissions"] == {"actions": "read", "contents": "write"}
    assert release["needs"] == "prepare"
    assert release["if"] == "needs.prepare.outputs.publish == 'true'"
    assert "GH_TOKEN" not in prepare.get("env", {})
    checkout_action = f"actions/checkout@{RELEASE_ACTION_PINS['actions/checkout']}"
    checkouts = [step for step in prepare["steps"] if step.get("uses") == checkout_action]
    assert len(checkouts) == 2
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)
    assert all(
        not step.get("uses", "").startswith("actions/checkout@") for step in release["steps"]
    )
    assert any(
        step.get("uses")
        == f"actions/upload-artifact@{RELEASE_ACTION_PINS['actions/upload-artifact']}"
        and step.get("with", {}).get("name") == "topoforge-publication-bundle"
        for step in prepare["steps"]
    )
    assert any(
        step.get("uses")
        == f"actions/download-artifact@{RELEASE_ACTION_PINS['actions/download-artifact']}"
        and step.get("with", {}).get("name") == "topoforge-publication-bundle"
        for step in release["steps"]
    )
    observed_actions = {
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    }
    assert observed_actions == {
        f"{action}@{commit}" for action, commit in RELEASE_ACTION_PINS.items()
    }
    for action, commit in RELEASE_ACTION_PINS.items():
        assert f"{action}@{commit}" in workflow_text
        assert f"{action}@{commit} # v" in workflow_text
        assert f"{action}@v" not in workflow_text
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"]
    assert 'test "v$version" = "$RELEASE_TAG"' in workflow_text
    assert "source_commit: ${{ steps.version.outputs.source_commit }}" in workflow_text
    assert 'source_commit="$(git rev-parse "refs/tags/${RELEASE_TAG}^{commit}")"' in workflow_text
    assert "SOURCE_COMMIT: ${{ needs.prepare.outputs.source_commit }}" in workflow_text
    assert "resolve_release_tag_commit" in workflow_text
    assert "git/ref/tags/${RELEASE_TAG}" in workflow_text
    assert "git/tags/${object_sha}" in workflow_text
    assert 'test "$resolved_source_commit" = "$SOURCE_COMMIT"' in workflow_text
    assert workflow_text.index('test "$resolved_source_commit" = "$SOURCE_COMMIT"') < (
        workflow_text.index('gh release create "$RELEASE_TAG"')
    )
    assert workflow_text.count("uv build --no-sources") == 2
    assert workflow_text.count("--build-constraints packaging/build-constraints.txt") == 2
    assert workflow_text.count("--require-hashes") == 2
    assert "scripts/verify_release_evidence.py" in workflow_text
    assert "unzip -q" not in workflow_text
    assert workflow_text.count("--extract-destination") == 3
    assert "Assemble exact downloaded release evidence" in workflow_text
    assert workflow_text.index(
        "Verify tracked Windows release evidence metadata"
    ) < workflow_text.index("Publish GitHub Release")
    assert workflow_text.index(
        "Verify exact Windows archive and clean-system reports"
    ) < workflow_text.index("Publish GitHub Release")
    assert "--repeat-dir dist/repeat" in workflow_text
    assert "--install" in workflow_text
    assert "SHA256SUMS differs from the canonical publication manifest" in workflow_text
    assert 'gh release create "$RELEASE_TAG"' in workflow_text
    assert "dist/release/*" not in workflow_text
    assert '"${asset_filenames[@]}" SHA256SUMS' in workflow_text
    assert "steps.release-assets.outputs.checksums_sha256" in workflow_text
    assert "publication bundle closure differs" in workflow_text
    assert "os.scandir(release_root)" in workflow_text
    assert "sha256sum" not in workflow_text
    assert "cmp --silent" not in workflow_text
    assert "-printf" not in workflow_text
    assert "--verify-tag" in workflow_text
    release_docs = (root / "docs/release.md").read_text(encoding="utf-8")
    assert "automatic publication contract starts with `0.11.x`" in release_docs
    assert "unsupported tag fails before checkout" in release_docs
    assert "historical workflow or retained release assets" in release_docs


def _tar_member(
    name: str,
    *,
    payload: bytes | None = b"extra\n",
    member_type: bytes = tarfile.REGTYPE,
) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = member_type
    if payload is not None:
        info.size = len(payload)
    if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        info.linkname = "topoforge-0.10.3/README.md"
    return info, payload


@pytest.mark.parametrize(
    ("member_name", "message"),
    [
        ("/absolute.txt", "canonical relative path"),
        (f"topoforge-{VERSION}/../escape.txt", "canonical relative path"),
        (f"topoforge-{VERSION}//double.txt", "canonical relative path"),
        ("another-root/file.txt", "single"),
    ],
)
def test_sdist_rejects_unsafe_or_second_root_members(
    tmp_path: Path,
    member_name: str,
    message: str,
) -> None:
    archive = tmp_path / f"topoforge-{VERSION}.tar.gz"
    _write_sdist(archive, extra_members=[_tar_member(member_name)])

    with pytest.raises(ValueError, match=message):
        inspect_sdist(archive, VERSION)


def test_sdist_rejects_duplicates_case_and_file_directory_collisions(
    tmp_path: Path,
) -> None:
    cases = {
        "duplicate": [_tar_member(f"topoforge-{VERSION}/README.md")],
        "case": [_tar_member(f"topoforge-{VERSION}/readme.md")],
        "file-directory": [
            _tar_member(f"topoforge-{VERSION}/collision"),
            _tar_member(f"topoforge-{VERSION}/collision/child.txt"),
        ],
    }
    for label, members in cases.items():
        archive = tmp_path / f"topoforge-{VERSION}-{label}.tar.gz"
        _write_sdist(archive, extra_members=members)
        with pytest.raises(ValueError, match=r"duplicate|case|file and directory"):
            inspect_sdist(archive, VERSION)


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
    ],
)
def test_sdist_rejects_links_devices_and_fifos(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    archive = tmp_path / f"topoforge-{VERSION}-{member_type!r}.tar.gz"
    _write_sdist(
        archive,
        extra_members=[
            _tar_member(
                f"topoforge-{VERSION}/unsafe-special",
                payload=None,
                member_type=member_type,
            )
        ],
    )

    with pytest.raises(ValueError, match="non-regular member"):
        inspect_sdist(archive, VERSION)


def test_sdist_enforces_archive_member_and_expansion_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / f"topoforge-{VERSION}.tar.gz"
    _write_sdist(archive)

    monkeypatch.setattr(release_verifier, "SDIST_ARCHIVE_MAX_BYTES", archive.stat().st_size - 1)
    with pytest.raises(ValueError, match="archive bound"):
        inspect_sdist(archive, VERSION)
    monkeypatch.setattr(release_verifier, "SDIST_ARCHIVE_MAX_BYTES", archive.stat().st_size)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_COUNT_MAX", 1)
    with pytest.raises(ValueError, match="member bound"):
        inspect_sdist(archive, VERSION)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_COUNT_MAX", len(REQUIRED_SDIST_FILES))
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="member bound"):
        inspect_sdist(archive, VERSION)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_MAX_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(release_verifier, "ARCHIVE_EXPANDED_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="expands above"):
        inspect_sdist(archive, VERSION)


def test_wheel_rejects_duplicate_case_collision_and_symlink(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / f"topoforge-{VERSION}-duplicate-py3-none-any.whl"
    with pytest.warns(UserWarning, match="Duplicate name"):
        _write_wheel(duplicate, extra_members=[("topoforge/__init__.py", b"duplicate")])
    with pytest.raises(ValueError, match="duplicate member"):
        inspect_wheel(duplicate, VERSION)

    collision = tmp_path / f"topoforge-{VERSION}-case-py3-none-any.whl"
    _write_wheel(collision, extra_members=[("TopoForge/collision.py", b"")])
    with pytest.raises(ValueError, match="case"):
        inspect_wheel(collision, VERSION)

    link = zipfile.ZipInfo("topoforge/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    symlink = tmp_path / f"topoforge-{VERSION}-symlink-py3-none-any.whl"
    _write_wheel(symlink, extra_members=[(link, b"target")])
    with pytest.raises(ValueError, match="non-regular member"):
        inspect_wheel(symlink, VERSION)


@pytest.mark.parametrize(
    "member_name",
    ["/absolute", "topoforge/../escape", "topoforge//double"],
)
def test_wheel_rejects_unsafe_member_paths(tmp_path: Path, member_name: str) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-unsafe-py3-none-any.whl"
    _write_wheel(wheel, extra_members=[(member_name, b"unsafe")])

    with pytest.raises(ValueError, match="canonical relative path"):
        inspect_wheel(wheel, VERSION)


@pytest.mark.parametrize(
    "manifest_overrides",
    [
        {"assets": ["index.html", "index.html"]},
        {"sha256": {"index.html": "0" * 64, "stale.js": "1" * 64}},
        {"sizes": {}},
    ],
)
def test_wheel_rejects_inconsistent_web_manifest_sets(
    tmp_path: Path,
    manifest_overrides: dict[str, object],
) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-web-manifest-py3-none-any.whl"
    _write_wheel(wheel, manifest_overrides=manifest_overrides)
    with pytest.raises(ValueError, match=r"duplicate asset|path sets differ"):
        inspect_wheel(wheel, VERSION)


def test_wheel_rejects_unmanifested_static_asset(tmp_path: Path) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-stale-web-py3-none-any.whl"
    _write_wheel(
        wheel,
        extra_members=[("topoforge/web/static/assets/stale-private.js", b"stale")],
    )
    with pytest.raises(ValueError, match="unmanifested"):
        inspect_wheel(wheel, VERSION)


def test_wheel_enforces_archive_member_and_expansion_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-py3-none-any.whl"
    _write_wheel(wheel)

    monkeypatch.setattr(release_verifier, "WHEEL_ARCHIVE_MAX_BYTES", wheel.stat().st_size - 1)
    with pytest.raises(ValueError, match="archive bound"):
        inspect_wheel(wheel, VERSION)
    monkeypatch.setattr(release_verifier, "WHEEL_ARCHIVE_MAX_BYTES", wheel.stat().st_size)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_COUNT_MAX", 1)
    with pytest.raises(ValueError, match="member bound"):
        inspect_wheel(wheel, VERSION)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_COUNT_MAX", 4096)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="member bound"):
        inspect_wheel(wheel, VERSION)
    monkeypatch.setattr(release_verifier, "ARCHIVE_MEMBER_MAX_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(release_verifier, "ARCHIVE_EXPANDED_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="expands above"):
        inspect_wheel(wheel, VERSION)


def test_release_archive_reports_bounds_and_phase12_required_files(tmp_path: Path) -> None:
    sdist = tmp_path / f"topoforge-{VERSION}.tar.gz"
    wheel = tmp_path / f"topoforge-{VERSION}-py3-none-any.whl"
    _write_sdist(sdist)
    _write_wheel(wheel)

    sdist_report = inspect_sdist(sdist, VERSION)
    wheel_report = inspect_wheel(wheel, VERSION)
    assert f"scripts/rollback-topoforge-{VERSION}.sh" in sdist_report["required_files_present"]
    assert not any("rollback-topoforge-" in path for path in REQUIRED_SDIST_FILES)
    assert sdist_report["top_level_paths"] == [f"topoforge-{VERSION}"]
    assert wheel_report["top_level_paths"] == [
        "topoforge",
        f"topoforge-{VERSION}.dist-info",
    ]
    for report in (sdist_report, wheel_report):
        assert report["bytes"] > 0
        assert report["member_count"] >= report["file_count"]
        assert report["expanded_bytes"] > 0
        assert set(report["bounds"]) == {
            "archive_max_bytes",
            "member_count_max",
            "member_max_bytes",
            "expanded_max_bytes",
        }
    assert {
        ".github/workflows/windows-clean-release-evidence.yml",
        "docs/windows-support.md",
        "packaging/build-constraints.txt",
        "packaging/release-evidence.schema.json",
        "scripts/verify_release_evidence.py",
        "scripts/verify_release_rollback.py",
        "scripts/windows_acceptance.py",
        "src/topoforge/exporters/three_mf.py",
        "src/topoforge/provenance/writer.py",
        "src/topoforge/raster/processing.py",
        "src/topoforge/util/atomic.py",
        "src/topoforge/validation/manufacturing.py",
        "src/topoforge/web/security.py",
        "src/topoforge/workflow/local.py",
        "src/topoforge/workflow/maintenance.py",
        "src/topoforge/workflow/ux.py",
        "tests/integration/test_aoi_clipping.py",
        "tests/integration/test_completed_workflow_verifier.py",
        "tests/integration/test_geographic_crs_aoi.py",
        "tests/integration/test_workflow_maintenance.py",
        "tests/release/test_release_evidence.py",
        "tests/unit/test_manufacturing_gate.py",
        "tests/unit/test_atomic_io.py",
        "tests/unit/test_provenance_writer.py",
        "tests/unit/test_synthetic_raster.py",
    } <= REQUIRED_SDIST_FILES


@pytest.mark.parametrize(
    ("sdist_name", "wheel_name", "message"),
    [
        (
            "renamed.tar.gz",
            f"topoforge-{VERSION}-py3-none-any.whl",
            "release archive directory",
        ),
        (f"topoforge-{VERSION}.tar.gz", "renamed.whl", "release archive directory"),
    ],
)
def test_release_verifier_rejects_noncanonical_archive_names(
    tmp_path: Path,
    sdist_name: str,
    wheel_name: str,
    message: str,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _write_sdist(primary / sdist_name)
    _write_wheel(primary / wheel_name)

    with pytest.raises(ValueError, match=message):
        release_verifier.verify_release(
            primary,
            repeat_dir=None,
            version=VERSION,
            install=False,
            repository_root=Path(__file__).parents[2],
            wheelhouse=None,
        )


def test_release_verifier_rejects_an_injected_primary_asset(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _write_sdist(primary / f"topoforge-{VERSION}.tar.gz")
    _write_wheel(primary / f"topoforge-{VERSION}-py3-none-any.whl")
    (primary / "topoforge-unverified.bin").write_bytes(b"unverified")

    with pytest.raises(ValueError, match="exactly uv's marker"):
        release_verifier.verify_release(
            primary,
            repeat_dir=None,
            version=VERSION,
            install=False,
            repository_root=Path(__file__).parents[2],
            wheelhouse=None,
        )


def test_release_verifier_emits_immutable_archive_outputs(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".gitignore").write_bytes(b"*")
    _write_sdist(primary / f"topoforge-{VERSION}.tar.gz")
    _write_wheel(primary / f"topoforge-{VERSION}-py3-none-any.whl")
    report = release_verifier.verify_release(
        primary,
        repeat_dir=None,
        version=VERSION,
        install=False,
        repository_root=Path(__file__).parents[2],
        wheelhouse=None,
    )
    github_output = tmp_path / "github-output.txt"
    github_output.write_text("retained=true\n", encoding="utf-8")

    release_verifier._write_github_output(github_output, report)

    assert github_output.read_text(encoding="utf-8").splitlines() == [
        "retained=true",
        f"wheel_filename=topoforge-{VERSION}-py3-none-any.whl",
        f"wheel_sha256={report['wheel']['sha256']}",
        f"sdist_filename=topoforge-{VERSION}.tar.gz",
        f"sdist_sha256={report['sdist']['sha256']}",
    ]


def test_build_backend_and_build_commands_are_exactly_pinned() -> None:
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["build-system"]["requires"] == ["hatchling==1.31.0"]
    assert (
        "/release-evidence" not in project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    )
    assert set(project["dependency-groups"]["build"]) == {
        "hatchling==1.31.0",
        "packaging==26.2",
        "pathspec==1.1.1",
        "pluggy==1.6.0",
        "trove-classifiers==2026.6.1.19",
    }

    constraints = (root / "packaging/build-constraints.txt").read_text(encoding="utf-8")
    requirement_lines = [
        line for line in constraints.splitlines() if line and not line.startswith("#")
    ]
    assert len(requirement_lines) == 5
    assert all("==" in line and " --hash=sha256:" in line for line in requirement_lines)

    workflows = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml")
    )
    build_lines = [line for line in workflows.splitlines() if "uv build --no-sources" in line]
    assert len(build_lines) == 5
    assert all(
        "--build-constraints packaging/build-constraints.txt" in line for line in build_lines
    )
    assert all("--require-hashes" in line for line in build_lines)

    builder = (root / "scripts/build_windows_portable.py").read_text(encoding="utf-8")
    assert '"--build-constraints"' in builder
    assert '"--require-hashes"' in builder
    assert "provenance / build_constraints.name" in builder


@pytest.mark.parametrize(
    "component",
    [
        "CON.txt",
        "NUL",
        "file:stream",
        "trailing.",
        "trailing ",
        "bad?.txt",
        "control\x01.txt",
        "cafe\u0301.txt",
    ],
)
def test_sdist_rejects_cross_platform_alias_components(
    tmp_path: Path,
    component: str,
) -> None:
    archive = tmp_path / f"topoforge-{VERSION}-platform-alias.tar.gz"
    _write_sdist(
        archive,
        extra_members=[_tar_member(f"topoforge-{VERSION}/unsafe/{component}")],
    )
    with pytest.raises(ValueError, match="unsafe platform component"):
        inspect_sdist(archive, VERSION)


@pytest.mark.parametrize(
    "component",
    [
        "CON.txt",
        "NUL",
        "file:stream",
        "trailing.",
        "trailing ",
        "bad?.txt",
        "control\x01.txt",
        "cafe\u0301.txt",
    ],
)
def test_wheel_rejects_cross_platform_alias_components(
    tmp_path: Path,
    component: str,
) -> None:
    wheel = tmp_path / f"topoforge-{VERSION}-platform-alias-py3-none-any.whl"
    _write_wheel(wheel, extra_members=[(f"topoforge/unsafe/{component}", b"unsafe")])
    with pytest.raises(ValueError, match="unsafe platform component"):
        inspect_wheel(wheel, VERSION)
