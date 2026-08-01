"""Per-tile DEM/mask extraction and deterministic assembly manifests."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Self

import numpy as np
import rasterio
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rasterio.windows import Window
from rasterio.windows import transform as window_transform

from topoforge.exceptions import ConfigurationError, RasterProcessingError
from topoforge.tiling.layout import (
    TerrainTile,
    TileLayout,
    canonical_tile_layout_bytes,
    read_tile_layout,
)

_TILE_ARTIFACT_SCHEMA_VERSION = "topoforge-tile-artifact-v1"
_ASSEMBLY_SCHEMA_VERSION = "topoforge-assembly-manifest-v1"
_COVERAGE_SCHEMA_VERSION = "topoforge-tile-coverage-v1"
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TileValidation(BaseModel):
    """Measured checks for one extracted overlapped tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_ARTIFACT_SCHEMA_VERSION
    tile_id: str
    layout_id: str
    sample_grid_shape: tuple[int, int]
    core_sample_shape: tuple[int, int]
    crs: str
    transform: tuple[float, float, float, float, float, float]
    finite_elevations: bool
    binary_original_nodata_mask: bool
    original_nodata_pixels: int = Field(ge=0)
    original_nodata_fraction: float = Field(ge=0, le=1)
    sample_elevation_min_m: float
    sample_elevation_max_m: float
    core_elevation_min_m: float
    core_elevation_max_m: float
    required_checks_passed: bool


class TileProvenance(BaseModel):
    """Source and coordinate evidence carried by one tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_ARTIFACT_SCHEMA_VERSION
    tile_id: str
    tile_key: str
    layout_id: str
    source_bundle_manifest_sha256: Sha256Hex
    raw_source_dem_sha256: Sha256Hex
    processed_dem_sha256: Sha256Hex
    source_grid_shape: tuple[int, int]
    core_cell_window: dict[str, int]
    core_sample_window: dict[str, int]
    sampling_window: dict[str, int]
    physical_bounds_mm: dict[str, float]
    crs: str
    transform: tuple[float, float, float, float, float, float]
    east_axis: str = "+X = East"
    north_axis: str = "+Y = North"
    row_origin: str = "north"
    column_origin: str = "west"
    overlap_cells: int = Field(ge=0)
    source_bounds: dict[str, Any] | None = None
    dataset: dict[str, Any] = Field(default_factory=dict)
    orientation: dict[str, Any] = Field(default_factory=dict)


class TileArtifactManifest(BaseModel):
    """Checksummed files and layout identity for one tile directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _TILE_ARTIFACT_SCHEMA_VERSION
    tile_id: str
    tile_key: str
    layout_id: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    files: dict[str, str]
    sha256: dict[str, Sha256Hex]
    validation: TileValidation

    @model_validator(mode="after")
    def validate_file_roles(self) -> Self:
        """Require the complete v1 artifact role set with safe local filenames."""
        required = {"processed_dem", "original_nodata_mask", "provenance", "validation"}
        if set(self.files) != required or set(self.sha256) != required:
            raise ValueError("tile manifest must contain the complete v1 artifact role set")
        if any(Path(name).name != name for name in self.files.values()):
            raise ValueError("tile manifest artifact paths must be local filenames")
        return self


