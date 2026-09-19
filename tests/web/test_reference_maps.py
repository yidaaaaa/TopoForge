from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from topoforge.exceptions import ConfigurationError
from topoforge.web.api import create_app
from topoforge.web.models import WebAppConfig
from topoforge.web.reference_maps import (
    MAX_IMAGE_BYTES,
    STANDARD_MAP_IMAGE,
    STANDARD_MAP_METADATA,
    prepare_standard_map_tiles,
    read_local_standard_map,
    read_standard_map_tile,
)


def _install(state: Path) -> tuple[Path, Path, bytes]:
    root = state / "reference-map"
    root.mkdir(parents=True, exist_ok=True)
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "white").save(stream, format="JPEG")
    image_bytes = stream.getvalue()
    metadata = {
        "schema_version": "topoforge-local-standard-map-v1",
        "title": "Synthetic test image",
        "source_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "width_px": 12,
        "height_px": 8,
    }
    image = root / STANDARD_MAP_IMAGE
    info = root / STANDARD_MAP_METADATA
    image.write_bytes(image_bytes)
    info.write_text(json.dumps(metadata), encoding="utf-8")
    prepare_standard_map_tiles(state)
    return info, image, image_bytes


def test_absence_and_exact_original_bytes(web_config: WebAppConfig, web_static_dir: Path) -> None:
    with TestClient(
        create_app(web_config, static_dir=web_static_dir, allow_testserver_host=True)
    ) as client:
        assert client.get("/api/v1/reference/standard-map").json() is None
        assert client.get("/api/v1/reference/standard-map/image").status_code == 404
        _, _, expected = _install(web_config.state_dir)
        response = client.get("/api/v1/reference/standard-map")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        metadata = response.json()
        tile_url = (
            metadata["tile_url_template"]
            .replace("{level}", "0")
            .replace("{x}", "0")
            .replace("{y}", "0")
        )
        tile = client.get(tile_url)
        assert tile.status_code == 200
        with (
            Image.open(io.BytesIO(tile.content)) as decoded,
            Image.open(io.BytesIO(expected)) as source,
        ):
            assert decoded.convert("RGB").tobytes() == source.convert("RGB").tobytes()
        assert client.get(tile_url.replace("/0/0/0.png", "/7/0/0.png")).status_code == 404
        original = client.get(metadata["image_url"])
        assert original.status_code == 200
        assert original.headers["content-type"] == "image/jpeg"
        assert original.content == expected
        assert client.get("/api/v1/reference/standard-map/image?sha256=stale").status_code == 409


