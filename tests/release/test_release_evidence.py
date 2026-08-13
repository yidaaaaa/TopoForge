from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema
import pytest
import scripts.verify_release_evidence as evidence_verifier
import scripts.verify_release_rollback as rollback_verifier
import scripts.verify_windows_portable as portable_verifier
import yaml
from scripts.verify_release_evidence import (
    BAMBU_REPORT_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    PORTABLE_REPORT_SCHEMA_VERSION,
    SYSTEM_REPORT_SCHEMA_VERSION,
    TARGET_ARGUMENTS,
    VERIFIER_PATHS,
    _canonical_json_sha256,
    _manufacturing_signature,
    _tracked_file_bytes,
    _validate_bambu_identity_policy,
    _validate_hosted_report,
    _validate_public_projection_pair,
    _validate_source_transition,
    verify_windows_release_evidence,
    windows_evidence_required,
)
from scripts.verify_release_evidence import (
    _write_github_output as _write_evidence_github_output,
)
from scripts.verify_release_rollback import canonical_rollback_script

SOURCE_COMMIT = "1" * 40
RELEASE_ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "astral-sh/setup-uv": "d0d8abe699bfb85fec6de9f7adb5ae17292296ff",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}


@cache
def _release_bash() -> str:
    try:
        return rollback_verifier._working_bash()
    except RuntimeError:
        pytest.skip("release workflow shell regression requires a working Bash executable")


def _path_for_release_bash(path: str) -> str:
    entry = path.replace("\\", "/")
    if len(entry) >= 2 and entry[0].isalpha() and entry[1] == ":":
        return f"/{entry[0].lower()}{entry[2:]}"
    return entry


def _release_bash_script(script: str, environment: dict[str, str]) -> str:
    tools = environment.get("TOPOFORGE_RELEASE_TEST_TOOLS")
    if tools is None:
        return script
    prefix = shlex.quote(_path_for_release_bash(tools))
    return f"export PATH={prefix}:$PATH\n{script}"


def test_windows_tool_path_is_explicitly_translated_for_git_bash() -> None:
    assert _path_for_release_bash(r"C:\Fixture Tools") == "/c/Fixture Tools"
    assert _path_for_release_bash(r"\\server\share\bin") == "//server/share/bin"


def test_release_bash_runs_a_fixture_tool_from_the_explicit_prefix(tmp_path: Path) -> None:
    tools = tmp_path / "Fixture Tools"
    tools.mkdir()
    fixture = tools / "topoforge-release-fixture"
    fixture.write_text("#!/usr/bin/env bash\nprintf 'fixture-ran\\n'\n", encoding="utf-8")
    fixture.chmod(0o755)
    environment = {
        **os.environ,
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(tools),
    }

    completed = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(fixture.name, environment)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "fixture-ran\n"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_minimal_release_wheel(
    root: Path,
    *,
    version: str,
    metadata_version: str | None = None,
    entry_points: str = "[console_scripts]\ntopoforge = topoforge.cli.app:app\n",
) -> tuple[Path, str]:
    wheel = root / f"topoforge-{version}-py3-none-any.whl"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    dist_info = f"topoforge-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("topoforge/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr("topoforge/cli/app.py", "app = object()\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            (f"Metadata-Version: 2.3\nName: topoforge\nVersion: {metadata_version or version}\n"),
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            entry_points,
        )
    return wheel, _sha256(wheel.read_bytes())


BAMBU_EXECUTABLE_PATH = "C:/Program Files/Bambu Studio/bambu-studio.exe"
BAMBU_PROFILE_ROOT = "C:/Program Files/Bambu Studio/resources/profiles/BBL"
PUBLIC_EVIDENCE_ROOT = "C:/TopoForge Public Evidence/work_root"
BAMBU_PROFILE_SHA256 = {
    "machine": "3" * 64,
    "process": "4" * 64,
    "filament": "5" * 64,
}
BAMBU_SOURCE_RECORDS = {
    role: [
        {
            "kind": role,
            "name": f"fixture-{role}",
            "path": f"{role}/Fixture {role}.json",
            "sha256": str(index + 6) * 64,
            "size_bytes": 256 + index,
        }
    ]
    for index, role in enumerate(("machine", "process", "filament"))
}
BAMBU_SOURCE_RECORDS_SHA256 = _canonical_json_sha256(BAMBU_SOURCE_RECORDS)
BAMBU_PROFILE_MANIFEST_SHA256 = "9" * 64
BAMBU_PROFILE_CONTENT_IDENTITY_SHA256 = _canonical_json_sha256(
    {
        "schema_version": "topoforge-bambu-profile-bundle-v1",
        "executable": {
            "sha256": "b" * 64,
            "size_bytes": 123456,
            "version": "02.03.00.70",
        },
        "profiles": {
            role: {
                "name": f"fixture-{role}",
                "resolved_path": f"{role}.json",
                "resolved_sha256": BAMBU_PROFILE_SHA256[role],
                "resolved_size_bytes": 1024,
                "sources": BAMBU_SOURCE_RECORDS[role],
            }
            for role in ("machine", "process", "filament")
        },
    }
)
BAMBU_SOURCE_ROOT_IDENTITY_SHA256 = _canonical_json_sha256(
    {
        "relative_to_executable": "resources/profiles/BBL",
        "is_executable_sibling": True,
        "profile_content_identity_sha256": BAMBU_PROFILE_CONTENT_IDENTITY_SHA256,
        "source_records_sha256": BAMBU_SOURCE_RECORDS_SHA256,
    }
)
BAMBU_IDENTITY = {
    "publisher_subject": "CN=Bambu Lab",
    "certificate_thumbprint": "A" * 40,
    "executable_sha256": "b" * 64,
    "version": "02.03.00.70",
    "profile_content_identity_sha256": BAMBU_PROFILE_CONTENT_IDENTITY_SHA256,
    "resolved_profile_sha256": copy.deepcopy(BAMBU_PROFILE_SHA256),
    "source_records_sha256": BAMBU_SOURCE_RECORDS_SHA256,
    "source_root_identity_sha256": BAMBU_SOURCE_ROOT_IDENTITY_SHA256,
}
ORIENTATION = {
    "east_axis": "+X = East",
    "north_axis": "+Y = North",
    "up_axis": "+Z = Up",
    "north_edge": "y=model_depth_mm",
}


def _core_report(
    *, system: str, machine: str, python_version: str, topoforge_version: str = "0.11.0"
) -> dict[str, Any]:
    roles = ("model.stl", "model.3mf", "preview.glb")
    hashes = {role: hashlib.sha256(role.encode()).hexdigest() for role in roles}
    geometry = {
        "watertight": True,
        "winding_consistent": True,
        "manifold": True,
        "positive_volume": True,
    }
    dimensions = [64.0, 48.0, 20.0]
    python_executable = (
        "C:/TopoForge Public Evidence/work_root/runtime/python.exe"
        if system == "Windows"
        else "/usr/bin/python3.12"
    )
    path_root = (
        "C:/TopoForge Public Evidence/work_root/core path/地形"
        if system == "Windows"
        else "/tmp/TopoForge Evidence/core path/地形"
    )
    return {
        "schema_version": "topoforge-platform-core-verification-v1",
        "platform": {
            "system": system,
            "release": "fixture",
            "version": "fixture",
            "machine": machine,
            "python": python_version,
            "python_executable": python_executable,
        },
        "path_contract": {
            "root": path_root,
            "contains_spaces": True,
            "contains_non_ascii": True,
            "required_checks_passed": True,
        },
        "doctor": {"topoforge": topoforge_version, "python": python_version},
        "synthetic": {
            "path": f"{path_root}/synthetic terrain.tif",
            "terrain": "saddle",
            "sha256": "a" * 64,
        },
        "web": {
            "status": "ok",
            "loopback_only": True,
            "assets": {
                "asset_count": 3,
                "languages": ["zh-CN", "en"],
                "required_checks_passed": True,
            },
            "required_checks_passed": True,
        },
        "builds": {
            "dimensions_mm": dimensions,
            "volume_mm3": 43210.5,
            "triangle_count": 4096,
            "connected_components": 1,
            "degenerate_faces": 0,
            "duplicate_faces": 0,
            "bottom_planarity_error_mm": 0.0,
            "orientation": copy.deepcopy(ORIENTATION),
        },
        "artifacts": {
            "first_sha256": hashes,
            "repeat_sha256": copy.deepcopy(hashes),
            "deterministic": {role: True for role in roles},
        },
        "strict_reopen": {
            "three_mf": {"strict_warning_count": 0, "dimensions_mm": dimensions},
            "stl": copy.deepcopy(geometry),
            "glb": copy.deepcopy(geometry),
        },
        "required_checks_passed": True,
    }


def _target_record(target_id: str) -> dict[str, Any]:
    if target_id == "windows-10-22h2-x64":
        product_name = "Windows 10 Pro"
        display_version = "22H2"
        build = 19045
    else:
        # Windows 11 can retain this compatibility registry ProductName.
        product_name = "Windows 10 Pro"
        display_version = "24H2"
        build = 26100
    return {
        "expected_target": TARGET_ARGUMENTS[target_id],
        "target_id": target_id,
        "product_name": product_name,
        "display_version": display_version,
        "current_build_number": build,
        "ubr": 1234,
        "full_build": f"{build}.1234",
        "installation_type": "Client",
        "system": "Windows",
        "machine": "AMD64",
        "process_machine_code": 0,
        "process_machine": "UNKNOWN",
        "native_machine_code": 0x8664,
        "native_machine": "AMD64",
        "native_x64_verified": True,
        "native_windows_verified": True,
        "target_verified": True,
    }


def _nested_binding(
    *,
    role: str,
    target_id: str,
    archive_sha256: str,
    archive_bytes: int,
    config_sha256: str,
    build_constraints_sha256: str,
    verifier_sha256: dict[str, str],
) -> dict[str, Any]:
    return {
        "binding_path": "C:/TopoForge Public Evidence/work_root/candidate-binding.json",
        "binding_sha256": "f" * 64,
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_bytes,
        "source_commit": SOURCE_COMMIT,
        "source_tracked_dirty": False,
        "config_sha256": config_sha256,
        "build_constraints_sha256": build_constraints_sha256,
        "verifier_role": role,
        "verifier_sha256": verifier_sha256[role],
        "expected_target": TARGET_ARGUMENTS[target_id],
        "target_id": target_id,
        "required_checks_passed": True,
    }


