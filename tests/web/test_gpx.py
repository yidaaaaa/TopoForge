from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from topoforge.exceptions import ConfigurationError
from topoforge.overlays import OverlayConfig, OverlayFormat, OverlayKind, OverlaySourceConfig
from topoforge.overlays.sources import parse_gpx_bytes, parse_gpx_source
from topoforge.web import gpx as gpx_module
from topoforge.web.gpx import GpxPreviewRequest, preview_gpx
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import WebAppConfig

from .conftest import make_job_request

_TRACK = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">'
    b"<metadata><name>Local route</name></metadata>"
    b'<trk><trkseg><trkpt lon="100.1" lat="30.2"><ele>99</ele></trkpt>'
    b'<trkpt lon="100.3" lat="30.4"><ele>105</ele></trkpt></trkseg>'
    b'<trkseg><trkpt lon="100.4" lat="30.5"/></trkseg></trk>'
    b'<rte><rtept lon="100.5" lat="30.6"/><rtept lon="100.7" lat="30.8"/></rte>'
    b"</gpx>"
)


@pytest.fixture
def gpx_manager(
    web_config: WebAppConfig, monkeypatch: pytest.MonkeyPatch
) -> Iterator[LocalJobManager]:
    manager = LocalJobManager(web_config)
    monkeypatch.setattr(manager, "_start_queued_jobs", lambda: None)
    monkeypatch.setattr(manager, "_monitor_loop", lambda: None)
    manager.start()
    try:
        yield manager
    finally:
        manager.close()


def _track(manager: LocalJobManager, payload: bytes = _TRACK) -> Path:
    path = manager.config.input_roots[0] / "walk.GPX"
    path.write_bytes(payload)
    return path


def test_preview_preserves_core_coordinates_segments_hash_and_elevation_metadata(
    gpx_manager: LocalJobManager,
) -> None:
    path = _track(gpx_manager)
    source = OverlaySourceConfig(
        source_id="route",
        kind=OverlayKind.GPX,
        format=OverlayFormat.GPX,
        path=path,
        dataset_name="Test route",
        license="unknown",
        attribution="unknown",
    )
    parsed = parse_gpx_source(source)
    result = preview_gpx(gpx_manager, GpxPreviewRequest(path=path))
    assert result.path == str(path)
    assert result.filename == "walk.GPX"
    assert result.sha256 == hashlib.sha256(_TRACK).hexdigest()
    assert result.size_bytes == len(_TRACK)
    assert result.point_count == 4
    assert result.segment_count == 2
    assert result.bounds_wgs84 == (100.1, 30.2, 100.7, 30.8)
    assert result.source_crs == "EPSG:4326"
    for feature, original in zip(result.geojson.features, parsed, strict=True):
        assert feature.geometry.coordinates == tuple(original.geometry.coords)
        assert feature.properties.segment_id == original.feature_id
    properties = result.geojson.features[0].properties
    assert properties.source_elevation_sample_count == 2
    assert properties.source_elevation_min_m == 99
    assert properties.source_elevation_max_m == 105
    assert result.geojson.features[1].properties.source_elevation_min_m is None
    assert "license" not in result.model_dump()
    assert "attribution" not in result.model_dump()
    assert gpx_manager.list() == ()
    assert list(gpx_manager.config.workspace_root.iterdir()) == [gpx_manager.workspace_trash_dir]


def test_preview_returns_new_hash_when_source_changes(gpx_manager: LocalJobManager) -> None:
    path = _track(gpx_manager)
    old = preview_gpx(gpx_manager, GpxPreviewRequest(path=path))
    path.write_bytes(_TRACK.replace(b'lat="30.2"', b'lat="30.1"'))
    new = preview_gpx(gpx_manager, GpxPreviewRequest(path=path))
    assert new.sha256 != old.sha256
    assert new.bounds_wgs84 == (100.1, 30.1, 100.7, 30.8)


@pytest.mark.parametrize("case", ["outside", "hidden", "missing", "wrong-suffix", "directory"])
def test_preview_rejects_unexposed_inputs(gpx_manager: LocalJobManager, case: str) -> None:
    root = gpx_manager.config.input_roots[0]
    path = root / "route.gpx"
    if case == "outside":
        path = root.parent / "outside.gpx"
    if case == "hidden":
        path = root / ".hidden.gpx"
    if case == "wrong-suffix":
        path = root / "route.xml"
    if case == "directory":
        path.mkdir()
    elif case != "missing":
        path.write_bytes(_TRACK)
    with pytest.raises(ConfigurationError):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path))


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_preview_rejects_linked_files(gpx_manager: LocalJobManager, link_kind: str) -> None:
    original = _track(gpx_manager)
    path = original.with_name("linked.gpx")
    try:
        if link_kind == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    except OSError:
        pytest.skip(f"host cannot create {link_kind}")
    with pytest.raises(ConfigurationError):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path))


