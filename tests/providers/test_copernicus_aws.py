from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from topoforge.exceptions import ProviderFetchError
from topoforge.models import AreaOfInterestInput, TerrainMode
from topoforge.providers.cache import ContentAddressedCache
from topoforge.providers.copernicus_aws import (
    CopernicusAwsConfig,
    CopernicusAwsProvider,
    CopernicusProduct,
    CopernicusQualityMaskRole,
    required_tile_coordinates,
    tile_id,
)
from topoforge.providers.protocol import ProviderDescriptor
from topoforge.providers.selection import ProviderSelectionPolicy, fetch_with_provider_selection
from topoforge.providers.transport import CachingHttpClient, HttpTransportConfig
from topoforge.raster import normalize_area_of_interest


class FakeResponse:
    def __init__(self, payload: bytes, media_type: str) -> None:
        self.status = 200
        self._stream = BytesIO(payload)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(payload))
        self.headers["Content-Type"] = media_type
        self.headers["ETag"] = '"fixture"'

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def close(self) -> None:
        pass


def tiny_geotiff(*, right: float = 102.0) -> bytes:
    values = np.arange(64, dtype=np.float32).reshape(8, 8) + 1000
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            height=8,
            width=8,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_bounds(101, 29, right, 30, 8, 8),
            nodata=-9999.0,
        ) as dataset:
            dataset.write(values, 1)
        return memory.read()


def tiny_quality_geotiff(
    *, right: float = 102.0, dtype: str = "uint8", value_offset: float = 0.0
) -> bytes:
    values = (np.arange(64, dtype=np.float32).reshape(8, 8) + value_offset).astype(dtype)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            height=8,
            width=8,
            count=1,
            dtype=dtype,
            crs="EPSG:4326",
            transform=from_bounds(101, 29, right, 30, 8, 8),
            nodata=None,
        ) as dataset:
            dataset.write(values, 1)
        return memory.read()


def asset_listing_url(base_url: str, identifier: str) -> str:
    return f"{base_url}/?{urlencode({'list-type': '2', 'prefix': identifier + '/'})}"


def quality_url(base_url: str, identifier: str, role: str) -> str:
    stem = identifier.removesuffix("_DEM")
    return f"{base_url}/{identifier}/AUXFILES/{stem}_{role.upper()}.tif"


def asset_listing(identifier: str, roles: tuple[str, ...] = ()) -> bytes:
    keys = [f"{identifier}/{identifier}.tif"]
    keys.extend(
        f"{identifier}/AUXFILES/{identifier.removesuffix('_DEM')}_{role.upper()}.tif"
        for role in roles
    )
    contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys)
    return f"<ListBucketResult>{contents}</ListBucketResult>".encode()


def provider(tmp_path: Path, responses: dict[str, bytes]) -> CopernicusAwsProvider:
    def opener(request: object, _timeout: float) -> FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        payload = responses[url]
        media_type = "image/tiff" if url.endswith(".tif") else "text/plain"
        return FakeResponse(payload, media_type)

    client = CachingHttpClient(
        ContentAddressedCache(tmp_path / "cache"),
        HttpTransportConfig(
            timeout_seconds=1,
            max_attempts=2,
            backoff_base_seconds=0,
            max_backoff_seconds=0,
            min_request_interval_seconds=0,
            max_download_bytes=1_000_000,
        ),
        open_url=opener,  # type: ignore[arg-type]
    )
    return CopernicusAwsProvider(
        client,
        CopernicusAwsConfig(
            glo30_base_url="https://fixture.test/glo30",
            glo30_tile_list_url="https://fixture.test/glo30/tileList.txt",
            glo90_base_url="https://fixture.test/glo90",
            glo90_tile_list_url="https://fixture.test/glo90/tileList.txt",
            max_output_cells=1_000_000,
        ),
    )


