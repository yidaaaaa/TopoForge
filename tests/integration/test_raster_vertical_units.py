"""Encoded elevations must retain physical units through manufacturing and reopen."""

import json
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
import rasterio
from rasterio.transform import from_origin

from topoforge.engine import build_local_terrain, verify_artifact_bundle
from topoforge.exceptions import RasterProcessingError
from topoforge.models import (
    AreaOfInterestInput,
    BuildConfig,
    DatasetMetadata,
    SamplingMode,
    VerticalScaleMode,
)
from topoforge.providers import ProviderSelectionTrace
from topoforge.raster import process_local_raster
from topoforge.util import sha256_file
from topoforge.workflow import local as workflow_local
from topoforge.workflow.acquisition import GlobalAcquisitionConfig, GlobalSourceEvidence


def _write_dem(
    path: Path,
    *,
    scale: float = 1.0,
    offset: float = 0.0,
    unit: str | None = "metre",
    raw: npt.NDArray[np.int16] | npt.NDArray[np.int32] | None = None,
    nodata: int | None = None,
) -> Path:
    values = np.arange(100, 116, dtype=np.int16).reshape(4, 4) if raw is None else raw
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        crs="EPSG:32648",
        transform=from_origin(500_000, 3_300_000, 10, 10),
        nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)
        dataset.scales = [scale]
        dataset.offsets = [offset]
        if unit is not None:
            dataset.set_band_unit(1, unit)
    return path


def _config(source: Path, output: Path) -> BuildConfig:
    return BuildConfig(
        dem_path=source,
        output_dir=output,
        model_width_mm=40,
        sampling_mode=SamplingMode.SOURCE_PRESERVING,
        vertical_scale_mode=VerticalScaleMode.NATURAL,
    )


@pytest.mark.parametrize(
    ("scale", "offset", "unit", "unit_to_m"),
    [(0.1, 1000.0, "metre", 1.0), (0.25, 50.0, "ft", 0.3048)],
)
def test_encoded_dem_produces_physical_metres_and_correct_printed_relief(
    tmp_path: Path, scale: float, offset: float, unit: str, unit_to_m: float
) -> None:
    source = _write_dem(tmp_path / "encoded.tif", scale=scale, offset=offset, unit=unit)
    config = _config(source, tmp_path / "build")
    result = build_local_terrain(config)
    expected_m = (np.arange(100, 116).reshape(4, 4) * scale + offset) * unit_to_m

    with rasterio.open(result.artifacts["processed_dem"]) as dataset:
        np.testing.assert_allclose(dataset.read(1), expected_m, rtol=0, atol=0.0001)
        assert dataset.units == ("metre",)
        assert dataset.scales == (1.0,)
        assert dataset.offsets == (0.0,)
    assert result.validation["raw_elevation_min_m"] == pytest.approx(expected_m.min())
    assert result.validation["raw_elevation_max_m"] == pytest.approx(expected_m.max())
    assert result.validation["dimensions_mm"][2] - config.base_thickness_mm == pytest.approx(
        float(np.ptp(expected_m)), abs=0.0001
    )
    assert result.provenance["dataset"]["elevation_conversion"] == {
        "scale": scale,
        "offset": offset,
        "source_unit": unit,
        "unit_source": "band",
        "unit_to_m": unit_to_m,
        "output_unit": "metre",
        "formula": "(raw * scale + offset) * unit_to_m",
    }
    assert result.provenance["dataset"]["vertical_datum"] == "unknown"
    assert result.validation["required_checks_passed"] is True
    assert verify_artifact_bundle(result.output_dir)["required_checks_passed"] is True


@pytest.mark.parametrize("scale", [-0.5, 0.0, 0.5])
def test_decoding_preserves_source_mask_and_physical_peak(tmp_path: Path, scale: float) -> None:
    raw = np.arange(100, 125, dtype=np.int16).reshape(5, 5)
    raw[2, 2] = -9999
    source = _write_dem(
        tmp_path / "masked.tif", raw=raw, scale=scale, offset=1000, unit="ft", nodata=-9999
    )
    result = process_local_raster(_config(source, tmp_path / "out"))
    expected_mask = raw == -9999
    expected_m = (raw.astype(np.float64) * scale + 1000) * 0.3048
    np.testing.assert_array_equal(result.original_nodata_mask, expected_mask)
    np.testing.assert_allclose(result.elevations_m[~expected_mask], expected_m[~expected_mask])
    assert result.report.original_nodata_fraction == pytest.approx(1 / 25)
    assert result.report.interpolated_fraction == pytest.approx(1 / 25)
    peak = result.report.raw_peak_coordinate
    expected_peak = (0, 0) if scale <= 0 else (4, 4)
    assert (peak["row"], peak["column"]) == expected_peak
    with rasterio.open(result.report.original_nodata_mask_path) as dataset:
        np.testing.assert_array_equal(dataset.read(1).astype(bool), expected_mask)