def _clean_report(
    *,
    version: str,
    target_id: str,
    archive_sha256: str,
    archive_bytes: int,
    config_sha256: str,
    build_constraints_sha256: str,
    verifier_sha256: dict[str, str],
    include_bambu: bool,
) -> dict[str, Any]:
    target = _target_record(target_id)
    system_binding = _nested_binding(
        role="system",
        target_id=target_id,
        archive_sha256=archive_sha256,
        archive_bytes=archive_bytes,
        config_sha256=config_sha256,
        build_constraints_sha256=build_constraints_sha256,
        verifier_sha256=verifier_sha256,
    )
    model_sha256 = "c" * 64
    backup_sha256 = "d" * 64
    three_mf = {
        "unit": "millimeter",
        "object_count": 1,
        "build_item_count": 1,
        "vertex_count": 2048,
        "triangle_count": 4096,
        "dimensions_mm": [64.0, 48.0, 20.0],
        "strict_warning_count": 0,
        "lib3mf_version": [2, 5, 0],
    }
    containment_mode_common = {
        "leader_process_group_id": 4100,
        "child_pid": 4200,
        "leader_process_identity": "windows:4100:fixture",
        "child_process_identity": "windows:4200:fixture",
        "containment_enabled": True,
        "required_checks_passed": True,
    }
    system = {
        "schema_version": SYSTEM_REPORT_SCHEMA_VERSION,
        "path_contract": {
            "root": "C:/TopoForge Public Evidence/work_root/system path/地形",
            "contains_spaces": True,
            "contains_non_ascii": True,
            "required_checks_passed": True,
        },
        "expected_target": target_id,
        "windows_target": copy.deepcopy(target),
        "candidate_binding": system_binding,
        "real_http_web": {
            "base_url": "http://127.0.0.1:8123",
            "launcher": {
                "kind": "candidate-batch-launcher",
                "path": "C:/TopoForge Public Evidence/work_root/portable/TopoForge-Web.cmd",
                "sha256": "b" * 64,
                "launcher_no_open": True,
                "containment": "kill-on-close-job-wrapper",
                "contained_process_tree": True,
            },
            "health": {"status": "ok"},
            "root": {
                "status": 200,
                "bytes": 4096,
                "packaged_application_served": True,
            },
            "browser": {
                "url": "http://127.0.0.1:8123/",
                "launcher_no_open_is_not_browser_evidence": True,
                "mode": "require",
                "attempted": True,
                "opened": True,
                "dispatch": {
                    "attempted": True,
                    "accepted": True,
                    "required_checks_passed": True,
                },
                "confirmed_load": {
                    "required": True,
                    "confirmed": True,
                    "one_time_nonce": True,
                    "nonce_sha256": "d" * 64,
                    "callback_origin": "http://127.0.0.1:50123",
                    "callback_timeout_seconds": 15.0,
                    "elapsed_seconds": 0.25,
                    "request_method": "GET",
                    "request_path": "/__topoforge_browser_loaded__",
                    "remote_address": "127.0.0.1",
                    "redirect_target": "http://127.0.0.1:8123/",
                    "required_checks_passed": True,
                },
                "required_checks_passed": True,
            },
            "job": {
                "state": "completed",
                "expected_stages": ["build"],
                "ready_stages": ["build"],
                "model_3mf_sha256": model_sha256,
                "required_checks_passed": True,
            },
            "download": {
                "sha256": model_sha256,
                "bytes": 1024,
                "three_mf": copy.deepcopy(three_mf),
                "required_checks_passed": True,
            },
            "shutdown": {
                "method": "identity-bound-process-tree",
                "exit_code": 2,
                "port": 8123,
                "port_closed": True,
                "required_checks_passed": True,
            },
            "required_checks_passed": True,
        },
        "completed_job": {
            "exit_code": 0,
            "expected_stages": ["build"],
            "ready_stages": ["build"],
            "artifact_sha256": model_sha256,
            "three_mf": copy.deepcopy(three_mf),
            "event_count": 4,
            "required_checks_passed": True,
        },
        "restart_recovery": {
            "state": "completed",
            "summary_reopened": True,
            "artifact_reopened": True,
            "required_checks_passed": True,
        },
        "backup_restore": {
            "archive_sha256": backup_sha256,
            "archive_size_bytes": 2048,
            "file_count": 3,
            "restored_artifact_sha256": model_sha256,
            "restored_three_mf": copy.deepcopy(three_mf),
            "required_checks_passed": True,
        },
        "process_lifecycle": {
            "pid": 4300,
            "worker_options": {"creationflags": 512},
            "recovered_state": "running",
            "cancelling_state": "cancelling",
            "terminal_state": "cancelled",
            "process_alive_after_cancel": False,
            "event_keys": ["job.queued", "job.started", "job.cancelling", "job.cancelled"],
            "required_checks_passed": True,
        },
        "windows_process_containment": {
            "platform": "Windows",
            "executed": True,
            "containment_entrypoint": (
                "topoforge.web.processes.enable_current_process_containment"
            ),
            "probe_code_sha256": "e" * 64,
            "source_binding": {
                "candidate_bound": True,
                "candidate_binding_sha256": system_binding["binding_sha256"],
                "source_commit": SOURCE_COMMIT,
                "system_verifier_sha256": verifier_sha256["system"],
                "system_verifier_matches_candidate": True,
                "required_checks_passed": True,
            },
            "leader_exit": {
                **containment_mode_common,
                "leader_pid": 4100,
                "mode": "leader-exit",
                "leader_exit_code": 0,
                "leader_alive_after_exit": False,
                "child_alive_after_exit": False,
                "kill_on_job_close_verified": True,
            },
            "cancellation": {
                **containment_mode_common,
                "leader_pid": 4100,
                "mode": "cancel",
                "leader_exit_code": 1,
                "leader_alive_after_cancel": False,
                "child_alive_after_cancel": False,
                "production_termination_adapter_exercised": True,
            },
            "job_object_kill_on_close_verified": True,
            "production_cancellation_verified": True,
            "required_checks_passed": True,
        },
        "required_checks_passed": True,
    }
    resolved_profiles = {
        role: {
            "path": f"C:/TopoForge Public Evidence/work_root/profile-cache/{role}.json",
            "sha256": BAMBU_PROFILE_SHA256[role],
            "size_bytes": 1024,
            "name": f"fixture-{role}",
            "expected_sha256": BAMBU_PROFILE_SHA256[role],
            "sha256_matched": True,
            "source_count": len(BAMBU_SOURCE_RECORDS[role]),
        }
        for role in ("machine", "process", "filament")
    }
    profile_binding = {
        "path": BAMBU_PROFILE_ROOT,
        "selection_mode": "executable-sibling-discovery",
        "expected_executable_sibling_path": BAMBU_PROFILE_ROOT,
        "relative_to_executable": "resources/profiles/BBL",
        "is_executable_sibling": True,
        "override_requested": False,
        "override_authorized_by_frozen_hashes": None,
        "profile_identity_frozen": True,
        "profile_manifest_sha256": BAMBU_PROFILE_MANIFEST_SHA256,
        "profile_content_identity_sha256": BAMBU_PROFILE_CONTENT_IDENTITY_SHA256,
        "expected_profile_content_identity_sha256": BAMBU_PROFILE_CONTENT_IDENTITY_SHA256,
        "profile_content_identity_sha256_matched": True,
        "resolved_profiles": resolved_profiles,
        "expected_resolved_profile_sha256": copy.deepcopy(BAMBU_PROFILE_SHA256),
        "source_records": copy.deepcopy(BAMBU_SOURCE_RECORDS),
        "source_records_sha256": BAMBU_SOURCE_RECORDS_SHA256,
        "source_root_identity_sha256": BAMBU_SOURCE_ROOT_IDENTITY_SHA256,
        "required_checks_passed": True,
    }
    bambu: dict[str, Any] | None = None
    if include_bambu:
        bambu = {
            "schema_version": BAMBU_REPORT_SCHEMA_VERSION,
            "path_contract": {
                "root": "C:/TopoForge Public Evidence/work_root/Bambu path/地形",
                "contains_spaces": True,
                "contains_non_ascii": True,
                "required_checks_passed": True,
            },
            "expected_target": target_id,
            "windows_target": copy.deepcopy(target),
            "candidate_binding": _nested_binding(
                role="bambu",
                target_id=target_id,
                archive_sha256=archive_sha256,
                archive_bytes=archive_bytes,
                config_sha256=config_sha256,
                build_constraints_sha256=build_constraints_sha256,
                verifier_sha256=verifier_sha256,
            ),
            "bambu_studio": {
                "probe": {"version": BAMBU_IDENTITY["version"]},
                "executable": {
                    "path": BAMBU_EXECUTABLE_PATH,
                    "sha256": BAMBU_IDENTITY["executable_sha256"],
                    "size_bytes": 123456,
                },
                "authenticode": {
                    "status": "Valid",
                    "status_message": "Signature verified.",
                    "executable_sha256": BAMBU_IDENTITY["executable_sha256"],
                    "publisher_subject": BAMBU_IDENTITY["publisher_subject"],
                    "certificate_thumbprint": BAMBU_IDENTITY["certificate_thumbprint"],
                    "certificate_not_before": "2026-01-01T00:00:00Z",
                    "certificate_not_after": "2027-01-01T00:00:00Z",
                    "expected_publisher_subjects": [BAMBU_IDENTITY["publisher_subject"]],
                    "expected_certificate_thumbprints": [BAMBU_IDENTITY["certificate_thumbprint"]],
                    "publisher_subject_matched": True,
                    "certificate_thumbprint_matched": True,
                    "operator_identity_frozen": True,
                    "required_checks_passed": True,
                },
                "profiles_root": BAMBU_PROFILE_ROOT,
                "profiles_root_binding": copy.deepcopy(profile_binding),
                "required_checks_passed": True,
            },
            "profile_bundle": {
                "manifest": {
                    "path": f"{PUBLIC_EVIDENCE_ROOT}/profile-cache/profile-manifest.json",
                    "sha256": BAMBU_PROFILE_MANIFEST_SHA256,
                    "size_bytes": 2048,
                },
                "profile_content_identity_sha256": BAMBU_PROFILE_CONTENT_IDENTITY_SHA256,
                "expected_profile_content_identity_sha256": (BAMBU_PROFILE_CONTENT_IDENTITY_SHA256),
                "profile_content_identity_sha256_matched": True,
                **copy.deepcopy(resolved_profiles),
                "source_records_sha256": BAMBU_SOURCE_RECORDS_SHA256,
                "profile_identity_frozen": True,
                "required_checks_passed": True,
            },
            "workflow": {
                "workflow_id": "fixture-workflow",
                "state": "completed",
                "final_stage": "project",
                "completed_stages": ["connect", "slice", "project"],
                "reused_stages": [],
                "manifest": {
                    "path": "C:/TopoForge Public Evidence/work_root/workflow/manifest.json",
                    "sha256": "1" * 64,
                    "size_bytes": 100,
                },
                "status": {
                    "path": "C:/TopoForge Public Evidence/work_root/workflow/status.json",
                    "sha256": "2" * 64,
                    "size_bytes": 100,
                },
                "summary": {
                    "path": "C:/TopoForge Public Evidence/work_root/workflow/summary.json",
                    "sha256": "3" * 64,
                    "size_bytes": 100,
                },
                "report": {
                    "path": "C:/TopoForge Public Evidence/work_root/workflow/report.json",
                    "sha256": "4" * 64,
                    "size_bytes": 100,
                },
                "source": {
                    "path": "C:/TopoForge Public Evidence/work_root/workflow/source.tif",
                    "sha256": "5" * 64,
                    "size_bytes": 1024,
                },
                "required_checks_passed": True,
            },
            "official_slice": {
                "manifest": {
                    "path": "C:/TopoForge Public Evidence/work_root/slice/tile-slice-manifest.json",
                    "sha256": "6" * 64,
                    "size_bytes": 2048,
                },
                "tile_count": 1,
                "release_role": "official-p2s-release",
                "official_p2s_release_gate_passed": True,
                "all_parameter_checks_passed": True,
                "required_checks_passed": True,
            },
            "official_project": {
                "manifest": {
                    "path": f"{PUBLIC_EVIDENCE_ROOT}/project/bambu-tile-project-manifest.json",
                    "sha256": "7" * 64,
                    "size_bytes": 4096,
                },
                "tile_count": 1,
                "bambu_studio_version": BAMBU_IDENTITY["version"],
                "all_projects_reopened": True,
                "all_release_gates_passed": True,
                "external_profiles_loaded_on_reopen": False,
                "verification": {
                    "status": "verified",
                    "tile_count": 1,
                    "all_projects_reopened": True,
                    "all_release_gates_passed": True,
                    "required_checks_passed": True,
                },
                "tiles": [
                    {
                        "tile_id": "tile-r000-c000",
                        "validation": {
                            "path": f"{PUBLIC_EVIDENCE_ROOT}/project/tile-validation.json",
                            "sha256": "8" * 64,
                            "size_bytes": 1024,
                        },
                        "external_profiles_loaded_on_reopen": False,
                        "required_checks_passed": True,
                    }
                ],
                "required_checks_passed": True,
            },
            "required_checks_passed": True,
        }
    return {
        "schema_version": PORTABLE_REPORT_SCHEMA_VERSION,
        "public_evidence_projection": {
            "schema_version": "topoforge-windows-public-evidence-v1",
            "private_report_sha256": "f" * 64,
            "removed_fields": ["command", "commands", "cwd", "stderr", "stdout"],
            "redacted_root_labels": ["candidate_archive", "repository", "work_root"],
            "required_checks_passed": True,
        },
        "topoforge_version": version,
        "archive": {"sha256": archive_sha256, "bytes": archive_bytes},
        "target": "windows-x64",
        "runtime": {"implementation": "CPython", "version": "3.12.9"},
        "contents": {"file_count": 100},
        "project_wheel": {"sha256": "a" * 64},
        "provenance": {
            "source_commit": SOURCE_COMMIT,
            "source_dirty": False,
            "source_tracked_dirty": False,
            "config_sha256": config_sha256,
            "build_constraints_sha256": build_constraints_sha256,
            "verifier_sha256": verifier_sha256,
        },
        "launchers": {"cli": "topoforge.cmd", "web": "TopoForge-Web.cmd"},
        "cross_host_inspection_passed": True,
        "reproducibility": None,
        "execution": {
            "evidence_scope": "clean-client-target",
            "extraction_path": "C:/TopoForge Public Evidence/work_root/portable path/地形",
            "archive_sha256": archive_sha256,
            "archive_sha256_verified_before_after_and_at_completion": True,
            "path_contains_spaces": True,
            "path_contains_non_ascii": True,
            "cli_launcher": {
                "topoforge": version,
                "python": "3.12.9",
                "platform": "Windows fixture",
            },
            "web_launcher_installation_check": {
                "status": "ok",
                "loopback_only": True,
                "required_checks_passed": True,
            },
            "expected_target": target_id,
            "hosted_server": False,
            "windows_target": target,
            "platform": {
                "system": "Windows",
                "machine": "AMD64",
                "target_id": target_id,
            },
            "candidate_binding": {
                "archive_sha256": archive_sha256,
                "source_commit": SOURCE_COMMIT,
                "source_tracked_dirty": False,
                "config_sha256": config_sha256,
                "build_constraints_sha256": build_constraints_sha256,
                "verifier_sha256": verifier_sha256,
                "expected_target": target_id,
                "required_checks_passed": True,
            },
            "core": _core_report(
                system="Windows",
                machine="AMD64",
                python_version="3.12.9",
                topoforge_version=version,
            ),
            "real_http_web": copy.deepcopy(system["real_http_web"]),
            "windows_process_containment": copy.deepcopy(system["windows_process_containment"]),
            "system": system,
            "bambu": bambu,
            "claim_boundary": "clean support requires matching Win10 and Win11 reports",
            "required_checks_passed": True,
        },
        "required_checks_passed": True,
    }