def test_corrupt_reference_is_reported_while_app_stays_usable(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    info, _, _ = _install(web_config.state_dir)
    info.write_text("broken", encoding="utf-8")
    with TestClient(
        create_app(web_config, static_dir=web_static_dir, allow_testserver_host=True)
    ) as client:
        response = client.get("/api/v1/reference/standard-map")
        assert response.status_code == 422
        assert "source metadata" in response.json()["detail"]["message"]
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize(
    "problem", ["hash", "dimensions", "format", "oversized", "missing", "pixels"]
)
def test_invalid_original_is_rejected(tmp_path: Path, problem: str) -> None:
    info, image, expected = _install(tmp_path)
    data = json.loads(info.read_text())
    if problem == "hash":
        image.write_bytes(expected + b"changed")
    elif problem == "dimensions":
        data["width_px"] = 20
    elif problem == "format":
        image.write_bytes(b"not an image")
        data["source_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    elif problem == "oversized":
        with image.open("wb") as stream:
            stream.truncate(MAX_IMAGE_BYTES + 1)
    elif problem == "missing":
        image.unlink()
    elif problem == "pixels":
        data["width_px"] = data["height_px"] = 20_000
    info.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        read_local_standard_map(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation may require a Windows privilege")
@pytest.mark.parametrize("directory_link", [False, True])
def test_original_must_stay_within_state(tmp_path: Path, directory_link: bool) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    _, external, _ = _install(outside)
    state.mkdir()
    if directory_link:
        (state / "reference-map").symlink_to(external.parent, target_is_directory=True)
    else:
        root = state / "reference-map"
        root.mkdir()
        (root / STANDARD_MAP_IMAGE).symlink_to(external)
    with pytest.raises(ConfigurationError, match="inside state_dir"):
        read_local_standard_map(state)


def test_original_viewer_assets_do_not_fall_through_to_spa(
    web_config: WebAppConfig,
    web_static_dir: Path,
) -> None:
    files = {
        "standard-map.html": '<!doctype html><main id="viewport">original</main>',
        "standard-map-viewer.js": 'document.body.dataset.original = "true";',
    }
    manifest_path = web_static_dir / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name, text in files.items():
        payload = text.encode()
        (web_static_dir / name).write_bytes(payload)
        manifest["assets"].append(name)
        manifest["sha256"][name] = hashlib.sha256(payload).hexdigest()
        manifest["sizes"][name] = len(payload)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with TestClient(
        create_app(web_config, static_dir=web_static_dir, allow_testserver_host=True)
    ) as client:
        for name, content in files.items():
            response = client.get("/" + name)
            assert response.status_code == 200
            assert response.text == content
        assert (
            client.get("/standard-map-viewer.js")
            .headers["content-type"]
            .startswith("text/javascript")
        )


def test_pyramid_preserves_all_native_pixels_across_partial_edge_tiles(tmp_path: Path) -> None:
    root = tmp_path / "reference-map"
    root.mkdir()
    image = Image.new("RGB", (1025, 769), "white")
    image.putpixel((1024, 768), (255, 0, 0))
    image.putpixel((512, 511), (0, 0, 255))
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    original = stream.getvalue()
    digest = hashlib.sha256(original).hexdigest()
    (root / STANDARD_MAP_IMAGE).write_bytes(original)
    (root / STANDARD_MAP_METADATA).write_text(
        json.dumps(
            {
                "schema_version": "topoforge-local-standard-map-v1",
                "title": "Synthetic edge fixture",
                "source_sha256": digest,
                "width_px": 1025,
                "height_px": 769,
            }
        ),
        encoding="utf-8",
    )
    pyramid = prepare_standard_map_tiles(tmp_path)
    assert pyramid.max_level == 2
    assert len(pyramid.tiles) == 9
    reconstructed = Image.new("RGB", (1025, 769))
    for x in range(3):
        for y in range(2):
            payload = read_standard_map_tile(tmp_path, digest, 2, x, y)
            with Image.open(io.BytesIO(payload)) as tile:
                reconstructed.paste(tile, (x * 512, y * 512))
    with Image.open(io.BytesIO(original)) as decoded:
        assert reconstructed.tobytes() == decoded.convert("RGB").tobytes()
    assert (root / STANDARD_MAP_IMAGE).read_bytes() == original


def test_tile_reads_do_not_decode_the_full_image_and_detect_changed_tiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, original = _install(tmp_path)
    digest = hashlib.sha256(original).hexdigest()

    def reject_decode(*args: object, **kwargs: object) -> None:
        raise AssertionError("A display tile must not decode the full source image")

    monkeypatch.setattr(Image, "open", reject_decode)
    assert read_standard_map_tile(tmp_path, digest, 0, 0, 0).startswith(b"\x89PNG")
    tile = tmp_path / "reference-map" / "standard-map-tiles" / digest / "0" / "0" / "0.png"
    tile.write_bytes(b"changed")
    with pytest.raises(ConfigurationError, match="recorded digest"):
        read_standard_map_tile(tmp_path, digest, 0, 0, 0)


@pytest.mark.skipif(os.name == "nt", reason="Symlink creation may require a Windows privilege")
def test_pyramid_cache_cannot_escape_reference_map(tmp_path: Path) -> None:
    _, _, original = _install(tmp_path)
    digest = hashlib.sha256(original).hexdigest()
    cache = tmp_path / "reference-map" / "standard-map-tiles"
    outside = tmp_path / "outside"
    cache.rename(outside)
    cache.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="inside state_dir/reference-map"):
        read_standard_map_tile(tmp_path, digest, 0, 0, 0)


def test_preparing_again_repairs_failed_cache_and_preserves_original(tmp_path: Path) -> None:
    _, original_path, original = _install(tmp_path)
    digest = hashlib.sha256(original).hexdigest()
    cache = tmp_path / "reference-map" / "standard-map-tiles" / digest
    (cache / "0" / "0" / "0.png").write_bytes(b"broken")
    restored = prepare_standard_map_tiles(tmp_path)
    assert restored.source_sha256 == digest
    assert read_standard_map_tile(tmp_path, digest, 0, 0, 0).startswith(b"\x89PNG")
    assert original_path.read_bytes() == original
    assert len(list(cache.parent.glob(digest + ".invalid-*"))) == 1