class AssemblyTileRecord(BaseModel):
    """Stable assembly-manifest record for one tile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tile_id: str
    tile_key: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    directory: str
    tile_manifest: str
    tile_manifest_sha256: Sha256Hex
    files: dict[str, str]
    sha256: dict[str, Sha256Hex]
    core_cell_window: dict[str, int]
    sampling_window: dict[str, int]
    physical_bounds_mm: dict[str, float]


class AssemblyManifest(BaseModel):
    """Deterministic manifest binding layout, source bundle, tiles, and checksums."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _ASSEMBLY_SCHEMA_VERSION
    layout_id: str
    layout_path: str = "tile-layout.json"
    layout_sha256: Sha256Hex
    source_bundle_manifest_sha256: Sha256Hex
    raw_source_dem_sha256: Sha256Hex
    processed_dem_sha256: Sha256Hex
    tile_grid_shape: tuple[int, int]
    tile_count: int = Field(gt=0)
    overlap_cells: int = Field(ge=0)
    row_origin: str = "north"
    column_origin: str = "west"
    tiles: list[AssemblyTileRecord]
    coverage_map_path: str = "coverage_map.json"
    coverage_map_sha256: Sha256Hex

    @model_validator(mode="after")
    def validate_tile_grid(self) -> Self:
        """Require one unique row-major tile record for every grid position."""
        rows, columns = self.tile_grid_shape
        expected = [(row, column) for row in range(rows) for column in range(columns)]
        actual = [(tile.row, tile.column) for tile in self.tiles]
        if self.tile_count != rows * columns or len(self.tiles) != self.tile_count:
            raise ValueError("assembly tile_count does not match tile_grid_shape")
        if actual != expected or len({tile.tile_id for tile in self.tiles}) != self.tile_count:
            raise ValueError(
                "assembly tiles must be unique and ordered north-to-south/west-to-east"
            )
        return self


class TileCoverageMap(BaseModel):
    """Stable row-major tile-id map for assembly and future Web clients."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _COVERAGE_SCHEMA_VERSION
    layout_id: str
    tile_grid_shape: tuple[int, int]
    row_origin: str = "north"
    column_origin: str = "west"
    rows: list[list[str]]

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        if len(self.rows) != self.tile_grid_shape[0] or any(
            len(row) != self.tile_grid_shape[1] for row in self.rows
        ):
            raise ValueError("coverage map rows do not match tile_grid_shape")
        flattened = [tile_id for row in self.rows for tile_id in row]
        if len(set(flattened)) != len(flattened):
            raise ValueError("coverage map tile ids must be unique")
        return self


class TileExtractionResult(BaseModel):
    """Published tile-layout, tile directories, and assembly-manifest paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_dir: Path
    layout_path: Path
    assembly_manifest_path: Path
    coverage_map_path: Path
    tile_manifest_paths: list[Path]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_canonical_json(path: Path, value: BaseModel | dict[str, Any]) -> None:
    path.write_bytes(_canonical_json_bytes(value))