def _release_fixture(
    root: Path,
    *,
    version: str = "0.11.0",
    include_bambu: bool = False,
    archive_payload: bytes = b"portable candidate fixture",
) -> dict[str, Any]:
    repository = Path(__file__).parents[2]
    source_files = [
        "packaging/windows-x64-runtime.json",
        "packaging/build-constraints.txt",
        "scripts/verify_platform_core.py",
        "scripts/verify_release_rollback.py",
        *VERIFIER_PATHS.values(),
    ]
    for relative in source_files:
        source = repository / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    identity_policy_path = root / "packaging/bambu-studio-windows-identity-policy.json"
    identity_policy_path.write_bytes(
        _json_bytes(
            {
                "allowed_identities": [copy.deepcopy(BAMBU_IDENTITY)],
                "note": "Fixture identity independently frozen for release-gate tests.",
                "policy_status": "frozen",
                "required_checks_passed": True,
                "schema_version": "topoforge-bambu-windows-identity-policy-v1",
            }
        )
    )
    patch_version = int(version.rsplit(".", maxsplit=1)[1])
    previous_version = "0.10.3" if patch_version == 0 else f"0.11.{patch_version - 1}"
    current_wheel_filename = f"topoforge-{version}-py3-none-any.whl"
    previous_wheel_filename = f"topoforge-{previous_version}-py3-none-any.whl"
    current_wheel_sha256 = "1" * 64
    previous_wheel_sha256 = "3" * 64
    previous_checksums_sha256 = "c" * 64
    previous_release_id = 510003
    previous_release_published_at = "2026-07-31T12:34:56Z"
    previous_wheel_asset_id = 510004
    previous_checksums_asset_id = 510005
    rollback_path = root / "scripts" / f"rollback-topoforge-{version}.sh"
    rollback_path.write_bytes(canonical_rollback_script(version, previous_version))
    config_sha256 = _sha256((root / "packaging/windows-x64-runtime.json").read_bytes())
    constraints_sha256 = _sha256((root / "packaging/build-constraints.txt").read_bytes())
    verifier_sha256 = {
        role: _sha256((root / relative).read_bytes()) for role, relative in VERIFIER_PATHS.items()
    }
    platform_verifier_sha256 = _sha256((root / "scripts/verify_platform_core.py").read_bytes())
    archive_sha256 = _sha256(archive_payload)
    archive_bytes = len(archive_payload)
    hosted_target = {
        "product_name": "Windows Server 2022 Datacenter",
        "display_version": "21H2",
        "current_build_number": 20348,
        "ubr": 1234,
        "full_build": "20348.1234",
        "installation_type": "Server",
        "system": "Windows",
        "machine": "AMD64",
        "process_machine_code": 0,
        "process_machine": "UNKNOWN",
        "native_machine_code": 0x8664,
        "native_machine": "AMD64",
        "native_x64_verified": True,
        "native_windows_verified": True,
        "target_verified": False,
        "evidence_scope": "hosted/unclassified Windows; not clean-client target evidence",
    }
    hosted_report = {
        "schema_version": PORTABLE_REPORT_SCHEMA_VERSION,
        "topoforge_version": version,
        "archive": {"sha256": archive_sha256, "bytes": archive_bytes},
        "provenance": {
            "source_commit": SOURCE_COMMIT,
            "source_dirty": False,
            "source_tracked_dirty": False,
            "config_sha256": config_sha256,
            "build_constraints_sha256": constraints_sha256,
            "verifier_sha256": verifier_sha256,
        },
        "reproducibility": {
            "primary_sha256": archive_sha256,
            "repeat_sha256": archive_sha256,
            "byte_reproducible": True,
        },
        "execution": {
            "expected_target": None,
            "hosted_server": True,
            "windows_target": hosted_target,
            "system": {
                "schema_version": SYSTEM_REPORT_SCHEMA_VERSION,
                "real_http_web": {
                    "browser": {
                        "mode": "skip",
                        "dispatch": {
                            "attempted": False,
                            "accepted": None,
                            "required_checks_passed": True,
                        },
                        "confirmed_load": {
                            "required": False,
                            "confirmed": None,
                            "required_checks_passed": True,
                        },
                    }
                },
            },
            "required_checks_passed": True,
        },
        "required_checks_passed": True,
    }
    hosted_bytes = _json_bytes(hosted_report)
    evidence_dir = root / "release-evidence" / version
    evidence_dir.mkdir(parents=True)
    linux_report = _core_report(system="Linux", machine="x86_64", python_version="3.12.9")
    linux_bytes = _json_bytes(linux_report)
    linux_path = evidence_dir / "linux-x86_64-python-3.12-core.json"
    linux_path.write_bytes(linux_bytes)
    clean_entries: list[dict[str, Any]] = []
    report_paths: dict[str, Path] = {}
    clean_hashes: dict[str, str] = {}
    clean_artifact_bytes: dict[str, tuple[bytes, bytes]] = {}
    for target_id in ("windows-10-22h2-x64", "windows-11-x64"):
        report = _clean_report(
            version=version,
            target_id=target_id,
            archive_sha256=archive_sha256,
            archive_bytes=archive_bytes,
            config_sha256=config_sha256,
            build_constraints_sha256=constraints_sha256,
            verifier_sha256=verifier_sha256,
            include_bambu=include_bambu,
        )
        private_report = json.loads(
            json.dumps(report, ensure_ascii=False).replace(
                PUBLIC_EVIDENCE_ROOT,
                "D:/TopoForge Private Evidence/clean root/地形",
            )
        )
        private_report.pop("public_evidence_projection")
        private_report.update(
            {
                "command": "private command",
                "commands": [],
                "cwd": "D:/private operator checkout",
                "stderr": "",
                "stdout": "",
            }
        )
        private_bytes = _json_bytes(private_report)
        report["public_evidence_projection"]["private_report_sha256"] = _sha256(private_bytes)
        report_bytes = _json_bytes(report)
        report_path = evidence_dir / f"{target_id}.json"
        report_path.write_bytes(report_bytes)
        report_paths[target_id] = report_path
        clean_hashes[target_id] = _sha256(report_bytes)
        clean_artifact_bytes[target_id] = (private_bytes, report_bytes)
        clean_entries.append(
            {
                "target_id": target_id,
                "report_path": report_path.relative_to(root).as_posix(),
                "report_sha256": clean_hashes[target_id],
                "github_actions_run_id": 200000 + len(clean_entries),
                "github_actions_run_attempt": 1,
                "github_actions_workflow_id": 300000,
                "github_actions_workflow_path": (
                    ".github/workflows/windows-clean-release-evidence.yml"
                ),
                "github_actions_event": "workflow_dispatch",
                "artifact_id": 400000 + len(clean_entries),
                "artifact_name": f"topoforge-{target_id}-clean-release-evidence",
                "artifact_digest": f"sha256:{str(len(clean_entries) + 5) * 64}",
                "private_report_relative_path": f"{target_id}/private-report.json",
                "private_report_sha256": _sha256(private_bytes),
                "public_report_relative_path": f"{target_id}/public-report.json",
                "public_report_sha256": clean_hashes[target_id],
            }
        )
    signature = _manufacturing_signature(
        linux_report,
        label="fixture Linux",
        require_linux=True,
    )
    comparison_report = {
        "schema_version": "topoforge-cross-platform-comparison-v1",
        "topoforge_version": version,
        "source_commit": SOURCE_COMMIT,
        "archive_sha256": archive_sha256,
        "input_report_sha256": {"linux-x86_64": _sha256(linux_bytes), **clean_hashes},
        "platform_ids": [
            "linux-x86_64",
            "windows-10-22h2-x64",
            "windows-11-x64",
        ],
        "manufacturing_signature_sha256": _canonical_json_sha256(signature),
        "required_checks_passed": True,
    }
    comparison_bytes = _json_bytes(comparison_report)
    comparison_path = evidence_dir / "linux-windows-manufacturing-comparison.json"
    comparison_path.write_bytes(comparison_bytes)
    rollback_sha = _sha256(rollback_path.read_bytes())
    rollback_report = {
        "schema_version": "topoforge-rollback-runtime-verification-v4",
        "topoforge_version": version,
        "source_commit": SOURCE_COMMIT,
        "release_commit": "3" * 40,
        "producer_sha256": _sha256((root / "scripts/verify_release_rollback.py").read_bytes()),
        "script_sha256": rollback_sha,
        "previous_version": previous_version,
        "release_artifacts": {
            "current": {
                "role": "formal-current-release-primary-wheel",
                "filename": current_wheel_filename,
                "sha256": current_wheel_sha256,
                "bytes": 2048,
                "metadata_name": "topoforge",
                "metadata_version": version,
                "console_entry_point": "topoforge = topoforge.cli.app:app",
                "required_checks_passed": True,
            },
            "previous": {
                "role": "verified-previous-public-release-wheel",
                "release_tag": f"v{previous_version}",
                "release_id": previous_release_id,
                "published_at": previous_release_published_at,
                "wheel_asset_id": previous_wheel_asset_id,
                "filename": previous_wheel_filename,
                "sha256": previous_wheel_sha256,
                "bytes": 2047,
                "metadata_name": "topoforge",
                "metadata_version": previous_version,
                "console_entry_point": "topoforge = topoforge.cli.app:app",
                "checksums": {
                    "asset_id": previous_checksums_asset_id,
                    "filename": "SHA256SUMS",
                    "sha256": previous_checksums_sha256,
                    "wheel_entry": (f"{previous_wheel_sha256}  {previous_wheel_filename}"),
                    "required_checks_passed": True,
                },
                "required_checks_passed": True,
            },
            "required_checks_passed": True,
        },
        "installed_environment": {
            "strategy": "parallel-isolated-environments-atomic-pointer-switch",
            "current": {
                "version": version,
                "wheel_filename": current_wheel_filename,
                "wheel_sha256": current_wheel_sha256,
                "launcher_relative_path": "bin/topoforge",
                "launcher_sha256": "a" * 64,
                "doctor_output_sha256": "2" * 64,
                "doctor_exit_code": 0,
                "dependency_install_mode": (
                    "uv-lock-hashed-dependencies-plus-project-wheel-no-deps"
                ),
                "uv_lock_sha256": "6" * 64,
                "locked_requirements_sha256": "8" * 64,
                "required_checks_passed": True,
            },
            "previous": {
                "version": previous_version,
                "wheel_filename": previous_wheel_filename,
                "wheel_sha256": previous_wheel_sha256,
                "launcher_relative_path": "bin/topoforge",
                "launcher_sha256": "b" * 64,
                "doctor_output_sha256": "4" * 64,
                "doctor_exit_code": 0,
                "dependency_install_mode": (
                    "uv-lock-hashed-dependencies-plus-project-wheel-no-deps"
                ),
                "uv_lock_sha256": "7" * 64,
                "locked_requirements_sha256": "9" * 64,
                "required_checks_passed": True,
            },
            "activation": {
                "entrypoint": "active-installation/topoforge",
                "before_target": "current",
                "before_launcher_target": "current-environment/bin/topoforge",
                "before_launcher_sha256": "a" * 64,
                "before_version": version,
                "before_output_sha256": "2" * 64,
                "before_exit_code": 0,
                "after_target": "previous",
                "after_launcher_target": "previous-environment/bin/topoforge",
                "after_launcher_sha256": "b" * 64,
                "after_version": previous_version,
                "after_output_sha256": "4" * 64,
                "after_exit_code": 0,
                "atomic_pointer_switch": True,
                "required_checks_passed": True,
            },
            "required_checks_passed": True,
        },
        "source_checkout": {
            "release_tag": f"v{version}",
            "release_commit": "3" * 40,
            "previous_tag": f"v{previous_version}",
            "previous_commit": "2" * 40,
            "script_exit_code": 0,
            "rollback_worktree_commit": "2" * 40,
            "rollback_worktree_clean": True,
            "required_checks_passed": True,
        },
        "retained_evidence": {
            "before_rollback": {
                "file_count": 7,
                "total_bytes": 4096,
                "manifest_sha256": "5" * 64,
            },
            "after_rollback": {
                "file_count": 7,
                "total_bytes": 4096,
                "manifest_sha256": "5" * 64,
            },
            "required_checks_passed": True,
        },
        "required_checks_passed": True,
    }
    rollback_bytes = _json_bytes(rollback_report)
    filename = f"topoforge-{version}-windows-x64-portable.zip"
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "topoforge_version": version,
        "release_tag": f"v{version}",
        "release_role": ("phase12a-core-web-portable" if version == "0.11.0" else "phase12b-bambu"),
        "source_commit": SOURCE_COMMIT,
        "portable_archive": {
            "filename": filename,
            "sha256": archive_sha256,
            "bytes": archive_bytes,
            "config_sha256": config_sha256,
            "build_constraints_sha256": constraints_sha256,
            "verifier_sha256": verifier_sha256,
        },
        "candidate_artifact": {
            "github_actions_run_id": 123456,
            "github_actions_run_attempt": 1,
            "github_actions_workflow_id": 123400,
            "github_actions_workflow_path": ".github/workflows/ci.yml",
            "github_actions_event": "push",
            "artifact_id": 123457,
            "artifact_name": "topoforge-windows-x64-portable-candidate",
            "artifact_digest": f"sha256:{'c' * 64}",
            "archive_relative_path": filename,
            "verification_relative_path": "artifacts/logs/ci-windows-portable-verification.json",
            "verification_sha256": _sha256(hosted_bytes),
        },
        "clean_system_reports": clean_entries,
        "bambu_studio_identity": copy.deepcopy(BAMBU_IDENTITY) if include_bambu else None,
        "bambu_policy_approval_commit": "0" * 40 if include_bambu else None,
        "cross_platform": {
            "linux_report_path": linux_path.relative_to(root).as_posix(),
            "linux_report_sha256": _sha256(linux_bytes),
            "linux_source_commit": SOURCE_COMMIT,
            "linux_verifier_sha256": platform_verifier_sha256,
            "linux_ci_artifact_id": 123458,
            "linux_ci_artifact_name": "topoforge-linux-x86_64-python-3.12-core-evidence",
            "linux_ci_artifact_digest": f"sha256:{'d' * 64}",
            "linux_ci_relative_path": "ci-linux-x86_64-python-3.12-core.json",
            "comparison_report_path": comparison_path.relative_to(root).as_posix(),
            "comparison_report_sha256": _sha256(comparison_bytes),
        },
        "rollback": {
            "script_path": rollback_path.relative_to(root).as_posix(),
            "script_sha256": rollback_sha,
            "producer_path": "scripts/verify_release_rollback.py",
            "producer_sha256": _sha256((root / "scripts/verify_release_rollback.py").read_bytes()),
            "current_wheel": {
                "filename": current_wheel_filename,
                "sha256": current_wheel_sha256,
            },
            "previous_release": {
                "release_tag": f"v{previous_version}",
                "release_id": previous_release_id,
                "published_at": previous_release_published_at,
                "wheel_filename": previous_wheel_filename,
                "wheel_asset_id": previous_wheel_asset_id,
                "wheel_sha256": previous_wheel_sha256,
                "checksums_filename": "SHA256SUMS",
                "checksums_asset_id": previous_checksums_asset_id,
                "checksums_sha256": previous_checksums_sha256,
            },
            "runtime_report_relative_path": "rollback-verification-runtime.json",
        },
        "required_checks_passed": True,
    }
    manifest_path = evidence_dir / "windows-release.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return {
        "root": root,
        "version": version,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "report_paths": report_paths,
        "archive_payload": archive_payload,
        "hosted_bytes": hosted_bytes,
        "linux_bytes": linux_bytes,
        "clean_artifact_bytes": clean_artifact_bytes,
        "rollback_bytes": rollback_bytes,
        "config_sha256": config_sha256,
        "build_constraints_sha256": constraints_sha256,
        "verifier_sha256": verifier_sha256,
    }