def test_decode_integer_offset_before_float32_rounding(tmp_path: Path) -> None:
    raw = np.arange(1_000_000_000, 1_000_000_016, dtype=np.int32).reshape(4, 4)
    source = _write_dem(tmp_path / "packed.tif", raw=raw, scale=0.25, offset=-250_000_000)
    result = process_local_raster(_config(source, tmp_path / "out"))
    np.testing.assert_array_equal(result.elevations_m, np.arange(16).reshape(4, 4) * 0.25)
    assert result.report.raw_elevation_max_m == 3.75


@pytest.mark.parametrize(
    ("unit", "factor"),
    [(" METERS ", 1.0), ("feet", 0.3048), ("US survey foot", 1200 / 3937), ("ft_us", 1200 / 3937)],
)
def test_explicit_vertical_unit_aliases_are_normalized(
    tmp_path: Path, unit: str, factor: float
) -> None:
    source = _write_dem(tmp_path / "units.tif", unit=unit)
    result = process_local_raster(_config(source, tmp_path / "out"))
    np.testing.assert_allclose(result.elevations_m, np.arange(100, 116).reshape(4, 4) * factor)
    conversion = result.report.metadata.elevation_conversion
    assert conversion is not None
    with rasterio.open(source) as dataset:
        assert conversion.source_unit == dataset.units[0]
    assert conversion.unit_to_m == factor


@pytest.mark.parametrize("band_tag", [False, True])
def test_explicit_unit_tags_are_used_when_band_unit_is_absent(
    tmp_path: Path, band_tag: bool
) -> None:
    source = _write_dem(tmp_path / "tagged.tif", unit=None)
    with rasterio.open(source, "r+") as dataset:
        dataset.update_tags(bidx=1 if band_tag else 0, UNITS="feet")
    result = process_local_raster(_config(source, tmp_path / "out"))
    np.testing.assert_allclose(result.elevations_m, np.arange(100, 116).reshape(4, 4) * 0.3048)
    conversion = result.report.metadata.elevation_conversion
    assert conversion is not None
    assert conversion.unit_source == ("band-tag" if band_tag else "dataset-tag")


def test_missing_units_records_existing_metres_assumption(tmp_path: Path) -> None:
    source = _write_dem(tmp_path / "unlabelled.tif", unit=None, scale=0.5, offset=100)
    result = process_local_raster(_config(source, tmp_path / "out"))
    np.testing.assert_array_equal(
        result.elevations_m, np.arange(100, 116).reshape(4, 4) * 0.5 + 100
    )
    conversion = result.report.metadata.elevation_conversion
    assert conversion is not None
    assert conversion.source_unit is None
    assert conversion.unit_source == "assumed-metres"
    assert conversion.unit_to_m == 1.0


