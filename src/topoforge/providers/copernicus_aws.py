"""No-key Copernicus DEM AWS 2021 COG provider using normalized AOIs."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import numpy as np
import rasterio
from affine import Affine
from pydantic import BaseModel, ConfigDict, Field
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.errors import RasterioError
from rasterio.transform import from_bounds
from rasterio.warp import reproject
from shapely.geometry import shape
from shapely.ops import transform as transform_geometry

from topoforge.exceptions import ProviderFetchError
from topoforge.models import AreaOfInterest, DatasetMetadata, DatasetType
from topoforge.providers.cache import CacheIdentity
from topoforge.providers.protocol import CoverageInfo
from topoforge.providers.transport import CachingHttpClient, DownloadResult
from topoforge.raster.aoi import aoi_provenance
from topoforge.raster.processing import largest_true_rectangle
from topoforge.util import sha256_file

_LICENSE_URL = (
    "https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/"
    "copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf"
)
_ATTRIBUTION = (
    "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved."
)
_TILE_PATTERN = re.compile(r"Copernicus_DSM_COG_(?:10|30)_[NS]\d{2}_00_[EW]\d{3}_00_DEM")


class CopernicusProduct(StrEnum):
    """AWS mirror product identifiers kept distinct in provenance."""

    GLO_30 = "glo-30"
    GLO_90 = "glo-90"


class CopernicusQualityMaskRole(StrEnum):
    """Official Copernicus DEM ancillary raster roles exposed beside a DEM tile."""

    EDM = "edm"
    FLM = "flm"
    HEM = "hem"
    WBM = "wbm"


@dataclass(frozen=True, slots=True)
class _QualityMaskSpec:
    role: CopernicusQualityMaskRole
    long_name: str
    semantics: str


_QUALITY_MASK_SPECS = {
    CopernicusQualityMaskRole.EDM: _QualityMaskSpec(
        role=CopernicusQualityMaskRole.EDM,
        long_name="Editing Mask",
        semantics="Indicates whether a DEM pixel was edited.",
    ),
    CopernicusQualityMaskRole.FLM: _QualityMaskSpec(
        role=CopernicusQualityMaskRole.FLM,
        long_name="Filling Mask",
        semantics="Indicates whether a DEM pixel was filled by an ancillary source.",
    ),
    CopernicusQualityMaskRole.HEM: _QualityMaskSpec(
        role=CopernicusQualityMaskRole.HEM,
        long_name="Height Error Mask",
        semantics=(
            "Reports the corresponding height error as a standard deviation for each DEM pixel."
        ),
    ),
    CopernicusQualityMaskRole.WBM: _QualityMaskSpec(
        role=CopernicusQualityMaskRole.WBM,
        long_name="Water Body Mask",
        semantics="Indicates whether a DEM pixel is a modified water pixel.",
    ),
}


@dataclass(frozen=True, slots=True)
class _ProductSpec:
    product: CopernicusProduct
    dataset_id: str
    dataset_name: str
    resolution_m: float
    cog_code: str
    base_url: str
    tile_list_url: str
    license_id: str
    liability_notice: str


_DEFAULT_SPECS = {
    CopernicusProduct.GLO_30: _ProductSpec(
        product=CopernicusProduct.GLO_30,
        dataset_id="copernicus-dem-glo-30-aws-2021",
        dataset_name="Copernicus DEM GLO-30 AWS 2021",
        resolution_m=30.0,
        cog_code="10",
        base_url="https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com",
        tile_list_url="https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/tileList.txt",
        license_id="Copernicus-DEM-GLO-30-F",
        liability_notice=(
            "The organisations in charge of the Copernicus programme by law or by "
            "delegation do not incur any liability for any use of the Copernicus WorldDEM-30."
        ),
    ),
    CopernicusProduct.GLO_90: _ProductSpec(
        product=CopernicusProduct.GLO_90,
        dataset_id="copernicus-dem-glo-90-aws-2021",
        dataset_name="Copernicus DEM GLO-90 AWS 2021",
        resolution_m=90.0,
        cog_code="30",
        base_url="https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com",
        tile_list_url="https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com/tileList.txt",
        license_id="Copernicus-DEM-GLO-90-F",
        liability_notice=(
            "The organisations in charge of the Copernicus programme by law or by "
            "delegation do not incur any liability for any use of the Copernicus WorldDEM™-90."
        ),
    ),
}


class CopernicusAwsConfig(BaseModel):
    """Configurable mirror endpoints and local acquisition resource bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    glo30_base_url: str = _DEFAULT_SPECS[CopernicusProduct.GLO_30].base_url
    glo30_tile_list_url: str = _DEFAULT_SPECS[CopernicusProduct.GLO_30].tile_list_url
    glo90_base_url: str = _DEFAULT_SPECS[CopernicusProduct.GLO_90].base_url
    glo90_tile_list_url: str = _DEFAULT_SPECS[CopernicusProduct.GLO_90].tile_list_url
    max_aoi_area_m2: float = Field(default=25_000_000_000.0, gt=0)
    max_output_cells: int = Field(default=25_000_000, ge=16)