def _verify(
    fixture: dict[str, Any],
    *,
    metadata_only: bool = True,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    version = str(fixture["version"])
    return verify_windows_release_evidence(
        version=version,
        release_tag=f"v{version}",
        repository_root=Path(fixture["root"]),
        manifest_path=None,
        artifact_root=artifact_root,
        release_commit=None,
        require_tracked=False,
        metadata_only=metadata_only,
    )


def _write_runtime_artifacts(fixture: dict[str, Any], artifact_root: Path) -> None:
    for target_id, (private_bytes, public_bytes) in fixture["clean_artifact_bytes"].items():
        target_root = artifact_root / target_id
        target_root.mkdir(parents=True, exist_ok=True)
        (target_root / "private-report.json").write_bytes(private_bytes)
        (target_root / "public-report.json").write_bytes(public_bytes)
    (artifact_root / "rollback-verification-runtime.json").write_bytes(fixture["rollback_bytes"])


def test_evidence_github_output_binds_exact_portable_archive(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    report = _verify(fixture)
    github_output = tmp_path / "github-output.txt"
    github_output.write_text("retained=true\n", encoding="utf-8")

    _write_evidence_github_output(github_output, report)

    values = dict(
        line.split("=", maxsplit=1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
    )
    assert values["retained"] == "true"
    assert values["required"] == "true"
    assert values["archive_filename"] == fixture["manifest"]["portable_archive"]["filename"]
    assert values["archive_sha256"] == fixture["manifest"]["portable_archive"]["sha256"]


def test_safe_artifact_extractor_accepts_only_declared_inventory(tmp_path: Path) -> None:
    archive_path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("evidence/", b"")
        archive.writestr("evidence/report.json", b"{}\n")
        archive.writestr("evidence/optional.txt", b"retained\n")
    destination = tmp_path / "extracted"

    report = evidence_verifier.extract_exact_artifact(
        archive_path,
        destination,
        required_members=["evidence/report.json"],
        allowed_members=["evidence/optional.txt"],
    )

    assert report["files"] == ["evidence/optional.txt", "evidence/report.json"]
    assert (destination / "evidence/report.json").read_bytes() == b"{}\n"
    assert (destination / "evidence/optional.txt").read_bytes() == b"retained\n"
    assert report["required_checks_passed"] is True


def test_safe_artifact_extractor_rejects_traversal_without_touching_checkout(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "scripts/marker.py"
    marker.parent.mkdir()
    marker.write_text("trusted\n", encoding="utf-8")
    archive_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("evidence/report.json", b"{}\n")
        archive.writestr("../scripts/marker.py", b"overwritten\n")
    destination = tmp_path / "traversal-extracted"

    with pytest.raises(ValueError, match="canonical safe relative path"):
        evidence_verifier.extract_exact_artifact(
            archive_path,
            destination,
            required_members=["evidence/report.json"],
        )

    assert marker.read_text(encoding="utf-8") == "trusted\n"
    assert not destination.exists()


def test_safe_artifact_extractor_rejects_symlink_member(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("evidence/report.json")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(link, "../../scripts/marker.py")
    destination = tmp_path / "symlink-extracted"

    with pytest.raises(ValueError, match="link or special"):
        evidence_verifier.extract_exact_artifact(
            archive_path,
            destination,
            required_members=["evidence/report.json"],
        )

    assert not destination.exists()


def test_safe_artifact_extractor_rejects_duplicate_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("evidence/report.json", b"first\n")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("evidence/report.json", b"second\n")
    destination = tmp_path / "duplicate-extracted"

    with pytest.raises(ValueError, match="duplicate member"):
        evidence_verifier.extract_exact_artifact(
            archive_path,
            destination,
            required_members=["evidence/report.json"],
        )

    assert not destination.exists()


def test_safe_artifact_extractor_rejects_case_alias_and_extra_member(tmp_path: Path) -> None:
    for name, extra, message in (
        ("case", "Evidence/report.json", "collide across platforms"),
        ("extra", "evidence/unbound.json", "unexpected file"),
    ):
        archive_path = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("evidence/report.json", b"{}\n")
            archive.writestr(extra, b"untrusted\n")
        destination = tmp_path / f"{name}-extracted"

        with pytest.raises(ValueError, match=message):
            evidence_verifier.extract_exact_artifact(
                archive_path,
                destination,
                required_members=["evidence/report.json"],
            )

        assert not destination.exists()


def test_safe_artifact_extractor_rejects_oversize_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "oversize.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("evidence/report.json", b"12345")
    destination = tmp_path / "oversize-extracted"
    monkeypatch.setattr(evidence_verifier, "ARTIFACT_ZIP_MEMBER_MAX_BYTES", 4)

    with pytest.raises(ValueError, match="size bound"):
        evidence_verifier.extract_exact_artifact(
            archive_path,
            destination,
            required_members=["evidence/report.json"],
        )

    assert not destination.exists()


@pytest.mark.parametrize(
    ("bound_name", "bound", "message"),
    [
        ("ARTIFACT_ZIP_ARCHIVE_MAX_BYTES", 1, "bounded real single-link file"),
        ("ARTIFACT_ZIP_MEMBER_COUNT_MAX", 1, "member-count bound"),
        ("ARTIFACT_ZIP_EXPANDED_MAX_BYTES", 7, "total expansion bound"),
    ],
)
def test_safe_artifact_extractor_enforces_archive_count_and_expansion_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_name: str,
    bound: int,
    message: str,
) -> None:
    archive_path = tmp_path / f"{bound_name}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("evidence/one.json", b"1234")
        archive.writestr("evidence/two.json", b"5678")
    destination = tmp_path / f"{bound_name}-extracted"
    monkeypatch.setattr(evidence_verifier, bound_name, bound)

    with pytest.raises(ValueError, match=message):
        evidence_verifier.extract_exact_artifact(
            archive_path,
            destination,
            required_members=["evidence/one.json", "evidence/two.json"],
        )

    assert not destination.exists()


def test_safe_artifact_extractor_cli_uses_the_same_exact_contract(tmp_path: Path) -> None:
    archive_path = tmp_path / "cli-artifact.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("evidence/report.json", b"{}\n")
    destination = tmp_path / "cli-extracted"
    script = Path(__file__).parents[2] / "scripts/verify_release_evidence.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--extract-artifact",
            str(archive_path),
            "--extract-destination",
            str(destination),
            "--extract-member",
            "evidence/report.json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["files"] == ["evidence/report.json"]
    assert (destination / "evidence/report.json").read_bytes() == b"{}\n"


def _rewrite_report(
    fixture: dict[str, Any],
    target_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    report_path = fixture["report_paths"][target_id]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mutate(report)
    report_bytes = _json_bytes(report)
    report_path.write_bytes(report_bytes)
    manifest_path = Path(fixture["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["clean_system_reports"]:
        if entry["target_id"] == target_id:
            entry["report_sha256"] = _sha256(report_bytes)
            entry["public_report_sha256"] = _sha256(report_bytes)
    manifest_path.write_bytes(_json_bytes(manifest))


def test_evidence_gate_applies_to_all_0_11_patch_versions(tmp_path: Path) -> None:
    assert windows_evidence_required("0.11.0") is True
    assert windows_evidence_required("0.11.12") is True
    assert windows_evidence_required("0.11.0rc1") is True
    assert windows_evidence_required("0.10.3") is False

    report = verify_windows_release_evidence(
        version="0.10.3",
        release_tag="v0.10.3",
        repository_root=tmp_path,
        manifest_path=None,
        artifact_root=None,
        release_commit=None,
        require_tracked=True,
        metadata_only=False,
    )
    assert report["gate_required"] is False
    assert report["required_checks_passed"] is True


def test_0_11_release_fails_closed_without_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="release evidence manifest"):
        verify_windows_release_evidence(
            version="0.11.0",
            release_tag="v0.11.0",
            repository_root=tmp_path,
            manifest_path=None,
            artifact_root=None,
            release_commit=None,
            require_tracked=False,
            metadata_only=True,
        )


def test_valid_phase12a_metadata_binds_both_clean_targets(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    report = _verify(fixture)

    assert report["release_role"] == "phase12a-core-web-portable"
    assert report["clean_targets"] == ["windows-10-22h2-x64", "windows-11-x64"]
    assert report["portable_archive"]["verifier_sha256"] == fixture["verifier_sha256"]
    assert report["candidate_artifact"]["github_actions_workflow_path"] == (
        ".github/workflows/ci.yml"
    )
    assert report["candidate_artifact"]["github_actions_event"] == "push"
    assert report["candidate_artifact"]["artifact_name"] == (
        "topoforge-windows-x64-portable-candidate"
    )
    assert report["metadata_only"] is True
    assert report["required_checks_passed"] is True


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda report: report["provenance"].update(source_commit="2" * 40),
            "provenance field source_commit changed",
        ),
        (
            lambda report: report["archive"].update(sha256="2" * 64),
            "different portable archive",
        ),
        (
            lambda report: report["execution"]["windows_target"].update(target_verified=False),
            "OS/build target was not verified",
        ),
        (
            lambda report: report["execution"]["system"]["candidate_binding"].update(
                verifier_sha256="2" * 64
            ),
            "verifier_sha256 does not match",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["browser"].update(
                mode="skip", attempted=False, opened=None
            ),
            "default-browser evidence did not pass",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["browser"][
                "confirmed_load"
            ].update(confirmed=False),
            "browser load was not confirmed",
        ),
        (
            lambda report: report["execution"]["system"]["windows_process_containment"][
                "leader_exit"
            ].update(child_alive_after_exit=True),
            "child_alive_after_exit changed",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["download"].update(
                required_checks_passed=False
            ),
            "synthetic job/download/shutdown did not pass",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["download"][
                "three_mf"
            ].update(strict_warning_count=1),
            "download.three_mf strict warnings changed",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["download"][
                "three_mf"
            ].update(unit="inch"),
            "download.three_mf unit changed",
        ),
        (
            lambda report: report["execution"]["system"]["completed_job"]["three_mf"].update(
                object_count=0
            ),
            "completed_job.three_mf object_count is invalid",
        ),
        (
            lambda report: report.update(schema_version="old-portable-schema"),
            "clean report schema is unsupported",
        ),
        (
            lambda report: report["execution"]["system"].update(schema_version="old-system-schema"),
            "system schema is unsupported",
        ),
        (
            lambda report: report["execution"].update(evidence_scope="hosted-server-non-release"),
            "clean portable execution contract changed",
        ),
        (
            lambda report: report["execution"].update(
                archive_sha256_verified_before_after_and_at_completion=False
            ),
            "clean portable execution contract changed",
        ),
        (
            lambda report: report["execution"]["cli_launcher"].update(topoforge="0.0.0"),
            "clean portable execution contract changed",
        ),
        (
            lambda report: report["execution"]["web_launcher_installation_check"].update(
                required_checks_passed=False
            ),
            "clean portable execution contract changed",
        ),
        (
            lambda report: report["execution"]["core"]["platform"].update(machine="ARM64"),
            "not native Windows x64 on Python 3.12",
        ),
        (
            lambda report: report["execution"]["core"]["path_contract"].update(
                contains_non_ascii=False
            ),
            "path_contract did not prove",
        ),
        (
            lambda report: report["execution"]["core"]["doctor"].update(topoforge="0.0.0"),
            "doctor version contract changed",
        ),
        (
            lambda report: report["execution"]["core"]["web"].update(status="failed"),
            "web installation contract changed",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["browser"].update(
                url="https://example.invalid/"
            ),
            "browser URL/launcher semantics changed",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"].update(
                base_url="http://example.invalid:8123"
            ),
            "base_url is not the measured loopback endpoint",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["launcher"].update(
                kind="hosted-python-module"
            ),
            "launcher is not the candidate batch launcher",
        ),
        (
            lambda report: report["execution"]["system"]["real_http_web"]["health"].update(
                status="failed"
            ),
            "health did not report ok",
        ),
        (
            lambda report: report["execution"]["system"]["process_lifecycle"][
                "worker_options"
            ].update(creationflags=1),
            "did not prove production cancellation",
        ),
    ],
)
def test_clean_report_semantic_tampering_fails_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(fixture, "windows-10-22h2-x64", mutation)

    with pytest.raises(ValueError, match=message):
        _verify(fixture)


@pytest.mark.parametrize(
    "section",
    [
        "completed_job",
        "restart_recovery",
        "backup_restore",
        "process_lifecycle",
        "windows_process_containment",
    ],
)
def test_clean_report_requires_every_measured_lifecycle_section(
    tmp_path: Path,
    section: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"]["system"].pop(section),
    )
    with pytest.raises(ValueError, match=section):
        _verify(fixture)


def test_manifest_rejects_duplicate_or_missing_clean_target(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    manifest_path = Path(fixture["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clean_system_reports"][1]["target_id"] = "windows-10-22h2-x64"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(ValueError, match="duplicate clean report target"):
        _verify(fixture)


def test_phase12b_requires_bambu_signer_and_reopen_evidence(tmp_path: Path) -> None:
    missing = _release_fixture(tmp_path / "missing", version="0.11.1", include_bambu=False)
    with pytest.raises(ValueError, match="bambu_studio_identity must be a JSON object"):
        _verify(missing)

    valid = _release_fixture(tmp_path / "valid", version="0.11.1", include_bambu=True)
    assert _verify(valid)["release_role"] == "phase12b-bambu"

    _rewrite_report(
        valid,
        "windows-11-x64",
        lambda report: report["execution"]["bambu"]["official_project"].update(
            all_projects_reopened=False
        ),
    )
    with pytest.raises(ValueError, match="official_project verification contract did not pass"):
        _verify(valid)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda report: report["execution"]["bambu"].update(schema_version="old-bambu-schema"),
            "bambu schema is unsupported",
        ),
        (
            lambda report: report["execution"]["bambu"]["path_contract"].update(
                contains_spaces=False
            ),
            "path_contract did not prove",
        ),
        (
            lambda report: report["execution"]["bambu"]["workflow"].update(final_stage="slice"),
            "workflow did not complete",
        ),
        (
            lambda report: report["execution"]["bambu"].pop("official_slice"),
            "official_slice must be a JSON object",
        ),
        (
            lambda report: report["execution"]["bambu"]["official_slice"].update(
                release_role="interoperable"
            ),
            "official_slice release gate did not pass",
        ),
        (
            lambda report: report["execution"]["bambu"]["official_project"].update(tiles=[]),
            "official_project verification contract did not pass",
        ),
        (
            lambda report: report["execution"]["bambu"]["official_project"]["verification"].update(
                required_checks_passed=False
            ),
            "official_project verification contract did not pass",
        ),
        (
            lambda report: report["execution"]["bambu"]["official_project"].update(
                bambu_studio_version="00.00.00.00"
            ),
            "official_project verification contract did not pass",
        ),
        (
            lambda report: report["execution"]["bambu"]["official_project"]["tiles"][0][
                "validation"
            ].update(sha256="invalid"),
            "validation.sha256 must be a lowercase SHA-256 digest",
        ),
    ],
)
def test_phase12b_semantic_tampering_fails_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    fixture = _release_fixture(tmp_path, version="0.11.1", include_bambu=True)
    _rewrite_report(fixture, "windows-10-22h2-x64", mutation)

    with pytest.raises(ValueError, match=message):
        _verify(fixture)


def test_full_gate_requires_downloaded_artifact_root(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    with pytest.raises(ValueError, match="requires --artifact-root"):
        _verify(fixture, metadata_only=False)


def test_full_gate_verifies_exact_hosted_artifact_and_cross_host_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    artifact_root = tmp_path / "downloaded-artifact"
    artifact_root.mkdir()
    manifest = fixture["manifest"]
    archive_relative = manifest["candidate_artifact"]["archive_relative_path"]
    verification_relative = manifest["candidate_artifact"]["verification_relative_path"]
    archive_path = artifact_root / archive_relative
    archive_path.write_bytes(fixture["archive_payload"])
    hosted_path = artifact_root / verification_relative
    hosted_path.parent.mkdir(parents=True)
    hosted_path.write_bytes(fixture["hosted_bytes"])
    linux_relative = manifest["cross_platform"]["linux_ci_relative_path"]
    linux_path = artifact_root / linux_relative
    linux_path.parent.mkdir(parents=True, exist_ok=True)
    linux_path.write_bytes(fixture["linux_bytes"])
    _write_runtime_artifacts(fixture, artifact_root)

    monkeypatch.setattr(
        portable_verifier,
        "inspect_windows_portable",
        lambda *_args, **_kwargs: {
            "provenance": {
                "source_commit": SOURCE_COMMIT,
                "source_dirty": False,
                "source_tracked_dirty": False,
                "config_sha256": fixture["config_sha256"],
                "build_constraints_sha256": fixture["build_constraints_sha256"],
                "verifier_sha256": fixture["verifier_sha256"],
            }
        },
    )
    report = _verify(fixture, metadata_only=False, artifact_root=artifact_root)
    assert report["archive_verification"]["strict_cross_host_inspection_passed"] is True
    assert report["hosted_verification"]["byte_reproducible"] is True
    assert report["manifest"]["path"] == "release-evidence/0.11.0/windows-release.json"
    assert report["archive_verification"]["path"] == archive_relative
    assert report["hosted_verification"]["path"] == verification_relative

    hosted_path.write_bytes(fixture["hosted_bytes"] + b" ")
    with pytest.raises(ValueError, match="verification SHA-256 changed"):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


def test_full_gate_rejects_clean_artifact_public_bytes_not_in_tag(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    _write_runtime_artifacts(fixture, artifact_root)
    public_path = artifact_root / "windows-10-22h2-x64/public-report.json"
    public_path.write_bytes(public_path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="artifact public report differs from the tracked"):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


def test_full_gate_rejects_self_certified_private_projection(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    _write_runtime_artifacts(fixture, artifact_root)
    target_id = "windows-10-22h2-x64"
    private_path = artifact_root / target_id / "private-report.json"
    private_report = json.loads(private_path.read_bytes())
    private_report["operator_diagnostic"] = "private details omitted from the public report"
    private_bytes = _json_bytes(private_report)
    private_path.write_bytes(private_bytes)

    tracked_path = fixture["report_paths"][target_id]
    public_report = json.loads(tracked_path.read_bytes())
    public_report["public_evidence_projection"]["private_report_sha256"] = _sha256(private_bytes)
    public_bytes = _json_bytes(public_report)
    tracked_path.write_bytes(public_bytes)
    (artifact_root / target_id / "public-report.json").write_bytes(public_bytes)
    _rewrite_manifest(
        fixture,
        lambda manifest: next(
            entry for entry in manifest["clean_system_reports"] if entry["target_id"] == target_id
        ).update(
            report_sha256=_sha256(public_bytes),
            public_report_sha256=_sha256(public_bytes),
            private_report_sha256=_sha256(private_bytes),
        ),
    )

    with pytest.raises(ValueError, match="public projection changed fields"):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


def test_public_projection_requires_exact_private_root_replacement() -> None:
    private_report = {
        "commands": [],
        "execution": {
            "extraction_path": "D:/Private Runner Root/portable/path",
            "claim_boundary": "SEMANTICALLY DIFFERENT PRIVATE VALUE",
        },
    }
    private_bytes = _json_bytes(private_report)
    projection = {
        "schema_version": "topoforge-windows-public-evidence-v1",
        "private_report_sha256": _sha256(private_bytes),
        "removed_fields": ["commands"],
        "redacted_root_labels": ["work_root"],
        "required_checks_passed": True,
    }
    public_report = {
        "execution": {
            "extraction_path": "C:/TopoForge Public Evidence/work_root/portable/path",
            "claim_boundary": "SEMANTICALLY DIFFERENT PRIVATE VALUE",
        },
        "public_evidence_projection": projection,
    }
    _validate_public_projection_pair(
        private_report,
        public_report,
        private_bytes=private_bytes,
        label="exact projection fixture",
    )

    spoofed = copy.deepcopy(public_report)
    spoofed["execution"]["claim_boundary"] = "C:/TopoForge Public Evidence/work_root"
    with pytest.raises(ValueError, match="canonical exact private projection"):
        _validate_public_projection_pair(
            private_report,
            spoofed,
            private_bytes=private_bytes,
            label="spoofed projection fixture",
        )


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _release_target_step_script() -> str:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    return next(
        step["run"]
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Select unpublished release tag"
    )


def _prepare_release_contract_tag_repository(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "TopoForge test")
    _git(root, "config", "user.email", "test@topoforge.invalid")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "topoforge"\nversion = "0.11.0"\n',
        encoding="utf-8",
    )
    _git(root, "add", "pyproject.toml")
    _git(root, "commit", "-qm", "legacy release")
    _git(root, "tag", "v0.10.3")

    contract_files = {
        "scripts/verify_release.py": 'parser.add_argument("--github-output")\n',
        "scripts/verify_release_evidence.py": (
            'parser.add_argument("--metadata-only")\n'
            'parser.add_argument("--github-output")\n'
            'parser.add_argument("--extract-artifact")\n'
            + "# large release-evidence verifier payload\n"
            * 20_000
        ),
        "scripts/verify_release_rollback.py": "# phase12 rollback\n",
        "packaging/build-constraints.txt": "hatchling==1.31.0\n",
        "packaging/release-evidence.schema.json": "{}\n",
    }
    for relative, payload in contract_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    _git(root, "add", "scripts", "packaging")
    _git(root, "commit", "-qm", "phase12 release contract")
    _git(root, "tag", "v0.10.4")

    _git(root, "rm", "-qr", "scripts", "packaging")
    _git(root, "commit", "-qm", "unsupported newer legacy tag")
    _git(root, "tag", "v0.10.5")

    tools = root / "tools"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    gh.chmod(0o755)
    return tools


def test_main_release_selection_skips_tags_without_phase12_contract(tmp_path: Path) -> None:
    tools = _prepare_release_contract_tag_repository(tmp_path)
    github_output = tmp_path / "github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_REF_NAME": "main",
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(tools),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [
            _release_bash(),
            "-c",
            _release_bash_script(_release_target_step_script(), environment),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        "publish=true",
        "tag=v0.10.4",
    ]
    assert "Skipping v0.10.5: Phase 12 release contract is unavailable." in completed.stderr


def test_explicit_unsupported_release_tag_fails_before_checkout(tmp_path: Path) -> None:
    tools = _prepare_release_contract_tag_repository(tmp_path)
    github_output = tmp_path / "github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REF_NAME": "v0.10.5",
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(tools),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [
            _release_bash(),
            "-c",
            _release_bash_script(_release_target_step_script(), environment),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not github_output.exists()
    assert "predates the Phase 12 release contract" in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "payload"),
    [
        ("scripts/verify_release.py", "# output option is unavailable\n"),
        (
            "scripts/verify_release_evidence.py",
            'parser.add_argument("--github-output")\nparser.add_argument("--extract-artifact")\n',
        ),
        (
            "scripts/verify_release_evidence.py",
            'parser.add_argument("--metadata-only")\nparser.add_argument("--extract-artifact")\n',
        ),
        (
            "scripts/verify_release_evidence.py",
            'parser.add_argument("--metadata-only")\nparser.add_argument("--github-output")\n',
        ),
    ],
)
def test_explicit_tag_rejects_each_missing_release_contract_capability(
    tmp_path: Path,
    relative_path: str,
    payload: str,
) -> None:
    tools = _prepare_release_contract_tag_repository(tmp_path)
    _git(tmp_path, "checkout", "-q", "v0.10.4")
    (tmp_path / relative_path).write_text(payload, encoding="utf-8")
    _git(tmp_path, "add", relative_path)
    _git(tmp_path, "commit", "-qm", "partial release contract")
    _git(tmp_path, "tag", "v0.10.6")
    github_output = tmp_path / "github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_REF_TYPE": "tag",
        "GITHUB_REF_NAME": "v0.10.6",
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(tools),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [
            _release_bash(),
            "-c",
            _release_bash_script(_release_target_step_script(), environment),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not github_output.exists()
    assert "predates the Phase 12 release contract" in completed.stderr


def test_release_transition_allows_only_tracked_evidence_files(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "TopoForge test")
    _git(tmp_path, "config", "user.email", "test@topoforge.invalid")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "code.py").write_text("SOURCE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "scripts/code.py")
    _git(tmp_path, "commit", "-qm", "source")
    source_commit = _git(tmp_path, "rev-parse", "HEAD")

    evidence = tmp_path / "release-evidence/0.11.0"
    evidence.mkdir(parents=True)
    (evidence / "report.json").write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", "release-evidence/0.11.0/report.json")
    _git(tmp_path, "commit", "-qm", "evidence")
    release_commit = _git(tmp_path, "rev-parse", "HEAD")
    allowed = {"release-evidence/0.11.0/report.json"}
    _validate_source_transition(
        tmp_path,
        source_commit=source_commit,
        release_commit=release_commit,
        allowed_paths=allowed,
    )

    (scripts / "code.py").write_text("SOURCE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "scripts/code.py")
    _git(tmp_path, "commit", "-qm", "unexpected source change")
    changed_release = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="changed source outside tracked release evidence"):
        _validate_source_transition(
            tmp_path,
            source_commit=source_commit,
            release_commit=changed_release,
            allowed_paths=allowed,
        )


@pytest.mark.parametrize(
    ("version", "include_bambu"),
    [("0.11.0", False), ("0.11.1", True)],
)
def test_release_fixture_validates_against_manifest_schema(
    tmp_path: Path,
    version: str,
    include_bambu: bool,
) -> None:
    fixture = _release_fixture(tmp_path, version=version, include_bambu=include_bambu)
    schema = json.loads(
        (Path(__file__).parents[2] / "packaging/release-evidence.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(fixture["manifest"])


def test_evidence_schema_and_release_workflow_are_hard_blockers() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "packaging/release-evidence.schema.json").read_text())
    assert schema["additionalProperties"] is False
    verifier_schema = schema["properties"]["portable_archive"]["properties"]["verifier_sha256"]
    assert verifier_schema["additionalProperties"] is False
    assert set(verifier_schema["required"]) == set(VERIFIER_PATHS)
    assert schema["properties"]["clean_system_reports"]["uniqueItems"] is True
    clean_schema = schema["properties"]["clean_system_reports"]["items"]
    assert {
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
    } <= set(clean_schema["required"])
    assert schema["properties"]["candidate_artifact"]["properties"]["artifact_name"] == {
        "const": "topoforge-windows-x64-portable-candidate"
    }
    assert {
        "github_actions_run_id",
        "github_actions_run_attempt",
        "github_actions_workflow_id",
        "github_actions_workflow_path",
        "github_actions_event",
        "artifact_id",
        "artifact_name",
        "artifact_digest",
    } <= set(schema["properties"]["candidate_artifact"]["required"])
    assert {
        "linux_ci_artifact_id",
        "linux_ci_artifact_name",
        "linux_ci_artifact_digest",
    } <= set(schema["properties"]["cross_platform"]["required"])
    rollback_schema = schema["properties"]["rollback"]
    assert {"current_wheel", "previous_release"} <= set(rollback_schema["required"])
    assert rollback_schema["properties"]["current_wheel"]["additionalProperties"] is False
    assert rollback_schema["properties"]["previous_release"]["additionalProperties"] is False
    assert {
        "release_id",
        "published_at",
        "wheel_asset_id",
        "checksums_asset_id",
    } <= set(rollback_schema["properties"]["previous_release"]["required"])
    bambu_schema = schema["$defs"]["bambu_identity"]
    assert {
        "profile_content_identity_sha256",
        "resolved_profile_sha256",
        "source_records_sha256",
        "source_root_identity_sha256",
    } <= set(bambu_schema["required"])
    assert set(bambu_schema["properties"]["resolved_profile_sha256"]["required"]) == {
        "machine",
        "process",
        "filament",
    }

    workflow_path = root / ".github/workflows/release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    prepare_job = workflow["jobs"]["prepare"]
    release_job = workflow["jobs"]["release"]
    assert prepare_job["permissions"] == {"actions": "read", "contents": "read"}
    assert release_job["permissions"] == {"actions": "read", "contents": "write"}
    assert release_job["needs"] == "prepare"
    assert release_job["if"] == "needs.prepare.outputs.publish == 'true'"
    assert "GH_TOKEN" not in prepare_job.get("env", {})
    checkout_action = f"actions/checkout@{RELEASE_ACTION_PINS['actions/checkout']}"
    prepare_checkouts = [
        step for step in prepare_job["steps"] if step.get("uses") == checkout_action
    ]
    assert len(prepare_checkouts) == 2
    assert all(
        step.get("with", {}).get("persist-credentials") is False for step in prepare_checkouts
    )
    assert all(
        not step.get("uses", "").startswith("actions/checkout@") for step in release_job["steps"]
    )
    assert any(
        step.get("uses")
        == f"actions/upload-artifact@{RELEASE_ACTION_PINS['actions/upload-artifact']}"
        and step.get("with", {}).get("name") == "topoforge-publication-bundle"
        for step in prepare_job["steps"]
    )
    assert any(
        step.get("uses")
        == f"actions/download-artifact@{RELEASE_ACTION_PINS['actions/download-artifact']}"
        and step.get("with", {}).get("name") == "topoforge-publication-bundle"
        for step in release_job["steps"]
    )
    assert {
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    } == {f"{action}@{commit}" for action, commit in RELEASE_ACTION_PINS.items()}
    for action, commit in RELEASE_ACTION_PINS.items():
        assert f"{action}@{commit} # v" in workflow_text
        assert f"{action}@v" not in workflow_text
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"]
    assert "continue-on-error" not in workflow_text
    assert "source_commit: ${{ steps.version.outputs.source_commit }}" in workflow_text
    assert 'source_commit="$(git rev-parse "refs/tags/${RELEASE_TAG}^{commit}")"' in workflow_text
    assert "SOURCE_COMMIT: ${{ needs.prepare.outputs.source_commit }}" in workflow_text
    assert "resolve_release_tag_commit" in workflow_text
    assert "declare -A" not in workflow_text
    assert 'seen_tag_objects="${seen_tag_objects}|${object_sha}|"' in workflow_text
    assert "git/ref/tags/${RELEASE_TAG}" in workflow_text
    assert "git/tags/${object_sha}" in workflow_text
    tag_recheck = workflow_text.index('test "$resolved_source_commit" = "$SOURCE_COMMIT"')
    assert tag_recheck < workflow_text.index('gh release create "$RELEASE_TAG"')
    metadata = workflow_text.index("Verify tracked Windows release evidence metadata")
    identity = workflow_text.index("Verify Windows candidate workflow identity")
    download = workflow_text.index("Download exact verified Windows candidate artifact")
    full = workflow_text.index("Verify exact Windows archive and clean-system reports")
    clean_identity = workflow_text.index("Verify independent clean Windows workflow identities")
    clean_download = workflow_text.index("Download exact independent clean-system artifacts")
    archive_build = workflow_text.index("Build reproducible archives")
    archive_verify = workflow_text.index("Verify archives and isolated installation")
    previous_download = workflow_text.index(
        "Download and verify exact previous public release artifacts"
    )
    rollback_runtime = workflow_text.index("Generate executed rollback runtime evidence")
    publish = workflow_text.index("Publish GitHub Release")
    assert (
        metadata
        < identity
        < download
        < clean_identity
        < clean_download
        < archive_build
        < archive_verify
        < previous_download
        < rollback_runtime
        < full
    )
    assert full < publish
    assert "workflowName" not in workflow_text
    assert "actions/runs/${run_id}" in workflow_text
    assert "actions/workflows/${workflow_id}" in workflow_text
    assert "actions/artifacts/${artifact_id}" in workflow_text
    assert ".workflow_id == $workflow_id and .path == $workflow_path" in workflow_text
    assert ".run_attempt == $run_attempt" in workflow_text
    assert ".id == $artifact_id" in workflow_text
    assert ".digest == $artifact_digest" in workflow_text
    assert ".workflow_run.id == $run_id" in workflow_text
    assert "gh run download" not in workflow_text
    assert "Download exact canonical Linux core evidence" in workflow_text
    assert "steps.windows-evidence.outputs.linux_artifact_name" in workflow_text
    assert ".event == $workflow_event" in workflow_text
    assert "scripts/verify_release_rollback.py" in workflow_text
    assert "releases/tags/${previous_tag}" in workflow_text
    assert "releases/assets/${asset_id}" in workflow_text
    assert '.state == "uploaded"' in workflow_text
    assert ".id == $expected_release_id" in workflow_text
    assert ".published_at == $expected_published_at" in workflow_text
    assert "expected exactly one previous wheel asset" in workflow_text
    assert 'grep -Fxc -- "$expected_checksum_line"' in workflow_text
    assert 'current_wheel="dist/primary/${CURRENT_WHEEL_FILENAME}"' in workflow_text
    assert '--current-wheel "$current_wheel"' in workflow_text
    assert '--previous-wheel "${previous_root}/' in workflow_text
    assert '--previous-release-id "$PREVIOUS_RELEASE_ID"' in workflow_text
    assert '--previous-release-published-at "$PREVIOUS_PUBLISHED_AT"' in workflow_text
    assert '--previous-wheel-asset-id "$PREVIOUS_WHEEL_ASSET_ID"' in workflow_text
    assert '--previous-checksums-asset-id "$PREVIOUS_CHECKSUMS_ASSET_ID"' in workflow_text
    assert "cp dist/primary/* dist/release/" not in workflow_text
    portable_stage = workflow_text[
        workflow_text.index("Stage verified Windows portable archive") : workflow_text.index(
            "Stage release assets and checksums"
        )
    ]
    stage_assets = workflow_text[workflow_text.index("Stage release assets and checksums") :]
    publish_assets = workflow_text[workflow_text.index("Publish GitHub Release") :]
    assert "id: archives" in workflow_text
    assert "id: release-assets" in workflow_text
    assert '--github-output "$GITHUB_OUTPUT"' in workflow_text
    assert 'current_wheel_filename="$WHEEL_FILENAME"' in stage_assets
    assert 'current_wheel_sha256="$WHEEL_SHA256"' in stage_assets
    assert 'current_sdist_filename="$SDIST_FILENAME"' in stage_assets
    assert 'current_sdist_sha256="$SDIST_SHA256"' in stage_assets
    assert '"topoforge-${version}-py3-none-any.whl"' in stage_assets
    assert "${{ steps.windows-evidence.outputs.current_wheel_filename }}" in stage_assets
    assert "${{ steps.windows-evidence.outputs.current_wheel_sha256 }}" in stage_assets
    assert 'current_wheel="dist/primary/${current_wheel_filename}"' in stage_assets
    assert 'staged_current_wheel="dist/release/${current_wheel_filename}"' in stage_assets
    assert stage_assets.count('= "$current_wheel_sha256"') >= 2
    assert 'current_sdist="dist/primary/${current_sdist_filename}"' in stage_assets
    assert 'staged_current_sdist="dist/release/${current_sdist_filename}"' in stage_assets
    assert stage_assets.count('= "$current_sdist_sha256"') >= 2
    assert 'archive_sha256="$ARCHIVE_SHA256"' in portable_stage
    assert portable_stage.count('= "$archive_sha256"') == 2
    assert 'cp -- "$archive_source" "$archive_destination"' in portable_stage
    assert "dist/windows-release/*" not in workflow_text
    assert 'portable_archive_sha256="$PORTABLE_SHA256"' in stage_assets
    assert "checksums_sha256=${checksums_sha256}" in stage_assets
    assert "needs.prepare.outputs.checksums_sha256" in publish_assets
    assert "os.scandir(release_root)" in publish_assets
    assert "publication bundle closure differs" in publish_assets
    assert "SHA256SUMS differs from the canonical publication manifest" in publish_assets
    assert "-printf" not in publish_assets
    assert "sort -z" not in publish_assets
    assert "cmp --silent" not in publish_assets
    assert '"${asset_filenames[@]}" SHA256SUMS' in publish_assets
    assert "stat -c" not in workflow_text
    assert "sha256sum" not in workflow_text
    assert "cmp --silent" not in workflow_text
    assert workflow_text.count("os.lstat(sys.argv[1]).st_nlink") == 8
    assert "dist/release/*" not in publish_assets
    assert 'gh release create "$RELEASE_TAG"' in publish_assets
    assert "release_contract_supported" in workflow_text[: workflow_text.index("ref: ${{")]
    assert "predates the Phase 12 release contract" in workflow_text
    clean_workflow_text = (root / ".github/workflows/windows-clean-release-evidence.yml").read_text(
        encoding="utf-8"
    )
    assert "name: windows-clean-release-evidence" in clean_workflow_text
    assert "runs-on: [self-hosted, Windows, X64, clean" in clean_workflow_text
    assert '"--public-report"' in clean_workflow_text
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in clean_workflow_text
    assert "workflowName" not in clean_workflow_text
    assert "actions/runs/$env:CANDIDATE_RUN_ID" in clean_workflow_text
    assert "actions/workflows/$env:CANDIDATE_WORKFLOW_ID" in clean_workflow_text
    assert "actions/artifacts/$env:CANDIDATE_ARTIFACT_ID" in clean_workflow_text
    assert '$run.path -ne ".github/workflows/ci.yml"' in clean_workflow_text
    assert "$run.run_attempt" in clean_workflow_text
    assert "$artifact.digest" in clean_workflow_text
    assert "gh run download" not in clean_workflow_text
    assert "unzip -q" not in workflow_text
    assert "Expand-Archive" not in clean_workflow_text
    assert workflow_text.count("--extract-destination") == 3
    assert '--extract-member "$ARCHIVE_RELATIVE_PATH"' in workflow_text
    assert "Assemble exact downloaded release evidence" in workflow_text
    assert '"--extract-artifact", $artifactZip' in clean_workflow_text
    assert clean_workflow_text.count('"--allow-extract-member"') == 10
    assert "$env:RUNNER_TEMP" in clean_workflow_text
    assert "New-Item -ItemType Directory -Force candidate" not in clean_workflow_text
    assert "Get-ChildItem candidate" not in clean_workflow_text
    assert "path: evidence" not in clean_workflow_text
    clean_workflow = yaml.safe_load(clean_workflow_text)
    clean_checkout = clean_workflow["jobs"]["acceptance"]["steps"][0]
    assert clean_checkout["uses"] == "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    assert clean_checkout["with"]["persist-credentials"] is False
    assert "Remove-Item Env:GH_TOKEN" in clean_workflow_text
    for step in clean_workflow["jobs"]["acceptance"]["steps"]:
        if "run" in step:
            assert "${{" not in step["run"]
    rollback_source = (root / "scripts/verify_release_rollback.py").read_text(encoding="utf-8")
    assert "write_canonical_json(path, report)" in rollback_source
    assert "parallel-isolated-environments-atomic-pointer-switch" in rollback_source
    assert '"uv",\n            "export",\n            "--locked"' in rollback_source
    assert '"--require-hashes"' in rollback_source
    assert '"--no-deps"' in rollback_source
    assert '"active-installation/topoforge"' in rollback_source
    assert '"-m", "topoforge.cli.app"' not in rollback_source
    assert "uv build" not in rollback_source
    assert 'parser.add_argument("--current-wheel"' in rollback_source
    assert 'parser.add_argument("--previous-wheel"' in rollback_source
    assert 'parser.add_argument("--previous-checksums"' in rollback_source
    assert 'parser.add_argument("--previous-release-id"' in rollback_source
    assert 'parser.add_argument("--previous-wheel-asset-id"' in rollback_source
    assert "raw_name != canonical_name" in rollback_source
    assert "_snapshot_release_wheel(" in rollback_source
    assert "WHEEL_MAX_EXPANDED_BYTES" in rollback_source
    assert "_ExactEntryPointConfigParser(interpolation=None, strict=True)" in rollback_source
    web_build = workflow_text.index("Build packaged Web application")
    clean_gate = workflow_text.index("Verify packaged Web build matches release tag")
    assert web_build < clean_gate < archive_build
    assert "git diff --exit-code" in workflow_text
    assert "git status --porcelain --untracked-files=all" in workflow_text
    ci_text = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "Run canonical Linux x86_64 Python 3.12 core acceptance" in ci_text
    assert "topoforge-linux-x86_64-python-3.12-core-evidence" in ci_text
    assert "ci-linux-x86_64-python-3.12-core.json" in ci_text
    verifier_source = (root / "scripts/verify_release_evidence.py").read_text(encoding="utf-8")
    assert '"release-evidence"' in verifier_source
    assert '"windows-release.json"' in verifier_source


@pytest.mark.parametrize("tamper_at", ["before-stage", "during-copy"])
def test_release_staging_rejects_a_wheel_changed_after_archive_verification(
    tmp_path: Path,
    tamper_at: str,
) -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    stage = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Stage release assets and checksums"
    )
    version = "0.11.0"
    wheel_filename = f"topoforge-{version}-py3-none-any.whl"
    primary = tmp_path / "dist" / "primary"
    primary.mkdir(parents=True)
    wheel = primary / wheel_filename
    wheel.write_bytes(b"rollback-tested-wheel\n")
    expected_sha256 = _sha256(wheel.read_bytes())
    (primary / f"topoforge-{version}.tar.gz").write_bytes(b"verified-sdist\n")
    report_root = tmp_path / "artifacts" / "logs"
    report_root.mkdir(parents=True)
    (report_root / "github-release-verification.json").write_text(
        '{"required_checks_passed":true}\n',
        encoding="utf-8",
    )

    script = stage["run"]
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(tmp_path / "github-output.txt"),
        "RELEASE_VERSION": version,
        "WHEEL_FILENAME": wheel_filename,
        "WHEEL_SHA256": expected_sha256,
        "SDIST_FILENAME": f"topoforge-{version}.tar.gz",
        "SDIST_SHA256": _sha256(b"verified-sdist\n"),
        "WINDOWS_REQUIRED": "false",
        "EVIDENCE_WHEEL_FILENAME": wheel_filename,
        "EVIDENCE_WHEEL_SHA256": expected_sha256,
        "PORTABLE_FILENAME": "unused.zip",
        "PORTABLE_SHA256": "0" * 64,
    }
    if tamper_at == "before-stage":
        wheel.write_bytes(b"changed-after-verification\n")
    else:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        cp_wrapper = tools_dir / "cp"
        cp_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            '/bin/cp "$@"\n'
            f'if [[ "$*" == *"dist/primary"* ]]; then '
            f"printf 'changed-during-copy\\n' > 'dist/release/{wheel_filename}'; fi\n",
            encoding="utf-8",
        )
        cp_wrapper.chmod(0o755)
        environment["TOPOFORGE_RELEASE_TEST_TOOLS"] = str(tools_dir)
        environment["PATH"] = f"{tools_dir}{os.pathsep}{environment['PATH']}"

    completed = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not (tmp_path / "dist" / "release" / "SHA256SUMS").exists()


@pytest.mark.parametrize("tamper_at", ["before-stage", "during-copy"])
def test_windows_portable_staging_rejects_changed_archive(
    tmp_path: Path,
    tamper_at: str,
) -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    stage = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Stage verified Windows portable archive"
    )
    version = "0.11.0"
    archive_filename = f"topoforge-{version}-windows-x64-portable.zip"
    evidence_root = tmp_path / "dist" / "windows-evidence"
    evidence_root.mkdir(parents=True)
    archive = evidence_root / archive_filename
    archive.write_bytes(b"verified portable archive\n")
    archive_sha256 = _sha256(archive.read_bytes())
    script = stage["run"]
    environment = {
        **os.environ,
        "ARCHIVE_FILENAME": archive_filename,
        "ARCHIVE_SHA256": archive_sha256,
        "ARCHIVE_RELATIVE_PATH": archive_filename,
    }
    if tamper_at == "before-stage":
        archive.write_bytes(b"changed after evidence verification\n")
    else:
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        cp_wrapper = tools_dir / "cp"
        cp_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            '/bin/cp "$@"\n'
            f"printf 'changed during copy\\n' > "
            f"'dist/windows-release/{archive_filename}'\n",
            encoding="utf-8",
        )
        cp_wrapper.chmod(0o755)
        environment["TOPOFORGE_RELEASE_TEST_TOOLS"] = str(tools_dir)
        environment["PATH"] = f"{tools_dir}{os.pathsep}{environment['PATH']}"

    completed = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0