def test_tile_enumeration_handles_antimeridian_without_expansion() -> None:
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(179.5, -1.0, -179.5, 1.0)))

    assert required_tile_coordinates(aoi) == [
        (-1, -180),
        (-1, 179),
        (0, -180),
        (0, 179),
    ]
    assert tile_id(CopernicusProduct.GLO_30, -1, -180).endswith("S01_00_W180_00_DEM")


def test_plan_uses_authoritative_glo30_catalog_when_complete(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    source_url = f"https://fixture.test/glo30/{identifier}/{identifier}.tif"
    responses = {
        "https://fixture.test/glo30/tileList.txt": f"{identifier}/{identifier}.tif\n".encode(),
        source_url: tiny_geotiff(),
        asset_listing_url("https://fixture.test/glo30", identifier): asset_listing(identifier),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    plan, catalogs = instance.plan(aoi)

    assert plan.product is CopernicusProduct.GLO_30
    assert [tile.tile_id for tile in plan.tiles] == [identifier]
    assert len(catalogs) == 1


def test_plan_falls_back_wholly_to_glo90_instead_of_blending(tmp_path: Path) -> None:
    identifier90 = tile_id(CopernicusProduct.GLO_90, 29, 101)
    responses = {
        "https://fixture.test/glo30/tileList.txt": b"Copernicus_DSM_COG_10_N28_00_E101_00_DEM\n",
        "https://fixture.test/glo90/tileList.txt": f"{identifier90}\n".encode(),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    plan, catalogs = instance.plan(aoi)

    assert plan.product is CopernicusProduct.GLO_90
    assert plan.missing_glo30_tiles == [tile_id(CopernicusProduct.GLO_30, 29, 101)]
    assert len(catalogs) == 2
    assert "not blended" in plan.decisions[-1]


def test_acquire_writes_metric_aoi_raster_and_source_manifest(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    source_url = f"https://fixture.test/glo30/{identifier}/{identifier}.tif"
    responses = {
        "https://fixture.test/glo30/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(),
        asset_listing_url("https://fixture.test/glo30", identifier): asset_listing(identifier),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))
    destination = tmp_path / "acquired.tif"

    result = instance.acquire(aoi, destination)

    assert result.dataset.provider == "copernicus-aws"
    assert result.dataset.dataset_type.value == "dsm"
    assert result.acquisition_manifest_path.is_file()
    with rasterio.open(result.raster_path) as dataset:
        assert dataset.crs is not None and dataset.crs.is_projected
        assert dataset.tags()["DATASET_ID"] == "copernicus-dem-glo-30-aws-2021"
        assert np.count_nonzero(np.isfinite(dataset.read(1))) > 0
    assert instance.client.cache.summary().content_objects == 3


def test_provider_acquisition_reuses_local_build_and_enters_provenance(tmp_path: Path) -> None:
    from topoforge.engine import build_local_terrain
    from topoforge.models import BuildConfig

    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    source_url = f"https://fixture.test/glo30/{identifier}/{identifier}.tif"
    responses = {
        "https://fixture.test/glo30/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(),
        **quality_fixture_responses(
            identifier, base_url="https://fixture.test/glo30", roles=("edm",)
        ),
    }
    instance = provider(tmp_path, responses)
    request = AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.21, 29.21))
    aoi = normalize_area_of_interest(request)
    selection = fetch_with_provider_selection(
        aoi=aoi,
        destination=tmp_path / "source" / "dem.tif",
        providers={"copernicus-aws": instance},
        descriptors=[
            ProviderDescriptor(
                provider_id="copernicus-aws",
                name="Copernicus fixture",
                implemented=True,
                requires_api_key=False,
                dataset_types=[instance.metadata().dataset_type],
                notes="offline fixture",
            )
        ],
        policy=ProviderSelectionPolicy(
            requested_terrain_mode=TerrainMode.DSM,
        ),
    )
    acquisition = selection.acquisition
    assert hasattr(acquisition, "dataset")
    dataset = acquisition.dataset

    result = build_local_terrain(
        BuildConfig(
            dem_path=acquisition.raster_path,
            output_dir=tmp_path / "bundle",
            model_width_mm=80,
            max_height_mm=30,
            max_grid_cells=100_000,
            aoi=request,
            dataset_type=dataset.dataset_type,
            dataset_name=dataset.dataset_name,
            dataset_version=dataset.dataset_version,
            acquisition_period=dataset.acquisition_period,
            source_urls=dataset.source_urls,
            vertical_crs=dataset.vertical_crs,
            vertical_datum=dataset.vertical_datum,
            data_license=dataset.license,
            attribution=dataset.attribution,
            source_provider=dataset.provider,
            source_download_time=dataset.download_time,
            source_checksums=dataset.checksums,
            source_acquisition_manifest=acquisition.acquisition_manifest_path,
        )
    )

    assert result.validation["required_checks_passed"] is True
    acquisition_manifest = json.loads(
        acquisition.acquisition_manifest_path.read_text(encoding="utf-8")
    )
    assert result.provenance["provider_selection"] == selection.trace.model_dump(mode="json")
    assert result.provenance["provider_selection"] == acquisition_manifest["provider_selection"]
    assert result.provenance["provider_selection"]["selected_provider"] == "copernicus-aws"
    assert result.provenance["source_acquisition"]["plan"]["dataset_id"] == (
        "copernicus-dem-glo-30-aws-2021"
    )
    assert result.artifacts["source_acquisition"].is_file()
    assert result.artifacts["source_quality_edm"].is_file()
    assert result.provenance["source_acquisition"]["quality_masks"][0]["output"][
        "bundled_artifact"
    ] == ("source_quality_edm.tif")


def test_acquire_crops_reprojection_only_gap_at_source_tile_edge(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    source_url = f"https://fixture.test/glo30/{identifier}/{identifier}.tif"
    responses = {
        "https://fixture.test/glo30/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(right=101.995),
        asset_listing_url("https://fixture.test/glo30", identifier): asset_listing(identifier),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.98, 29.2, 102.0, 29.22)))

    result = instance.acquire(aoi, tmp_path / "edge.tif")

    with rasterio.open(result.raster_path) as dataset:
        assert np.all(np.isfinite(dataset.read(1)))
    manifest = json.loads(result.acquisition_manifest_path.read_text(encoding="utf-8"))
    assert manifest["coverage_crop"]["discarded_reprojection_gap_cells"] > 0
    assert manifest["output_source_nodata_pixels"] == 0