def _read_canonical_json[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    value = model.model_validate_json(path.read_text(encoding="utf-8"))
    if path.read_bytes() != _canonical_json_bytes(value):
        raise ConfigurationError(f"JSON artifact is not canonical: {path}")
    return value


def _resolve_relative(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or value != relative.as_posix() or ".." in relative.parts:
        raise ConfigurationError(f"unsafe relative artifact path: {value}")
    return root / relative


def _transform_tuple(transform: Any) -> tuple[float, float, float, float, float, float]:
    return (
        float(transform[0]),
        float(transform[1]),
        float(transform[2]),
        float(transform[3]),
        float(transform[4]),
        float(transform[5]),
    )


def _window_from_layout(window: Any) -> Window:
    return Window.from_slices(
        (window.row_start, window.row_stop),
        (window.column_start, window.column_stop),
    )


def _window_dict(window: Any) -> dict[str, int]:
    return {
        "row_start": window.row_start,
        "row_stop": window.row_stop,
        "column_start": window.column_start,
        "column_stop": window.column_stop,
    }


def _bounds_dict(tile: TerrainTile) -> dict[str, float]:
    return tile.physical_bounds_mm.model_dump(mode="json")


def _write_raster_window(
    source: rasterio.DatasetReader,
    window: Window,
    destination: Path,
) -> tuple[tuple[float, float, float, float, float, float], np.ndarray]:
    values = source.read(1, window=window)
    transform = window_transform(window, source.transform)
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "height": values.shape[0],
        "width": values.shape[1],
        "count": 1,
        "dtype": values.dtype,
        "crs": source.crs,
        "transform": transform,
    }
    if source.nodata is not None:
        profile["nodata"] = source.nodata
    with rasterio.open(destination, "w", **profile) as target:
        target.write(values, 1)
    with rasterio.open(destination) as reopened:
        reopened_values = reopened.read(1)
        if (
            reopened.shape != values.shape
            or reopened.crs != source.crs
            or not reopened.transform.almost_equals(transform)
            or not np.array_equal(reopened_values, values, equal_nan=True)
        ):
            raise RasterProcessingError(f"extracted raster failed strict reopen: {destination}")
    return _transform_tuple(transform), values


def _verify_source_bundle(
    bundle_dir: Path,
) -> tuple[str, str, str, tuple[float, float]]:
    manifest_path = bundle_dir / "build_manifest.json"
    dem_path = bundle_dir / "processed_dem.tif"
    mask_path = bundle_dir / "original_nodata_mask.tif"
    if not all(
        path.is_file() and path.stat().st_size > 0 for path in (manifest_path, dem_path, mask_path)
    ):
        raise RasterProcessingError(
            "tile extraction requires processed_dem.tif, original_nodata_mask.tif, "
            "and build_manifest.json"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ConfigurationError("source build_manifest.json must contain an object")
    role_map = manifest.get("artifacts", {})
    checksums = manifest.get("sha256", {})
    if not isinstance(role_map, dict) or not isinstance(checksums, dict):
        raise ConfigurationError("source bundle artifact/checksum maps are invalid")
    for role, expected_sha256 in checksums.items():
        artifact_name = role_map.get(role)
        if (
            not isinstance(artifact_name, str)
            or Path(artifact_name).name != artifact_name
            or not isinstance(expected_sha256, str)
        ):
            raise ConfigurationError(f"source bundle manifest role is invalid: {role}")
        artifact_path = bundle_dir / artifact_name
        if not artifact_path.is_file() or _sha256(artifact_path) != expected_sha256:
            raise ConfigurationError(f"source bundle checksum verification failed for {role}")
    for role, expected_name in {
        "processed_dem": "processed_dem.tif",
        "original_nodata_mask": "original_nodata_mask.tif",
    }.items():
        if role_map.get(role) != expected_name or checksums.get(role) != _sha256(
            bundle_dir / expected_name
        ):
            raise ConfigurationError(f"source bundle checksum verification failed for {role}")
    raw_source_sha256 = manifest.get("source_sha256")
    if (
        not isinstance(raw_source_sha256, str)
        or len(raw_source_sha256) != 64
        or any(character not in "0123456789abcdef" for character in raw_source_sha256)
    ):
        raise ConfigurationError("source bundle manifest has no valid raw source SHA-256")
    validation = json.loads((bundle_dir / "validation.json").read_text(encoding="utf-8"))
    dimensions = validation.get("dimensions_mm") if isinstance(validation, dict) else None
    if not isinstance(dimensions, list) or len(dimensions) < 2:
        raise ConfigurationError("source validation.json has no model dimensions")
    model_size_mm = (float(dimensions[0]), float(dimensions[1]))
    if not all(np.isfinite(value) and value > 0 for value in model_size_mm):
        raise ConfigurationError("source validation.json model dimensions are invalid")
    return (
        _sha256(manifest_path),
        raw_source_sha256,
        checksums["processed_dem"],
        model_size_mm,
    )


def _measure_tile_validation(
    tile: TerrainTile,
    *,
    layout: TileLayout,
    crs: str,
    transform: tuple[float, float, float, float, float, float],
    elevations: np.ndarray[Any, Any],
    mask_values: np.ndarray[Any, Any],
) -> TileValidation:
    core_row_start = tile.core_sample_window.row_start - tile.sampling_window.row_start
    core_row_stop = tile.core_sample_window.row_stop - tile.sampling_window.row_start
    core_column_start = tile.core_sample_window.column_start - tile.sampling_window.column_start
    core_column_stop = tile.core_sample_window.column_stop - tile.sampling_window.column_start
    core = elevations[core_row_start:core_row_stop, core_column_start:core_column_stop]
    finite = bool(np.all(np.isfinite(elevations)))
    binary_mask = bool(np.all(np.isin(mask_values, (0, 1))))
    nodata_pixels = int(np.count_nonzero(mask_values))
    return TileValidation(
        tile_id=tile.tile_id,
        layout_id=layout.layout_id,
        sample_grid_shape=(int(elevations.shape[0]), int(elevations.shape[1])),
        core_sample_shape=(int(core.shape[0]), int(core.shape[1])),
        crs=crs,
        transform=transform,
        finite_elevations=finite,
        binary_original_nodata_mask=binary_mask,
        original_nodata_pixels=nodata_pixels,
        original_nodata_fraction=float(nodata_pixels / mask_values.size),
        sample_elevation_min_m=float(np.min(elevations)),
        sample_elevation_max_m=float(np.max(elevations)),
        core_elevation_min_m=float(np.min(core)),
        core_elevation_max_m=float(np.max(core)),
        required_checks_passed=finite and binary_mask,
    )


def _extract_one(
    tile: TerrainTile,
    *,
    layout: TileLayout,
    source_bundle_manifest_sha256: str,
    raw_source_dem_sha256: str,
    processed_dem_sha256: str,
    source_bounds: dict[str, Any] | None,
    dataset: dict[str, Any],
    orientation: dict[str, Any],
    dem: rasterio.DatasetReader,
    mask: rasterio.DatasetReader,
    tile_dir: Path,
) -> tuple[TileArtifactManifest, AssemblyTileRecord]:
    tile_dir.mkdir(parents=True, exist_ok=False)
    sample_window = _window_from_layout(tile.sampling_window)
    dem_transform, elevations = _write_raster_window(
        dem, sample_window, tile_dir / "processed_dem.tif"
    )
    mask_transform, mask_values = _write_raster_window(
        mask, sample_window, tile_dir / "original_nodata_mask.tif"
    )
    if dem_transform != mask_transform:
        raise RasterProcessingError(f"tile {tile.tile_id} DEM and mask transforms differ")

    validation = _measure_tile_validation(
        tile,
        layout=layout,
        crs=str(dem.crs),
        transform=dem_transform,
        elevations=elevations,
        mask_values=mask_values,
    )
    provenance = TileProvenance(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        layout_id=layout.layout_id,
        source_bundle_manifest_sha256=source_bundle_manifest_sha256,
        raw_source_dem_sha256=raw_source_dem_sha256,
        processed_dem_sha256=processed_dem_sha256,
        source_grid_shape=layout.source_grid_shape,
        core_cell_window=_window_dict(tile.core_cell_window),
        core_sample_window=_window_dict(tile.core_sample_window),
        sampling_window=_window_dict(tile.sampling_window),
        physical_bounds_mm=_bounds_dict(tile),
        crs=str(dem.crs),
        transform=dem_transform,
        overlap_cells=layout.overlap_cells,
        source_bounds=source_bounds,
        dataset=dataset,
        orientation=orientation,
    )
    if not validation.required_checks_passed:
        raise RasterProcessingError(f"tile validation failed: {tile.tile_id}")
    _write_canonical_json(tile_dir / "tile_provenance.json", provenance)
    _write_canonical_json(tile_dir / "tile_validation.json", validation)
    if (
        TileProvenance.model_validate_json(
            (tile_dir / "tile_provenance.json").read_text(encoding="utf-8")
        )
        != provenance
    ):
        raise ConfigurationError(f"tile provenance failed strict reopen: {tile.tile_id}")
    if (
        TileValidation.model_validate_json(
            (tile_dir / "tile_validation.json").read_text(encoding="utf-8")
        )
        != validation
    ):
        raise ConfigurationError(f"tile validation failed strict reopen: {tile.tile_id}")
    files = {
        "processed_dem": "processed_dem.tif",
        "original_nodata_mask": "original_nodata_mask.tif",
        "provenance": "tile_provenance.json",
        "validation": "tile_validation.json",
    }
    checksums = {role: _sha256(tile_dir / name) for role, name in files.items()}
    artifact = TileArtifactManifest(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        layout_id=layout.layout_id,
        row=tile.row,
        column=tile.column,
        files=files,
        sha256=checksums,
        validation=validation,
    )
    _write_canonical_json(tile_dir / "tile_manifest.json", artifact)
    if (
        TileArtifactManifest.model_validate_json(
            (tile_dir / "tile_manifest.json").read_text(encoding="utf-8")
        )
        != artifact
    ):
        raise ConfigurationError(f"tile manifest failed strict reopen: {tile.tile_id}")
    tile_manifest_sha256 = _sha256(tile_dir / "tile_manifest.json")
    assembly_record = AssemblyTileRecord(
        tile_id=tile.tile_id,
        tile_key=tile.tile_key,
        row=tile.row,
        column=tile.column,
        directory=f"tiles/{tile.tile_id}",
        tile_manifest=f"tiles/{tile.tile_id}/tile_manifest.json",
        tile_manifest_sha256=tile_manifest_sha256,
        files={role: f"tiles/{tile.tile_id}/{name}" for role, name in files.items()},
        sha256={role: checksums[role] for role in files},
        core_cell_window=_window_dict(tile.core_cell_window),
        sampling_window=_window_dict(tile.sampling_window),
        physical_bounds_mm=_bounds_dict(tile),
    )
    return artifact, assembly_record


def verify_tile_set(tile_set_dir: Path, source_bundle_dir: Path | None = None) -> dict[str, Any]:
    """Strictly reopen a tile set and cross-check every raster, JSON, and SHA-256."""
    root = tile_set_dir.expanduser().resolve()
    required_root = (
        root / "tile-layout.json",
        root / "coverage_map.json",
        root / "assembly_manifest.json",
    )
    if not all(path.is_file() and path.stat().st_size > 0 for path in required_root):
        raise ConfigurationError(
            "tile set requires non-empty tile-layout.json, coverage_map.json, "
            "and assembly_manifest.json"
        )

    layout_path = root / "tile-layout.json"
    layout = read_tile_layout(layout_path)
    if layout_path.read_bytes() != canonical_tile_layout_bytes(layout):
        raise ConfigurationError("tile-layout.json is not canonical")
    coverage = _read_canonical_json(root / "coverage_map.json", TileCoverageMap)
    assembly = _read_canonical_json(root / "assembly_manifest.json", AssemblyManifest)
    if (
        assembly.layout_id != layout.layout_id
        or assembly.tile_grid_shape != layout.tile_grid_shape
        or assembly.tile_count != layout.tile_count
        or assembly.overlap_cells != layout.overlap_cells
        or assembly.row_origin != layout.row_origin
        or assembly.column_origin != layout.column_origin
        or assembly.layout_path != "tile-layout.json"
        or assembly.layout_sha256 != _sha256(layout_path)
    ):
        raise ConfigurationError("assembly manifest does not match tile-layout.json")
    if (
        coverage.layout_id != layout.layout_id
        or coverage.tile_grid_shape != layout.tile_grid_shape
        or coverage.row_origin != layout.row_origin
        or coverage.column_origin != layout.column_origin
        or assembly.coverage_map_path != "coverage_map.json"
        or assembly.coverage_map_sha256 != _sha256(root / "coverage_map.json")
    ):
        raise ConfigurationError("coverage_map.json does not match layout/assembly identities")
    expected_coverage = [
        [
            layout.tiles[row * layout.tile_grid_shape[1] + column].tile_id
            for column in range(layout.tile_grid_shape[1])
        ]
        for row in range(layout.tile_grid_shape[0])
    ]
    if coverage.rows != expected_coverage:
        raise ConfigurationError("coverage_map.json does not match row-major layout tiles")

    source_dem_values: np.ndarray[Any, Any] | None = None
    source_mask_values: np.ndarray[Any, Any] | None = None
    if source_bundle_dir is not None:
        bundle = source_bundle_dir.expanduser().resolve()
        (
            source_manifest_sha256,
            raw_source_sha256,
            processed_dem_sha256,
            source_model_size_mm,
        ) = _verify_source_bundle(bundle)
        if (
            assembly.source_bundle_manifest_sha256 != source_manifest_sha256
            or assembly.raw_source_dem_sha256 != raw_source_sha256
            or assembly.processed_dem_sha256 != processed_dem_sha256
        ):
            raise ConfigurationError("assembly source hashes do not match the source bundle")
        if not np.allclose(
            layout.model_size_mm,
            source_model_size_mm,
            rtol=0.0,
            atol=1e-9,
        ):
            raise ConfigurationError(
                "tile layout model_size_mm does not match source bundle dimensions"
            )
        with (
            rasterio.open(bundle / "processed_dem.tif") as source_dem,
            rasterio.open(bundle / "original_nodata_mask.tif") as source_mask,
        ):
            if (
                source_dem.count != 1
                or source_mask.count != 1
                or source_dem.shape != layout.source_grid_shape
                or source_mask.shape != layout.source_grid_shape
                or source_dem.crs is None
                or source_dem.crs != source_mask.crs
                or not source_dem.transform.almost_equals(source_mask.transform)
            ):
                raise RasterProcessingError("source bundle DEM/mask alignment is invalid")
            source_dem_values = source_dem.read(1)
            source_mask_values = source_mask.read(1)

    for tile, record in zip(layout.tiles, assembly.tiles, strict=True):
        expected_directory = f"tiles/{tile.tile_id}"
        expected_manifest = f"{expected_directory}/tile_manifest.json"
        if (
            record.tile_id != tile.tile_id
            or record.tile_key != tile.tile_key
            or (record.row, record.column) != (tile.row, tile.column)
            or record.directory != expected_directory
            or record.tile_manifest != expected_manifest
            or record.core_cell_window != _window_dict(tile.core_cell_window)
            or record.sampling_window != _window_dict(tile.sampling_window)
            or record.physical_bounds_mm != _bounds_dict(tile)
        ):
            raise ConfigurationError(f"assembly tile record does not match layout: {tile.tile_id}")
        tile_dir = _resolve_relative(root, record.directory)
        manifest_path = _resolve_relative(root, record.tile_manifest)
        if (
            manifest_path.parent != tile_dir
            or _sha256(manifest_path) != record.tile_manifest_sha256
        ):
            raise ConfigurationError(f"tile manifest path/hash mismatch: {tile.tile_id}")
        artifact = _read_canonical_json(manifest_path, TileArtifactManifest)
        if (
            artifact.tile_id != tile.tile_id
            or artifact.tile_key != tile.tile_key
            or artifact.layout_id != layout.layout_id
            or (artifact.row, artifact.column) != (tile.row, tile.column)
            or artifact.sha256 != record.sha256
            or record.files
            != {role: f"{expected_directory}/{name}" for role, name in artifact.files.items()}
        ):
            raise ConfigurationError(f"tile artifact manifest mismatch: {tile.tile_id}")
        for role, filename in artifact.files.items():
            local_path = tile_dir / filename
            assembly_path = _resolve_relative(root, record.files[role])
            if local_path != assembly_path or _sha256(local_path) != artifact.sha256[role]:
                raise ConfigurationError(f"tile artifact checksum mismatch: {tile.tile_id}/{role}")

        provenance = _read_canonical_json(tile_dir / artifact.files["provenance"], TileProvenance)
        validation = _read_canonical_json(tile_dir / artifact.files["validation"], TileValidation)
        if artifact.validation != validation:
            raise ConfigurationError(f"embedded tile validation mismatch: {tile.tile_id}")
        if (
            provenance.tile_id != tile.tile_id
            or provenance.tile_key != tile.tile_key
            or provenance.layout_id != layout.layout_id
            or provenance.source_bundle_manifest_sha256 != assembly.source_bundle_manifest_sha256
            or provenance.raw_source_dem_sha256 != assembly.raw_source_dem_sha256
            or provenance.processed_dem_sha256 != assembly.processed_dem_sha256
            or provenance.source_grid_shape != layout.source_grid_shape
            or provenance.core_cell_window != _window_dict(tile.core_cell_window)
            or provenance.core_sample_window != _window_dict(tile.core_sample_window)
            or provenance.sampling_window != _window_dict(tile.sampling_window)
            or provenance.physical_bounds_mm != _bounds_dict(tile)
            or provenance.overlap_cells != layout.overlap_cells
            or provenance.east_axis != layout.east_axis
            or provenance.north_axis != layout.north_axis
            or provenance.row_origin != layout.row_origin
            or provenance.column_origin != layout.column_origin
        ):
            raise ConfigurationError(
                f"tile provenance does not match layout/assembly: {tile.tile_id}"
            )

        dem_path = tile_dir / artifact.files["processed_dem"]
        mask_path = tile_dir / artifact.files["original_nodata_mask"]
        with rasterio.open(dem_path) as dem, rasterio.open(mask_path) as mask:
            if (
                dem.count != 1
                or mask.count != 1
                or dem.crs is None
                or dem.crs != mask.crs
                or dem.shape != mask.shape
                or not dem.transform.almost_equals(mask.transform)
            ):
                raise RasterProcessingError(f"tile DEM/mask alignment is invalid: {tile.tile_id}")
            elevations = dem.read(1)
            mask_values = mask.read(1)
            transform = _transform_tuple(dem.transform)
            measured = _measure_tile_validation(
                tile,
                layout=layout,
                crs=str(dem.crs),
                transform=transform,
                elevations=elevations,
                mask_values=mask_values,
            )
        if (
            measured != validation
            or provenance.crs != validation.crs
            or provenance.transform != transform
        ):
            raise RasterProcessingError(f"tile measurements do not match reports: {tile.tile_id}")
        if source_dem_values is not None and source_mask_values is not None:
            window = tile.sampling_window
            expected_dem = source_dem_values[
                window.row_start : window.row_stop,
                window.column_start : window.column_stop,
            ]
            expected_mask = source_mask_values[
                window.row_start : window.row_stop,
                window.column_start : window.column_stop,
            ]
            if not np.array_equal(elevations, expected_dem, equal_nan=True) or not np.array_equal(
                mask_values, expected_mask
            ):
                raise RasterProcessingError(
                    f"tile raster values do not match the source sampling window: {tile.tile_id}"
                )

    return {
        "status": "verified",
        "output_dir": str(root),
        "layout_id": layout.layout_id,
        "tile_grid_shape": layout.tile_grid_shape,
        "tile_count": layout.tile_count,
        "overlap_cells": layout.overlap_cells,
        "row_origin": layout.row_origin,
        "column_origin": layout.column_origin,
        "source_bundle_verified": source_bundle_dir is not None,
        "required_checks_passed": True,
    }


def extract_tile_set(bundle_dir: Path, layout_path: Path, output_dir: Path) -> TileExtractionResult:
    """Extract every layout sampling window and publish a deterministic assembly bundle."""
    bundle = bundle_dir.expanduser().resolve()
    layout = read_tile_layout(layout_path.expanduser().resolve())
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise ConfigurationError(f"tile extraction destination already exists: {output}")
    (
        source_bundle_manifest_sha256,
        raw_source_dem_sha256,
        processed_dem_sha256,
        source_model_size_mm,
    ) = _verify_source_bundle(bundle)
    if not np.allclose(
        layout.model_size_mm,
        source_model_size_mm,
        rtol=0.0,
        atol=1e-9,
    ):
        raise ConfigurationError(
            "tile layout model_size_mm does not match source bundle dimensions"
        )
    dem_path = bundle / "processed_dem.tif"
    mask_path = bundle / "original_nodata_mask.tif"
    source_bounds: dict[str, Any] | None = None
    dataset: dict[str, Any] = {}
    orientation: dict[str, Any] = {}
    provenance_path = bundle / "provenance.json"
    if provenance_path.is_file():
        source_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        processing = source_provenance.get("processing")
        if isinstance(processing, dict) and isinstance(processing.get("source_bounds"), dict):
            source_bounds = processing["source_bounds"]
        if isinstance(source_provenance.get("dataset"), dict):
            dataset = source_provenance["dataset"]
        if isinstance(source_provenance.get("orientation"), dict):
            orientation = source_provenance["orientation"]

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=parent))
    try:
        layout_path_out = staging / "tile-layout.json"
        layout_path_out.write_bytes(canonical_tile_layout_bytes(layout))
        records: list[AssemblyTileRecord] = []
        with rasterio.open(dem_path) as dem, rasterio.open(mask_path) as mask:
            if dem.shape != layout.source_grid_shape or mask.shape != layout.source_grid_shape:
                raise ConfigurationError(
                    f"layout source_grid_shape {layout.source_grid_shape} does not match "
                    f"bundle raster {dem.shape}"
                )
            if (
                dem.count != 1
                or mask.count != 1
                or dem.crs is None
                or dem.crs != mask.crs
                or not dem.transform.almost_equals(mask.transform)
            ):
                raise RasterProcessingError("source DEM and NoData mask alignment is invalid")
            for tile in layout.tiles:
                _, record = _extract_one(
                    tile,
                    layout=layout,
                    source_bundle_manifest_sha256=source_bundle_manifest_sha256,
                    raw_source_dem_sha256=raw_source_dem_sha256,
                    processed_dem_sha256=processed_dem_sha256,
                    source_bounds=source_bounds,
                    dataset=dataset,
                    orientation=orientation,
                    dem=dem,
                    mask=mask,
                    tile_dir=staging / "tiles" / tile.tile_id,
                )
                records.append(record)
        coverage = TileCoverageMap(
            layout_id=layout.layout_id,
            tile_grid_shape=layout.tile_grid_shape,
            rows=[
                [
                    layout.tiles[row * layout.tile_grid_shape[1] + column].tile_id
                    for column in range(layout.tile_grid_shape[1])
                ]
                for row in range(layout.tile_grid_shape[0])
            ],
        )
        _write_canonical_json(staging / "coverage_map.json", coverage)
        coverage_map_sha256 = _sha256(staging / "coverage_map.json")
        assembly = AssemblyManifest(
            layout_id=layout.layout_id,
            layout_sha256=_sha256(layout_path_out),
            source_bundle_manifest_sha256=source_bundle_manifest_sha256,
            raw_source_dem_sha256=raw_source_dem_sha256,
            processed_dem_sha256=processed_dem_sha256,
            tile_grid_shape=layout.tile_grid_shape,
            tile_count=layout.tile_count,
            overlap_cells=layout.overlap_cells,
            tiles=records,
            coverage_map_sha256=coverage_map_sha256,
        )
        _write_canonical_json(staging / "assembly_manifest.json", assembly)
        verify_tile_set(staging, bundle)
        staging.replace(output)
        return TileExtractionResult(
            output_dir=output,
            layout_path=output / "tile-layout.json",
            assembly_manifest_path=output / "assembly_manifest.json",
            coverage_map_path=output / "coverage_map.json",
            tile_manifest_paths=[output / record.tile_manifest for record in records],
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