def _stage_release_assets_for_publish(tmp_path: Path) -> dict[str, str]:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    stage = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Stage release assets and checksums"
    )
    version = "0.11.0"
    wheel_filename = f"topoforge-{version}-py3-none-any.whl"
    sdist_filename = f"topoforge-{version}.tar.gz"
    wheel_payload = b"verified release wheel\n"
    sdist_payload = b"verified release sdist\n"
    primary = tmp_path / "dist" / "primary"
    primary.mkdir(parents=True)
    (primary / wheel_filename).write_bytes(wheel_payload)
    (primary / sdist_filename).write_bytes(sdist_payload)
    logs = tmp_path / "artifacts" / "logs"
    logs.mkdir(parents=True)
    (logs / "github-release-verification.json").write_text(
        '{"required_checks_passed":true}\n',
        encoding="utf-8",
    )
    script = stage["run"]
    github_output = tmp_path / "stage-github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_OUTPUT": str(github_output),
        "RELEASE_VERSION": version,
        "WHEEL_FILENAME": wheel_filename,
        "WHEEL_SHA256": _sha256(wheel_payload),
        "SDIST_FILENAME": sdist_filename,
        "SDIST_SHA256": _sha256(sdist_payload),
        "WINDOWS_REQUIRED": "false",
        "EVIDENCE_WHEEL_FILENAME": wheel_filename,
        "EVIDENCE_WHEEL_SHA256": _sha256(wheel_payload),
        "PORTABLE_FILENAME": "unused.zip",
        "PORTABLE_SHA256": "0" * 64,
    }
    completed = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    stage_outputs = dict(
        line.split("=", maxsplit=1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
    )
    return {
        "version": version,
        "source_commit": "a" * 40,
        "wheel_filename": wheel_filename,
        "wheel_sha256": _sha256(wheel_payload),
        "sdist_filename": sdist_filename,
        "sdist_sha256": _sha256(sdist_payload),
        "checksums_sha256": stage_outputs["checksums_sha256"],
    }


def _publish_step_script() -> str:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    publish = next(
        step
        for step in workflow["jobs"]["release"]["steps"]
        if step.get("name") == "Publish GitHub Release"
    )
    script = publish["run"]
    assert "${{" not in script
    return script


def _mock_gh_environment(
    tmp_path: Path,
    values: dict[str, str],
) -> tuple[dict[str, str], Path]:
    tools_dir = tmp_path / "publish-tools"
    tools_dir.mkdir()
    gh_log = tmp_path / "gh-arguments.txt"
    gh = tools_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "arguments = sys.argv[1:]\n"
        "if arguments[:1] == ['api']:\n"
        "    endpoint = arguments[1]\n"
        "    chain = json.loads(os.environ.get('MOCK_TAG_CHAIN', '[]'))\n"
        "    commit = os.environ['MOCK_TAG_COMMIT']\n"
        "    if '/git/ref/tags/' in endpoint:\n"
        "        target = ({'type': 'tag', 'sha': chain[0]} if chain else "
        "{'type': 'commit', 'sha': commit})\n"
        "    else:\n"
        "        current = endpoint.rsplit('/', 1)[-1]\n"
        "        index = chain.index(current)\n"
        "        target = ({'type': 'tag', 'sha': chain[index + 1]} "
        "if index + 1 < len(chain) else {'type': 'commit', 'sha': commit})\n"
        "    print(json.dumps({'object': target}, separators=(',', ':')))\n"
        "    raise SystemExit(0)\n"
        "pathlib.Path(os.environ['MOCK_GH_LOG']).write_text("
        "'\\n'.join(arguments) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    environment = {
        **os.environ,
        "MOCK_GH_LOG": str(gh_log),
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(tools_dir),
        "PATH": f"{tools_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_REPOSITORY": "topoforge/topoforge",
        "RELEASE_TAG": f"v{values['version']}",
        "RELEASE_VERSION": values["version"],
        "SOURCE_COMMIT": values["source_commit"],
        "WHEEL_FILENAME": values["wheel_filename"],
        "WHEEL_SHA256": values["wheel_sha256"],
        "SDIST_FILENAME": values["sdist_filename"],
        "SDIST_SHA256": values["sdist_sha256"],
        "WINDOWS_REQUIRED": "false",
        "PORTABLE_FILENAME": "unused.zip",
        "PORTABLE_SHA256": "0" * 64,
        "CHECKSUMS_SHA256": values["checksums_sha256"],
        "MOCK_TAG_COMMIT": values["source_commit"],
        "MOCK_TAG_CHAIN": "[]",
    }
    return environment, gh_log


