from __future__ import annotations

import hashlib
import io
import json
import math
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from PIL import Image
from rasterio.transform import from_bounds

from topoforge.exceptions import ConfigurationError
from topoforge.util import sha256_file
from topoforge.web.api import create_app
from topoforge.web.jobs import LocalJobManager
from topoforge.web.map_tiles import MapTileStyle, WebVisualizationService
from topoforge.web.models import JobCreateRequest, WebAppConfig

from .conftest import make_job_request


def _xyz(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    count = 1 << zoom
    x = int((longitude + 180.0) / 360.0 * count)
    y = int((1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2.0 * count)
    return x, y


def _write_projected_fixture(
    path: Path,
    *,
    crs: str,
    bounds: tuple[float, float, float, float],
) -> None:
    values = np.arange(16, dtype=np.float32).reshape(4, 4)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_bounds(*bounds, 4, 4),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(values, 1)


def _wait_for_completion(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        record = response.json()
        if record["state"] in {"completed", "failed", "cancelled"}:
            return record
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not complete")


def test_completed_job_map_tiles_cache_and_assembly_contract(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    base = make_job_request(web_config, name="phase10-map")
    build = base.launch.build.model_copy(
        update={
            "model_width_mm": 64.0,
            "model_depth_mm": 48.0,
        }
    )
    request = JobCreateRequest(
        launch=base.launch.model_copy(
            update={
                "build": build,
                "maximum_tile_width_mm": 32.0,
                "maximum_tile_depth_mm": 24.0,
            }
        )
    )
    app = create_app(web_config, static_dir=web_static_dir)
    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/jobs",
            json=request.model_dump(mode="json"),
        )
        assert submitted.status_code == 201
        job_id = submitted.json()["job_id"]
        completed = _wait_for_completion(client, job_id)
        assert completed["state"] == "completed", completed.get("error")
        assert completed["exit_code"] == 0

        manifest_response = client.get(f"/api/v1/jobs/{job_id}/map/manifest")
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        assert manifest["schema_version"] == "topoforge-web-map-v1"
        assert manifest["generator"] == "topoforge-map-tiles-v2"
        assert manifest["crosses_antimeridian"] is False
        assert manifest["web_mercator_latitude_clipped"] is False
        assert manifest["styles"] == ["terrain", "elevation", "hillshade"]
        assert manifest["tile_count"] == 4
        assert manifest["tile_grid_shape"] == [2, 2]
        assert len(manifest["tile_footprints_geojson"]["features"]) == 4
        assert manifest["required_checks_passed"] is True

        zoom = manifest["maxzoom"]
        longitude, latitude = manifest["center_wgs84"]
        x, y = _xyz(float(longitude), float(latitude), int(zoom))
        first_hashes: dict[str, str] = {}
        for style in manifest["styles"]:
            url = f"/api/v1/jobs/{job_id}/map/tiles/{style}/{zoom}/{x}/{y}.png"
            first = client.get(url)
            assert first.status_code == 200
            assert first.headers["x-topoforge-cache"] == "miss"
            assert first.headers["content-type"] == "image/png"
            with Image.open(io.BytesIO(first.content)) as image:
                image.load()
                assert image.mode == "RGBA"
                assert image.size == (256, 256)
                assert image.getbbox() is not None
            digest = hashlib.sha256(first.content).hexdigest()
            assert first.headers["x-topoforge-tile-sha256"] == digest
            first_hashes[style] = digest

            second = client.get(url)
            assert second.status_code == 200
            assert second.headers["x-topoforge-cache"] == "hit"
            assert second.content == first.content

        service = app.state.visualization_service
        tile_path, _, _ = service.tile(job_id, MapTileStyle.TERRAIN, zoom, x, y)
        tile_path.write_bytes(b"corrupt")
        regenerated = client.get(f"/api/v1/jobs/{job_id}/map/tiles/terrain/{zoom}/{x}/{y}.png")
        assert regenerated.status_code == 200
        assert regenerated.headers["x-topoforge-cache"] == "regenerated-corrupt"
        assert hashlib.sha256(regenerated.content).hexdigest() == first_hashes["terrain"]

        record_path = tile_path.with_suffix(".json")
        cache_record = json.loads(record_path.read_text(encoding="utf-8"))
        cache_record["generator"] = "tampered-generator"
        record_path.write_text(
            json.dumps(cache_record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        regenerated_record = client.get(
            f"/api/v1/jobs/{job_id}/map/tiles/terrain/{zoom}/{x}/{y}.png"
        )
        assert regenerated_record.headers["x-topoforge-cache"] == "regenerated-corrupt"
        assert regenerated_record.content == regenerated.content

        outside = client.get(f"/api/v1/jobs/{job_id}/map/tiles/terrain/0/0/0.png")
        assert outside.status_code == 404

        assembly_response = client.get(f"/api/v1/jobs/{job_id}/assembly")
        assert assembly_response.status_code == 200
        assembly = assembly_response.json()
        assert assembly["schema_version"] == "topoforge-web-assembly-v1"
        assert assembly["tile_count"] == 4
        assert assembly["connector_count"] > 0
        assert assembly["required_checks_passed"] is True
        assert [tile["tile_id"] for tile in assembly["tiles"]] == [
            "tile-r0000-c0000",
            "tile-r0000-c0001",
            "tile-r0001-c0000",
            "tile-r0001-c0001",
        ]
        tile = assembly["tiles"][0]
        glb = client.get(tile["glb_url"])
        assert glb.status_code == 200
        assert glb.headers["content-type"] == "model/gltf-binary"
        assert hashlib.sha256(glb.content).hexdigest() == tile["glb_sha256"]

        assembly_root, _ = service.manager.directory_artifact_path(job_id, "print_tiles_directory")
        assembly_manifest_path = assembly_root / "print-tile-assembly-manifest.json"
        tampered = json.loads(assembly_manifest_path.read_text(encoding="utf-8"))
        tampered["east_axis"] = "tampered-axis"
        assembly_manifest_path.write_text(
            json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        rejected_assembly = client.get(f"/api/v1/jobs/{job_id}/assembly")
        assert rejected_assembly.status_code == 422
        assert "bound to the connect stage" in rejected_assembly.json()["detail"]["message"]
        rejected_map = client.get(f"/api/v1/jobs/{job_id}/map/manifest")
        assert rejected_map.status_code == 422


def test_web_mercator_coverage_handles_antimeridian_and_latitude_limits(
    web_config: WebAppConfig,
    tmp_path: Path,
) -> None:
    service = WebVisualizationService(LocalJobManager(web_config))
    dateline = tmp_path / "dateline.tif"
    _write_projected_fixture(
        dateline,
        crs=("+proj=aeqd +lat_0=-15.75 +lon_0=-179.95 +datum=WGS84 +units=m +no_defs"),
        bounds=(-30_000.0, -30_000.0, 30_000.0, 30_000.0),
    )
    dateline_info = service._raster_info(dateline, sha256_file(dateline))
    assert dateline_info.crosses_antimeridian is True
    assert dateline_info.bounds_wgs84[0] > dateline_info.bounds_wgs84[2]
    assert dateline_info.center_wgs84[0] == pytest.approx(-179.95, abs=0.05)
    assert len(dateline_info.bounds_mercator_m) == 2
    assert all(bounds[2] - bounds[0] < 100_000 for bounds in dateline_info.bounds_mercator_m)

    partial = tmp_path / "partial-polar.tif"
    _write_projected_fixture(
        partial,
        crs=("+proj=aeqd +lat_0=85.0 +lon_0=20.0 +datum=WGS84 +units=m +no_defs"),
        bounds=(-30_000.0, -30_000.0, 30_000.0, 30_000.0),
    )
    partial_info = service._raster_info(partial, sha256_file(partial))
    assert partial_info.web_mercator_latitude_clipped is True
    assert partial_info.bounds_wgs84[3] == pytest.approx(85.05112878)
    assert partial_info.bounds_wgs84[1] < partial_info.bounds_wgs84[3]

    outside = tmp_path / "outside-polar.tif"
    _write_projected_fixture(
        outside,
        crs=("+proj=aeqd +lat_0=86.0 +lon_0=20.0 +datum=WGS84 +units=m +no_defs"),
        bounds=(-10_000.0, -10_000.0, 10_000.0, 10_000.0),
    )
    with pytest.raises(ConfigurationError, match="outside Web Mercator latitude coverage"):
        service._raster_info(outside, sha256_file(outside))
