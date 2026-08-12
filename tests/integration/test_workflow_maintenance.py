from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig, SamplingMode
from topoforge.overlays import (
    OverlayConfig,
    OverlayFormat,
    OverlayKind,
    OverlaySourceConfig,
)
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.web.security import CommittedStateUncertainError
from topoforge.workflow import (
    LocalWorkflowResult,
    WorkflowLaunchConfig,
    apply_workflow_cleanup,
    create_workflow_backup,
    execute_workflow_launch,
    inspect_workflow_workspace,
    plan_workflow_cleanup,
    publish_workflow_summary,
    read_workflow_launch_config,
    restore_workflow_backup,
    verify_workflow_backup,
    write_workflow_launch_config,
)
from topoforge.workflow import maintenance as workflow_maintenance
from topoforge.workflow import ux as workflow_ux

runner = CliRunner()


@pytest.fixture(scope="module")
def completed_workflow_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("workflow-maintenance-security")
    source = create_synthetic_geotiff(
        root / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=10,
        columns=12,
        pixel_size_m=20.0,
    )
    workspace = root / "workflow"
    launch = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
        ),
        maximum_tile_width_mm=100.0,
        maximum_tile_depth_mm=100.0,
        slicing_enabled=False,
    )
    execute_workflow_launch(launch)
    create_workflow_backup(workspace, root / "completed-workflow.zip")
    return workspace


def _copy_completed_workspace(template: Path, destination: Path) -> Path:
    workspace = destination / "workflow"
    restore_workflow_backup(
        template.parent / "completed-workflow.zip",
        workspace,
    )
    return workspace


def _copy_completed_backup(template: Path, destination: Path) -> Path:
    archive = destination / "completed-workflow.zip"
    shutil.copyfile(template.parent / "completed-workflow.zip", archive)
    return archive


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_canonical_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _build_directory(workspace: Path) -> Path:
    workflow_manifest = _json_object(workspace / "workflow-manifest.json")
    build_record = next(
        record for record in workflow_manifest["stages"] if record["name"] == "build"
    )
    return workspace / build_record["output_path"]


def _reseal_build_stage(workspace: Path, artifact_role: str) -> None:
    build_dir = _build_directory(workspace)
    build_manifest_path = build_dir / "build_manifest.json"
    build_manifest = _json_object(build_manifest_path)
    artifact_name = build_manifest["artifacts"][artifact_role]
    build_manifest["sha256"][artifact_role] = sha256_file(build_dir / artifact_name)
    _write_canonical_json(build_manifest_path, build_manifest)

    workflow_manifest_path = workspace / "workflow-manifest.json"
    workflow_manifest = _json_object(workflow_manifest_path)
    build_record = next(
        record for record in workflow_manifest["stages"] if record["name"] == "build"
    )
    build_record["manifest_sha256"] = sha256_file(build_manifest_path)
    _write_canonical_json(workflow_manifest_path, workflow_manifest)


