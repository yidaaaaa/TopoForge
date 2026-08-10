"""Deterministic local raster tiles and assembly visualization contracts."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, TypeVar

import numpy as np
import numpy.typing as npt
import rasterio
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Transformer
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

from topoforge.exceptions import ConfigurationError
from topoforge.tiling.connectors import (
    ConnectorPlan,
    PrintTileAssemblyManifest,
    PrintTileAssemblyValidation,
)
from topoforge.util import sha256_file
from topoforge.web.jobs import LocalJobManager
from topoforge.web.models import JobRecord, JobState
from topoforge.workflow import LocalWorkflowManifest, WorkflowStage

_MAP_SCHEMA_VERSION = "topoforge-web-map-v1"
_ASSEMBLY_SCHEMA_VERSION = "topoforge-web-assembly-v1"
_CACHE_SCHEMA_VERSION = "topoforge-web-map-tile-cache-v1"
_GENERATOR_VERSION = "topoforge-map-tiles-v2"
_TILE_SIZE = 256
_WEB_MERCATOR_HALF_WORLD_M = 20_037_508.342789244
_WEB_MERCATOR_MAX_LATITUDE = 85.05112878
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BoundsWgs84 = tuple[float, float, float, float]
BoundsMm = tuple[float, float, float, float, float, float]
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class MapTileStyle(StrEnum):
    """Stable visual styles generated from the same processed elevation raster."""

    TERRAIN = "terrain"
    ELEVATION = "elevation"
    HILLSHADE = "hillshade"


class MapAssemblyTile(BaseModel):
    """One print tile exposed to the map and 3D assembly views."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_id: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    physical_bounds_mm: tuple[float, float, float, float]
    global_bounds_mm: BoundsMm
    triangle_count: int = Field(gt=0)
    volume_mm3: float = Field(gt=0)
    male_connector_ids: tuple[str, ...]
    female_connector_ids: tuple[str, ...]
    glb_url: str
    glb_sha256: Sha256Hex


class MapAssemblyConnector(BaseModel):
    """One stable connector placement used by the 2D assembly diagram."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_id: str
    seam_id: str
    direction: str
    male_tile_id: str
    female_tile_id: str
    seam_coordinate_mm: float
    center_along_seam_mm: float
    insertion_axis: str


class JobAssemblyOverview(BaseModel):
    """Strict assembly metadata reused by both browser assembly modes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["topoforge-web-assembly-v1"] = _ASSEMBLY_SCHEMA_VERSION
    job_id: str
    layout_id: str
    model_size_mm: tuple[float, float]
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    seam_count: int = Field(ge=0)
    connector_count: int = Field(ge=0)
    row_origin: str
    column_origin: str
    east_axis: str
    north_axis: str
    up_axis: str
    aggregate_glb_url: str
    connector_map_url: str
    tiles: tuple[MapAssemblyTile, ...]
    connectors: tuple[MapAssemblyConnector, ...]
    required_checks_passed: bool


