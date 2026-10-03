from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from topoforge.exceptions import ConfigurationError
from topoforge.models import AreaOfInterestInput
from topoforge.overlays import OverlayConfig, OverlaySourceConfig
from topoforge.web import jobs as jobs_module
from topoforge.web import worker as worker_module
from topoforge.web.api import create_app
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import JobCreateRequest, JobRecord, WebAppConfig, WorkerResult
from topoforge.web.project_reuse import prepare_project_reuse
from topoforge.web.security import canonical_json_bytes
from topoforge.workflow.acquisition import GlobalAcquisitionConfig

from .conftest import make_job_request


@pytest.fixture
def reuse_manager(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LocalJobManager]:
    manager = LocalJobManager(web_config)
    monkeypatch.setattr(manager, "_start_queued_jobs", lambda: None)
    monkeypatch.setattr(manager, "_monitor_loop", lambda: None)
    manager.start()
    try:
        yield manager
    finally:
        manager.close()


def _saved_request(manager: LocalJobManager, request: JobCreateRequest) -> JobRecord:
    return manager.submit(request)


def test_reuse_preserves_complete_local_request_without_writes_or_execution(
    reuse_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config, name="original")
    aoi = AreaOfInterestInput(bbox_wgs84=(99.8, 29.4, 100.1, 29.7))
    build = request.launch.build.model_copy(
        update={
            "aoi": aoi,
            "attribution": "Retained attribution",
            "nodata_max_hole_pixels": 17,
            "robust_low_percentile": 2.0,
            "output_formats": ["glb", "stl", "3mf"],
        }
    )
    request = request.model_copy(
        update={
            "launch": request.launch.model_copy(
                update={"build": build, "slice_timeout_seconds": 87.0}
            )
        }
    )
    record = _saved_request(manager, request)
    original_record_bytes = manager._record_path(record.job_id).read_bytes()
    original_request_bytes = manager._request_path(record.job_id).read_bytes()
    monkeypatch.setattr(
        manager, "refresh", lambda: pytest.fail("copy must not refresh or start jobs")
    )
    monkeypatch.setattr(
        manager, "probe_slicer", lambda *_args: pytest.fail("copy must not execute slicers")
    )
    response = prepare_project_reuse(manager, record.job_id)
    copy = response.request
    assert response.source_job_id == record.job_id
    assert response.original_workspace_dir == request.launch.workspace_dir
    assert copy.reuse_source_job_id == record.job_id
    assert (
        copy.launch.workspace_dir
        == manager.config.workspace_root / response.suggested_workspace_name
    )
    assert copy.launch.workspace_dir != request.launch.workspace_dir
    assert copy.launch.build.output_dir == copy.launch.workspace_dir
    assert copy.launch.build.dem_path == request.launch.build.dem_path
    expected = request.model_dump(mode="json")
    expected["reuse_source_job_id"] = record.job_id
    expected["launch"]["workspace_dir"] = str(copy.launch.workspace_dir)
    expected["launch"]["build"]["output_dir"] = str(copy.launch.workspace_dir)
    assert copy.model_dump(mode="json") == expected
    assert response.issues == ()
    assert not copy.launch.workspace_dir.exists()
    assert manager._record_path(record.job_id).read_bytes() == original_record_bytes
    assert manager._request_path(record.job_id).read_bytes() == original_request_bytes
    assert len(tuple(manager.jobs_dir.iterdir())) == 1


def test_reuse_preserves_global_policy_and_relocates_only_generated_paths(
    reuse_manager: LocalJobManager,
) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config, name="global")
    source = GlobalAcquisitionConfig(
        aoi=AreaOfInterestInput(center_wgs84=(99.95, 29.55), radius_m=4321),
        requested_provider_id="auto",
        allow_semantic_fallback=True,
        cache_dir=request.launch.workspace_dir / "cache" / "provider",
        timeout_seconds=41,
        max_attempts=2,
    )
    request = request.model_copy(
        update={"launch": request.launch.model_copy(update={"global_source": source})}
    )
    record = _saved_request(manager, request)
    request.launch.build.dem_path.unlink()
    response = prepare_project_reuse(manager, record.job_id)
    launch = response.request.launch
    assert launch.global_source is not None
    assert launch.global_source.model_dump(exclude={"cache_dir"}) == source.model_dump(
        exclude={"cache_dir"}
    )
    assert launch.global_source.cache_dir == launch.workspace_dir / "cache" / "provider"
    assert launch.build.dem_path == launch.workspace_dir / "global-source-managed.tif"
    assert response.issues == ()
    assert not launch.workspace_dir.exists()