def test_cleanup_rejects_link_like_stage_identity_without_following_targets(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    family = workspace / "stages" / "10-build"
    external_target = tmp_path / "preserved-external"
    external_target.mkdir()
    (external_target / "marker.bin").write_bytes(b"external")
    link = family / "external-link"
    try:
        link.symlink_to(external_target, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create workflow cleanup symlink fixtures")

    with pytest.raises(ConfigurationError, match="link-like component"):
        plan_workflow_cleanup(workspace)

    assert link.is_symlink()
    assert (external_target / "marker.bin").read_bytes() == b"external"


def test_active_stage_symlink_swap_blocks_inspect_and_cleanup_without_deleting_target(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    build_dir = _build_directory(workspace)
    moved = build_dir.with_name(f"{build_dir.name}-moved")
    build_dir.rename(moved)
    marker = moved / "do-not-delete.marker"
    marker.write_bytes(b"preserve")
    try:
        build_dir.symlink_to(moved, target_is_directory=True)
    except OSError:
        moved.rename(build_dir)
        pytest.skip("host cannot create active-stage symlink fixture")

    with pytest.raises(ConfigurationError, match="link-like component"):
        inspect_workflow_workspace(workspace)
    with pytest.raises(ConfigurationError, match="link-like component"):
        plan_workflow_cleanup(workspace)

    assert build_dir.is_symlink()
    assert marker.read_bytes() == b"preserve"


@pytest.mark.parametrize("change", ["add", "expand"])
def test_cleanup_old_plan_id_rejects_changed_candidate_set_without_deleting_anything(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    change: str,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    family = workspace / "stages" / "10-build"
    first = family / "stale-first"
    first.mkdir()
    first_marker = first / "marker.bin"
    first_marker.write_bytes(b"first")
    plan = plan_workflow_cleanup(workspace)
    assert any(candidate.path.endswith("stale-first") for candidate in plan.candidates)

    if change == "add":
        changed = family / "stale-second"
        changed.mkdir()
        changed_marker = changed / "marker.bin"
        changed_marker.write_bytes(b"second")
    else:
        changed = first
        changed_marker = first / "larger.bin"
        changed_marker.write_bytes(b"expanded after review")

    with pytest.raises(ConfigurationError, match="cleanup plan changed after review"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=plan.workflow_id,
            confirm_plan_id=plan.plan_id,
        )

    assert first_marker.read_bytes() == b"first"
    assert changed_marker.is_file()


def test_cleanup_plan_binds_same_size_file_content(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    stale = workspace / "stages" / "10-build" / "stale-same-size"
    stale.mkdir()
    marker = stale / "marker.bin"
    marker.write_bytes(b"AAAA")
    reviewed = plan_workflow_cleanup(workspace)

    marker.write_bytes(b"BBBB")
    changed = plan_workflow_cleanup(workspace)

    assert changed.plan_id != reviewed.plan_id
    with pytest.raises(ConfigurationError, match="cleanup plan changed after review"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=reviewed.workflow_id,
            confirm_plan_id=reviewed.plan_id,
        )
    assert marker.read_bytes() == b"BBBB"


def test_cleanup_quarantine_ignores_directory_allocation_size_drift(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    stale = workspace / "stages" / "10-build" / "stale-directory-size"
    stale.mkdir()
    marker = stale / "marker.bin"
    marker.write_bytes(b"content-bound")
    reviewed = plan_workflow_cleanup(workspace)
    original_lstat = Path.lstat

    class DirectorySizeView:
        def __init__(self, original: os.stat_result) -> None:
            self._original = original

        @property
        def st_size(self) -> int:
            return self._original.st_size + 8

        def __getattr__(self, name: str) -> Any:
            return getattr(self._original, name)

    def lstat_with_relocated_directory_size(self: Path) -> os.stat_result:
        result = original_lstat(self)
        if self.name == stale.name and any(
            part.startswith(".topoforge-cleanup-") for part in self.parts
        ):
            return DirectorySizeView(result)  # type: ignore[return-value]
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_relocated_directory_size)
    applied = apply_workflow_cleanup(
        workspace,
        confirm_workflow_id=reviewed.workflow_id,
        confirm_plan_id=reviewed.plan_id,
    )

    assert applied.removed_paths == ("stages/10-build/stale-directory-size",)
    assert not stale.exists()
    assert not list(workspace.glob(".topoforge-cleanup-*"))


def test_cleanup_rejects_hard_link_in_active_stage_without_touching_target(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    external = tmp_path / "external-hardlink-target.bin"
    external.write_bytes(b"preserve")
    hard_link = _build_directory(workspace) / "untrusted-hardlink.bin"
    try:
        os.link(external, hard_link)
    except OSError:
        pytest.skip("host cannot create workflow cleanup hard-link fixtures")

    with pytest.raises(ConfigurationError, match="hard-linked"):
        plan_workflow_cleanup(workspace)

    assert hard_link.read_bytes() == b"preserve"
    assert external.read_bytes() == b"preserve"


def test_cleanup_rolls_back_all_candidates_when_quarantine_move_fails(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    family = workspace / "stages" / "10-build"
    first = family / "stale-first"
    second = family / "stale-second"
    first.mkdir()
    second.mkdir()
    first_marker = first / "marker.bin"
    second_marker = second / "marker.bin"
    first_marker.write_bytes(b"first")
    second_marker.write_bytes(b"second")
    plan = plan_workflow_cleanup(workspace)
    original_move = workflow_maintenance.move_owned_path

    def fail_second_move(source: Path, destination: Path, **kwargs: Any) -> None:
        if source == second:
            raise OSError("injected quarantine move failure")
        original_move(source, destination, **kwargs)

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_second_move)
    with pytest.raises(ConfigurationError, match="parent or identity changed"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=plan.workflow_id,
            confirm_plan_id=plan.plan_id,
        )

    assert first_marker.read_bytes() == b"first"
    assert second_marker.read_bytes() == b"second"
    assert not list(workspace.glob(".topoforge-cleanup-*"))


def test_cleanup_rolls_back_a_move_that_raises_after_native_rename(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    stale = workspace / "stages" / "10-build" / "stale-post-rename"
    stale.mkdir()
    marker = stale / "marker.bin"
    marker.write_bytes(b"preserve after committed move")
    plan = plan_workflow_cleanup(workspace)
    original_move = workflow_maintenance.move_owned_path
    injected = False

    def fail_after_move(source: Path, destination: Path, **kwargs: Any) -> None:
        nonlocal injected
        original_move(source, destination, **kwargs)
        if source == stale and not injected:
            injected = True
            raise ValueError("injected post-rename parent check failure")

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_move)

    with pytest.raises(ConfigurationError, match="parent or identity changed"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=plan.workflow_id,
            confirm_plan_id=plan.plan_id,
        )

    assert injected is True
    assert marker.read_bytes() == b"preserve after committed move"
    assert not list(workspace.glob(".topoforge-cleanup-*"))


def test_cleanup_does_not_downgrade_committed_durability_uncertainty(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    stale = workspace / "stages" / "10-build" / "stale-uncertain"
    stale.mkdir()
    marker = stale / "marker.bin"
    marker.write_bytes(b"preserve after uncertain move")
    plan = plan_workflow_cleanup(workspace)
    original_move = workflow_maintenance.move_owned_path
    injected = False

    def fail_after_move(source: Path, destination: Path, **kwargs: Any) -> None:
        nonlocal injected
        original_move(source, destination, **kwargs)
        if source == stale and not injected:
            injected = True
            raise CommittedStateUncertainError(
                operation="move",
                path=destination,
                context="injected cleanup move",
                cause=OSError("injected parent fsync failure"),
            )

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_move)
    with pytest.raises(ConfigurationError, match="durability is uncertain"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=plan.workflow_id,
            confirm_plan_id=plan.plan_id,
        )

    assert injected is True
    assert marker.read_bytes() == b"preserve after uncertain move"
    assert not list(workspace.glob(".topoforge-cleanup-*"))


def test_workflow_launch_uses_random_temporary_without_touching_predictable_symlink(
    tmp_path: Path,
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=4,
        columns=4,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "workflow"
    config = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
        ),
        slicing_enabled=False,
    )
    destination = tmp_path / "saved-launch.yaml"
    predictable = destination.with_name(f".{destination.name}.tmp")
    external_marker = tmp_path / "launch-external.marker"
    external_marker.write_bytes(b"do-not-overwrite")
    try:
        predictable.symlink_to(external_marker)
    except OSError:
        pytest.skip("host cannot create predictable workflow temporary symlink fixtures")

    assert write_workflow_launch_config(config, destination) == destination
    assert read_workflow_launch_config(destination) == config
    assert predictable.is_symlink()
    assert external_marker.read_bytes() == b"do-not-overwrite"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


@pytest.mark.parametrize(
    "writer",
    [workflow_maintenance._atomic_write_bytes, workflow_ux._atomic_write_bytes],
    ids=["maintenance", "ux"],
)
def test_workflow_atomic_writers_reject_parent_swap_without_writing_external_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: Any,
) -> None:
    trusted = tmp_path / "trusted-output"
    trusted.mkdir()
    displaced = tmp_path / "trusted-output-displaced"
    external = tmp_path / "external-output"
    external.mkdir()
    destination = trusted / "record.json"
    victim = external / destination.name
    victim.write_bytes(b"preserve external content")
    probe = tmp_path / "atomic-writer-symlink-probe"
    try:
        probe.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create atomic-writer parent-swap fixtures")
    probe.unlink()
    original_atomic_write = workflow_maintenance.atomic_write_owned_regular_bytes
    swapped = False

    def swap_before_atomic_write(*args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            trusted.rename(displaced)
            trusted.symlink_to(external, target_is_directory=True)
        try:
            original_atomic_write(*args, **kwargs)
        finally:
            if trusted.is_symlink():
                trusted.unlink()
                displaced.rename(trusted)

    monkeypatch.setattr(
        workflow_maintenance,
        "atomic_write_owned_regular_bytes",
        swap_before_atomic_write,
    )
    with pytest.raises(ConfigurationError, match="could not be safely written"):
        writer(destination, b"must not escape")

    assert swapped is True
    assert victim.read_bytes() == b"preserve external content"
    assert not destination.exists()


def test_workflow_launch_strict_reopen_rejects_post_write_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "strict-source.tif",
        SyntheticTerrain.SADDLE,
        rows=4,
        columns=4,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "strict-workflow"
    config = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
        ),
        slicing_enabled=False,
    )
    trusted = tmp_path / "strict-parent"
    trusted.mkdir()
    displaced = tmp_path / "strict-parent-displaced"
    external = tmp_path / "strict-external"
    external.mkdir()
    destination = trusted / "workflow-launch.yaml"
    original_atomic_yaml = workflow_ux._atomic_yaml
    swapped = False

    def swap_after_atomic_yaml(*args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        written = original_atomic_yaml(*args, **kwargs)
        (external / destination.name).write_bytes(destination.read_bytes())
        trusted.rename(displaced)
        trusted.symlink_to(external, target_is_directory=True)
        swapped = True
        return written

    try:
        probe = tmp_path / "strict-reopen-symlink-probe"
        probe.symlink_to(external, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("host cannot create strict-reopen parent-swap fixtures")
    monkeypatch.setattr(workflow_ux, "_atomic_yaml", swap_after_atomic_yaml)
    try:
        with pytest.raises(ConfigurationError, match="changed before strict reopen"):
            write_workflow_launch_config(config, destination)
    finally:
        if trusted.is_symlink():
            trusted.unlink()
            displaced.rename(trusted)

    assert swapped is True
    assert read_workflow_launch_config(destination) == config


def test_summary_publication_keeps_one_workspace_identity_across_artifacts(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    inspected = inspect_workflow_workspace(workspace)
    result = LocalWorkflowResult(
        workspace_dir=workspace,
        workflow_id=inspected.workflow_id,
        manifest_path=workspace / "workflow-manifest.json",
        status_path=workspace / "workflow-status.json",
        completed_stages=inspected.completed_stages,
        reused_stages=(),
        stage_outputs={},
        required_checks_passed=True,
    )
    displaced = tmp_path / "workflow-displaced"
    attacker = tmp_path / "attacker-workflow"
    external_payload = b"external report must not be overwritten"
    original_atomic_json = workflow_ux._atomic_json
    swapped = False

    def swap_after_summary(*args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        written = original_atomic_json(*args, **kwargs)
        workspace.rename(displaced)
        workspace.mkdir()
        (workspace / "workflow-report.html").write_bytes(external_payload)
        swapped = True
        return written

    monkeypatch.setattr(workflow_ux, "_atomic_json", swap_after_summary)
    try:
        with pytest.raises(ConfigurationError, match="could not be safely written"):
            publish_workflow_summary(result)
        assert swapped is True
        assert (workspace / "workflow-report.html").read_bytes() == external_payload
    finally:
        if swapped:
            workspace.rename(attacker)
            displaced.rename(workspace)

    assert (attacker / "workflow-report.html").read_bytes() == external_payload


def test_execute_binds_workspace_identity_before_entering_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "identity-source.tif",
        SyntheticTerrain.SADDLE,
        rows=4,
        columns=4,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "identity-workflow"
    displaced = tmp_path / "identity-workflow-displaced"
    attacker = tmp_path / "identity-workflow-replacement"
    victim_payload = b"replacement status must remain unchanged"
    config = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=40.0,
            max_height_mm=20.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
        ),
        slicing_enabled=False,
    )
    original_run = workflow_ux.run_local_workflow
    swapped = False

    def swap_before_core(*args: Any, **kwargs: Any) -> LocalWorkflowResult:
        nonlocal swapped
        workspace.rename(displaced)
        workspace.mkdir()
        (workspace / "workflow-status.json").write_bytes(victim_payload)
        swapped = True
        return original_run(*args, **kwargs)

    monkeypatch.setattr(workflow_ux, "run_local_workflow", swap_before_core)
    try:
        with pytest.raises(ConfigurationError, match=r"workspace.*changed"):
            execute_workflow_launch(config)
        assert swapped is True
        assert (workspace / "workflow-status.json").read_bytes() == victim_payload
    finally:
        if swapped:
            workspace.rename(attacker)
            displaced.rename(workspace)

    assert (attacker / "workflow-status.json").read_bytes() == victim_payload


def test_cleanup_requires_exact_plan_id(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    stale = workspace / "stages" / "10-build" / "stale"
    stale.mkdir()
    marker = stale / "marker.bin"
    marker.write_bytes(b"preserve")
    plan = plan_workflow_cleanup(workspace)

    with pytest.raises(ConfigurationError, match="--confirm-plan-id"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=plan.workflow_id,
        )

    with pytest.raises(ConfigurationError, match="confirmation is incorrect"):
        apply_workflow_cleanup(
            workspace,
            confirm_workflow_id=plan.workflow_id,
            confirm_plan_id="0" * 64,
        )

    assert marker.read_bytes() == b"preserve"


@pytest.mark.parametrize("tamper_kind", ["invalid-3mf", "failed-validation"])
def test_inspect_and_backup_reject_resealed_build_artifact_tampering_without_partial_archive(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    tamper_kind: str,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    build_dir = _build_directory(workspace)
    if tamper_kind == "invalid-3mf":
        (build_dir / "model.3mf").write_bytes(b"not a 3mf package")
        artifact_role = "model_3mf"
    else:
        validation_path = build_dir / "validation.json"
        validation = _json_object(validation_path)
        validation["required_checks_passed"] = False
        _write_canonical_json(validation_path, validation)
        artifact_role = "validation_json"
    _reseal_build_stage(workspace, artifact_role)

    rejected = "build artifact verification failed|restored backup file changed"
    with pytest.raises(ConfigurationError, match=rejected):
        inspect_workflow_workspace(workspace)

    archive = tmp_path / "rejected-backup.zip"
    with pytest.raises(
        ConfigurationError,
        match=rejected,
    ):
        create_workflow_backup(workspace, archive)
    assert not archive.exists()
    assert not archive.with_name(f".{archive.name}.tmp").exists()


def test_backup_semantically_verifies_the_exact_snapshot_before_publication(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    model = _build_directory(workspace) / "model.3mf"
    archive = tmp_path / "semantic-race.zip"
    original_inspect = workflow_ux.inspect_workflow_workspace
    mutated = False

    def mutate_after_initial_inspection(*args: Any, **kwargs: Any) -> Any:
        nonlocal mutated
        summary = original_inspect(*args, **kwargs)
        if not mutated and Path(args[0]).resolve() == workspace.resolve():
            mutated = True
            model.write_bytes(b"invalid model introduced after initial inspection")
        return summary

    monkeypatch.setattr(
        workflow_ux,
        "inspect_workflow_workspace",
        mutate_after_initial_inspection,
    )
    with pytest.raises(ConfigurationError, match=r"artifact verification failed|checksum mismatch"):
        create_workflow_backup(workspace, archive)

    assert mutated is True
    assert not archive.exists()
    assert not list(tmp_path.glob(f".{archive.name}.*.publishing"))
    assert not list(tmp_path.glob(".topoforge-backup-verify-*"))


def test_backup_uses_random_temporary_without_touching_predictable_symlink(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    archive = tmp_path / "workflow-backup.zip"
    predictable = archive.with_name(f".{archive.name}.tmp")
    external_marker = tmp_path / "external.marker"
    external_marker.write_bytes(b"do-not-overwrite")
    try:
        predictable.symlink_to(external_marker)
    except OSError:
        pytest.skip("host cannot create predictable temporary symlink fixture")

    result = create_workflow_backup(workspace, archive)

    assert result.archive_path == archive
    assert verify_workflow_backup(archive) == result.manifest
    assert predictable.is_symlink()
    assert external_marker.read_bytes() == b"do-not-overwrite"
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_backup_publication_accepts_a_committed_move_with_a_late_error(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    archive = tmp_path / "committed-backup.zip"
    original_move = workflow_maintenance.move_owned_path
    injected = False

    def fail_after_publication(source: Path, destination: Path, **kwargs: Any) -> None:
        nonlocal injected
        original_move(source, destination, **kwargs)
        if kwargs.get("context") == "workflow backup publication" and not injected:
            injected = True
            raise ValueError("injected post-publication check failure")

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_publication)
    result = create_workflow_backup(workspace, archive)

    assert injected is True
    assert result.archive_path == archive
    assert verify_workflow_backup(archive) == result.manifest
    assert not list(tmp_path.glob(f".{archive.name}.*.publishing"))


def test_backup_publication_reports_committed_durability_uncertainty(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    archive = tmp_path / "uncertain-backup.zip"
    original_move = workflow_maintenance.move_owned_path

    def fail_after_publication(source: Path, destination: Path, **kwargs: Any) -> None:
        original_move(source, destination, **kwargs)
        if kwargs.get("context") == "workflow backup publication":
            raise CommittedStateUncertainError(
                operation="move",
                path=destination,
                context="injected backup publication",
                cause=OSError("injected parent fsync failure"),
            )

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_publication)
    with pytest.raises(ConfigurationError, match="durability is uncertain"):
        create_workflow_backup(workspace, archive)

    assert archive.is_file()
    assert verify_workflow_backup(archive).required_checks_passed is True
    assert not list(tmp_path.glob(f".{archive.name}.*.publishing"))


def test_backup_rejects_workspace_hard_links_without_publishing_archive(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    first = workspace / "hardlink-first.bin"
    second = workspace / "hardlink-second.bin"
    first.write_bytes(b"shared-inode")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("host cannot create workflow backup hard-link fixtures")

    archive = tmp_path / "hardlink-rejected.zip"
    with pytest.raises(ConfigurationError, match="hard-linked"):
        create_workflow_backup(workspace, archive)

    assert first.read_bytes() == b"shared-inode"
    assert second.read_bytes() == b"shared-inode"
    assert not archive.exists()
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_identity_sensitive_enumeration_refreshes_cached_direntry_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "workflow-launch.yaml"
    source.write_bytes(b"fixture: true\n")
    real_scandir = workflow_maintenance.os.scandir
    cleanup_parent = tmp_path / "cleanup-parent"
    cleanup_tree = cleanup_parent / "restore-staging"
    cleanup_child = cleanup_tree / "backup-external" / "source.tif"
    cleanup_child.parent.mkdir(parents=True)
    cleanup_child.write_bytes(b"fixture")

    class CachedEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise AssertionError("backup enumeration trusted cached DirEntry metadata")

    class CachedScandir:
        def __init__(self, path: Path) -> None:
            with real_scandir(path) as entries:
                self._entries = [CachedEntry(entry) for entry in entries]

        def __enter__(self) -> list[CachedEntry]:
            return self._entries

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        workflow_maintenance.os,
        "scandir",
        lambda path: CachedScandir(Path(path)),
    )

    files = workflow_maintenance._workspace_backup_files(
        workspace,
        (workspace.stat().st_dev, workspace.stat().st_ino),
    )

    assert [item.path for item in files] == [source]
    workflow_maintenance._remove_owned_tree(
        cleanup_tree,
        parent_root=cleanup_parent,
        parent_root_identity=workflow_maintenance._identity(cleanup_parent.lstat()),
        expected_tree_identity=workflow_maintenance._identity(cleanup_tree.lstat()),
        context="test restore cleanup",
    )

    assert not cleanup_tree.exists()


def test_backup_atomic_publication_never_overwrites_racing_destination(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    archive = tmp_path / "raced-backup.zip"
    marker = b"created-after-initial-check"
    original_publish = workflow_maintenance._publish_backup_no_clobber

    def create_destination_during_publish(*args: Any, **kwargs: Any) -> None:
        if not archive.exists():
            archive.write_bytes(marker)
        original_publish(*args, **kwargs)

    monkeypatch.setattr(
        workflow_maintenance,
        "_publish_backup_no_clobber",
        create_destination_during_publish,
    )
    with pytest.raises(ConfigurationError, match="already exists"):
        create_workflow_backup(workspace, archive)

    assert archive.read_bytes() == marker
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


def test_backup_rejects_workspace_link_like_entry_without_following_target(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker.bin"
    marker.write_bytes(b"preserve")
    link = workspace / "untrusted-link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create backup symlink fixture")

    archive = tmp_path / "rejected.zip"
    with pytest.raises(ConfigurationError, match="do not follow link-like"):
        create_workflow_backup(workspace, archive)

    assert link.is_symlink()
    assert marker.read_bytes() == b"preserve"
    assert not archive.exists()
    assert not list(tmp_path.glob(f".{archive.name}.*.tmp"))


@pytest.mark.parametrize(
    ("attack", "message"),
    [
        ("duplicate", "duplicate archive paths"),
        ("casefold", "Unicode/casefold path aliases"),
        ("unicode-nfd", "canonical Unicode NFC"),
        ("windows-reserved", "portable to Windows"),
        ("windows-superscript-reserved", "portable to Windows"),
        ("windows-overlong-component", "portable to Windows"),
        ("symlink", "not a regular file"),
        ("unsupported-compression", "unsupported compression"),
        ("compression-ratio", "compression ratio is unsafe"),
    ],
)
def test_backup_verification_rejects_hostile_central_directory_entries(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    attack: str,
    message: str,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    with zipfile.ZipFile(archive_path, mode="a") as archive:
        name = "workspace/hostile.bin"
        payload = b"hostile"
        compression = zipfile.ZIP_DEFLATED
        if attack == "duplicate":
            name = workflow_maintenance._BACKUP_MANIFEST_NAME
        elif attack == "casefold":
            name = "WORKSPACE/workflow-launch.yaml"
        elif attack == "unicode-nfd":
            name = "workspace/cafe\u0301.bin"
        elif attack == "windows-reserved":
            name = "workspace/CON.txt"
        elif attack == "windows-superscript-reserved":
            name = "workspace/COM¹.txt"
        elif attack == "windows-overlong-component":
            name = f"workspace/{'a' * 256}"
        elif attack == "unsupported-compression":
            compression = zipfile.ZIP_BZIP2
        elif attack == "compression-ratio":
            name = "workspace/compression-bomb.bin"
            payload = bytes(2 * 1024 * 1024)

        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = ((stat.S_IFREG | 0o600) & 0xFFFF) << 16
        info.compress_type = compression
        if attack == "symlink":
            info.external_attr = ((stat.S_IFLNK | 0o777) & 0xFFFF) << 16
        if attack == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(info, payload)
        else:
            archive.writestr(info, payload)

    with pytest.raises(ConfigurationError, match=message):
        verify_workflow_backup(archive_path)


def test_backup_central_directory_limits_run_before_any_member_payload_open(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    payload = bytearray(archive_path.read_bytes())
    eocd_offset = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<2H", payload, eocd_offset + 8, 1, 1)
    archive_path.write_bytes(payload)
    zipfile_construction_count = 0
    original_zipfile = zipfile.ZipFile

    def tracked_zipfile(*args: Any, **kwargs: Any) -> zipfile.ZipFile:
        nonlocal zipfile_construction_count
        zipfile_construction_count += 1
        return original_zipfile(*args, **kwargs)

    monkeypatch.setattr(workflow_maintenance, "_MAX_BACKUP_MEMBER_COUNT", 1)
    monkeypatch.setattr(workflow_maintenance.zipfile, "ZipFile", tracked_zipfile)
    with pytest.raises(ConfigurationError, match=r"member count.*exceeds the maximum"):
        verify_workflow_backup(archive_path)

    assert zipfile_construction_count == 0


def test_backup_central_directory_rejects_encrypted_member_flag(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        archive.infolist()[0].flag_bits |= 0x1
        with pytest.raises(ConfigurationError, match="encrypted"):
            workflow_maintenance._validate_backup_central_directory(
                archive,
                archive_size_bytes=archive_path.stat().st_size,
            )


def test_backup_creation_accepts_caller_owned_pinned_read_write_stream(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    archive_path = tmp_path / "caller-owned.zip"
    with archive_path.open("x+b") as stream:
        result = create_workflow_backup(
            completed_workflow_workspace,
            stream,
            source_path=archive_path,
        )

        assert stream.closed is False
        assert stream.tell() == 0
        assert stream.read(4) == b"PK\x03\x04"
        assert verify_workflow_backup(stream, source_path=archive_path) == result.manifest
        assert stream.closed is False

    assert result.archive_path == archive_path
    assert result.archive_sha256 == sha256_file(archive_path)


def test_backup_stream_rejects_source_metadata_for_a_different_file(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    other_path = tmp_path / "different-source.zip"
    shutil.copyfile(archive_path, other_path)

    with archive_path.open("rb") as stream:
        with pytest.raises(ConfigurationError, match="does not name the opened archive"):
            verify_workflow_backup(stream, source_path=other_path)
        assert stream.closed is False


def test_restore_stream_rejects_missing_source_metadata_without_creating_destination(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    missing_source = tmp_path / "missing-source.zip"
    destination = tmp_path / "must-not-be-restored"

    with archive_path.open("rb") as stream:
        with pytest.raises(ConfigurationError, match="unavailable"):
            restore_workflow_backup(
                stream,
                destination,
                source_path=missing_source,
            )
        assert stream.closed is False

    assert not destination.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows denies replacement of the pinned archive handle",
)
def test_open_verified_backup_stream_survives_archive_path_replacement(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    original_sha256 = sha256_file(archive_path)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")

    with archive_path.open("rb") as stream:
        with workflow_maintenance.open_verified_workflow_backup(
            stream,
            source_path=archive_path,
        ) as verified:
            os.replace(replacement, archive_path)
            assert verified.sha256 == original_sha256
            assert verified.stream.tell() == 0
            assert verified.stream.read(4) == b"PK\x03\x04"
        assert stream.closed is False

    assert archive_path.read_bytes() == b"replacement"


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows denies replacement of the pinned archive handle",
)
def test_restore_rejects_archive_path_replacement_before_durable_publication(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"not-a-zip")
    original_copy = workflow_maintenance._copy_zip_member_bounded
    swapped = False

    def swap_during_first_copy(*args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped:
            os.replace(replacement, archive_path)
            swapped = True
        original_copy(*args, **kwargs)

    monkeypatch.setattr(
        workflow_maintenance,
        "_copy_zip_member_bounded",
        swap_during_first_copy,
    )
    restored = tmp_path / "restored"
    with pytest.raises(ConfigurationError, match="archive path changed"):
        restore_workflow_backup(archive_path, restored)

    assert swapped is True
    assert archive_path.read_bytes() == b"not-a-zip"
    assert not restored.exists()
    assert not list(tmp_path.glob(f".{restored.name}.restore-*"))


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows denies replacement of the pinned archive handle",
)
def test_restore_rolls_back_if_archive_path_changes_during_publication(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    replacement = tmp_path / "publication-replacement.bin"
    replacement.write_bytes(b"not-a-zip")
    destination = tmp_path / "restore-publication-source-race"
    original_move = workflow_maintenance.move_owned_path
    swapped = False

    def swap_before_publication_move(source: Path, target: Path, **kwargs: Any) -> None:
        nonlocal swapped
        if kwargs.get("context") == "workflow restore publication" and not swapped:
            os.replace(replacement, archive_path)
            swapped = True
        original_move(source, target, **kwargs)

    monkeypatch.setattr(
        workflow_maintenance,
        "move_owned_path",
        swap_before_publication_move,
    )
    with pytest.raises(ConfigurationError, match="archive path changed"):
        restore_workflow_backup(archive_path, destination)

    assert swapped is True
    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.restore-*"))


def test_restore_rejects_destination_parent_swap_without_writing_external_tree(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    parent = tmp_path / "restore-parent"
    parent.mkdir()
    external = tmp_path / "restore-external"
    external.mkdir()
    marker = external / "marker.bin"
    marker.write_bytes(b"preserve")
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create restore parent-swap symlink fixtures")
    probe.unlink()

    destination = parent / "restored"
    displaced = tmp_path / "restore-parent-displaced"
    original_move = workflow_maintenance.move_owned_path
    swapped = False

    def swap_parent(source: Path, target: Path, **kwargs: Any) -> None:
        nonlocal swapped
        if kwargs.get("context") != "workflow restore publication":
            original_move(source, target, **kwargs)
            return
        swapped = True
        parent.rename(displaced)
        parent.symlink_to(external, target_is_directory=True)
        try:
            original_move(source, target, **kwargs)
        finally:
            parent.unlink()
            displaced.rename(parent)

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", swap_parent)
    with pytest.raises(ConfigurationError, match="destination parent changed"):
        restore_workflow_backup(archive_path, destination)

    assert swapped is True
    assert marker.read_bytes() == b"preserve"
    assert sorted(path.name for path in external.iterdir()) == ["marker.bin"]
    assert not destination.exists()
    assert not list(parent.glob(f".{destination.name}.restore-*"))


def test_restore_accepts_a_committed_publication_with_a_late_error(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    destination = tmp_path / "restored-after-late-publication-error"
    original_move = workflow_maintenance.move_owned_path
    injected = False

    def fail_after_publication(source: Path, target: Path, **kwargs: Any) -> None:
        nonlocal injected
        original_move(source, target, **kwargs)
        if kwargs.get("context") == "workflow restore publication" and not injected:
            injected = True
            raise ValueError("injected restore publication postcondition failure")

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_publication)
    result = restore_workflow_backup(archive_path, destination)

    assert injected is True
    assert result.workspace == destination
    assert inspect_workflow_workspace(destination).required_checks_passed is True
    assert not list(tmp_path.glob(f".{destination.name}.restore-*"))


def test_restore_publication_reports_committed_durability_uncertainty(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    destination = tmp_path / "restored-publication-uncertain"
    original_move = workflow_maintenance.move_owned_path

    def fail_after_publication(source: Path, target: Path, **kwargs: Any) -> None:
        original_move(source, target, **kwargs)
        if kwargs.get("context") == "workflow restore publication":
            raise CommittedStateUncertainError(
                operation="move",
                path=target,
                context="injected restore publication",
                cause=OSError("injected parent fsync failure"),
            )

    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_publication)
    with pytest.raises(ConfigurationError, match="durability is uncertain"):
        restore_workflow_backup(archive_path, destination)

    assert destination.is_dir()
    assert (destination / "workflow-manifest.json").is_file()
    assert (destination / "workflow-status.json").is_file()
    assert not list(tmp_path.glob(f".{destination.name}.restore-*"))


def test_restore_reconciles_a_rollback_that_commits_before_raising(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    destination = tmp_path / "restored-rollback-late-error"
    original_move = workflow_maintenance.move_owned_path
    original_atomic_write = workflow_maintenance.atomic_write_owned_regular_bytes
    rollback_injected = False

    def fail_restore_evidence(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("context") == "workflow restore evidence":
            raise ValueError("injected restore evidence write failure")
        original_atomic_write(*args, **kwargs)

    def fail_after_rollback(source: Path, target: Path, **kwargs: Any) -> None:
        nonlocal rollback_injected
        original_move(source, target, **kwargs)
        if kwargs.get("context") == "workflow restore rollback" and not rollback_injected:
            rollback_injected = True
            raise ValueError("injected restore rollback postcondition failure")

    monkeypatch.setattr(
        workflow_maintenance,
        "atomic_write_owned_regular_bytes",
        fail_restore_evidence,
    )
    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_rollback)
    with pytest.raises(ConfigurationError, match="restore evidence could not be safely published"):
        restore_workflow_backup(archive_path, destination)

    assert rollback_injected is True
    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.restore-*"))


def test_restore_rollback_reports_committed_durability_uncertainty(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    destination = tmp_path / "restored-rollback-uncertain"
    original_move = workflow_maintenance.move_owned_path
    original_atomic_write = workflow_maintenance.atomic_write_owned_regular_bytes

    def fail_restore_evidence(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("context") == "workflow restore evidence":
            raise ValueError("injected restore evidence write failure")
        original_atomic_write(*args, **kwargs)

    def fail_after_rollback(source: Path, target: Path, **kwargs: Any) -> None:
        original_move(source, target, **kwargs)
        if kwargs.get("context") == "workflow restore rollback":
            raise CommittedStateUncertainError(
                operation="move",
                path=target,
                context="injected restore rollback",
                cause=OSError("injected parent fsync failure"),
            )

    monkeypatch.setattr(
        workflow_maintenance,
        "atomic_write_owned_regular_bytes",
        fail_restore_evidence,
    )
    monkeypatch.setattr(workflow_maintenance, "move_owned_path", fail_after_rollback)
    with pytest.raises(
        ConfigurationError,
        match=r"rollback committed.*durability is uncertain",
    ):
        restore_workflow_backup(archive_path, destination)

    assert not destination.exists()
    assert len(list(tmp_path.glob(f".{destination.name}.restore-*"))) == 1


def test_restore_rolls_back_when_private_archive_context_exit_fails(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    destination = tmp_path / "restored-context-exit-failure"
    original_snapshot = workflow_maintenance._private_workflow_backup_snapshot

    @contextmanager
    def fail_after_snapshot_context(*args: Any, **kwargs: Any) -> Any:
        with original_snapshot(*args, **kwargs) as snapshot:
            yield snapshot
        raise ConfigurationError("injected private snapshot context-exit failure")

    monkeypatch.setattr(
        workflow_maintenance,
        "_private_workflow_backup_snapshot",
        fail_after_snapshot_context,
    )
    with pytest.raises(ConfigurationError, match="context-exit failure"):
        restore_workflow_backup(archive_path, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.restore-*"))


def test_restore_trusted_root_rejects_pre_call_swap_without_writing_external_tree(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    trusted_root = tmp_path / "trusted-workspaces"
    trusted_root.mkdir()
    trusted_stat = trusted_root.lstat()
    trusted_identity = (trusted_stat.st_dev, trusted_stat.st_ino)
    displaced = tmp_path / "trusted-workspaces-displaced"
    external = tmp_path / "restore-pre-call-external"
    external.mkdir()
    marker = external / "marker.bin"
    marker.write_bytes(b"preserve")
    probe = tmp_path / "trusted-root-symlink-probe"
    try:
        probe.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create trusted-root swap symlink fixtures")
    probe.unlink()

    trusted_root.rename(displaced)
    trusted_root.symlink_to(external, target_is_directory=True)
    destination = trusted_root / "restored"
    try:
        with pytest.raises(ConfigurationError, match="trusted destination root"):
            restore_workflow_backup(
                archive_path,
                destination,
                destination_root=trusted_root,
                destination_root_identity=trusted_identity,
            )
    finally:
        trusted_root.unlink()
        displaced.rename(trusted_root)

    assert marker.read_bytes() == b"preserve"
    assert sorted(path.name for path in external.iterdir()) == ["marker.bin"]
    assert not destination.exists()


def test_restore_report_write_rejects_trusted_root_swap_and_rolls_back(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = _copy_completed_backup(completed_workflow_workspace, tmp_path)
    trusted_root = tmp_path / "report-workspaces"
    trusted_root.mkdir()
    trusted_stat = trusted_root.lstat()
    trusted_identity = (trusted_stat.st_dev, trusted_stat.st_ino)
    displaced = tmp_path / "report-workspaces-displaced"
    external = tmp_path / "report-external"
    external.mkdir()
    marker = external / "marker.bin"
    marker.write_bytes(b"preserve")
    probe = tmp_path / "report-symlink-probe"
    try:
        probe.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create report parent-swap symlink fixtures")
    probe.unlink()

    destination = trusted_root / "restored"
    original_write = workflow_maintenance.atomic_write_owned_regular_bytes
    swapped = False

    def swap_on_report(path: Path, payload: bytes, **kwargs: Any) -> None:
        nonlocal swapped
        if kwargs.get("context") != "restored workflow report":
            original_write(path, payload, **kwargs)
            return
        swapped = True
        trusted_root.rename(displaced)
        trusted_root.symlink_to(external, target_is_directory=True)
        try:
            original_write(path, payload, **kwargs)
        finally:
            trusted_root.unlink()
            displaced.rename(trusted_root)

    monkeypatch.setattr(
        workflow_maintenance,
        "atomic_write_owned_regular_bytes",
        swap_on_report,
    )
    with pytest.raises(ConfigurationError, match="report could not be safely written"):
        restore_workflow_backup(
            archive_path,
            destination,
            destination_root=trusted_root,
            destination_root_identity=trusted_identity,
        )

    assert swapped is True
    assert marker.read_bytes() == b"preserve"
    assert sorted(path.name for path in external.iterdir()) == ["marker.bin"]
    assert not destination.exists()
    assert not list(trusted_root.glob(f".{destination.name}.restore-*"))


def test_backup_rejects_source_parent_swap_without_reading_external_file(
    tmp_path: Path,
    completed_workflow_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    source_parent = workspace / "race-source"
    source_parent.mkdir()
    victim = source_parent / "victim.bin"
    victim.write_bytes(b"trusted")
    displaced = workspace / "race-source-displaced"
    external = tmp_path / "attacker-source"
    external.mkdir()
    attacker = external / "victim.bin"
    attacker.write_bytes(b"attacker")

    probe = tmp_path / "source-symlink-probe"
    try:
        probe.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create backup source-swap symlink fixtures")
    probe.unlink()

    original_write = workflow_maintenance._write_zip_file
    swapped = False

    def swap_source_parent(archive: zipfile.ZipFile, source: Any) -> None:
        nonlocal swapped
        if source.path != victim:
            original_write(archive, source)
            return
        swapped = True
        source_parent.rename(displaced)
        source_parent.symlink_to(external, target_is_directory=True)
        try:
            original_write(archive, source)
        finally:
            source_parent.unlink()
            displaced.rename(source_parent)

    monkeypatch.setattr(workflow_maintenance, "_write_zip_file", swap_source_parent)
    output = tmp_path / "source-swap.zip"
    with pytest.raises(ConfigurationError, match="unsafe or changed"):
        create_workflow_backup(workspace, output)

    assert swapped is True
    assert victim.read_bytes() == b"trusted"
    assert attacker.read_bytes() == b"attacker"
    assert not output.exists()


@pytest.mark.skipif(
    os.name != "nt",
    reason="external native Windows junction gate",
)
def test_cleanup_rejects_native_windows_junction(
    tmp_path: Path,
    completed_workflow_workspace: Path,
) -> None:
    workspace = _copy_completed_workspace(completed_workflow_workspace, tmp_path)
    target = tmp_path / "junction-target"
    target.mkdir()
    marker = target / "marker.bin"
    marker.write_bytes(b"preserve")
    junction = workspace / "stages" / "10-build" / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"host cannot create a native junction: {created.stderr}")

    with pytest.raises(ConfigurationError, match="link-like component"):
        plan_workflow_cleanup(workspace)

    assert junction.is_dir()
    assert marker.read_bytes() == b"preserve"


def test_storage_cleanup_backup_restore_and_offline_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    workspace = tmp_path / "workflow"
    overlay_path = tmp_path / "road.geojson"
    overlay_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[500010.0, 3299990.0], [500290.0, 3299790.0]],
                        },
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    launch = WorkflowLaunchConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source,
            output_dir=workspace,
            model_width_mm=60.0,
            max_height_mm=25.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
        ),
        overlay=OverlayConfig(
            sources=(
                OverlaySourceConfig(
                    source_id="road",
                    kind=OverlayKind.ROAD,
                    format=OverlayFormat.GEOJSON,
                    path=overlay_path,
                    source_crs="EPSG:32648",
                    dataset_name="maintenance road fixture",
                    license="CC0-1.0",
                    attribution="TopoForge tests",
                ),
            )
        ),
        maximum_tile_width_mm=40.0,
        maximum_tile_depth_mm=35.0,
        slicing_enabled=False,
    )
    launch_path = write_workflow_launch_config(launch)

    pre_run_storage = runner.invoke(app, ["storage", str(launch_path)])
    assert pre_run_storage.exit_code == 0, pre_run_storage.output
    pre_run_payload = json.loads(pre_run_storage.output)
    assert pre_run_payload["estimate_basis"] == "configured_resource_ceilings"
    assert pre_run_payload["estimated_additional_bytes"] > 0
    assert pre_run_payload["backup_input_bytes"] >= source.stat().st_size

    execution = execute_workflow_launch(launch)
    assert execution.summary.metrics["storage"]["estimate_basis"] == (
        "completed_workflow_measurements"
    )
    assert (workspace / "workflow-storage.json").is_file()

    stale = workspace / "stages" / "10-build" / "stale-identity" / "retained.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"obsolete-stage-bytes")
    reviewed = runner.invoke(app, ["cleanup", str(workspace)])
    assert reviewed.exit_code == 0, reviewed.output
    review_payload = json.loads(reviewed.output)
    assert review_payload["status"] == "review"
    assert len(review_payload["plan_id"]) == 64
    assert review_payload["reclaimable_bytes"] == len(b"obsolete-stage-bytes")
    assert [item["path"] for item in review_payload["candidates"]] == [
        "stages/10-build/stale-identity"
    ]
    assert stale.is_file()
    reviewed_plan_path = Path(review_payload["plan"])
    reviewed_plan_payload = json.loads(reviewed_plan_path.read_text(encoding="utf-8"))
    reviewed_plan_path.write_text(
        json.dumps(reviewed_plan_payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    reviewed_plan_bytes = reviewed_plan_path.read_bytes()

    rejected = runner.invoke(
        app,
        [
            "cleanup",
            str(workspace),
            "--apply",
            "--confirm-workflow-id",
            "wrong-id",
            "--confirm-plan-id",
            review_payload["plan_id"],
        ],
    )
    assert rejected.exit_code == 2
    assert stale.is_file()
    assert reviewed_plan_path.read_bytes() == reviewed_plan_bytes

    applied = runner.invoke(
        app,
        [
            "cleanup",
            str(workspace),
            "--apply",
            "--confirm-workflow-id",
            execution.summary.workflow_id,
            "--confirm-plan-id",
            review_payload["plan_id"],
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert not stale.exists()
    assert reviewed_plan_path.read_bytes() == reviewed_plan_bytes
    assert inspect_workflow_workspace(workspace).required_checks_passed is True

    first_archive = tmp_path / "workflow-backup-1.zip"
    second_archive = tmp_path / "workflow-backup-2.zip"
    first_backup = create_workflow_backup(workspace, first_archive)
    second_backup = create_workflow_backup(workspace, second_archive)
    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_backup.archive_sha256 == second_backup.archive_sha256
    assert verify_workflow_backup(first_archive) == first_backup.manifest
    monkeypatch.setattr(workflow_maintenance, "__version__", "99.0.0")
    assert verify_workflow_backup(first_archive) == first_backup.manifest
    assert sum(item.kind == "external" for item in first_backup.manifest.files) >= 2

    tampered = tmp_path / "workflow-backup-tampered.zip"
    with (
        zipfile.ZipFile(first_archive, "r") as original,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as changed,
    ):
        changed_name = next(name for name in original.namelist() if name.startswith("workspace/"))
        for name in original.namelist():
            payload = original.read(name)
            changed.writestr(name, payload + b"tamper" if name == changed_name else payload)
    tampered_result = runner.invoke(
        app,
        ["restore", str(tampered), "--output", str(tmp_path / "tampered-restore")],
    )
    assert tampered_result.exit_code == 2

    source.unlink()
    restored_workspace = tmp_path / "restored-workflow"
    restored = restore_workflow_backup(first_archive, restored_workspace)
    assert restored.required_checks_passed is True
    assert restored.external_directory == restored_workspace / "backup-external"
    restored_launch = read_workflow_launch_config(restored_workspace / "workflow-launch.yaml")
    assert restored_launch.build.dem_path.is_file()
    assert restored_workspace in restored_launch.build.dem_path.parents
    assert restored_launch.overlay is not None
    assert restored_launch.overlay.sources[0].path is not None
    assert restored_launch.overlay.sources[0].path.is_file()
    assert restored_workspace in restored_launch.overlay.sources[0].path.parents
    assert inspect_workflow_workspace(restored_workspace).required_checks_passed is True

    resumed = execute_workflow_launch(restored_launch)
    assert resumed.summary.required_checks_passed is True
    assert resumed.workflow.completed_stages