@pytest.mark.parametrize("unit", ["furlong", "degree", "unknown"])
def test_unsupported_declared_units_fail_with_corrective_action(tmp_path: Path, unit: str) -> None:
    source = _write_dem(tmp_path / "invalid-unit.tif", unit=unit)
    with pytest.raises(RasterProcessingError, match=r"Unsupported elevation unit.*convert the DEM"):
        process_local_raster(_config(source, tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_conflicting_unit_declarations_fail(tmp_path: Path) -> None:
    source = _write_dem(tmp_path / "conflict.tif", unit="ft")
    with rasterio.open(source, "r+") as dataset:
        dataset.update_tags(UNITS="metre")
    with pytest.raises(RasterProcessingError, match=r"conflicting elevation units.*correct"):
        process_local_raster(_config(source, tmp_path / "out"))


@pytest.mark.parametrize(
    ("scale", "offset"),
    [
        (float("nan"), 0),
        (float("inf"), 0),
        (-float("inf"), 0),
        (1, float("nan")),
        (1, float("inf")),
    ],
)
def test_nonfinite_encoding_metadata_fails_before_processing(
    tmp_path: Path, scale: float, offset: float
) -> None:
    source = _write_dem(tmp_path / "invalid-encoding.tif", scale=scale, offset=offset)
    with pytest.raises(
        RasterProcessingError, match="non-finite elevation scale or offset; correct"
    ):
        process_local_raster(_config(source, tmp_path / "out"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("scale", "offset"), [(1e308, 0), (1, 1e100)])
def test_decoding_overflow_cannot_be_disguised_as_nodata(
    tmp_path: Path, scale: float, offset: float
) -> None:
    source = _write_dem(tmp_path / "overflow.tif", scale=scale, offset=offset)
    with pytest.raises(RasterProcessingError, match=r"Decoded elevations exceed.*correct"):
        process_local_raster(_config(source, tmp_path / "out"))


def test_source_and_external_mask_checksums_describe_observed_inputs(tmp_path: Path) -> None:
    raw = np.arange(100, 125, dtype=np.int16).reshape(5, 5)
    source = _write_dem(tmp_path / "external-mask.tif", raw=raw)
    mask = np.full((5, 5), 255, dtype=np.uint8)
    mask[2, 2] = 0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False), rasterio.open(source, "r+") as dataset:
        dataset.write_mask(mask)
    sidecar = source.with_name(source.name + ".msk")
    assert sidecar.is_file()
    config = _config(source, tmp_path / "out").model_copy(
        update={
            "source_checksums": {
                source.name: "0" * 64,
                sidecar.name: "1" * 64,
                "upstream.tif": "2" * 64,
            }
        }
    )
    result = process_local_raster(config)
    assert result.report.metadata.checksums == {
        source.name: sha256_file(source),
        sidecar.name: sha256_file(sidecar),
        "upstream.tif": "2" * 64,
    }
    np.testing.assert_array_equal(result.original_nodata_mask, mask == 0)


def test_legacy_acquisition_stage_keeps_absent_conversion_metadata_canonical(
    tmp_path: Path,
) -> None:
    legacy_dataset = {
        "provider": "local",
        "dataset_name": "Legacy DEM",
        "dataset_version": "unknown",
        "dataset_type": "unknown",
        "horizontal_resolution_m": None,
        "horizontal_crs": "EPSG:32647",
        "vertical_crs": "unknown",
        "vertical_datum": "unknown",
        "license": "user-supplied; verify source terms",
        "attribution": "Provided by the user",
        "acquisition_period": "unknown",
        "download_time": "unknown",
        "source_urls": [],
        "checksums": {},
    }
    config = GlobalAcquisitionConfig(aoi=AreaOfInterestInput(bbox_wgs84=(100, 30, 100.01, 30.01)))
    evidence = GlobalSourceEvidence(
        raster_path=tmp_path / "global-aoi.tif",
        acquisition_manifest_path=tmp_path / "global-aoi.source_acquisition.json",
        raster_sha256="0" * 64,
        acquisition_manifest_sha256="1" * 64,
        dataset=DatasetMetadata.model_validate(legacy_dataset),
        normalized_aoi=config.normalized_aoi(),
        provider_selection=ProviderSelectionTrace(
            policy=config.selection_policy(),
            evaluations=[],
            ranked_provider_ids=[],
            fetch_attempts=[],
            selected_provider="local",
            selected_dataset="Legacy DEM",
            outcome="selected",
        ),
        required_checks_passed=True,
    )
    assert evidence.dataset.elevation_conversion is None
    assert evidence.dataset.model_dump(mode="json") == legacy_dataset
    assert json.loads(evidence.model_dump_json())["dataset"] == legacy_dataset
    legacy_stage = {
        "schema_version": "topoforge-global-acquisition-stage-v1",
        "acquisition_identity": config.identity_payload(),
        "raster_path": str(evidence.raster_path),
        "raster_sha256": evidence.raster_sha256,
        "acquisition_manifest_path": str(evidence.acquisition_manifest_path),
        "acquisition_manifest_sha256": evidence.acquisition_manifest_sha256,
        "dataset": legacy_dataset,
        "normalized_aoi": evidence.normalized_aoi.model_dump(mode="json"),
        "provider_selection": evidence.provider_selection.model_dump(mode="json"),
        "quality_masks": [],
        "required_checks_passed": True,
    }
    path = tmp_path / "acquire.json"
    original_bytes = (
        json.dumps(legacy_stage, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    path.write_bytes(original_bytes)
    assert workflow_local._verify_acquisition_stage_manifest(path, config, evidence) == legacy_stage
    assert path.read_bytes() == original_bytes