@pytest.mark.parametrize(
    "mutation",
    ["asset", "checksums", "asset-and-checksums", "inject-file", "inject-directory"],
)
def test_release_publish_rejects_stage_to_publish_tamper_or_injection(
    tmp_path: Path,
    mutation: str,
) -> None:
    values = _stage_release_assets_for_publish(tmp_path)
    release_root = tmp_path / "dist" / "release"
    if mutation == "asset":
        (release_root / values["wheel_filename"]).write_bytes(b"tampered wheel\n")
    elif mutation == "checksums":
        (release_root / "SHA256SUMS").write_text("tampered checksums\n", encoding="utf-8")
    elif mutation == "asset-and-checksums":
        report_filename = f"topoforge-{values['version']}-release-verification.json"
        (release_root / report_filename).write_text('{"tampered":true}\n', encoding="utf-8")
        filenames = [values["wheel_filename"], values["sdist_filename"], report_filename]
        (release_root / "SHA256SUMS").write_text(
            "".join(
                f"{_sha256((release_root / filename).read_bytes())}  {filename}\n"
                for filename in filenames
            ),
            encoding="ascii",
        )
    elif mutation == "inject-file":
        (release_root / "topoforge-unverified.bin").write_bytes(b"injected\n")
    else:
        (release_root / "unexpected-directory").mkdir()

    environment, gh_log = _mock_gh_environment(tmp_path, values)
    completed = subprocess.run(
        [
            _release_bash(),
            "-c",
            _release_bash_script(_publish_step_script(), environment),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not gh_log.exists()


@pytest.mark.parametrize(
    "tag_chain",
    ([], ["b" * 40], ["b" * 40, "c" * 40]),
    ids=("lightweight", "annotated", "nested-annotated"),
)
def test_release_publish_invokes_gh_with_exact_verified_assets(
    tmp_path: Path,
    tag_chain: list[str],
) -> None:
    values = _stage_release_assets_for_publish(tmp_path)
    environment, gh_log = _mock_gh_environment(tmp_path, values)
    environment["MOCK_TAG_CHAIN"] = json.dumps(tag_chain)

    completed = subprocess.run(
        [
            _release_bash(),
            "-c",
            _release_bash_script(_publish_step_script(), environment),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert gh_log.read_text(encoding="utf-8").splitlines() == [
        "release",
        "create",
        f"v{values['version']}",
        values["wheel_filename"],
        values["sdist_filename"],
        f"topoforge-{values['version']}-release-verification.json",
        "SHA256SUMS",
        "--verify-tag",
        "--title",
        f"TopoForge {values['version']}",
        "--generate-notes",
    ]


def test_release_publish_rejects_tag_moved_after_prepare(tmp_path: Path) -> None:
    values = _stage_release_assets_for_publish(tmp_path)
    environment, gh_log = _mock_gh_environment(tmp_path, values)
    environment["MOCK_TAG_COMMIT"] = "f" * 40

    completed = subprocess.run(
        [
            _release_bash(),
            "-c",
            _release_bash_script(_publish_step_script(), environment),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not gh_log.exists()


@pytest.mark.parametrize("mutation", ["workflow_path", "artifact_id"])
def test_release_workflow_rejects_same_name_wrong_rest_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    if shutil.which("jq") is None:
        pytest.skip("release workflow identity regression requires bash and jq")
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    identity_step = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Verify Windows candidate workflow identity"
    )
    script = identity_step["run"]
    assert "${{" not in script

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "endpoint = sys.argv[-1]\n"
        "if endpoint.endswith('/actions/runs/123456'):\n"
        "    print(os.environ['MOCK_RUN_JSON'])\n"
        "elif endpoint.endswith('/actions/workflows/123400'):\n"
        "    print(os.environ['MOCK_WORKFLOW_JSON'])\n"
        "elif endpoint.endswith('/actions/artifacts/123457'):\n"
        "    print(os.environ['MOCK_ARTIFACT_JSON'])\n"
        "elif endpoint.endswith('/actions/artifacts/123458'):\n"
        "    print(os.environ['MOCK_LINUX_ARTIFACT_JSON'])\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    run_payload = {
        "id": 123456,
        "name": "ci",
        "run_attempt": 1,
        "workflow_id": 123400,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_sha": SOURCE_COMMIT,
        "conclusion": "success",
    }
    artifact_payload = {
        "id": 123457,
        "name": "topoforge-windows-x64-portable-candidate",
        "expired": False,
        "digest": f"sha256:{'c' * 64}",
        "workflow_run": {"id": 123456, "head_sha": SOURCE_COMMIT},
    }
    environment = {
        **os.environ,
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(fake_bin),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "topoforge/topoforge",
        "EVIDENCE_RUN_ID": "123456",
        "EVIDENCE_RUN_ATTEMPT": "1",
        "EVIDENCE_WORKFLOW_ID": "123400",
        "EVIDENCE_WORKFLOW_PATH": ".github/workflows/ci.yml",
        "EVIDENCE_WORKFLOW_EVENT": "push",
        "EVIDENCE_ARTIFACT_ID": "123457",
        "EVIDENCE_ARTIFACT_NAME": "topoforge-windows-x64-portable-candidate",
        "EVIDENCE_ARTIFACT_DIGEST": f"sha256:{'c' * 64}",
        "EVIDENCE_SOURCE_COMMIT": SOURCE_COMMIT,
        "LINUX_ARTIFACT_ID": "123458",
        "LINUX_ARTIFACT_NAME": "topoforge-linux-x86_64-python-3.12-core-evidence",
        "LINUX_ARTIFACT_DIGEST": f"sha256:{'d' * 64}",
        "MOCK_RUN_JSON": json.dumps(run_payload),
        "MOCK_WORKFLOW_JSON": json.dumps({"id": 123400, "path": ".github/workflows/ci.yml"}),
        "MOCK_ARTIFACT_JSON": json.dumps(artifact_payload),
        "MOCK_LINUX_ARTIFACT_JSON": json.dumps(
            {
                "id": 123458,
                "name": "topoforge-linux-x86_64-python-3.12-core-evidence",
                "expired": False,
                "digest": f"sha256:{'d' * 64}",
                "workflow_run": {"id": 123456, "head_sha": SOURCE_COMMIT},
            }
        ),
    }
    passing = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert passing.returncode == 0, passing.stderr
    if mutation == "workflow_path":
        run_payload["path"] = ".github/workflows/attacker.yml"
        environment["MOCK_RUN_JSON"] = json.dumps(run_payload)
    else:
        artifact_payload["id"] = 999999
        environment["MOCK_ARTIFACT_JSON"] = json.dumps(artifact_payload)
    completed = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0


@pytest.mark.parametrize("mutation", ["release_id", "wheel_asset_id"])
def test_release_workflow_rejects_forged_previous_release_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    if shutil.which("jq") is None:
        pytest.skip("previous release identity regression requires bash and jq")
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    download_step = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Download and verify exact previous public release artifacts"
    )
    wheel_filename = "topoforge-0.10.3-py3-none-any.whl"
    wheel_payload = b"exact previous public release wheel"
    wheel_sha256 = _sha256(wheel_payload)
    checksums_payload = f"{wheel_sha256}  {wheel_filename}\n".encode("ascii")
    replacements = {
        "previous_release_tag": "v0.10.3",
        "previous_release_id": "510003",
        "previous_release_published_at": "2026-07-31T12:34:56Z",
        "previous_wheel_filename": wheel_filename,
        "previous_wheel_asset_id": "510004",
        "previous_wheel_sha256": wheel_sha256,
        "previous_checksums_filename": "SHA256SUMS",
        "previous_checksums_asset_id": "510005",
        "previous_checksums_sha256": _sha256(checksums_payload),
    }
    script = download_step["run"]
    assert "${{" not in script

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\nimport os\nprint(os.environ['MOCK_PREVIOUS_RELEASE_JSON'])\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, shutil, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "endpoint = sys.argv[sys.argv.index('--output') - 1]\n"
        "source = os.environ['MOCK_WHEEL_SOURCE'] if endpoint.endswith('/510004') "
        "else os.environ['MOCK_CHECKSUMS_SOURCE']\n"
        "shutil.copyfile(source, output)\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    wheel_source = tmp_path / "wheel-source"
    checksums_source = tmp_path / "checksums-source"
    wheel_source.write_bytes(wheel_payload)
    checksums_source.write_bytes(checksums_payload)
    release_payload = {
        "id": 510003,
        "tag_name": "v0.10.3",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-07-31T12:34:56Z",
        "assets": [
            {"id": 510004, "name": wheel_filename, "state": "uploaded"},
            {"id": 510005, "name": "SHA256SUMS", "state": "uploaded"},
        ],
    }
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    environment = {
        **os.environ,
        "TOPOFORGE_RELEASE_TEST_TOOLS": str(fake_bin),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "fixture-token",
        "GITHUB_REPOSITORY": "topoforge/topoforge",
        "GITHUB_RUN_ID": "700001",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "RUNNER_TEMP": str(runner_temp),
        "PREVIOUS_TAG": replacements["previous_release_tag"],
        "PREVIOUS_RELEASE_ID": replacements["previous_release_id"],
        "PREVIOUS_PUBLISHED_AT": replacements["previous_release_published_at"],
        "PREVIOUS_WHEEL_FILENAME": replacements["previous_wheel_filename"],
        "PREVIOUS_WHEEL_ASSET_ID": replacements["previous_wheel_asset_id"],
        "PREVIOUS_WHEEL_SHA256": replacements["previous_wheel_sha256"],
        "PREVIOUS_CHECKSUMS_FILENAME": replacements["previous_checksums_filename"],
        "PREVIOUS_CHECKSUMS_ASSET_ID": replacements["previous_checksums_asset_id"],
        "PREVIOUS_CHECKSUMS_SHA256": replacements["previous_checksums_sha256"],
        "MOCK_PREVIOUS_RELEASE_JSON": json.dumps(release_payload),
        "MOCK_WHEEL_SOURCE": str(wheel_source),
        "MOCK_CHECKSUMS_SOURCE": str(checksums_source),
    }
    passing = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert passing.returncode == 0, passing.stderr
    shutil.rmtree(runner_temp)
    runner_temp.mkdir()
    if mutation == "release_id":
        release_payload["id"] = 999999
    else:
        release_payload["assets"][0]["id"] = 999998
    environment["MOCK_PREVIOUS_RELEASE_JSON"] = json.dumps(release_payload)
    completed = subprocess.run(
        [_release_bash(), "-c", _release_bash_script(script, environment)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0


def _rewrite_manifest(fixture: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> None:
    path = Path(fixture["manifest_path"])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_bytes(_json_bytes(manifest))


def test_win11_accepts_compatibility_product_name_but_rejects_server(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    assert _verify(fixture)["required_checks_passed"] is True
    _rewrite_report(
        fixture,
        "windows-11-x64",
        lambda report: report["execution"]["windows_target"].update(
            product_name="Windows Server 2025"
        ),
    )
    with pytest.raises(ValueError, match="product/version/build identity does not match"):
        _verify(fixture)

    future_fixture = _release_fixture(tmp_path / "future")
    _rewrite_report(
        future_fixture,
        "windows-11-x64",
        lambda report: report["execution"]["windows_target"].update(
            product_name="Windows 12 Pro",
            current_build_number=30000,
        ),
    )
    with pytest.raises(ValueError, match="product/version/build identity does not match"):
        _verify(future_fixture)


def test_native_x64_architecture_is_not_satisfied_by_emulation(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"]["windows_target"].update(
            native_machine_code=0xAA64,
            native_machine="ARM64",
            native_x64_verified=False,
        ),
    )
    with pytest.raises(ValueError, match="did not prove native Windows x64"):
        _verify(fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume_mm3", float("nan")),
        ("triangle_count", True),
        ("bottom_planarity_error_mm", 0.011),
    ],
)
def test_manufacturing_signature_rejects_nonfinite_bool_or_excess_planarity(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"]["core"]["builds"].update({field: value}),
    )
    with pytest.raises(ValueError, match=field):
        _verify(fixture)


def test_cross_platform_manufacturing_mismatch_fails_closed(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-11-x64",
        lambda report: report["execution"]["core"]["builds"].update(volume_mm3=99999.0),
    )
    with pytest.raises(ValueError, match="manufacturing signatures differ"):
        _verify(fixture)


def test_rollback_script_and_verification_are_mandatory(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    rollback_path = tmp_path / fixture["manifest"]["rollback"]["script_path"]
    rollback_path.unlink()
    with pytest.raises(FileNotFoundError, match="release source file is missing"):
        _verify(fixture)


def test_rollback_script_cannot_satisfy_gate_with_comment_fragments(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    rollback_path = tmp_path / fixture["manifest"]["rollback"]["script_path"]
    rollback_path.write_text(
        "#!/usr/bin/env bash\n"
        "# set -euo pipefail\n"
        "# git worktree add --detach $rollback_dir $previous_tag\n"
        "# current_commit=$(git rev-parse HEAD)\n"
        "exit 0\n",
        encoding="utf-8",
    )
    _rewrite_manifest(
        fixture,
        lambda manifest: manifest["rollback"].update(
            script_sha256=_sha256(rollback_path.read_bytes())
        ),
    )

    with pytest.raises(ValueError, match="exact generated canonical script"):
        _verify(fixture)


def test_rollback_producer_binds_later_release_commit_and_rejects_nonancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "TopoForge test")
    _git(repository, "config", "user.email", "test@topoforge.invalid")
    (repository / "previous.txt").write_text("0.10.3\n", encoding="utf-8")
    _git(repository, "add", "previous.txt")
    _git(repository, "commit", "-qm", "previous release")
    previous_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v0.10.3")

    script_path = repository / "scripts/rollback-topoforge-0.11.0.sh"
    script_path.parent.mkdir()
    script_path.write_bytes(canonical_rollback_script("0.11.0", "0.10.3"))
    evidence_root = repository / "release-evidence/0.11.0"
    evidence_root.mkdir(parents=True)
    (evidence_root / "public.json").write_text('{"candidate":true}\n', encoding="utf-8")
    _git(repository, "add", "scripts", "release-evidence")
    _git(repository, "commit", "-qm", "candidate source")
    source_commit = _git(repository, "rev-parse", "HEAD")
    (evidence_root / "manifest.json").write_text('{"release":true}\n', encoding="utf-8")
    _git(repository, "add", "release-evidence/0.11.0/manifest.json")
    _git(repository, "commit", "-qm", "release evidence")
    release_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v0.11.0")
    assert release_commit != source_commit
    current_wheel, current_wheel_sha256 = _write_minimal_release_wheel(
        tmp_path / "current-release",
        version="0.11.0",
    )
    previous_wheel, previous_wheel_sha256 = _write_minimal_release_wheel(
        tmp_path / "previous-release",
        version="0.10.3",
    )
    previous_checksums = previous_wheel.parent / "SHA256SUMS"
    previous_checksums.write_text(
        f"{previous_wheel_sha256}  {previous_wheel.name}\n",
        encoding="ascii",
    )
    previous_checksums_sha256 = _sha256(previous_checksums.read_bytes())

    def fake_install(
        _source_root: Path,
        *,
        wheel: Path,
        expected_wheel_sha256: str,
        version: str,
        work_root: Path,
        label: str,
    ) -> tuple[dict[str, Any], Path]:
        launcher_relative = (
            Path("Scripts/topoforge.cmd") if os.name == "nt" else Path("bin/topoforge")
        )
        launcher = work_root / f"{label}-environment" / launcher_relative
        launcher.parent.mkdir(parents=True)
        doctor_bytes = f'{{"topoforge":"{version}"}}\n'.encode()
        launcher_payload = (
            f'@echo off\r\necho {{"topoforge":"{version}"}}\r\n'
            if os.name == "nt"
            else f"#!/bin/sh\nprintf '%s\\n' '{{\"topoforge\":\"{version}\"}}'\n"
        )
        launcher.write_bytes(launcher_payload.encode("utf-8"))
        launcher.chmod(0o755)
        return (
            {
                "version": version,
                "wheel_filename": wheel.name,
                "wheel_sha256": expected_wheel_sha256,
                "launcher_relative_path": launcher_relative.as_posix(),
                "launcher_sha256": _sha256(launcher.read_bytes()),
                "doctor_output_sha256": _sha256(doctor_bytes),
                "doctor_exit_code": 0,
                "dependency_install_mode": (
                    "uv-lock-hashed-dependencies-plus-project-wheel-no-deps"
                ),
                "uv_lock_sha256": _sha256(f"{label}-lock".encode()),
                "locked_requirements_sha256": _sha256(f"{label}-requirements".encode()),
                "required_checks_passed": True,
            },
            launcher,
        )

    monkeypatch.setattr(rollback_verifier, "_build_and_verify_install", fake_install)
    report = rollback_verifier.generate_runtime_report(
        repository_root=repository,
        version="0.11.0",
        source_commit=source_commit,
        script_path=script_path,
        current_wheel=current_wheel,
        current_wheel_sha256=current_wheel_sha256,
        previous_wheel=previous_wheel,
        previous_wheel_sha256=previous_wheel_sha256,
        previous_checksums=previous_checksums,
        previous_checksums_sha256=previous_checksums_sha256,
        previous_release_id=710003,
        previous_release_published_at="2026-07-31T12:34:56Z",
        previous_wheel_asset_id=710004,
        previous_checksums_asset_id=710005,
        retained_evidence_root=evidence_root,
        work_root=tmp_path / "rollback-work",
    )
    assert report["source_commit"] == source_commit
    assert report["release_commit"] == release_commit
    assert report["source_checkout"]["previous_commit"] == previous_commit
    activation = report["installed_environment"]["activation"]
    launcher_relative = "Scripts/topoforge.cmd" if os.name == "nt" else "bin/topoforge"
    assert activation["atomic_pointer_switch"] is True
    assert activation["before_launcher_target"] == (f"current-environment/{launcher_relative}")
    assert activation["after_launcher_target"] == (f"previous-environment/{launcher_relative}")

    _git(repository, "checkout", "-qb", "unrelated", previous_commit)
    (repository / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(repository, "commit", "-qm", "unrelated candidate")
    unrelated_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "--detach", release_commit)
    with pytest.raises(ValueError, match="is not an ancestor"):
        rollback_verifier.generate_runtime_report(
            repository_root=repository,
            version="0.11.0",
            source_commit=unrelated_commit,
            script_path=script_path,
            current_wheel=current_wheel,
            current_wheel_sha256=current_wheel_sha256,
            previous_wheel=previous_wheel,
            previous_wheel_sha256=previous_wheel_sha256,
            previous_checksums=previous_checksums,
            previous_checksums_sha256=previous_checksums_sha256,
            previous_release_id=710003,
            previous_release_published_at="2026-07-31T12:34:56Z",
            previous_wheel_asset_id=710004,
            previous_checksums_asset_id=710005,
            retained_evidence_root=evidence_root,
            work_root=tmp_path / "nonancestor-work",
        )


def test_rollback_bash_paths_are_portable_to_git_for_windows() -> None:
    assert rollback_verifier._path_for_bash(Path(r"C:\TopoForge Work\rollback.sh")) == (
        "/c/TopoForge Work/rollback.sh"
    )
    assert rollback_verifier._path_for_bash(Path("/tmp/TopoForge Work/rollback.sh")) == (
        "/tmp/TopoForge Work/rollback.sh"
    )


def test_rollback_bash_selection_rejects_nonworking_system_placeholders() -> None:
    bash = rollback_verifier._working_bash()

    completed = subprocess.run(
        [bash, "-c", "exit 0"],
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0


def test_rollback_command_failure_retains_bounded_stderr(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="fixture rollback stderr"):
        rollback_verifier._run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('fixture rollback stderr'); sys.exit(3)",
            ],
            cwd=tmp_path,
        )


def test_rollback_release_wheel_rejects_tampered_bytes(tmp_path: Path) -> None:
    wheel, expected_sha256 = _write_minimal_release_wheel(tmp_path, version="0.11.0")
    wheel.write_bytes(wheel.read_bytes() + b"malicious trailing bytes")

    with pytest.raises(ValueError, match="SHA-256 differs"):
        rollback_verifier._validate_release_wheel(
            wheel,
            version="0.11.0",
            expected_sha256=expected_sha256,
        )


def test_rollback_release_wheel_rejects_wrong_metadata_version(tmp_path: Path) -> None:
    wheel, expected_sha256 = _write_minimal_release_wheel(
        tmp_path,
        version="0.11.0",
        metadata_version="0.10.3",
    )

    with pytest.raises(ValueError, match="METADATA name/version"):
        rollback_verifier._validate_release_wheel(
            wheel,
            version="0.11.0",
            expected_sha256=expected_sha256,
        )


def test_rollback_release_wheel_rejects_entry_point_substring_spoof(tmp_path: Path) -> None:
    wheel, expected_sha256 = _write_minimal_release_wheel(
        tmp_path,
        version="0.11.0",
        entry_points=(
            "[console_scripts]\nattacker = invalid.module:app # topoforge = topoforge.cli.app:app\n"
        ),
    )

    with pytest.raises(ValueError, match="console entry point"):
        rollback_verifier._validate_release_wheel(
            wheel,
            version="0.11.0",
            expected_sha256=expected_sha256,
        )


def test_rollback_release_wheel_rejects_noncanonical_archive_member(tmp_path: Path) -> None:
    wheel, _ = _write_minimal_release_wheel(tmp_path, version="0.11.0")
    with zipfile.ZipFile(wheel, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("./topoforge/attacker.py", "payload = True\n")

    with pytest.raises(ValueError, match="unsafe member"):
        rollback_verifier._validate_release_wheel(
            wheel,
            version="0.11.0",
            expected_sha256=_sha256(wheel.read_bytes()),
        )


def test_rollback_previous_checksums_rejects_duplicate_wheel_entry(tmp_path: Path) -> None:
    wheel, wheel_sha256 = _write_minimal_release_wheel(tmp_path, version="0.10.3")
    checksums = tmp_path / "SHA256SUMS"
    line = f"{wheel_sha256}  {wheel.name}\n"
    checksums.write_text(line + line, encoding="ascii")

    with pytest.raises(ValueError, match="not canonical"):
        rollback_verifier._validate_previous_checksums(
            checksums,
            expected_sha256=_sha256(checksums.read_bytes()),
            wheel_filename=wheel.name,
            wheel_sha256=wheel_sha256,
        )


def test_rollback_pinned_reader_preserves_consumer_oserror(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"bounded input")
    failure = OSError("injected consumer read failure")

    with (
        pytest.raises(OSError, match="injected consumer read failure") as captured,
        rollback_verifier._open_pinned_regular_file(
            source,
            label="test input",
            maximum_bytes=1024,
        ),
    ):
        raise failure

    assert captured.value is failure


def test_rollback_pinned_reader_accepts_path_handle_ctime_representation_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"bounded input")
    original_lstat = Path.lstat

    class CtimeView:
        def __init__(self, original: os.stat_result) -> None:
            self._original = original

        @property
        def st_ctime_ns(self) -> int:
            return self._original.st_ctime_ns - 1

        def __getattr__(self, name: str) -> Any:
            return getattr(self._original, name)

    def lstat_with_ctime_drift(self: Path) -> os.stat_result:
        result = original_lstat(self)
        if self == source:
            return CtimeView(result)  # type: ignore[return-value]
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_ctime_drift)

    with rollback_verifier._open_pinned_regular_file(
        source,
        label="test input",
        maximum_bytes=1024,
    ) as (handle, _information):
        assert handle.read() == b"bounded input"


def test_rollback_release_wheel_rejects_lexical_symlink_component(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    wheel, wheel_sha256 = _write_minimal_release_wheel(real_directory, version="0.11.0")
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows policy may prohibit test symlinks
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="link or reparse point"):
        rollback_verifier._validate_release_wheel(
            linked_directory / wheel.name,
            version="0.11.0",
            expected_sha256=wheel_sha256,
        )


def test_rollback_release_wheel_rejects_hardlink_input(tmp_path: Path) -> None:
    wheel, wheel_sha256 = _write_minimal_release_wheel(
        tmp_path / "original",
        version="0.11.0",
    )
    linked_directory = tmp_path / "hardlink"
    linked_directory.mkdir()
    linked_wheel = linked_directory / wheel.name
    os.link(wheel, linked_wheel)

    with pytest.raises(ValueError, match="single-link file"):
        rollback_verifier._validate_release_wheel(
            linked_wheel,
            version="0.11.0",
            expected_sha256=wheel_sha256,
        )


def test_rollback_main_preserves_lexical_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    try:
        linked_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows policy may prohibit test symlinks
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    lexical_wheel = linked_directory / "topoforge-0.11.0-py3-none-any.whl"
    captured: dict[str, Any] = {}

    def fake_generate_runtime_report(**arguments: Any) -> dict[str, Any]:
        captured.update(arguments)
        return {"required_checks_passed": True}

    monkeypatch.setattr(
        rollback_verifier,
        "generate_runtime_report",
        fake_generate_runtime_report,
    )
    monkeypatch.setattr(rollback_verifier, "_write_report", lambda _path, _report: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_release_rollback.py",
            "--version",
            "0.11.0",
            "--source-commit",
            "1" * 40,
            "--repository-root",
            str(tmp_path),
            "--script",
            str(linked_directory / "rollback.sh"),
            "--current-wheel",
            str(lexical_wheel),
            "--current-wheel-sha256",
            "2" * 64,
            "--previous-wheel",
            str(linked_directory / "topoforge-0.10.3-py3-none-any.whl"),
            "--previous-wheel-sha256",
            "3" * 64,
            "--previous-checksums",
            str(linked_directory / "SHA256SUMS"),
            "--previous-checksums-sha256",
            "4" * 64,
            "--previous-release-id",
            "1",
            "--previous-release-published-at",
            "2026-08-12T00:00:00Z",
            "--previous-wheel-asset-id",
            "2",
            "--previous-checksums-asset-id",
            "3",
            "--retained-evidence-root",
            str(linked_directory / "retained"),
            "--work-root",
            str(linked_directory / "work"),
            "--report",
            str(linked_directory / "report.json"),
        ],
    )

    assert rollback_verifier.main() == 0
    assert captured["current_wheel"] == rollback_verifier._absolute_lexical_path(lexical_wheel)
    assert captured["current_wheel"] != lexical_wheel.resolve()


def test_rollback_snapshot_cleanup_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.whl"
    source.write_bytes(b"validated wheel bytes")
    destination = tmp_path / "private" / "snapshot.whl"
    detached = tmp_path / "private" / "detached.whl"
    triggered = False
    swap_blocked = False

    def replace_snapshot_then_fail(_descriptor: int) -> None:
        nonlocal swap_blocked, triggered
        assert not triggered
        try:
            rollback_verifier.os.replace(destination, detached)
        except PermissionError:
            if os.name != "nt":
                raise
            swap_blocked = True
        else:
            destination.write_bytes(b"replacement owned by another actor")
        triggered = True
        raise OSError("injected snapshot fsync failure")

    monkeypatch.setattr(rollback_verifier.os, "fsync", replace_snapshot_then_fail)
    with pytest.raises(OSError, match="injected snapshot fsync failure"):
        rollback_verifier._snapshot_release_wheel(
            source,
            destination,
            expected_sha256=_sha256(source.read_bytes()),
        )

    assert triggered is True
    if os.name == "nt":
        assert swap_blocked is True
        assert not destination.exists()
        assert not detached.exists()
    else:
        assert swap_blocked is False
        assert destination.read_bytes() == b"replacement owned by another actor"
        assert detached.read_bytes() == b"validated wheel bytes"


def test_rollback_project_install_is_hash_bound_before_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    wheel, wheel_sha256 = _write_minimal_release_wheel(
        tmp_path / "release",
        version="0.11.0",
    )
    work_root = tmp_path / "work"
    work_root.mkdir()
    events: list[str] = []
    project_install: list[str] = []
    project_requirement = ""
    environment_root: Path | None = None
    real_validate = rollback_verifier._validate_release_wheel

    def record_validate(
        path: Path,
        *,
        version: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        events.append("validate")
        return real_validate(
            path,
            version=version,
            expected_sha256=expected_sha256,
        )

    def fake_run(
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str] | None = None,
        standard_input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        nonlocal environment_root, project_requirement
        if arguments[:2] == ["uv", "export"]:
            requirements = Path(arguments[arguments.index("--output-file") + 1])
            requirements.write_text(
                f"dependency==1 --hash=sha256:{'a' * 64}\n",
                encoding="ascii",
            )
        elif arguments[:2] == ["uv", "venv"]:
            environment_root = Path(arguments[-1])
            python = (
                environment_root / "Scripts" / "python.exe"
                if os.name == "nt"
                else environment_root / "bin" / "python"
            )
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
        elif (
            arguments[:3] == ["uv", "pip", "install"]
            and arguments[arguments.index("--requirements") + 1] == "-"
        ):
            events.append("project-install")
            project_install.extend(arguments)
            assert standard_input is not None
            project_requirement = standard_input
            assert environment_root is not None
            launcher = (
                environment_root / "Scripts" / "topoforge.exe"
                if os.name == "nt"
                else environment_root / "bin" / "topoforge"
            )
            launcher.write_bytes(b"verified launcher")
        elif arguments[-1:] == ["doctor"]:
            events.append("doctor")
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout='{"topoforge":"0.11.0"}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

    monkeypatch.setattr(rollback_verifier, "_validate_release_wheel", record_validate)
    monkeypatch.setattr(rollback_verifier, "_run", fake_run)
    rollback_verifier._build_and_verify_install(
        source_root,
        wheel=wheel,
        expected_wheel_sha256=wheel_sha256,
        version="0.11.0",
        work_root=work_root,
        label="current",
    )

    assert events == ["validate", "project-install", "validate", "doctor"]
    assert "--require-hashes" in project_install
    assert "--no-index" in project_install
    assert project_install[project_install.index("--requirements") + 1] == "-"
    assert project_requirement.startswith("topoforge @ file:")
    assert project_requirement.endswith(f" --hash=sha256:{wheel_sha256}\n")


def test_rollback_report_preserves_committed_publication_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "report.json"
    temporary = tmp_path / ".report.json.tmp"
    failure = rollback_verifier.EvidencePublicationError(
        destination=destination,
        temporary=temporary,
        committed=True,
        cause=OSError("injected durability failure"),
    )

    def fail_publication(_path: Path, _report: dict[str, Any]) -> None:
        raise failure

    monkeypatch.setattr(rollback_verifier, "write_canonical_json", fail_publication)
    with pytest.raises(rollback_verifier.EvidencePublicationError) as captured:
        rollback_verifier._write_report(destination, {"required_checks_passed": True})

    assert captured.value is failure
    assert captured.value.committed is True
    assert any("preserve the reported destination" in note for note in failure.__notes__)


def test_full_gate_rejects_wrong_current_rollback_artifact(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    runtime = json.loads(fixture["rollback_bytes"])
    runtime["release_artifacts"]["current"]["sha256"] = "e" * 64
    runtime["installed_environment"]["current"]["wheel_sha256"] = "e" * 64
    fixture["rollback_bytes"] = _json_bytes(runtime)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    linux_relative = fixture["manifest"]["cross_platform"]["linux_ci_relative_path"]
    (artifact_root / linux_relative).write_bytes(fixture["linux_bytes"])
    _write_runtime_artifacts(fixture, artifact_root)

    with pytest.raises(ValueError, match=r"release artifacts[.]current changed"):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda previous: previous.update(release_id=999999),
            "previous release artifact source identity changed",
        ),
        (
            lambda previous: previous.update(wheel_asset_id=999998),
            "previous release artifact source identity changed",
        ),
        (
            lambda previous: previous["checksums"].update(asset_id=999997),
            "previous release checksums changed",
        ),
    ],
)
def test_full_gate_rejects_forged_previous_github_identity(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    runtime = json.loads(fixture["rollback_bytes"])
    mutate(runtime["release_artifacts"]["previous"])
    fixture["rollback_bytes"] = _json_bytes(runtime)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    linux_relative = fixture["manifest"]["cross_platform"]["linux_ci_relative_path"]
    (artifact_root / linux_relative).write_bytes(fixture["linux_bytes"])
    _write_runtime_artifacts(fixture, artifact_root)

    with pytest.raises(ValueError, match=message):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


def test_installed_rollback_rejects_broken_console_launcher(tmp_path: Path) -> None:
    launcher_relative = Path("Scripts/topoforge.cmd" if os.name == "nt" else "bin/topoforge")
    current_launcher = tmp_path / "current-environment" / launcher_relative
    previous_launcher = tmp_path / "previous-environment" / launcher_relative
    for launcher, version in (
        (current_launcher, "0.11.0"),
        (previous_launcher, "0.10.3"),
    ):
        launcher.parent.mkdir(parents=True)
        succeeds = launcher == current_launcher
        payload = (
            (
                f'@echo off\r\necho {{"topoforge":"{version}"}}\r\n'
                if succeeds
                else "@echo off\r\nexit /b 9\r\n"
            )
            if os.name == "nt"
            else (
                f"#!/bin/sh\nprintf '%s\\n' '{{\"topoforge\":\"{version}\"}}'\n"
                if succeeds
                else "#!/bin/sh\nexit 9\n"
            )
        )
        launcher.write_text(payload, encoding="utf-8")
        launcher.chmod(0o755)

    with pytest.raises(RuntimeError, match="rollback evidence command failed"):
        rollback_verifier._verify_installed_switch(
            work_root=tmp_path,
            current_launcher=current_launcher,
            current_version="0.11.0",
            previous_launcher=previous_launcher,
            previous_version="0.10.3",
        )
    active = tmp_path / "active-installation" / f"topoforge{previous_launcher.suffix}"
    assert active.resolve() == previous_launcher.resolve()


def test_phase12b_binds_exact_single_bambu_signer(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path, version="0.11.1", include_bambu=True)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"]["bambu"]["bambu_studio"]["authenticode"].update(
            publisher_subject="CN=Different Publisher"
        ),
    )
    with pytest.raises(ValueError, match="signer identity changed at publisher_subject"):
        _verify(fixture)


def test_phase12b_accepts_current_content_identity_binding_shape(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path, version="0.11.1", include_bambu=True)
    report = json.loads(fixture["report_paths"]["windows-10-22h2-x64"].read_bytes())
    binding = report["execution"]["bambu"]["bambu_studio"]["profiles_root_binding"]
    assert "expected_profile_manifest_sha256" not in binding
    assert "profile_manifest_sha256_matched" not in binding
    assert binding["expected_profile_content_identity_sha256"] == (
        BAMBU_PROFILE_CONTENT_IDENTITY_SHA256
    )
    assert binding["profile_content_identity_sha256_matched"] is True
    assert _verify(fixture)["required_checks_passed"] is True


def test_phase12b_identity_must_come_from_candidate_source_policy(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path, version="0.11.1", include_bambu=True)
    policy_path = tmp_path / "packaging/bambu-studio-windows-identity-policy.json"
    policy = json.loads(policy_path.read_bytes())
    policy["allowed_identities"][0]["publisher_subject"] = "CN=Different Publisher"
    policy_path.write_bytes(_json_bytes(policy))

    with pytest.raises(ValueError, match="not allowed by the candidate source policy"):
        _verify(fixture)


def test_phase12b_policy_requires_unchanged_prior_approval_blob(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "TopoForge test")
    _git(tmp_path, "config", "user.email", "test@topoforge.invalid")
    policy_path = tmp_path / "packaging/bambu-studio-windows-identity-policy.json"
    policy_path.parent.mkdir()
    policy = {
        "allowed_identities": [copy.deepcopy(BAMBU_IDENTITY)],
        "note": "Independently approved before the release source commit.",
        "policy_status": "frozen",
        "required_checks_passed": True,
        "schema_version": "topoforge-bambu-windows-identity-policy-v1",
    }
    policy_path.write_bytes(_json_bytes(policy))
    _git(tmp_path, "add", policy_path.relative_to(tmp_path).as_posix())
    _git(tmp_path, "commit", "-qm", "approve policy")
    approval_commit = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "source.txt").write_text("candidate\n", encoding="utf-8")
    _git(tmp_path, "add", "source.txt")
    _git(tmp_path, "commit", "-qm", "candidate source")
    source_commit = _git(tmp_path, "rev-parse", "HEAD")

    result = _validate_bambu_identity_policy(
        root=tmp_path,
        expected_identity=copy.deepcopy(BAMBU_IDENTITY),
        source_commit=source_commit,
        approval_commit=approval_commit,
        require_tracked=True,
        release_commit=source_commit,
    )
    assert result["approval_commit"] == approval_commit

    policy["note"] = "Changed after approval."
    policy_path.write_bytes(_json_bytes(policy))
    _git(tmp_path, "add", policy_path.relative_to(tmp_path).as_posix())
    _git(tmp_path, "commit", "-qm", "change approved policy")
    changed_source = _git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="differs from its independent prior approval"):
        _validate_bambu_identity_policy(
            root=tmp_path,
            expected_identity=copy.deepcopy(BAMBU_IDENTITY),
            source_commit=changed_source,
            approval_commit=approval_commit,
            require_tracked=True,
            release_commit=changed_source,
        )