class CopernicusTile(BaseModel):
    """One immutable COG source tile selected by an authoritative tile list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product: CopernicusProduct
    latitude_degree: int
    longitude_degree: int
    tile_id: str
    url: str


class CopernicusPlan(BaseModel):
    """Single-product complete-coverage plan; products are never silently blended."""

    model_config = ConfigDict(extra="forbid")

    product: CopernicusProduct
    dataset_id: str
    horizontal_resolution_m: float
    tiles: list[CopernicusTile]
    decisions: list[str]
    missing_glo30_tiles: list[str] = Field(default_factory=list)


class ProviderAcquisition(BaseModel):
    """Local metric raster and complete source/cache provenance for a normalized AOI."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    provider_id: str
    raster_path: Path
    acquisition_manifest_path: Path
    dataset: DatasetMetadata
    aoi: dict[str, Any]
    plan: CopernicusPlan
    catalog_downloads: list[dict[str, Any]]
    asset_catalog_downloads: list[dict[str, Any]]
    tile_downloads: list[dict[str, Any]]
    quality_masks: list[dict[str, Any]]
    generated_at: str


@dataclass(frozen=True, slots=True)
class _SourceGridSignature:
    crs: CRS
    transform: Affine
    height: int
    width: int


@dataclass(frozen=True, slots=True)
class _QualityMaskSource:
    tile: CopernicusTile
    role: CopernicusQualityMaskRole
    url: str
    result: DownloadResult


def _specs(config: CopernicusAwsConfig) -> dict[CopernicusProduct, _ProductSpec]:
    return {
        CopernicusProduct.GLO_30: replace(
            _DEFAULT_SPECS[CopernicusProduct.GLO_30],
            base_url=config.glo30_base_url.rstrip("/"),
            tile_list_url=config.glo30_tile_list_url,
        ),
        CopernicusProduct.GLO_90: replace(
            _DEFAULT_SPECS[CopernicusProduct.GLO_90],
            base_url=config.glo90_base_url.rstrip("/"),
            tile_list_url=config.glo90_tile_list_url,
        ),
    }


def _integer_cells(start: float, stop: float) -> range:
    first = math.floor(start)
    last = math.floor(math.nextafter(stop, -math.inf))
    return range(first, last + 1)


def required_tile_coordinates(aoi: AreaOfInterest) -> list[tuple[int, int]]:
    """Enumerate latitude/longitude degree cells without expanding the AOI."""
    west, south, east, north = aoi.bounds_wgs84
    latitudes = _integer_cells(max(-90.0, south), min(90.0, north))
    longitude_ranges = (
        [_integer_cells(west, 180.0), _integer_cells(-180.0, east)]
        if aoi.crosses_antimeridian
        else [_integer_cells(west, east)]
    )
    coordinates = {
        (latitude, longitude)
        for latitude in latitudes
        for longitudes in longitude_ranges
        for longitude in longitudes
    }
    return sorted(coordinates)


def tile_id(product: CopernicusProduct, latitude: int, longitude: int) -> str:
    spec = _DEFAULT_SPECS[product]
    latitude_label = f"{'N' if latitude >= 0 else 'S'}{abs(latitude):02d}_00"
    longitude_label = f"{'E' if longitude >= 0 else 'W'}{abs(longitude):03d}_00"
    return f"Copernicus_DSM_COG_{spec.cog_code}_{latitude_label}_{longitude_label}_DEM"