def test_preview_rejects_byte_budget_before_parsing(
    gpx_manager: LocalJobManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _track(gpx_manager)
    monkeypatch.setattr(gpx_module, "MAX_GPX_BYTES", len(_TRACK) - 1)
    with pytest.raises(ConfigurationError, match="byte safety limit"):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path))


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"<gpx>", "unreadable"),
        (b'<?xml version="1.0" encoding="made-up-encoding"?><gpx />', "unreadable"),
        (b"<xml />", "not <gpx>"),
        (b"<gpx><wpt lon='1' lat='2'/></gpx>", "no track/route"),
        (b"<gpx><rte><rtept lon='nan' lat='1'/><rtept lon='2' lat='3'/></rte></gpx>", "WGS84"),
        (b"<gpx><rte><rtept lon='181' lat='1'/><rtept lon='2' lat='3'/></rte></gpx>", "WGS84"),
        (b"<gpx><rte><rtept lon='1'/><rtept lon='2' lat='3'/></rte></gpx>", "lat/lon"),
        (b"<gpx><rte><rtept lon='1' lat='2'/><rtept lon='1' lat='2'/></rte></gpx>", "zero-length"),
        (
            b"<gpx><rte><rtept lon='179' lat='2'/><rtept lon='-179' lat='3'/></rte></gpx>",
            "antimeridian",
        ),
        (
            b"<gpx><rte><rtept lon='1' lat='2'><ele>inf</ele></rtept>"
            b"<rtept lon='2' lat='3'/></rte></gpx>",
            "non-finite",
        ),
    ],
)
def test_preview_rejects_invalid_or_unsupported_tracks(
    gpx_manager: LocalJobManager, payload: bytes, error: str
) -> None:
    path = _track(gpx_manager, payload)
    with pytest.raises(ConfigurationError, match=error):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_shared_parser_rejects_dtd_before_entity_expansion(encoding: str) -> None:
    payload = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE gpx [<!ENTITY a "1"><!ENTITY b "&a;&a;&a;">]>'
        '<gpx><rte><rtept lat="1" lon="&b;"/><rtept lat="2" lon="3"/></rte></gpx>'
    ).encode(encoding)
    with pytest.raises(ConfigurationError, match=r"DTD|unreadable"):
        parse_gpx_bytes(payload, source_name="entity.gpx")


@pytest.mark.parametrize(
    ("budget", "limit", "message"),
    [
        ("max_points", 4, "points"),
        ("max_segments", 2, "segments"),
        ("max_elements", 5, "XML elements"),
    ],
)
def test_shared_parser_budgets_count_incomplete_segments(
    budget: str, limit: int, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_gpx_bytes(_TRACK, source_name="track.gpx", **{budget: limit})


def test_shared_parser_rejects_excessive_nesting() -> None:
    with pytest.raises(ConfigurationError, match="nesting"):
        parse_gpx_bytes(b"<gpx>" + b"<a>" * 256 + b"</a>" * 256 + b"</gpx>", source_name="deep.gpx")


def test_preview_request_rejects_unexpected_launch_or_metadata_fields() -> None:
    with pytest.raises(ValidationError):
        GpxPreviewRequest.model_validate({"path": "route.gpx", "license": "CC0"})


def _save_retained_route(
    manager: LocalJobManager,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gpx_source: bool = True,
) -> str:
    monkeypatch.setattr(manager, "_start_queued_jobs", lambda: None)
    request = make_job_request(manager.config, name="retained-gpx")
    source = OverlaySourceConfig(
        source_id="route",
        kind=OverlayKind.GPX if gpx_source else OverlayKind.ROAD,
        format=OverlayFormat.GPX if gpx_source else OverlayFormat.GEOJSON,
        path=path,
        dataset_name="Retained route",
        license="User supplied",
        attribution="Track owner",
    )
    request = request.model_copy(
        update={
            "launch": request.launch.model_copy(
                update={
                    "overlay": OverlayConfig(sources=(source,)),
                }
            )
        }
    )
    return manager.submit(request).job_id


def _retained_track(manager: LocalJobManager) -> Path:
    path = manager.config.workspace_root / "retained-gpx" / "inputs" / "retained.gpx"
    path.parent.mkdir(parents=True)
    path.write_bytes(_TRACK)
    return path


def test_reused_gpx_preview_reads_only_the_exact_retained_source(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _retained_track(gpx_manager)
    source_id = _save_retained_route(gpx_manager, path, monkeypatch)
    record_before = gpx_manager._record_path(source_id).read_bytes()
    request_before = gpx_manager._request_path(source_id).read_bytes()
    with pytest.raises(ConfigurationError, match="outside configured input roots"):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path))
    result = preview_gpx(gpx_manager, GpxPreviewRequest(path=path, reuse_source_job_id=source_id))
    assert result.sha256 == hashlib.sha256(_TRACK).hexdigest()
    assert result.path == str(path)
    assert result.point_count == 4
    assert gpx_manager._record_path(source_id).read_bytes() == record_before
    assert gpx_manager._request_path(source_id).read_bytes() == request_before
    assert len(tuple(gpx_manager.jobs_dir.iterdir())) == 1