class JobMapManifest(BaseModel):
    """TileJSON-like contract for one completed workflow's processed DEM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["topoforge-web-map-v1"] = _MAP_SCHEMA_VERSION
    tilejson: Literal["3.0.0"] = "3.0.0"
    job_id: str
    source_sha256: Sha256Hex
    cache_key: Sha256Hex
    bounds_wgs84: BoundsWgs84
    center_wgs84: tuple[float, float]
    minzoom: int = Field(ge=0, le=22)
    maxzoom: int = Field(ge=0, le=22)
    tile_size: Literal[256] = _TILE_SIZE
    tile_url_template: str
    styles: tuple[MapTileStyle, ...] = tuple(MapTileStyle)
    default_style: MapTileStyle = MapTileStyle.TERRAIN
    elevation_min_m: float
    elevation_max_m: float
    layout_id: str
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    tile_footprints_geojson: dict[str, Any]
    attribution: str
    crosses_antimeridian: bool
    web_mercator_latitude_clipped: bool
    generator: Literal["topoforge-map-tiles-v2"] = _GENERATOR_VERSION
    required_checks_passed: bool

    @model_validator(mode="after")
    def validate_zoom_range(self) -> Self:
        if self.maxzoom < self.minzoom:
            raise ValueError("map maxzoom must be greater than or equal to minzoom")
        return self


class MapTileCacheRecord(BaseModel):
    """Checksum-bound cache identity for one generated XYZ tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["topoforge-web-map-tile-cache-v1"] = _CACHE_SCHEMA_VERSION
    generator: Literal["topoforge-map-tiles-v2"] = _GENERATOR_VERSION
    source_sha256: Sha256Hex
    style: MapTileStyle
    z: int = Field(ge=0, le=22)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    tile_size: Literal[256] = _TILE_SIZE
    elevation_min_m: float
    elevation_max_m: float
    valid_pixel_count: int = Field(gt=0)
    png_sha256: Sha256Hex
    png_size_bytes: int = Field(gt=0)


class MapTileNotFoundError(LookupError):
    """Raised when an XYZ tile does not intersect valid processed terrain."""


class _RasterInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_sha256: Sha256Hex
    bounds_wgs84: BoundsWgs84
    bounds_mercator_m: tuple[tuple[float, float, float, float], ...]
    center_wgs84: tuple[float, float]
    crosses_antimeridian: bool
    web_mercator_latitude_clipped: bool
    elevation_min_m: float
    elevation_max_m: float
    minzoom: int
    maxzoom: int


def _canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_canonical(path: Path, model_type: type[_ModelT]) -> _ModelT:
    try:
        value = model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"visualization record is unreadable: {path}") from exc
    if path.read_bytes() != _canonical_bytes(value):
        raise ConfigurationError(f"visualization source JSON is not canonical: {path}")
    return value


def _within(root: Path, path: Path) -> bool:
    resolved = path.resolve()
    return resolved != root and root in resolved.parents


def _verify_relative(root: Path, relative: str, expected_sha256: str) -> Path:
    path = (root / relative).resolve()
    if not _within(root, path) or not path.is_file():
        raise ConfigurationError(f"assembly artifact is missing or unsafe: {relative}")
    if sha256_file(path) != expected_sha256:
        raise ConfigurationError(f"assembly artifact checksum changed: {relative}")
    return path


def _tile_mercator_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    count = 1 << z
    if x >= count or y >= count:
        raise MapTileNotFoundError("XYZ coordinate is outside its zoom matrix")
    span = 2.0 * _WEB_MERCATOR_HALF_WORLD_M / count
    left = -_WEB_MERCATOR_HALF_WORLD_M + x * span
    right = left + span
    top = _WEB_MERCATOR_HALF_WORLD_M - y * span
    bottom = top - span
    return left, bottom, right, top


def _bounds_intersect(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1]
    )


def _longitude_center(west: float, east: float) -> float:
    span = east - west if west <= east else east + 360.0 - west
    center = west + span / 2.0
    return center - 360.0 if center > 180.0 else center


def _mercator_coverage(
    west: float,
    south: float,
    east: float,
    north: float,
) -> tuple[tuple[float, float, float, float], ...]:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    longitude_ranges = ((west, east),) if west <= east else ((west, 180.0), (-180.0, east))
    output: list[tuple[float, float, float, float]] = []
    for left, right in longitude_ranges:
        left_x, bottom_y = transformer.transform(left, south)
        right_x, top_y = transformer.transform(right, north)
        output.append(
            (
                min(float(left_x), float(right_x)),
                min(float(bottom_y), float(top_y)),
                max(float(left_x), float(right_x)),
                max(float(bottom_y), float(top_y)),
            )
        )
    return tuple(output)