def quality_fixture_responses(
    identifier: str,
    *,
    base_url: str = "https://fixture.test/glo30",
    roles: tuple[str, ...] = ("edm", "flm", "hem", "wbm"),
) -> dict[str, bytes]:
    responses: dict[str, bytes] = {
        asset_listing_url(base_url, identifier): asset_listing(identifier, roles)
    }
    for index, role in enumerate(roles):
        responses[quality_url(base_url, identifier, role)] = tiny_quality_geotiff(
            dtype="float32" if role == "hem" else "uint8",
            value_offset=index + (0.25 if role == "hem" else 0.0),
        )
    return responses


def test_acquire_preserves_all_exposed_quality_masks_on_exact_dem_grid(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    base_url = "https://fixture.test/glo30"
    source_url = f"{base_url}/{identifier}/{identifier}.tif"
    responses = {
        f"{base_url}/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(),
        **quality_fixture_responses(identifier, base_url=base_url),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    result = instance.acquire(aoi, tmp_path / "quality.tif")

    assert {record["role"] for record in result.quality_masks} == {
        role.value for role in CopernicusQualityMaskRole
    }
    with rasterio.open(result.raster_path) as dem:
        dem_shape = dem.shape
        dem_crs = dem.crs
        dem_transform = dem.transform
    for record in result.quality_masks:
        assert record["availability"] == "present"
        output = record["output"]
        assert isinstance(output, dict)
        assert output["raw_values_preserved"] is True
        path = Path(output["path"])
        assert path.is_file()
        with rasterio.open(path) as mask:
            assert mask.shape == dem_shape
            assert mask.crs == dem_crs
            assert mask.transform.almost_equals(dem_transform)
            assert mask.tags()["QUALITY_MASK_ROLE"] == record["role"]
    assert instance.client.cache.summary().content_objects == 7


def test_acquire_records_quality_masks_absent_without_inventing_outputs(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    base_url = "https://fixture.test/glo30"
    source_url = f"{base_url}/{identifier}/{identifier}.tif"
    responses = {
        f"{base_url}/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(),
        asset_listing_url(base_url, identifier): asset_listing(identifier),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    result = instance.acquire(aoi, tmp_path / "no-quality.tif")

    assert all(record["availability"] == "absent" for record in result.quality_masks)
    assert all(record["output"] is None for record in result.quality_masks)
    assert not list(tmp_path.glob("no-quality.quality-*.tif"))


def test_acquire_rejects_quality_mask_alignment_mismatch(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    base_url = "https://fixture.test/glo30"
    source_url = f"{base_url}/{identifier}/{identifier}.tif"
    responses = {
        f"{base_url}/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(),
        asset_listing_url(base_url, identifier): asset_listing(identifier, ("edm",)),
        quality_url(base_url, identifier, "edm"): tiny_quality_geotiff(right=101.9),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    with pytest.raises(ProviderFetchError, match="EDM grid does not align"):
        instance.acquire(aoi, tmp_path / "misaligned.tif")

    assert not (tmp_path / "misaligned.tif").exists()


def test_quality_mask_cache_hits_and_recovers_corrupt_object(tmp_path: Path) -> None:
    identifier = tile_id(CopernicusProduct.GLO_30, 29, 101)
    base_url = "https://fixture.test/glo30"
    source_url = f"{base_url}/{identifier}/{identifier}.tif"
    responses = {
        f"{base_url}/tileList.txt": f"{identifier}\n".encode(),
        source_url: tiny_geotiff(),
        **quality_fixture_responses(identifier, base_url=base_url, roles=("edm",)),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    first = instance.acquire(aoi, tmp_path / "first.tif")
    second = instance.acquire(aoi, tmp_path / "second.tif")
    second_edm = next(record for record in second.quality_masks if record["role"] == "edm")
    assert second_edm["source_assets"][0]["cache_status"] == "hit"

    first_edm = next(record for record in first.quality_masks if record["role"] == "edm")
    edm_sha256 = first_edm["source_assets"][0]["sha256"]
    instance.client.cache.object_path(edm_sha256).write_bytes(b"corrupt")

    third = instance.acquire(aoi, tmp_path / "third.tif")
    third_edm = next(record for record in third.quality_masks if record["role"] == "edm")
    assert third_edm["source_assets"][0]["cache_status"] == "corrupt"
    assert third_edm["output"]["sha256"] == first_edm["output"]["sha256"]


def test_glo90_uses_product_specific_quality_mask_assets(tmp_path: Path) -> None:
    identifier90 = tile_id(CopernicusProduct.GLO_90, 29, 101)
    base_url = "https://fixture.test/glo90"
    source_url = f"{base_url}/{identifier90}/{identifier90}.tif"
    responses = {
        "https://fixture.test/glo30/tileList.txt": (b"Copernicus_DSM_COG_10_N28_00_E101_00_DEM\n"),
        f"{base_url}/tileList.txt": f"{identifier90}\n".encode(),
        source_url: tiny_geotiff(),
        **quality_fixture_responses(
            identifier90,
            base_url=base_url,
            roles=("edm",),
        ),
    }
    instance = provider(tmp_path, responses)
    aoi = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.2, 29.2, 101.3, 29.3)))

    result = instance.acquire(aoi, tmp_path / "glo90.tif")

    assert result.plan.product is CopernicusProduct.GLO_90
    edm = next(record for record in result.quality_masks if record["role"] == "edm")
    assert edm["availability"] == "present"
    assert "Copernicus_DSM_COG_30_" in edm["source_assets"][0]["url"]
