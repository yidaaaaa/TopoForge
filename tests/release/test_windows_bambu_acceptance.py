from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.verify_windows_bambu as bambu_verifier
from scripts.verify_windows_bambu import (
    _authenticode_record,
    _bambu_override,
    verify_windows_bambu,
)


def test_native_bambu_acceptance_refuses_non_windows_before_creating_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bambu_verifier.platform, "system", lambda: "Linux")
    work_root = tmp_path / "must not be created"

    with pytest.raises(RuntimeError, match="native Windows"):
        verify_windows_bambu(
            work_root,
            expected_target="win10-22h2",
        )

    assert not work_root.exists()


def test_bambu_executable_override_restores_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "TOPOFORGE_BAMBU_STUDIO"
    monkeypatch.setenv(key, "original.exe")
    executable = tmp_path / "Bambu Studio" / "bambu-studio.exe"

    with _bambu_override(executable):
        assert bambu_verifier.os.environ[key] == str(executable)

    assert bambu_verifier.os.environ[key] == "original.exe"


def test_bambu_acceptance_main_retains_failure_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "failure report.json"

    def fail(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("synthetic official Bambu failure")

    monkeypatch.setattr(bambu_verifier, "verify_windows_bambu", fail)
    monkeypatch.setattr(
        bambu_verifier.sys,
        "argv",
        [
            "verify_windows_bambu.py",
            "--work-root",
            str(tmp_path / "work root"),
            "--report",
            str(report_path),
        ],
    )

    assert bambu_verifier.main() == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "topoforge-windows-bambu-verification-v2"
    assert report["error"]["type"] == "RuntimeError"
    assert report["error"]["message"] == "synthetic official Bambu failure"
    assert report["required_checks_passed"] is False


def test_bambu_report_ignores_legacy_fixed_temp_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "bambu-report.json"
    external = tmp_path / "external.txt"
    external.write_text("preserve\n", encoding="utf-8")
    legacy_temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        legacy_temporary.symlink_to(external)
    except OSError:
        pytest.skip("host cannot create symlink fixture")

    report = {"z": "地形", "a": 1}
    bambu_verifier._write_report(destination, report)

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert legacy_temporary.is_symlink()
    expected = '{\n  "a": 1,\n  "z": "地形"\n}\n'.encode()
    assert destination.read_bytes() == expected
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_bambu_report_replace_failure_preserves_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "bambu-report.json"
    destination.write_text("previous evidence\n", encoding="utf-8")

    def fail_replace(_source: str | Path, _target: str | Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(bambu_verifier._windows_evidence.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        bambu_verifier._write_report(destination, {"replacement": True})

    assert destination.read_text(encoding="utf-8") == "previous evidence\n"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_bambu_acceptance_contract_reuses_normative_workflow_gates() -> None:
    source = (Path(__file__).parents[2] / "scripts" / "verify_windows_bambu.py").read_text(
        encoding="utf-8"
    )

    assert "project_evidence_enabled=True" in source
    assert "verify_bambu_project_evidence" in source
    assert '"external_profiles_loaded_on_reopen"' in source
    assert '"--require-windows"' in source
    assert '"--expected-profile-content-identity-sha256"' in source
    assert '"--expected-profile-manifest-sha256"' not in source
    assert '"--expected-machine-profile-sha256"' in source
    assert '"--expected-process-profile-sha256"' in source
    assert '"--expected-filament-profile-sha256"' in source
    assert '"profiles_root_binding"' in source
    assert "official Bambu Studio software slice/export/reopen/reslice evidence" in source


def _valid_signature() -> dict[str, object]:
    return {
        "Status": "Valid",
        "StatusMessage": "Signature verified.",
        "Subject": "CN=Bambu Lab, O=Bambu Lab",
        "Thumbprint": "A" * 40,
        "NotBefore": "2026-01-01T00:00:00Z",
        "NotAfter": "2027-01-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("subjects", "thumbprints"),
    [
        ((), ()),
        (("CN=Bambu Lab, O=Bambu Lab",), ()),
        ((), ("A" * 40,)),
        (("CN=Bambu Lab, O=Bambu Lab",) * 2, ("A" * 40,)),
    ],
)
def test_authenticode_requires_one_frozen_subject_and_thumbprint(
    tmp_path: Path,
    subjects: tuple[str, ...],
    thumbprints: tuple[str, ...],
) -> None:
    with pytest.raises(RuntimeError, match="exactly one operator-frozen"):
        _authenticode_record(
            tmp_path / "bambu-studio.exe",
            expected_publisher_subjects=subjects,
            expected_thumbprints=thumbprints,
        )


def test_authenticode_accepts_valid_matching_subject_and_thumbprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bambu-studio.exe"
    executable.write_bytes(b"signed executable")
    monkeypatch.setattr(
        bambu_verifier,
        "_read_authenticode_signature",
        lambda _: _valid_signature(),
    )

    report = _authenticode_record(
        executable,
        expected_publisher_subjects=("CN=Bambu Lab, O=Bambu Lab",),
        expected_thumbprints=("aa aa aa aa aa aa aa aa aa aa aa aa aa aa aa aa aa aa aa aa",),
    )

    assert report["status"] == "Valid"
    assert report["publisher_subject_matched"] is True
    assert report["certificate_thumbprint_matched"] is True
    assert report["operator_identity_frozen"] is True
    assert report["expected_publisher_subjects"] == ["CN=Bambu Lab, O=Bambu Lab"]
    assert report["expected_certificate_thumbprints"] == ["A" * 40]


@pytest.mark.parametrize(
    ("signature", "subjects", "thumbprints", "message"),
    [
        (
            {**_valid_signature(), "Status": "NotSigned"},
            ("CN=Bambu Lab, O=Bambu Lab",),
            ("A" * 40,),
            "not Valid",
        ),
        (
            _valid_signature(),
            ("CN=Unrelated Publisher",),
            ("A" * 40,),
            "does not match",
        ),
        (
            _valid_signature(),
            ("CN=Bambu Lab, O=Bambu Lab",),
            ("B" * 40,),
            "does not match",
        ),
    ],
)
def test_authenticode_rejects_unsigned_or_mismatched_binary(
    tmp_path: Path,
    signature: dict[str, object],
    subjects: tuple[str, ...],
    thumbprints: tuple[str, ...],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bambu_verifier,
        "_read_authenticode_signature",
        lambda _: signature,
    )
    executable = tmp_path / "bambu-studio.exe"
    executable.write_bytes(b"candidate executable")

    with pytest.raises(RuntimeError, match=message):
        _authenticode_record(
            executable,
            expected_publisher_subjects=subjects,
            expected_thumbprints=thumbprints,
        )


def test_authenticode_rejects_executable_swap_during_signature_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bambu-studio.exe"
    executable.write_bytes(b"signed candidate")

    def swap(_: Path) -> dict[str, object]:
        executable.write_bytes(b"replacement binary")
        return _valid_signature()

    monkeypatch.setattr(bambu_verifier, "_read_authenticode_signature", swap)

    with pytest.raises(RuntimeError, match="changed during Authenticode"):
        _authenticode_record(
            executable,
            expected_publisher_subjects=("CN=Bambu Lab, O=Bambu Lab",),
            expected_thumbprints=("A" * 40,),
        )


def test_authenticode_gate_precedes_bambu_version_probe() -> None:
    source = (Path(__file__).parents[2] / "scripts" / "verify_windows_bambu.py").read_text(
        encoding="utf-8"
    )
    verify_body = source[source.index("def verify_windows_bambu(") :]

    assert verify_body.index("_authenticode_record(") < verify_body.index("_resolve_installation(")


def test_clean_bambu_target_requires_portable_candidate_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bambu_verifier,
        "windows_target_record",
        lambda *_args, **_kwargs: {
            "target_id": "windows-10-22h2-x64",
            "target_verified": True,
        },
    )
    work_root = tmp_path / "must not be created without binding"

    with pytest.raises(RuntimeError, match="--candidate-binding"):
        verify_windows_bambu(
            work_root,
            expected_target="win10-22h2",
        )

    assert not work_root.exists()


def _prepared_profile_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    bambu_verifier.PreparedBambuProfiles,
    dict[str, str | None],
]:
    executable = tmp_path / "Bambu Studio" / "bambu-studio.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"official signed Bambu binary fixture")
    profiles_root = executable.parent / "resources" / "profiles" / "BBL"
    bundle_dir = tmp_path / "profile-cache" / "bundle"
    bundle_dir.mkdir(parents=True)

    profiles: dict[str, dict[str, object]] = {}
    prepared_paths: dict[str, Path] = {}
    for kind in ("machine", "process", "filament"):
        source_path = profiles_root / kind / f"{kind}-source.json"
        source_path.parent.mkdir(parents=True)
        source_path.write_text(
            json.dumps({"name": f"{kind} source"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        resolved_path = bundle_dir / f"{kind}.json"
        resolved_path.write_text(
            json.dumps({"name": f"resolved {kind}"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        prepared_paths[kind] = resolved_path
        profiles[kind] = {
            "name": f"resolved {kind}",
            "resolved_path": f"{kind}.json",
            "resolved_sha256": bambu_verifier.sha256_file(resolved_path),
            "resolved_size_bytes": resolved_path.stat().st_size,
            "sources": [
                {
                    "kind": kind,
                    "name": f"{kind} source",
                    "path": f"{kind}/{kind}-source.json",
                    "sha256": bambu_verifier.sha256_file(source_path),
                    "size_bytes": source_path.stat().st_size,
                }
            ],
        }

    executable_record = {
        **bambu_verifier._file_record(executable),
        "version": "2.3.4",
    }
    identity = {
        "schema_version": "topoforge-bambu-profile-bundle-v1",
        "source_root": str(profiles_root.resolve()),
        "executable": executable_record,
        "profiles": profiles,
    }
    profile_content_identity = {
        "schema_version": identity["schema_version"],
        "executable": {
            "sha256": executable_record["sha256"],
            "size_bytes": executable_record["size_bytes"],
            "version": executable_record["version"],
        },
        "profiles": profiles,
    }
    manifest = {
        **identity,
        "bundle_id": bambu_verifier._canonical_sha256(identity),
        "required_checks_passed": True,
    }
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    prepared = bambu_verifier.PreparedBambuProfiles(
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        manifest_sha256=bambu_verifier.sha256_file(manifest_path),
        machine_profile=prepared_paths["machine"],
        process_profile=prepared_paths["process"],
        filament_profile=prepared_paths["filament"],
    )
    expectations: dict[str, str | None] = {
        "content_identity": bambu_verifier._canonical_sha256(profile_content_identity),
        "machine": bambu_verifier.sha256_file(prepared.machine_profile),
        "process": bambu_verifier.sha256_file(prepared.process_profile),
        "filament": bambu_verifier.sha256_file(prepared.filament_profile),
    }
    return executable, profiles_root, prepared, expectations


def test_profile_hash_expectations_require_complete_frozen_set() -> None:
    digest = "A" * 64
    expected = bambu_verifier._profile_hash_expectations(
        content_identity_sha256=digest,
        machine_sha256=digest,
        process_sha256=digest,
        filament_sha256=digest,
        require_frozen=True,
    )

    assert expected == {
        "content_identity": "a" * 64,
        "machine": "a" * 64,
        "process": "a" * 64,
        "filament": "a" * 64,
    }

    with pytest.raises(RuntimeError, match="all four frozen hashes"):
        bambu_verifier._profile_hash_expectations(
            content_identity_sha256=digest,
            machine_sha256=None,
            process_sha256=digest,
            filament_sha256=digest,
            require_frozen=False,
        )


def test_profile_bundle_binds_official_sibling_and_source_records(
    tmp_path: Path,
) -> None:
    executable, profiles_root, prepared, expectations = _prepared_profile_fixture(tmp_path)

    binding = bambu_verifier._profile_bundle_binding(
        executable=executable,
        executable_version="2.3.4",
        profiles_root=profiles_root,
        prepared=prepared,
        expectations=expectations,
        selection_mode="executable-sibling-discovery",
        require_executable_sibling=True,
    )

    assert binding["is_executable_sibling"] is True
    assert binding["relative_to_executable"] == "resources/profiles/BBL"
    assert binding["profile_identity_frozen"] is True
    assert binding["profile_content_identity_sha256_matched"] is True
    assert "expected_profile_manifest_sha256" not in binding
    assert "profile_manifest_sha256_matched" not in binding
    assert len(binding["profile_manifest_sha256"]) == 64
    assert len(binding["profile_content_identity_sha256"]) == 64
    assert binding["resolved_profiles"]["machine"]["sha256_matched"] is True
    assert binding["source_records"]["machine"][0]["path"].startswith("machine/")
    assert len(binding["source_records_sha256"]) == 64
    assert len(binding["source_root_identity_sha256"]) == 64


def test_profile_content_identity_is_independent_of_absolute_install_path(
    tmp_path: Path,
) -> None:
    first = _prepared_profile_fixture(tmp_path / "first installation")
    second = _prepared_profile_fixture(tmp_path / "second installation")

    assert first[3] == second[3]
    first_binding = bambu_verifier._profile_bundle_binding(
        executable=first[0],
        executable_version="2.3.4",
        profiles_root=first[1],
        prepared=first[2],
        expectations=first[3],
        selection_mode="executable-sibling-discovery",
        require_executable_sibling=True,
    )
    second_binding = bambu_verifier._profile_bundle_binding(
        executable=second[0],
        executable_version="2.3.4",
        profiles_root=second[1],
        prepared=second[2],
        expectations=first[3],
        selection_mode="executable-sibling-discovery",
        require_executable_sibling=True,
    )

    assert first_binding["profile_manifest_sha256"] != second_binding["profile_manifest_sha256"]
    assert (
        first_binding["profile_content_identity_sha256"]
        == second_binding["profile_content_identity_sha256"]
    )
    assert (
        second_binding["expected_profile_content_identity_sha256"] == first[3]["content_identity"]
    )
    assert second_binding["profile_content_identity_sha256_matched"] is True
    assert (
        first_binding["source_root_identity_sha256"]
        == second_binding["source_root_identity_sha256"]
    )


def test_profile_bundle_rejects_mismatched_content_identity(tmp_path: Path) -> None:
    executable, profiles_root, prepared, expectations = _prepared_profile_fixture(tmp_path)

    mismatched_expectations = {
        **expectations,
        "content_identity": "0" * 64,
    }

    with pytest.raises(RuntimeError, match="profile content identity"):
        bambu_verifier._profile_bundle_binding(
            executable=executable,
            executable_version="2.3.4",
            profiles_root=profiles_root,
            prepared=prepared,
            expectations=mismatched_expectations,
            selection_mode="executable-sibling-discovery",
            require_executable_sibling=True,
        )


def test_profile_bundle_rejects_unfrozen_override_and_non_sibling(
    tmp_path: Path,
) -> None:
    executable, profiles_root, prepared, expectations = _prepared_profile_fixture(tmp_path)

    with pytest.raises(RuntimeError, match="override is allowed only"):
        bambu_verifier._profile_bundle_binding(
            executable=executable,
            executable_version="2.3.4",
            profiles_root=profiles_root,
            prepared=prepared,
            expectations={
                "content_identity": None,
                "machine": None,
                "process": None,
                "filament": None,
            },
            selection_mode="explicit-cli-override",
            require_executable_sibling=False,
        )

    other_executable = tmp_path / "Other Bambu Studio" / "bambu-studio.exe"
    other_executable.parent.mkdir()
    other_executable.write_bytes(executable.read_bytes())
    with pytest.raises(RuntimeError, match="authenticated executable sibling"):
        bambu_verifier._profile_bundle_binding(
            executable=other_executable,
            executable_version="2.3.4",
            profiles_root=profiles_root,
            prepared=prepared,
            expectations=expectations,
            selection_mode="explicit-cli-override",
            require_executable_sibling=True,
        )


def test_profile_bundle_rechecks_source_record_files(tmp_path: Path) -> None:
    executable, profiles_root, prepared, expectations = _prepared_profile_fixture(tmp_path)
    source_path = profiles_root / "machine" / "machine-source.json"
    source_path.write_text('{"name":"tampered"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="source profile changed"):
        bambu_verifier._profile_bundle_binding(
            executable=executable,
            executable_version="2.3.4",
            profiles_root=profiles_root,
            prepared=prepared,
            expectations=expectations,
            selection_mode="executable-sibling-discovery",
            require_executable_sibling=True,
        )