def test_reuse_retains_missing_dem_and_overlay_paths_as_actionable_issues(
    reuse_manager: LocalJobManager,
) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config, name="missing", missing_source=True)
    route = manager.config.input_roots[0] / "missing.gpx"
    overlay = OverlayConfig(
        sources=(
            OverlaySourceConfig(
                source_id="route",
                kind="gpx",
                format="gpx",
                path=route,
                dataset_name="Personal route",
                license="User supplied",
                attribution="Track owner",
            ),
        )
    )
    request = request.model_copy(
        update={"launch": request.launch.model_copy(update={"overlay": overlay})}
    )
    record = _saved_request(manager, request)
    response = prepare_project_reuse(manager, record.job_id)
    assert response.request.launch.overlay == overlay
    assert response.request.launch.build.dem_path == request.launch.build.dem_path
    assert [(item.field, item.code) for item in response.issues] == [
        ("launch.build.dem_path", "missing-input"),
        ("launch.overlay.sources.0.path", "missing-input"),
    ]


def test_restored_job_without_request_hash_keeps_embedded_input_dependency(
    reuse_manager: LocalJobManager,
) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config, name="restored")
    workspace = request.launch.workspace_dir
    workspace.mkdir()
    embedded = workspace / "restored-input.tif"
    embedded.write_bytes(request.launch.build.dem_path.read_bytes())
    request = request.model_copy(
        update={
            "launch": request.launch.model_copy(
                update={
                    "build": request.launch.build.model_copy(update={"dem_path": embedded}),
                }
            )
        }
    )
    record = _saved_request(manager, request)
    manager._write_record(record.model_copy(update={"request_sha256": None}))
    response = prepare_project_reuse(manager, record.job_id)
    assert response.request.launch.build.dem_path == embedded
    assert response.issues[0].code == "workspace-dependency"
    assert embedded.is_file()


def test_reuse_reports_existing_inputs_outside_file_browser_roots(
    reuse_manager: LocalJobManager,
    tmp_path: Path,
) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config, name="outside")
    outside = tmp_path / "outside.tif"
    outside.write_bytes(request.launch.build.dem_path.read_bytes())
    request = request.model_copy(
        update={
            "launch": request.launch.model_copy(
                update={
                    "build": request.launch.build.model_copy(update={"dem_path": outside}),
                }
            )
        }
    )
    record = _saved_request(manager, request)
    response = prepare_project_reuse(manager, record.job_id)
    assert response.issues[0].code == "outside-input-roots"
    assert response.request.launch.build.dem_path == outside


def test_reuse_rejects_modified_original_request(reuse_manager: LocalJobManager) -> None:
    manager = reuse_manager
    record = _saved_request(manager, make_job_request(manager.config))
    manager._request_path(record.job_id).write_bytes(b"{}\n")
    with pytest.raises(ConfigurationError, match="saved project request changed"):
        prepare_project_reuse(manager, record.job_id)


def test_reuse_rejects_original_workspace_mismatch(reuse_manager: LocalJobManager) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config)
    record = _saved_request(manager, request)
    manager._write_record(
        record.model_copy(update={"workspace_dir": request.launch.workspace_dir.with_name("other")})
    )
    with pytest.raises(ConfigurationError, match="does not match its workspace"):
        prepare_project_reuse(manager, record.job_id)


def test_reuse_rejects_oversized_original_request(reuse_manager: LocalJobManager) -> None:
    manager = reuse_manager
    record = _saved_request(manager, make_job_request(manager.config))
    manager._request_path(record.job_id).write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ConfigurationError, match="unreadable"):
        prepare_project_reuse(manager, record.job_id)