def _hillshade(
    elevations: npt.NDArray[np.float32],
    valid: npt.NDArray[np.bool_],
    pixel_size_m: float,
) -> npt.NDArray[np.float32]:
    minimum = float(np.nanmin(elevations[valid]))
    filled = np.where(valid, elevations, minimum).astype(np.float64)
    gradient_y, gradient_x = np.gradient(filled, pixel_size_m, pixel_size_m)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gradient_x, gradient_y))
    aspect = np.arctan2(-gradient_x, gradient_y)
    azimuth = math.radians(315.0)
    altitude = math.radians(45.0)
    shaded = np.sin(altitude) * np.sin(slope) + np.cos(altitude) * np.cos(slope) * np.cos(
        azimuth - aspect
    )
    return np.clip(shaded, 0.0, 1.0).astype(np.float32)


def _elevation_colors(normalized: npt.NDArray[np.float32]) -> npt.NDArray[np.uint8]:
    positions = np.asarray([0.0, 0.22, 0.48, 0.72, 1.0], dtype=np.float32)
    colors = np.asarray(
        [
            [31, 83, 77],
            [75, 124, 91],
            [176, 158, 94],
            [154, 157, 151],
            [243, 242, 237],
        ],
        dtype=np.float32,
    )
    output = np.empty((*normalized.shape, 3), dtype=np.float32)
    for channel in range(3):
        output[..., channel] = np.interp(normalized, positions, colors[:, channel])
    return np.clip(np.rint(output), 0, 255).astype(np.uint8)


def _render_rgba(
    elevations: npt.NDArray[np.float32],
    valid: npt.NDArray[np.bool_],
    *,
    style: MapTileStyle,
    elevation_min_m: float,
    elevation_max_m: float,
    pixel_size_m: float,
) -> npt.NDArray[np.uint8]:
    span = max(elevation_max_m - elevation_min_m, 1e-9)
    normalized = np.where(
        valid, np.clip((elevations - elevation_min_m) / span, 0.0, 1.0), 0.0
    ).astype(np.float32)
    shade = _hillshade(elevations, valid, pixel_size_m)
    if style is MapTileStyle.HILLSHADE:
        gray = np.rint(35.0 + 215.0 * shade).astype(np.uint8)
        rgb = np.repeat(gray[..., None], 3, axis=2)
    else:
        rgb = _elevation_colors(normalized)
        if style is MapTileStyle.TERRAIN:
            light = (0.58 + 0.52 * shade)[..., None]
            rgb = np.clip(np.rint(rgb.astype(np.float32) * light), 0, 255).astype(np.uint8)
    alpha = np.where(valid, 238, 0).astype(np.uint8)
    return np.dstack((rgb, alpha))