@pytest.mark.parametrize("case", ["neighbor", "different-directory", "non-gpx-source"])
def test_reused_gpx_never_authorizes_other_files_or_source_kinds(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path = _retained_track(gpx_manager)
    source_id = _save_retained_route(
        gpx_manager, path, monkeypatch, gpx_source=case != "non-gpx-source"
    )
    target = path
    if case == "neighbor":
        target = path.with_name("neighbor.gpx")
    elif case == "different-directory":
        target = path.parent.parent / path.name
    if target != path:
        target.write_bytes(_TRACK)
    monkeypatch.setattr(
        gpx_module,
        "read_owned_regular_bytes",
        lambda *_args, **_kwargs: pytest.fail("unapproved GPX must not be read"),
    )
    with pytest.raises(ConfigurationError, match="outside configured input roots"):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=target, reuse_source_job_id=source_id))


def test_reused_gpx_allows_new_route_within_normal_browser_roots(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = _save_retained_route(gpx_manager, _retained_track(gpx_manager), monkeypatch)
    new_path = _track(gpx_manager)
    result = preview_gpx(
        gpx_manager, GpxPreviewRequest(path=new_path, reuse_source_job_id=source_id)
    )
    assert result.path == str(new_path)


def test_reused_gpx_rejects_unknown_source_job_before_reading_input(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _retained_track(gpx_manager)
    monkeypatch.setattr(
        gpx_module,
        "read_owned_regular_bytes",
        lambda *_args, **_kwargs: pytest.fail("unknown project cannot authorize GPX reads"),
    )
    with pytest.raises(ConfigurationError, match="original project is no longer available"):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path, reuse_source_job_id="f" * 32))


@pytest.mark.parametrize("source_id", ["../jobs", "", "F" * 32, "f" * 31])
def test_reused_gpx_rejects_invalid_source_job_ids(source_id: str) -> None:
    with pytest.raises(ValidationError):
        GpxPreviewRequest(path=Path("track.gpx"), reuse_source_job_id=source_id)


def test_reused_gpx_rejects_changed_original_request(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _retained_track(gpx_manager)
    source_id = _save_retained_route(gpx_manager, path, monkeypatch)
    gpx_manager._request_path(source_id).write_bytes(b"{}\n")
    monkeypatch.setattr(
        gpx_module,
        "read_owned_regular_bytes",
        lambda *_args, **_kwargs: pytest.fail("changed project cannot authorize GPX reads"),
    )
    with pytest.raises(ConfigurationError, match="saved project request changed"):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path, reuse_source_job_id=source_id))


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink", "parent-symlink"])
def test_reused_gpx_keeps_leaf_and_parent_link_protection(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    path = _retained_track(gpx_manager)
    source_id = _save_retained_route(gpx_manager, path, monkeypatch)
    original = path.with_name("original.gpx")
    path.rename(original)
    try:
        if link_kind == "symlink":
            path.symlink_to(original)
        elif link_kind == "hardlink":
            os.link(original, path)
        else:
            original.rename(path)
            parent = path.parent
            moved = parent.with_name("moved-inputs")
            parent.rename(moved)
            parent.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip(f"host cannot create {link_kind}")
    with pytest.raises(ConfigurationError):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path, reuse_source_job_id=source_id))


def test_reused_gpx_keeps_byte_budget_before_parsing(
    gpx_manager: LocalJobManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _retained_track(gpx_manager)
    source_id = _save_retained_route(gpx_manager, path, monkeypatch)
    monkeypatch.setattr(gpx_module, "MAX_GPX_BYTES", len(_TRACK) - 1)
    with pytest.raises(ConfigurationError, match="byte safety limit"):
        preview_gpx(gpx_manager, GpxPreviewRequest(path=path, reuse_source_job_id=source_id))