def test_reuse_rejects_linked_original_request(
    reuse_manager: LocalJobManager,
    tmp_path: Path,
) -> None:
    manager = reuse_manager
    record = _saved_request(manager, make_job_request(manager.config))
    path = manager._request_path(record.job_id)
    outside = tmp_path / "request.json"
    path.rename(outside)
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    with pytest.raises(ConfigurationError, match="unreadable"):
        prepare_project_reuse(manager, record.job_id)


def test_reuse_rejects_unknown_or_traversal_job_ids(reuse_manager: LocalJobManager) -> None:
    for job_id in ("f" * 32, "../request", "not-a-job"):
        with pytest.raises(KeyError):
            prepare_project_reuse(reuse_manager, job_id)


@pytest.mark.parametrize("destination", ["original", "existing", "nested"])
def test_copy_submission_rejects_original_existing_or_nested_workspaces(
    reuse_manager: LocalJobManager,
    destination: str,
) -> None:
    manager = reuse_manager
    request = make_job_request(manager.config, name="original")
    record = _saved_request(manager, request)
    copy = prepare_project_reuse(manager, record.job_id).request
    target = manager.config.workspace_root / destination
    if destination == "existing":
        target.mkdir()
        (target / "original.txt").write_text("untouched", encoding="utf-8")
    elif destination == "nested":
        request.launch.workspace_dir.mkdir()
        target = request.launch.workspace_dir / "nested"
    copy = copy.model_copy(
        update={"launch": copy.launch.model_copy(update={"workspace_dir": target})}
    )
    with pytest.raises(ConfigurationError, match=r"choose a new|new workspace name"):
        manager.submit(copy)
    if destination == "existing":
        assert (target / "original.txt").read_text(encoding="utf-8") == "untouched"
    assert len(tuple(manager.jobs_dir.iterdir())) == 1


def test_two_validated_copies_cannot_reserve_the_same_workspace(
    reuse_manager: LocalJobManager,
) -> None:
    manager = reuse_manager
    record = _saved_request(manager, make_job_request(manager.config))
    copy = prepare_project_reuse(manager, record.job_id).request
    first, _ = manager.validate_request(copy)
    second, _ = manager.validate_request(copy)
    assert first.reuse_source_job_id == record.job_id
    copied_record = manager.submit(first)
    with pytest.raises(ConfigurationError, match="already reserved"):
        manager.submit(second)
    alias = second.model_copy(
        update={
            "launch": second.launch.model_copy(
                update={
                    "workspace_dir": second.launch.workspace_dir.with_name(
                        second.launch.workspace_dir.name.upper()
                    ),
                }
            )
        }
    )
    with pytest.raises(ConfigurationError, match="already reserved"):
        manager.submit(alias)
    assert copied_record.workspace_dir != record.workspace_dir
    assert not copied_record.workspace_dir.exists()
    assert len(tuple(manager.jobs_dir.iterdir())) == 2


def test_legacy_request_canonical_bytes_do_not_gain_copy_field(web_config: WebAppConfig) -> None:
    request = make_job_request(web_config)
    payload = canonical_json_bytes(request)
    assert b"reuse_source_job_id" not in payload
    assert canonical_json_bytes(JobCreateRequest.model_validate_json(payload)) == payload


