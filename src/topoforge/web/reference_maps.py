"""Bounded access to a user-supplied map image, without geographic registration."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Literal

from PIL import Image, ImageCms, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, ValidationError

from topoforge.exceptions import ConfigurationError

STANDARD_MAP_IMAGE = "local-standard-map.jpg"
STANDARD_MAP_METADATA = "local-standard-map.json"
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024


class LocalStandardMap(BaseModel):
    """Source metadata for an unchanged local JPEG reference."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["topoforge-local-standard-map-v1"]
    title: str = Field(min_length=1, max_length=200)
    source_url: HttpUrl | None = None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: int = Field(strict=True, ge=1, le=32_768)
    height_px: int = Field(strict=True, ge=1, le=32_768)
    provenance: dict[str, JsonValue] = Field(default_factory=dict)


def _read_bounded(path: Path, limit_bytes: int) -> bytes:
    with path.open("rb") as stream:
        result = stream.read(limit_bytes + 1)
    if len(result) > limit_bytes:
        raise ConfigurationError(f"Reduce {path.name} to at most {limit_bytes} bytes")
    return result


def read_local_standard_map(state_dir: Path) -> tuple[LocalStandardMap, bytes] | None:
    """Validate the fixed local source files and return the exact image bytes."""
    state = state_dir.resolve()
    root = (state / "reference-map").resolve()
    metadata_path = root / STANDARD_MAP_METADATA
    image_path = root / STANDARD_MAP_IMAGE
    if not root.is_relative_to(state) or any(
        not path.resolve().is_relative_to(root) for path in (metadata_path, image_path)
    ):
        raise ConfigurationError("Keep local standard-map files inside state_dir/reference-map")
    if not metadata_path.exists() and not image_path.exists():
        return None
    try:
        metadata_bytes = _read_bounded(metadata_path, MAX_METADATA_BYTES)
        image_bytes = _read_bounded(image_path, MAX_IMAGE_BYTES)
    except OSError as exc:
        raise ConfigurationError(
            "Place readable local-standard-map.json and local-standard-map.jpg "
            "together in state_dir/reference-map"
        ) from exc
    try:
        metadata = LocalStandardMap.model_validate_json(metadata_bytes)
    except ValidationError as exc:
        raise ConfigurationError("Correct the source metadata in local-standard-map.json") from exc
    if metadata.width_px * metadata.height_px > 80_000_000:
        raise ConfigurationError("Use a local standard-map image of at most 80 million pixels")
    if hashlib.sha256(image_bytes).hexdigest() != metadata.source_sha256:
        raise ConfigurationError("Restore the original map image matching the recorded SHA-256")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format != "JPEG" or image.size != (metadata.width_px, metadata.height_px):
                raise ConfigurationError("Supply a JPEG matching the recorded image dimensions")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ConfigurationError(
            "Replace local-standard-map.jpg with a readable original JPEG"
        ) from exc
    return metadata, image_bytes