def _unwrap_ring(
    coordinates: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if not coordinates:
        return coordinates
    output = [coordinates[0]]
    for longitude, latitude in coordinates[1:]:
        previous = output[-1][0]
        while longitude - previous > 180.0:
            longitude -= 360.0
        while longitude - previous < -180.0:
            longitude += 360.0
        output.append((longitude, latitude))
    return output


class WebVisualizationService:
    """Strictly expose derived map tiles and existing manufacturing assemblies."""

    def __init__(self, manager: LocalJobManager) -> None:
        self.manager = manager
        self.cache_root = manager.config.state_dir / "map-tiles"
        self._lock = threading.RLock()
        self._raster_info_cache: dict[str, _RasterInfo] = {}

    def _completed(self, job_id: str) -> JobRecord:
        record = self.manager.get(job_id)
        if record.state is not JobState.COMPLETED or record.summary is None:
            raise ConfigurationError("map and assembly views require a completed workflow")
        return record

    def _assembly_sources(
        self, job_id: str
    ) -> tuple[
        Path,
        PrintTileAssemblyManifest,
        ConnectorPlan,
        PrintTileAssemblyValidation,
    ]:
        record = self._completed(job_id)
        root, _ = self.manager.directory_artifact_path(job_id, "print_tiles_directory")
        manifest_path = root / "print-tile-assembly-manifest.json"
        workflow_path, _ = self.manager.artifact_path(job_id, "workflow_manifest")
        workflow = _read_canonical(workflow_path, LocalWorkflowManifest)
        connect = next(
            (item for item in workflow.stages if item.name is WorkflowStage.CONNECT),
            None,
        )
        workspace = record.workspace_dir.resolve()
        if workflow.required_checks_passed is not True or connect is None:
            raise ConfigurationError("workflow has no validated connect stage")
        connect_output = (workspace / connect.output_path).resolve()
        connect_manifest = (workspace / connect.manifest_path).resolve()
        if (
            connect.required_checks_passed is not True
            or connect_output != root
            or connect_manifest != manifest_path
            or not _within(workspace, connect_manifest)
            or sha256_file(manifest_path) != connect.manifest_sha256
        ):
            raise ConfigurationError("assembly manifest is not bound to the connect stage")
        manifest = _read_canonical(manifest_path, PrintTileAssemblyManifest)
        plan_path = _verify_relative(
            root, manifest.connector_plan_path, manifest.connector_plan_sha256
        )
        plan = _read_canonical(plan_path, ConnectorPlan)
        _verify_relative(root, manifest.assembly_preview_path, manifest.assembly_preview_sha256)
        _verify_relative(root, manifest.connector_map_path, manifest.connector_map_sha256)
        validation_path = _verify_relative(
            root,
            manifest.assembly_validation_path,
            manifest.assembly_validation_sha256,
        )
        validation = _read_canonical(validation_path, PrintTileAssemblyValidation)
        if plan.layout_id != manifest.layout_id:
            raise ConfigurationError("connector plan and assembly layout ids differ")
        if (
            validation.required_checks_passed is not True
            or validation.layout_id != manifest.layout_id
            or validation.tile_count != manifest.tile_count
            or validation.seam_count != manifest.seam_count
            or validation.connector_count != manifest.connector_count
        ):
            raise ConfigurationError("assembly validation does not match the published manifest")
        for tile in manifest.tiles:
            _verify_relative(root, tile.tile_manifest, tile.tile_manifest_sha256)
        return root, manifest, plan, validation

    def assembly(self, job_id: str) -> JobAssemblyOverview:
        """Return checksum-verified assembly metadata for one completed job."""
        self._completed(job_id)
        root, manifest, plan, validation = self._assembly_sources(job_id)
        tiles: list[MapAssemblyTile] = []
        maximum_x = 0.0
        maximum_y = 0.0
        for tile in manifest.tiles:
            relative = tile.files["global_glb"]
            expected_sha256 = tile.sha256["global_glb"]
            _verify_relative(root, relative, expected_sha256)
            x_min, y_min, _, x_max, y_max, _ = tile.global_bounds_mm
            maximum_x = max(maximum_x, x_max)
            maximum_y = max(maximum_y, y_max)
            tiles.append(
                MapAssemblyTile(
                    tile_id=tile.tile_id,
                    row=tile.row,
                    column=tile.column,
                    physical_bounds_mm=(x_min, y_min, x_max, y_max),
                    global_bounds_mm=tile.global_bounds_mm,
                    triangle_count=tile.triangle_count,
                    volume_mm3=tile.volume_mm3,
                    male_connector_ids=tile.male_connector_ids,
                    female_connector_ids=tile.female_connector_ids,
                    glb_url=f"/api/v1/jobs/{job_id}/assembly/tiles/{tile.tile_id}.glb",
                    glb_sha256=expected_sha256,
                )
            )
        aggregate = next(
            item
            for item in self._completed(job_id).artifacts
            if item.artifact_id == "connector_assembly_glb"
        )
        connector_map = next(
            item
            for item in self._completed(job_id).artifacts
            if item.artifact_id == "connector_map"
        )
        connectors = tuple(
            MapAssemblyConnector(
                connector_id=item.connector_id,
                seam_id=item.seam_id,
                direction=item.direction,
                male_tile_id=item.male_tile_id,
                female_tile_id=item.female_tile_id,
                seam_coordinate_mm=item.seam_coordinate_mm,
                center_along_seam_mm=item.center_along_seam_mm,
                insertion_axis=item.insertion_axis,
            )
            for item in plan.connectors
        )
        return JobAssemblyOverview(
            job_id=job_id,
            layout_id=manifest.layout_id,
            model_size_mm=(maximum_x, maximum_y),
            tile_grid_shape=manifest.tile_grid_shape,
            tile_count=manifest.tile_count,
            seam_count=manifest.seam_count,
            connector_count=manifest.connector_count,
            row_origin=manifest.row_origin,
            column_origin=manifest.column_origin,
            east_axis=manifest.east_axis,
            north_axis=manifest.north_axis,
            up_axis=manifest.up_axis,
            aggregate_glb_url=aggregate.download_url or "",
            connector_map_url=connector_map.download_url or "",
            tiles=tuple(tiles),
            connectors=connectors,
            required_checks_passed=validation.required_checks_passed,
        )

    def assembly_tile_path(self, job_id: str, tile_id: str) -> tuple[Path, str]:
        """Strictly resolve one per-tile global GLB from the assembly manifest."""
        self._completed(job_id)
        root, manifest, _, _ = self._assembly_sources(job_id)
        tile = next((item for item in manifest.tiles if item.tile_id == tile_id), None)
        if tile is None:
            raise KeyError(tile_id)
        expected = tile.sha256["global_glb"]
        return _verify_relative(root, tile.files["global_glb"], expected), expected

    def _raster_info(self, path: Path, source_sha256: str) -> _RasterInfo:
        cached = self._raster_info_cache.get(source_sha256)
        if cached is not None:
            return cached
        with rasterio.open(path) as dataset:
            if dataset.count != 1 or dataset.crs is None:
                raise ConfigurationError("processed DEM must be one georeferenced raster band")
            values = dataset.read(1, masked=True)
            valid_values = np.asarray(values.compressed(), dtype=np.float64)
            if valid_values.size == 0:
                raise ConfigurationError("processed DEM has no valid elevations for map tiles")
            west, raw_south, east, raw_north = transform_bounds(
                dataset.crs,
                "EPSG:4326",
                *dataset.bounds,
                densify_pts=21,
            )
            south = max(raw_south, -_WEB_MERCATOR_MAX_LATITUDE)
            north = min(raw_north, _WEB_MERCATOR_MAX_LATITUDE)
            if south >= north:
                raise ConfigurationError(
                    "processed DEM is outside Web Mercator latitude coverage "
                    f"(+/-{_WEB_MERCATOR_MAX_LATITUDE} degrees)"
                )
            crosses_antimeridian = west > east
            mercator = _mercator_coverage(west, south, east, north)
            center_latitude = (south + north) / 2.0
            resolution_m = max(abs(float(dataset.res[0])), abs(float(dataset.res[1])))
            metres_per_pixel_at_zero = 156_543.03392804097 * max(
                math.cos(math.radians(center_latitude)), 0.01
            )
            recommended = round(math.log2(max(metres_per_pixel_at_zero / resolution_m, 1.0)))
            maxzoom = min(18, max(0, recommended))
            info = _RasterInfo(
                source_sha256=source_sha256,
                bounds_wgs84=(west, south, east, north),
                bounds_mercator_m=mercator,
                center_wgs84=(_longitude_center(west, east), center_latitude),
                crosses_antimeridian=crosses_antimeridian,
                web_mercator_latitude_clipped=(south != raw_south or north != raw_north),
                elevation_min_m=float(np.min(valid_values)),
                elevation_max_m=float(np.max(valid_values)),
                minzoom=max(0, maxzoom - 5),
                maxzoom=maxzoom,
            )
        self._raster_info_cache[source_sha256] = info
        return info

    def _footprints(
        self,
        path: Path,
        assembly: JobAssemblyOverview,
    ) -> dict[str, Any]:
        features: list[dict[str, Any]] = []
        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                raise ConfigurationError("processed DEM CRS is missing")
            transformer = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
            width_mm, depth_mm = assembly.model_size_mm

            def point(x_mm: float, y_mm: float) -> tuple[float, float]:
                column = x_mm / width_mm * (dataset.width - 1)
                row = (1.0 - y_mm / depth_mm) * (dataset.height - 1)
                source_x, source_y = dataset.transform * (column + 0.5, row + 0.5)
                longitude, latitude = transformer.transform(source_x, source_y)
                return float(longitude), float(latitude)

            for tile in assembly.tiles:
                x_min, y_min, x_max, y_max = tile.physical_bounds_mm
                ring = _unwrap_ring(
                    [
                        point(x_min, y_min),
                        point(x_max, y_min),
                        point(x_max, y_max),
                        point(x_min, y_max),
                        point(x_min, y_min),
                    ]
                )
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "tile_id": tile.tile_id,
                            "row": tile.row,
                            "column": tile.column,
                        },
                        "geometry": {"type": "Polygon", "coordinates": [ring]},
                    }
                )
        return {"type": "FeatureCollection", "features": features}

    def manifest(self, job_id: str) -> JobMapManifest:
        """Return one deterministic TileJSON-like manifest and assembly footprints."""
        self._completed(job_id)
        raster_path, artifact = self.manager.artifact_path(job_id, "processed_dem")
        if artifact.sha256 is None:
            raise ConfigurationError("processed DEM artifact has no SHA-256")
        info = self._raster_info(raster_path, artifact.sha256)
        assembly = self.assembly(job_id)
        cache_key = hashlib.sha256(
            f"{_GENERATOR_VERSION}:{artifact.sha256}".encode("ascii")
        ).hexdigest()
        return JobMapManifest(
            job_id=job_id,
            source_sha256=artifact.sha256,
            cache_key=cache_key,
            bounds_wgs84=info.bounds_wgs84,
            center_wgs84=info.center_wgs84,
            minzoom=info.minzoom,
            maxzoom=info.maxzoom,
            tile_url_template=(f"/api/v1/jobs/{job_id}/map/tiles/{{style}}/{{z}}/{{x}}/{{y}}.png"),
            elevation_min_m=info.elevation_min_m,
            elevation_max_m=info.elevation_max_m,
            layout_id=assembly.layout_id,
            tile_grid_shape=assembly.tile_grid_shape,
            tile_count=assembly.tile_count,
            tile_footprints_geojson=self._footprints(raster_path, assembly),
            attribution="TopoForge processed DEM; dataset attribution remains in provenance",
            crosses_antimeridian=info.crosses_antimeridian,
            web_mercator_latitude_clipped=info.web_mercator_latitude_clipped,
            required_checks_passed=assembly.required_checks_passed,
        )

    def _cache_paths(
        self, source_sha256: str, style: MapTileStyle, z: int, x: int, y: int
    ) -> tuple[Path, Path]:
        directory = self.cache_root / source_sha256 / style.value / str(z) / str(x)
        return directory / f"{y}.png", directory / f"{y}.json"

    def _valid_cache(
        self,
        png_path: Path,
        record_path: Path,
        *,
        source_sha256: str,
        style: MapTileStyle,
        z: int,
        x: int,
        y: int,
    ) -> MapTileCacheRecord | None:
        if not png_path.is_file() or not record_path.is_file():
            return None
        try:
            record = _read_canonical(record_path, MapTileCacheRecord)
            if (
                record.source_sha256 != source_sha256
                or record.style is not style
                or (record.z, record.x, record.y) != (z, x, y)
                or record.png_size_bytes != png_path.stat().st_size
                or record.png_sha256 != sha256_file(png_path)
            ):
                return None
            with Image.open(png_path) as image:
                if image.mode != "RGBA" or image.size != (_TILE_SIZE, _TILE_SIZE):
                    return None
            return record
        except (OSError, ValueError, ConfigurationError):
            return None

    def tile(
        self,
        job_id: str,
        style: MapTileStyle,
        z: int,
        x: int,
        y: int,
    ) -> tuple[Path, MapTileCacheRecord, str]:
        """Return a verified cached tile, generating it atomically when required."""
        if z < 0 or z > 22 or x < 0 or y < 0:
            raise MapTileNotFoundError("XYZ coordinate is invalid")
        raster_path, artifact = self.manager.artifact_path(job_id, "processed_dem")
        if artifact.sha256 is None:
            raise ConfigurationError("processed DEM artifact has no SHA-256")
        info = self._raster_info(raster_path, artifact.sha256)
        tile_bounds = _tile_mercator_bounds(z, x, y)
        if not any(_bounds_intersect(tile_bounds, coverage) for coverage in info.bounds_mercator_m):
            raise MapTileNotFoundError("XYZ tile is outside processed DEM coverage")
        png_path, record_path = self._cache_paths(artifact.sha256, style, z, x, y)
        with self._lock:
            cached = self._valid_cache(
                png_path,
                record_path,
                source_sha256=artifact.sha256,
                style=style,
                z=z,
                x=x,
                y=y,
            )
            if cached is not None:
                return png_path, cached, "hit"
            previous_cache_present = png_path.exists() or record_path.exists()
            png_path.unlink(missing_ok=True)
            record_path.unlink(missing_ok=True)
            png_path.parent.mkdir(parents=True, exist_ok=True)
            destination = np.full((_TILE_SIZE, _TILE_SIZE), np.nan, dtype=np.float32)
            transform = from_bounds(*tile_bounds, _TILE_SIZE, _TILE_SIZE)
            with rasterio.open(raster_path) as dataset:
                reproject(
                    source=rasterio.band(dataset, 1),
                    destination=destination,
                    src_transform=dataset.transform,
                    src_crs=dataset.crs,
                    src_nodata=dataset.nodata,
                    dst_transform=transform,
                    dst_crs="EPSG:3857",
                    dst_nodata=np.nan,
                    resampling=Resampling.bilinear,
                    num_threads=1,
                    init_dest_nodata=True,
                )
            valid = np.isfinite(destination)
            valid_count = int(np.count_nonzero(valid))
            if valid_count == 0:
                raise MapTileNotFoundError("XYZ tile has no valid processed DEM pixels")
            pixel_size_m = (tile_bounds[2] - tile_bounds[0]) / _TILE_SIZE
            rgba = _render_rgba(
                destination,
                valid,
                style=style,
                elevation_min_m=info.elevation_min_m,
                elevation_max_m=info.elevation_max_m,
                pixel_size_m=pixel_size_m,
            )
            temporary_png = png_path.with_name(f".{png_path.name}.tmp")
            Image.fromarray(rgba).save(
                temporary_png,
                format="PNG",
                compress_level=9,
                optimize=False,
            )
            temporary_png.replace(png_path)
            cache_record = MapTileCacheRecord(
                source_sha256=artifact.sha256,
                style=style,
                z=z,
                x=x,
                y=y,
                elevation_min_m=info.elevation_min_m,
                elevation_max_m=info.elevation_max_m,
                valid_pixel_count=valid_count,
                png_sha256=sha256_file(png_path),
                png_size_bytes=png_path.stat().st_size,
            )
            temporary_record = record_path.with_name(f".{record_path.name}.tmp")
            temporary_record.write_bytes(_canonical_bytes(cache_record))
            temporary_record.replace(record_path)
            reopened = self._valid_cache(
                png_path,
                record_path,
                source_sha256=artifact.sha256,
                style=style,
                z=z,
                x=x,
                y=y,
            )
            if reopened is None:
                raise ConfigurationError("generated map tile did not pass cache reread")
            return png_path, reopened, ("regenerated-corrupt" if previous_cache_present else "miss")