def test_clean_report_rejects_private_command_fields(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report.update(commands=[{"stdout": "private diagnostics"}]),
    )

    with pytest.raises(ValueError, match="retains private field"):
        _verify(fixture)


def test_clean_report_rejects_user_profile_paths(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"].update(
            extraction_path="C:/Users/Alice/TopoForge Evidence/地形"
        ),
    )

    with pytest.raises(ValueError, match="retains a private machine path"):
        _verify(fixture)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("operator_diagnostic", "Bearer SUPER-SECRET-TOKEN", "secret-like text"),
        ("operator_github", "ghp_0123456789abcdefghijklmnopqrstuvwxyz", "secret-like text"),
        (
            "operator_fine_grained_github",
            "github_pat_0123456789_abcdefghijklmnopqrstuvwxyz",
            "secret-like text",
        ),
        ("operator_aws", "AKIA0123456789ABCDEF", "secret-like text"),
        (
            "operator_private_key",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "secret-like text",
        ),
        ("operator_path", "D:/Customer-Alice/secret/log.txt", "unknown absolute path"),
        ("operator_unc", r"\\customer-host\private\log.txt", "private machine path"),
        ("operator_uri", "https://private.example.invalid/log", "non-loopback URI"),
    ],
)
def test_clean_report_rejects_unknown_operator_leakage(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"].update({field: value}),
    )

    with pytest.raises(ValueError, match=message):
        _verify(fixture)