def parse_tile_list(payload: bytes) -> set[str]:
    """Extract authoritative tile ids from either bare ids or full object paths."""
    text = payload.decode("utf-8-sig")
    return set(_TILE_PATTERN.findall(text))


def parse_s3_object_listing(payload: bytes) -> set[str]:
    """Return exact object keys from one S3 ListObjectsV2 response."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ProviderFetchError("Copernicus tile object listing is not valid XML") from exc
    keys = {
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Key" and (element.text or "").strip()
    }
    return keys


def _quality_mask_key(tile: CopernicusTile, role: CopernicusQualityMaskRole) -> str:
    stem = tile.tile_id.removesuffix("_DEM")
    return f"{tile.tile_id}/AUXFILES/{stem}_{role.value.upper()}.tif"


def _grid_signature(source: Any) -> _SourceGridSignature:
    if source.crs is None:
        raise ProviderFetchError("source raster is missing a CRS")
    return _SourceGridSignature(
        crs=CRS.from_user_input(source.crs),
        transform=source.transform,
        height=source.height,
        width=source.width,
    )


def _assert_quality_grid_alignment(
    *,
    tile: CopernicusTile,
    role: CopernicusQualityMaskRole,
    dem: _SourceGridSignature,
    mask: _SourceGridSignature,
) -> None:
    same_transform = bool(
        np.allclose(tuple(dem.transform), tuple(mask.transform), atol=1e-12, rtol=0.0)
    )
    if (
        dem.crs != mask.crs
        or dem.height != mask.height
        or dem.width != mask.width
        or not same_transform
    ):
        raise ProviderFetchError(
            f"Copernicus {role.value.upper()} grid does not align with DEM tile "
            f"{tile.tile_id}; preserve the source evidence and inspect the provider asset set"
        )


def _download_evidence(result: DownloadResult) -> dict[str, Any]:
    return {
        "url": result.cache_entry.url,
        "request_key": result.cache_entry.request_key,
        "cache_status": result.cache_status.value,
        "cache_lookup_reason": result.cache_lookup_reason,
        "sha256": result.cache_entry.object_sha256,
        "bytes": result.cache_entry.object_size_bytes,
        "etag": result.cache_entry.etag,
        "last_modified": result.cache_entry.last_modified,
        "media_type": result.cache_entry.media_type,
        "fetched_at": result.cache_entry.fetched_at,
        "attempts": [item.model_dump(mode="json") for item in result.attempts],
    }


class CopernicusAwsProvider:
    """Plan, cache, verify, and mosaic no-key Copernicus AWS COG assets."""

    provider_id = "copernicus-aws"

    def __init__(
        self,
        client: CachingHttpClient,
        config: CopernicusAwsConfig | None = None,
    ) -> None:
        self.client = client
        self.config = config or CopernicusAwsConfig()
        self._specs = _specs(self.config)

    def _catalog_identity(self, spec: _ProductSpec) -> CacheIdentity:
        return CacheIdentity(
            provider_id=self.provider_id,
            dataset_id=spec.dataset_id,
            dataset_version="AWS mirror 2021 catalog",
            url=spec.tile_list_url,
        )

    def _tile_identity(self, spec: _ProductSpec, tile: CopernicusTile) -> CacheIdentity:
        return CacheIdentity(
            provider_id=self.provider_id,
            dataset_id=spec.dataset_id,
            dataset_version="AWS mirror 2021",
            url=tile.url,
        )

    def _asset_catalog_url(self, spec: _ProductSpec, tile: CopernicusTile) -> str:
        query = urlencode({"list-type": "2", "prefix": f"{tile.tile_id}/"})
        return f"{spec.base_url}/?{query}"

    def _asset_catalog_identity(self, spec: _ProductSpec, tile: CopernicusTile) -> CacheIdentity:
        return CacheIdentity(
            provider_id=self.provider_id,
            dataset_id=spec.dataset_id,
            dataset_version="AWS mirror 2021 tile asset listing",
            url=self._asset_catalog_url(spec, tile),
        )

    def _quality_mask_identity(
        self,
        spec: _ProductSpec,
        role: CopernicusQualityMaskRole,
        url: str,
    ) -> CacheIdentity:
        return CacheIdentity(
            provider_id=self.provider_id,
            dataset_id=spec.dataset_id,
            dataset_version=f"AWS mirror 2021 {role.value.upper()} quality mask",
            url=url,
        )

    def _discover_quality_masks(
        self, spec: _ProductSpec, tile: CopernicusTile
    ) -> tuple[dict[CopernicusQualityMaskRole, str], DownloadResult]:
        listing = self.client.download(self._asset_catalog_identity(spec, tile))
        keys = parse_s3_object_listing(listing.path.read_bytes())
        discovered: dict[CopernicusQualityMaskRole, str] = {}
        for role in CopernicusQualityMaskRole:
            key = _quality_mask_key(tile, role)
            if key in keys:
                discovered[role] = f"{spec.base_url}/{quote(key, safe='/')}"
        return discovered, listing

    def _download_catalog(self, spec: _ProductSpec) -> tuple[set[str], DownloadResult]:
        result = self.client.download(self._catalog_identity(spec))
        tile_ids = parse_tile_list(result.path.read_bytes())
        if not tile_ids:
            raise ProviderFetchError(
                f"Copernicus tile list contained no recognized ids: {spec.tile_list_url}"
            )
        return tile_ids, result

    def plan(self, aoi: AreaOfInterest) -> tuple[CopernicusPlan, list[DownloadResult]]:
        if aoi.area_m2 > self.config.max_aoi_area_m2:
            raise ProviderFetchError(
                f"AOI area {aoi.area_m2:.0f} m² exceeds provider limit "
                f"{self.config.max_aoi_area_m2:.0f} m²; split the AOI explicitly"
            )
        coordinates = required_tile_coordinates(aoi)
        if not coordinates:
            raise ProviderFetchError("normalized AOI intersects no one-degree source cells")
        catalogs: list[DownloadResult] = []
        missing_glo30: list[str] = []
        for product in (CopernicusProduct.GLO_30, CopernicusProduct.GLO_90):
            spec = self._specs[product]
            available, catalog_result = self._download_catalog(spec)
            catalogs.append(catalog_result)
            expected = [
                tile_id(product, latitude, longitude) for latitude, longitude in coordinates
            ]
            missing = sorted(set(expected) - available)
            if not missing:
                tiles = [
                    CopernicusTile(
                        product=product,
                        latitude_degree=latitude,
                        longitude_degree=longitude,
                        tile_id=identifier,
                        url=f"{spec.base_url}/{identifier}/{identifier}.tif",
                    )
                    for (latitude, longitude), identifier in zip(coordinates, expected, strict=True)
                ]
                decisions = [
                    f"normalized AOI requires {len(coordinates)} one-degree source cell(s)",
                    f"{product.value} authoritative tileList.txt covers every required cell",
                    f"selected one dataset id {spec.dataset_id}; GLO-30/GLO-90 are not blended",
                ]
                if product is CopernicusProduct.GLO_90:
                    decisions.insert(
                        1,
                        f"GLO-30 lacked {len(missing_glo30)} required tile(s); "
                        "used complete GLO-90",
                    )
                return (
                    CopernicusPlan(
                        product=product,
                        dataset_id=spec.dataset_id,
                        horizontal_resolution_m=spec.resolution_m,
                        tiles=tiles,
                        decisions=decisions,
                        missing_glo30_tiles=missing_glo30,
                    ),
                    catalogs,
                )
            if product is CopernicusProduct.GLO_30:
                missing_glo30 = missing
        raise ProviderFetchError(
            "AOI is not completely covered by authoritative GLO-30 or GLO-90 tile lists; "
            "absent coastal/ocean cells remain NoData rather than being synthesized"
        )

    def metadata(self, product: CopernicusProduct = CopernicusProduct.GLO_30) -> DatasetMetadata:
        spec = self._specs[product]
        return DatasetMetadata(
            provider=self.provider_id,
            dataset_name=spec.dataset_name,
            dataset_version="AWS mirror 2021",
            dataset_type=DatasetType.DSM,
            horizontal_resolution_m=spec.resolution_m,
            horizontal_crs=(
                "EPSG:4326 source COGs; normalized AOI raster uses its recorded metric CRS"
            ),
            vertical_crs="EPSG:3855",
            vertical_datum="EGM2008",
            license=spec.license_id,
            attribution=_ATTRIBUTION,
            acquisition_period="TanDEM-X primarily 2011-2015; Copernicus DEM 2021 release",
            source_urls=[],
            checksums={},
        )

    def probe(self, aoi: AreaOfInterest) -> CoverageInfo:
        try:
            plan, _ = self.plan(aoi)
        except ProviderFetchError as exc:
            return CoverageInfo(
                covered=False,
                complete=False,
                dataset_type=DatasetType.DSM,
                requires_api_key=False,
                failure_probability="coverage-or-network-dependent",
                reason=[str(exc)],
            )
        return CoverageInfo(
            covered=True,
            complete=True,
            dataset_type=DatasetType.DSM,
            horizontal_resolution_m=plan.horizontal_resolution_m,
            requires_api_key=False,
            failure_probability="AWS mirror has no application-specific SLA",
            reason=plan.decisions,
        )

    def _target_grid(self, aoi: AreaOfInterest, resolution_m: float) -> tuple[CRS, Any, int, int]:
        target_crs = CRS.from_user_input(aoi.target_local_crs)
        transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
        geometry = shape(aoi.normalized_geometry_geojson)
        projected = transform_geometry(transformer.transform, geometry)
        left, bottom, right, top = projected.bounds
        width = max(2, math.ceil((right - left) / resolution_m))
        height = max(2, math.ceil((top - bottom) / resolution_m))
        if width * height > self.config.max_output_cells:
            raise ProviderFetchError(
                f"normalized provider raster would contain {width * height} cells, exceeding "
                f"max_output_cells={self.config.max_output_cells}; split the AOI"
            )
        return target_crs, from_bounds(left, bottom, right, top, width, height), height, width

    def acquire(self, aoi: AreaOfInterest, destination: Path) -> ProviderAcquisition:
        """Cache source COGs and quality masks, then write one AOI-bounded metric raster."""
        destination = destination.resolve()
        if destination.exists():
            raise ProviderFetchError(f"provider destination already exists: {destination}")
        manifest_path = destination.with_suffix(destination.suffix + ".source_acquisition.json")
        if manifest_path.exists():
            raise ProviderFetchError(
                f"provider acquisition manifest already exists: {manifest_path}"
            )
        quality_destinations = {
            role: destination.with_name(
                f"{destination.stem}.quality-{role.value}{destination.suffix}"
            )
            for role in CopernicusQualityMaskRole
        }
        existing_quality = [path for path in quality_destinations.values() if path.exists()]
        if existing_quality:
            raise ProviderFetchError(
                "provider quality-mask destination already exists: "
                + ", ".join(str(path) for path in existing_quality)
            )

        plan, catalogs = self.plan(aoi)
        spec = self._specs[plan.product]
        target_crs, target_transform, height, width = self._target_grid(
            aoi, plan.horizontal_resolution_m
        )
        destination_values = np.full((height, width), np.nan, dtype=np.float32)
        destination_coverage = np.zeros((height, width), dtype=np.uint8)
        tile_results: list[DownloadResult] = []
        asset_catalogs: list[DownloadResult] = []
        dem_signatures: dict[str, _SourceGridSignature] = {}
        quality_sources: dict[CopernicusQualityMaskRole, list[_QualityMaskSource]] = {
            role: [] for role in CopernicusQualityMaskRole
        }

        for index, tile in enumerate(plan.tiles):
            result = self.client.download(self._tile_identity(spec, tile))
            tile_results.append(result)
            try:
                with rasterio.open(result.path) as source:
                    if source.count != 1 or source.crs is None:
                        raise ProviderFetchError(
                            f"source COG is not a one-band georeferenced raster: {tile.url}"
                        )
                    dem_signatures[tile.tile_id] = _grid_signature(source)
                    reproject(
                        source=rasterio.band(source, 1),
                        destination=destination_values,
                        src_transform=source.transform,
                        src_crs=source.crs,
                        src_nodata=source.nodata,
                        dst_transform=target_transform,
                        dst_crs=target_crs,
                        dst_nodata=np.nan,
                        resampling=Resampling.bilinear,
                        init_dest_nodata=index == 0,
                    )
                    reproject(
                        source=np.ones((source.height, source.width), dtype=np.uint8),
                        destination=destination_coverage,
                        src_transform=source.transform,
                        src_crs=source.crs,
                        src_nodata=0,
                        dst_transform=target_transform,
                        dst_crs=target_crs,
                        dst_nodata=0,
                        resampling=Resampling.nearest,
                        init_dest_nodata=index == 0,
                    )
            except RasterioError as exc:
                raise ProviderFetchError(f"cached source COG did not reopen: {tile.url}") from exc

            discovered, asset_catalog = self._discover_quality_masks(spec, tile)
            asset_catalogs.append(asset_catalog)
            for role, url in discovered.items():
                mask_result = self.client.download(self._quality_mask_identity(spec, role, url))
                quality_sources[role].append(
                    _QualityMaskSource(tile=tile, role=role, url=url, result=mask_result)
                )

        if not bool(np.any(destination_coverage)):
            raise ProviderFetchError("provider source footprints do not cover the normalized AOI")
        original_shape = (height, width)
        top, bottom, left, right = largest_true_rectangle(destination_coverage.astype(bool))
        if bottom - top < 2 or right - left < 2:
            raise ProviderFetchError("provider source coverage left fewer than 2 x 2 AOI cells")
        destination_values = destination_values[top:bottom, left:right]
        selected_transform = target_transform * Affine.translation(left, top)
        height, width = destination_values.shape
        if not bool(np.any(np.isfinite(destination_values))):
            raise ProviderFetchError("provider acquisition produced no finite elevation samples")
        coverage_crop = {
            "original_grid_shape": list(original_shape),
            "selected_grid_shape": [height, width],
            "pixel_window": {
                "row_start": top,
                "row_stop": bottom,
                "column_start": left,
                "column_stop": right,
            },
            "discarded_reprojection_gap_cells": (
                original_shape[0] * original_shape[1] - height * width
            ),
            "method": (
                "largest all-source-footprint rectangle; source NoData remains masked and is not "
                "filled by provider acquisition"
            ),
        }

        quality_records: list[dict[str, Any]] = []
        quality_outputs: dict[
            CopernicusQualityMaskRole, tuple[np.ndarray, str, float | int | None]
        ] = {}
        for role in CopernicusQualityMaskRole:
            mask_spec = _QUALITY_MASK_SPECS[role]
            sources = quality_sources[role]
            source_evidence = [
                {
                    "tile_id": item.tile.tile_id,
                    "role": role.value,
                    "long_name": mask_spec.long_name,
                    "semantics": mask_spec.semantics,
                    **_download_evidence(item.result),
                }
                for item in sources
            ]
            base_record: dict[str, Any] = {
                "role": role.value,
                "long_name": mask_spec.long_name,
                "semantics": mask_spec.semantics,
                "product": plan.product.value,
                "expected_tile_count": len(plan.tiles),
                "exposed_tile_count": len(sources),
                "source_assets": source_evidence,
                "value_handling": (
                    "raw source values retained; nearest-neighbour reprojection only; no flag "
                    "remapping and no elevation modification"
                ),
            }
            if not sources:
                base_record.update(
                    {
                        "availability": "absent",
                        "decision": (
                            "the exact S3 tile-prefix listings exposed no asset for this role"
                        ),
                        "output": None,
                    }
                )
                quality_records.append(base_record)
                continue
            if len(sources) != len(plan.tiles):
                base_record.update(
                    {
                        "availability": "incomplete",
                        "decision": (
                            "exposed source assets were cached, but an AOI composite was omitted "
                            "because not every selected DEM tile exposed this role"
                        ),
                        "output": None,
                    }
                )
                quality_records.append(base_record)
                continue

            destination_mask: np.ndarray | None = None
            mask_coverage = np.zeros(original_shape, dtype=np.uint8)
            output_dtype = ""
            output_nodata: float | int | None = None
            for index, item in enumerate(sources):
                try:
                    with rasterio.open(item.result.path) as source:
                        if source.count != 1 or source.crs is None:
                            raise ProviderFetchError(
                                f"Copernicus {role.value.upper()} is not a one-band "
                                f"georeferenced raster: {item.url}"
                            )
                        _assert_quality_grid_alignment(
                            tile=item.tile,
                            role=role,
                            dem=dem_signatures[item.tile.tile_id],
                            mask=_grid_signature(source),
                        )
                        if destination_mask is None:
                            output_dtype = source.dtypes[0]
                            output_nodata = source.nodata
                            destination_mask = np.zeros(original_shape, dtype=output_dtype)
                        elif source.dtypes[0] != output_dtype or source.nodata != output_nodata:
                            raise ProviderFetchError(
                                f"Copernicus {role.value.upper()} dtype/NoData differs between "
                                "selected tiles; preserve the cached sources and inspect them"
                            )
                        reproject(
                            source=rasterio.band(source, 1),
                            destination=destination_mask,
                            src_transform=source.transform,
                            src_crs=source.crs,
                            src_nodata=source.nodata,
                            dst_transform=target_transform,
                            dst_crs=target_crs,
                            dst_nodata=output_nodata,
                            resampling=Resampling.nearest,
                            init_dest_nodata=index == 0,
                        )
                        reproject(
                            source=np.ones((source.height, source.width), dtype=np.uint8),
                            destination=mask_coverage,
                            src_transform=source.transform,
                            src_crs=source.crs,
                            src_nodata=0,
                            dst_transform=target_transform,
                            dst_crs=target_crs,
                            dst_nodata=0,
                            resampling=Resampling.nearest,
                            init_dest_nodata=index == 0,
                        )
                except RasterioError as exc:
                    raise ProviderFetchError(
                        f"cached Copernicus {role.value.upper()} did not reopen: {item.url}"
                    ) from exc
            if destination_mask is None:
                raise AssertionError("complete quality-mask source set produced no raster")
            selected_coverage = mask_coverage[top:bottom, left:right]
            if not bool(np.all(selected_coverage)):
                raise ProviderFetchError(
                    f"Copernicus {role.value.upper()} does not cover the exact selected DEM grid"
                )
            selected_mask = destination_mask[top:bottom, left:right]
            quality_outputs[role] = (selected_mask, output_dtype, output_nodata)
            base_record.update(
                {
                    "availability": "present",
                    "decision": (
                        "every selected DEM tile exposed this role; sources aligned exactly and "
                        "were cropped with the DEM pixel window"
                    ),
                    "output": {
                        "path": str(quality_destinations[role]),
                        "grid_shape": [height, width],
                        "crs": target_crs.to_string(),
                        "transform": list(selected_transform)[:6],
                        "dtype": output_dtype,
                        "nodata": output_nodata,
                        "resampling": "nearest",
                        "raw_value_min": float(np.min(selected_mask)),
                        "raw_value_max": float(np.max(selected_mask)),
                        "raw_values_preserved": True,
                    },
                }
            )
            quality_records.append(base_record)

        generated_at = datetime.now(UTC).isoformat()
        destination.parent.mkdir(parents=True, exist_ok=True)
        published_paths: list[Path] = []
        try:
            with rasterio.open(
                destination,
                "w",
                driver="GTiff",
                height=height,
                width=width,
                count=1,
                dtype="float32",
                crs=target_crs,
                transform=selected_transform,
                nodata=np.nan,
                compress="deflate",
                predictor=3,
            ) as output:
                output.write(destination_values, 1)
                output.update_tags(
                    PROVIDER=self.provider_id,
                    DATASET_ID=spec.dataset_id,
                    DATASET_VERSION="AWS mirror 2021",
                    DATASET_TYPE="dsm",
                    VERTICAL_CRS="EPSG:3855",
                    VERTICAL_DATUM="EGM2008",
                    LICENSE=spec.license_id,
                    LICENSE_URL=_LICENSE_URL,
                    ATTRIBUTION=_ATTRIBUTION,
                    LIABILITY_NOTICE=spec.liability_notice,
                    ACQUISITION_PERIOD="TanDEM-X primarily 2011-2015; Copernicus DEM 2021 release",
                    DOWNLOAD_TIME=generated_at,
                    SOURCE_URLS=json.dumps(
                        [tile.url for tile in plan.tiles], separators=(",", ":")
                    ),
                    NORMALIZED_AOI=json.dumps(
                        aoi_provenance(aoi), sort_keys=True, separators=(",", ":")
                    ),
                )
            published_paths.append(destination)
            if not destination.is_file():
                raise ProviderFetchError("provider raster was not published")

            for record in quality_records:
                role = CopernicusQualityMaskRole(record["role"])
                if role not in quality_outputs:
                    continue
                values, dtype, nodata = quality_outputs[role]
                path = quality_destinations[role]
                mask_spec = _QUALITY_MASK_SPECS[role]
                with rasterio.open(
                    path,
                    "w",
                    driver="GTiff",
                    height=height,
                    width=width,
                    count=1,
                    dtype=dtype,
                    crs=target_crs,
                    transform=selected_transform,
                    nodata=nodata,
                    compress="deflate",
                ) as output:
                    output.write(values, 1)
                    output.update_tags(
                        PROVIDER=self.provider_id,
                        DATASET_ID=spec.dataset_id,
                        QUALITY_MASK_ROLE=role.value,
                        QUALITY_MASK_LONG_NAME=mask_spec.long_name,
                        QUALITY_MASK_SEMANTICS=mask_spec.semantics,
                        VALUE_HANDLING=(
                            "raw values; nearest-neighbour reprojection; no flag remapping"
                        ),
                        SOURCE_URLS=json.dumps(
                            [item.url for item in quality_sources[role]], separators=(",", ":")
                        ),
                    )
                published_paths.append(path)
                if not path.is_file():
                    raise ProviderFetchError(
                        f"provider quality mask was not published: {role.value}"
                    )
                output_record = record["output"]
                if not isinstance(output_record, dict):
                    raise AssertionError("present quality mask is missing output metadata")
                output_record["sha256"] = sha256_file(path)
                with rasterio.open(path) as reopened:
                    if (
                        reopened.count != 1
                        or reopened.shape != (height, width)
                        or CRS.from_user_input(reopened.crs) != target_crs
                        or not np.allclose(
                            tuple(reopened.transform),
                            tuple(selected_transform),
                            atol=1e-12,
                            rtol=0.0,
                        )
                    ):
                        raise ProviderFetchError(
                            f"reopened provider quality mask is not aligned: {role.value}"
                        )

            dataset = self.metadata(plan.product).model_copy(
                update={
                    "download_time": generated_at,
                    "source_urls": [tile.url for tile in plan.tiles],
                    "checksums": {
                        tile.tile_id: result.cache_entry.object_sha256
                        for tile, result in zip(plan.tiles, tile_results, strict=True)
                    },
                }
            )
            acquisition = ProviderAcquisition(
                provider_id=self.provider_id,
                raster_path=destination,
                acquisition_manifest_path=manifest_path,
                dataset=dataset,
                aoi=aoi_provenance(aoi),
                plan=plan,
                catalog_downloads=[_download_evidence(result) for result in catalogs],
                asset_catalog_downloads=[
                    {"tile_id": tile.tile_id, **_download_evidence(result)}
                    for tile, result in zip(plan.tiles, asset_catalogs, strict=True)
                ],
                tile_downloads=[_download_evidence(result) for result in tile_results],
                quality_masks=quality_records,
                generated_at=generated_at,
            )
            manifest = acquisition.model_dump(mode="json")
            manifest["coverage_crop"] = coverage_crop
            manifest["output_source_nodata_pixels"] = int(
                np.count_nonzero(~np.isfinite(destination_values))
            )
            manifest["output_raster_sha256"] = sha256_file(destination)
            manifest["license_url"] = _LICENSE_URL
            manifest["liability_notice"] = spec.liability_notice
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            published_paths.append(manifest_path)
            return acquisition
        except BaseException:
            for path in reversed(published_paths):
                path.unlink(missing_ok=True)
            raise

    def fetch(self, aoi: AreaOfInterest, destination: Path) -> ProviderAcquisition:
        """Protocol-compatible alias for normalized AOI acquisition."""
        return self.acquire(aoi, destination)