class StandardMapPyramid(BaseModel):
    """A local display pyramid tied to the original image digest."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["topoforge-standard-map-pyramid-v1"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: int = Field(strict=True, ge=1, le=32_768)
    height_px: int = Field(strict=True, ge=1, le=32_768)
    tile_size_px: Literal[512] = 512
    max_level: int = Field(strict=True, ge=0, le=6)
    tiles: dict[str, str] = Field(min_length=1, max_length=600)


def _pyramid_root(state_dir: Path, source_sha256: str) -> Path:
    state = state_dir.resolve()
    reference = (state / "reference-map").resolve()
    cache = (reference / "standard-map-tiles").resolve()
    root = cache / source_sha256
    if (
        not reference.is_relative_to(state)
        or not cache.is_relative_to(reference)
        or not root.resolve().is_relative_to(cache)
    ):
        raise ConfigurationError("Keep standard-map-tiles inside state_dir/reference-map")
    return root


def read_standard_map_pyramid(state_dir: Path, metadata: LocalStandardMap) -> StandardMapPyramid:
    """Read the bounded local display index without decoding the original image."""
    import math

    root = _pyramid_root(state_dir, metadata.source_sha256)
    index = root / "pyramid.json"
    if not index.resolve().is_relative_to(root.resolve()):
        raise ConfigurationError("Keep the map pyramid index inside its source directory")
    try:
        result = StandardMapPyramid.model_validate_json(_read_bounded(index, 128 * 1024))
    except (OSError, ValidationError) as exc:
        raise ConfigurationError(
            "Run scripts/prepare_standard_map.py for this state directory"
        ) from exc
    max_level = max(0, math.ceil(math.log2(max(metadata.width_px, metadata.height_px) / 512)))
    expected = set()
    for level in range(max_level + 1):
        divisor = 2 ** (max_level - level)
        columns = math.ceil(math.ceil(metadata.width_px / divisor) / 512)
        rows = math.ceil(math.ceil(metadata.height_px / divisor) / 512)
        expected.update(f"{level}/{x}/{y}.png" for x in range(columns) for y in range(rows))
    if (
        result.source_sha256 != metadata.source_sha256
        or (result.width_px, result.height_px) != (metadata.width_px, metadata.height_px)
        or result.max_level != max_level
        or set(result.tiles) != expected
        or any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in result.tiles.values()
        )
    ):
        raise ConfigurationError("Rebuild the local map pyramid to match its source metadata")
    return result


def read_standard_map_tile(
    state_dir: Path, source_sha256: str, level: int, x: int, y: int
) -> bytes:
    """Read one verified display tile, never decoding the full image per request."""
    root = (state_dir.resolve() / "reference-map").resolve()
    if not root.is_relative_to(state_dir.resolve()):
        raise ConfigurationError("Keep reference-map inside state_dir")
    path = root / STANDARD_MAP_METADATA
    if not path.resolve().is_relative_to(root):
        raise ConfigurationError("Keep local map metadata inside reference-map")
    try:
        metadata = LocalStandardMap.model_validate_json(_read_bounded(path, MAX_METADATA_BYTES))
    except (OSError, ValidationError) as exc:
        raise ConfigurationError("Restore readable local standard-map metadata") from exc
    if source_sha256 != metadata.source_sha256:
        raise ConfigurationError("Refresh the original-map view to load the current image")
    pyramid = read_standard_map_pyramid(state_dir, metadata)
    key = f"{level}/{x}/{y}.png"
    if key not in pyramid.tiles:
        raise ConfigurationError("Select a tile within the local map image")
    tile_root = _pyramid_root(state_dir, source_sha256).resolve()
    tile = tile_root / key
    if not tile.resolve().is_relative_to(tile_root):
        raise ConfigurationError("Keep map tiles inside their source directory")
    try:
        payload = _read_bounded(tile, 2 * 1024 * 1024)
    except OSError as exc:
        raise ConfigurationError("Restore the missing local map display tile") from exc
    if hashlib.sha256(payload).hexdigest() != pyramid.tiles[key]:
        raise ConfigurationError("Restore the local map display tile matching its recorded digest")
    return payload


def prepare_standard_map_tiles(state_dir: Path) -> StandardMapPyramid:
    """Prepare a lossless RGB display pyramid; preserve the original JPEG bytes."""
    import math
    import uuid

    result = read_local_standard_map(state_dir)
    if result is None:
        raise ConfigurationError(
            "Install the original map and source metadata before preparing tiles"
        )
    metadata, image_bytes = result
    target = _pyramid_root(state_dir, metadata.source_sha256)
    if target.exists():
        try:
            existing = read_standard_map_pyramid(state_dir, metadata)
            for key in existing.tiles:
                level, x, y = (int(part) for part in key.removesuffix(".png").split("/"))
                read_standard_map_tile(state_dir, metadata.source_sha256, level, x, y)
            return existing
        except ConfigurationError:
            # Preserve the failed cache while preparing its verified replacement.
            target.rename(target.with_name(target.name + ".invalid-" + uuid.uuid4().hex))
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".preparing-" + uuid.uuid4().hex)
    staging.mkdir()
    max_level = max(0, math.ceil(math.log2(max(metadata.width_px, metadata.height_px) / 512)))
    hashes: dict[str, str] = {}
    with Image.open(io.BytesIO(image_bytes)) as source:
        profile = source.info.get("icc_profile")
        if profile:
            original = ImageCms.profileToProfile(
                source,
                ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                ImageCms.createProfile("sRGB"),
                outputMode="RGB",
            )
        else:
            original = source.convert("RGB")
    if original is None:
        raise ConfigurationError("Use a local map image with a readable embedded colour profile")
    try:
        for level in range(max_level, -1, -1):
            divisor = 2 ** (max_level - level)
            size = (math.ceil(metadata.width_px / divisor), math.ceil(metadata.height_px / divisor))
            scaled = original if divisor == 1 else original.resize(size, Image.Resampling.LANCZOS)
            try:
                for x in range(math.ceil(size[0] / 512)):
                    directory = staging / str(level) / str(x)
                    directory.mkdir(parents=True)
                    for y in range(math.ceil(size[1] / 512)):
                        bounds = (
                            x * 512,
                            y * 512,
                            min((x + 1) * 512, size[0]),
                            min((y + 1) * 512, size[1]),
                        )
                        with scaled.crop(bounds) as tile:
                            stream = io.BytesIO()
                            tile.save(stream, format="PNG", compress_level=3)
                            payload = stream.getvalue()
                        key = f"{level}/{x}/{y}.png"
                        (staging / key).write_bytes(payload)
                        hashes[key] = hashlib.sha256(payload).hexdigest()
            finally:
                if scaled is not original:
                    scaled.close()
    finally:
        original.close()
    pyramid = StandardMapPyramid(
        schema_version="topoforge-standard-map-pyramid-v1",
        source_sha256=metadata.source_sha256,
        width_px=metadata.width_px,
        height_px=metadata.height_px,
        max_level=max_level,
        tiles=hashes,
    )
    (staging / "pyramid.json").write_text(
        pyramid.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    staging.rename(target)
    return read_standard_map_pyramid(state_dir, metadata)