def test_phase12b_rejects_profile_identity_drift_between_clean_targets(
    tmp_path: Path,
) -> None:
    fixture = _release_fixture(tmp_path, version="0.11.1", include_bambu=True)
    _rewrite_report(
        fixture,
        "windows-11-x64",
        lambda report: report["execution"]["bambu"]["bambu_studio"]["profiles_root_binding"][
            "resolved_profiles"
        ]["process"].update(sha256="0" * 64),
    )
    with pytest.raises(ValueError, match="resolved process profile differs"):
        _verify(fixture)


def test_phase12b_rejects_non_sibling_profile_override(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path, version="0.11.1", include_bambu=True)
    _rewrite_report(
        fixture,
        "windows-10-22h2-x64",
        lambda report: report["execution"]["bambu"]["bambu_studio"]["profiles_root_binding"].update(
            path="D:/Modified Profiles/BBL",
            selection_mode="explicit-cli-override",
            is_executable_sibling=False,
            override_requested=True,
            override_authorized_by_frozen_hashes=True,
        ),
    )
    with pytest.raises(ValueError, match="not the signed executable sibling"):
        _verify(fixture)


def test_full_gate_rejects_release_commit_instead_of_candidate_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    manifest = fixture["manifest"]
    (artifact_root / manifest["candidate_artifact"]["archive_relative_path"]).write_bytes(
        fixture["archive_payload"]
    )
    hosted = artifact_root / manifest["candidate_artifact"]["verification_relative_path"]
    hosted.parent.mkdir(parents=True)
    hosted.write_bytes(fixture["hosted_bytes"])
    linux = artifact_root / manifest["cross_platform"]["linux_ci_relative_path"]
    linux.parent.mkdir(parents=True, exist_ok=True)
    linux.write_bytes(fixture["linux_bytes"])
    _write_runtime_artifacts(fixture, artifact_root)
    monkeypatch.setattr(
        portable_verifier,
        "inspect_windows_portable",
        lambda *_args, **_kwargs: {
            "provenance": {
                "source_commit": "2" * 40,
                "source_dirty": False,
                "source_tracked_dirty": False,
                "config_sha256": fixture["config_sha256"],
                "build_constraints_sha256": fixture["build_constraints_sha256"],
                "verifier_sha256": fixture["verifier_sha256"],
            }
        },
    )
    with pytest.raises(ValueError, match="source_commit changed"):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda report: report.update(schema_version="old-portable-schema"),
            "hosted portable report schema is unsupported",
        ),
        (
            lambda report: report["execution"]["system"].update(schema_version="old-system-schema"),
            "hosted system report schema is unsupported",
        ),
    ],
)
def test_hosted_report_rejects_old_report_schemas(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    hosted = json.loads(fixture["hosted_bytes"])
    mutation(hosted)

    with pytest.raises(ValueError, match=message):
        _validate_hosted_report(
            hosted,
            version=fixture["version"],
            source_commit=SOURCE_COMMIT,
            binding=fixture["manifest"]["portable_archive"],
        )


def test_hosted_report_cannot_claim_clean_client_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _release_fixture(tmp_path / "repository")
    hosted = json.loads(fixture["hosted_bytes"])
    hosted["execution"]["windows_target"]["target_verified"] = True
    hosted_bytes = _json_bytes(hosted)
    fixture["hosted_bytes"] = hosted_bytes
    _rewrite_manifest(
        fixture,
        lambda manifest: manifest["candidate_artifact"].update(
            verification_sha256=_sha256(hosted_bytes)
        ),
    )
    fixture["manifest"] = json.loads(Path(fixture["manifest_path"]).read_text())
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    manifest = fixture["manifest"]
    (artifact_root / manifest["candidate_artifact"]["archive_relative_path"]).write_bytes(
        fixture["archive_payload"]
    )
    hosted_path = artifact_root / manifest["candidate_artifact"]["verification_relative_path"]
    hosted_path.parent.mkdir(parents=True)
    hosted_path.write_bytes(hosted_bytes)
    linux = artifact_root / manifest["cross_platform"]["linux_ci_relative_path"]
    linux.parent.mkdir(parents=True, exist_ok=True)
    linux.write_bytes(fixture["linux_bytes"])
    _write_runtime_artifacts(fixture, artifact_root)
    monkeypatch.setattr(portable_verifier, "inspect_windows_portable", lambda *_a, **_k: {})
    with pytest.raises(ValueError, match="must not claim a clean Windows client target"):
        _verify(fixture, metadata_only=False, artifact_root=artifact_root)


def test_tracked_file_bytes_rejects_dirty_local_copy(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "TopoForge test")
    _git(tmp_path, "config", "user.email", "test@topoforge.invalid")
    path = tmp_path / "scripts" / "verifier.py"
    path.parent.mkdir()
    path.write_text("SOURCE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "scripts/verifier.py")
    _git(tmp_path, "commit", "-qm", "candidate")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    path.write_text("SOURCE = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"differs from the .* Git blob"):
        _tracked_file_bytes(
            tmp_path,
            PurePosixPath("scripts/verifier.py"),
            release_commit=commit,
        )