@pytest.mark.parametrize("occupied", [True, False])
def test_worker_reserves_copy_workspace_only_after_gate_and_never_overwrites(
    web_config: WebAppConfig,
    monkeypatch: pytest.MonkeyPatch,
    occupied: bool,
) -> None:
    request = make_job_request(web_config, name="worker-copy")
    request = request.model_copy(update={"reuse_source_job_id": "b" * 32})
    workspace = request.launch.workspace_dir
    workspace.parent.mkdir()
    request_path = web_config.state_dir / "jobs" / ("c" * 32) / "request.json"
    jobs_module._atomic_write(request_path, request)
    jobs_root = request_path.parent.parent
    worker_pid = os.getpid()
    monkeypatch.setattr(worker_module, "enable_current_process_containment", lambda: None)
    monkeypatch.setattr(worker_module, "process_identity", lambda _pid: "fixture-worker")
    monkeypatch.setattr(worker_module, "process_group_id", lambda _pid: worker_pid)
    executed: list[Path] = []

    def pass_gate(**_kwargs: object) -> None:
        assert not workspace.exists()
        if occupied:
            workspace.mkdir()
            (workspace / "original.txt").write_text("untouched", encoding="utf-8")

    def execute(_launch: object) -> None:
        assert workspace.is_dir()
        executed.append(workspace)
        raise RuntimeError("test stops before geometry generation")

    monkeypatch.setattr(worker_module, "_wait_for_launch_gate", pass_gate)
    monkeypatch.setattr(worker_module, "execute_workflow_launch", execute)
    result_path = request_path.with_name("result.json")
    code = worker_module.run_worker(
        request_path,
        result_path,
        gate_path=request_path.with_name("launch-gate.json"),
        ready_path=request_path.with_name("worker-ready.json"),
        jobs_root=jobs_root,
        jobs_root_identity=(jobs_root.stat().st_dev, jobs_root.stat().st_ino),
        launch_nonce="d" * 32,
        request_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
        parent_pid=worker_pid,
        parent_identity="fixture-parent",
        gate_timeout_seconds=1,
    )
    result = WorkerResult.model_validate_json(result_path.read_bytes())
    assert code == 2
    assert result.error is not None
    if occupied:
        assert executed == []
        assert (workspace / "original.txt").read_text(encoding="utf-8") == "untouched"
        assert "already exists" in result.error.message
    else:
        assert executed == [workspace]
        assert "test stops before geometry" in result.error.message


def test_api_copy_creates_a_new_completed_job_and_preserves_original_files(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    request = make_job_request(web_config, name="api-original")
    app = create_app(web_config, static_dir=web_static_dir)
    with TestClient(app, base_url="http://localhost") as client:
        created = client.post("/api/v1/jobs", json=request.model_dump(mode="json"))
        assert created.status_code == 201
        original_id = created.json()["job_id"]

        def wait_for_completed(job_id: str) -> dict[str, object]:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                response = client.get(f"/api/v1/jobs/{job_id}")
                assert response.status_code == 200
                value = response.json()
                if value["state"] in {"completed", "failed", "cancelled"}:
                    assert value["state"] == "completed", value.get("error")
                    return value
                time.sleep(0.05)
            pytest.fail("copied workflow did not complete within 90 seconds")

        wait_for_completed(original_id)
        original_workspace = request.launch.workspace_dir
        before = {
            str(path.relative_to(original_workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in original_workspace.rglob("*")
            if path.is_file()
        }
        response = client.get(f"/api/v1/jobs/{original_id}/reuse")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        copy_request = response.json()["request"]
        copy_workspace = Path(copy_request["launch"]["workspace_dir"])
        assert not copy_workspace.exists()
        assert len(client.get("/api/v1/jobs").json()) == 1
        copy_request["launch"]["build"]["model_width_mm"] = 50
        copied = client.post("/api/v1/jobs", json=copy_request)
        assert copied.status_code == 201
        copied_id = copied.json()["job_id"]
        assert copied_id != original_id
        completed = wait_for_completed(copied_id)
        completed_record = JobRecord.model_validate(completed)
        assert completed_record.summary is not None
        dimensions = cast(list[float], completed_record.summary.metrics["dimensions_mm"])
        assert dimensions[0] == pytest.approx(50.0)
        assert Path(str(completed["workspace_dir"])) == copy_workspace
        saved_copy = JobCreateRequest.model_validate_json(
            (web_config.state_dir / "jobs" / copied_id / "request.json").read_bytes()
        )
        assert saved_copy.reuse_source_job_id == original_id
        assert saved_copy.launch.build.model_width_mm == 50
        assert saved_copy.launch.build.dem_path == request.launch.build.dem_path
        after = {
            str(path.relative_to(original_workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in original_workspace.rglob("*")
            if path.is_file()
        }
        assert after == before
        assert client.get(f"/api/v1/jobs/{'f' * 32}/reuse").status_code == 404
