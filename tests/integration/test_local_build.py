import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from topoforge.engine import (
    build_local_terrain,
    record_slice_validation,
    verify_artifact_bundle,
)
from topoforge.exceptions import MeshValidationError
from topoforge.models import BuildConfig, DatasetType, VerticalScaleMode
from topoforge.provenance import write_json, write_validation_html
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file


def test_synthetic_geotiff_to_complete_validated_bundle(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "gaussian-hill.tif",
        SyntheticTerrain.GAUSSIAN_HILL,
        rows=36,
        columns=48,
        pixel_size_m=15.0,
    )
    source_acquisition_manifest = tmp_path / "source-acquisition.json"
    source_acquisition_manifest.write_text(
        json.dumps({"quality_masks": []}, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "build"
    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=180.0,
            base_thickness_mm=3.0,
            max_height_mm=42.0,
            vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
            dataset_type=DatasetType.DTM,
            dataset_name="Deterministic Gaussian hill",
            dataset_version="fixture-v1",
            acquisition_period="2026-07-31",
            source_urls=["https://example.test/synthetic-dem"],
            data_license="Apache-2.0 synthetic fixture",
            attribution="TopoForge analytic fixture",
            source_acquisition_manifest=source_acquisition_manifest,
        )
    )

    expected_names = {
        "model.stl",
        "model.3mf",
        "preview.glb",
        "processed_dem.tif",
        "original_nodata_mask.tif",
        "manufacturing_preflight.json",
        "provenance.json",
        "validation.json",
        "validation.html",
        "build_config.resolved.yaml",
        "preview.png",
        "build_manifest.json",
        "source_acquisition.json",
    }
    assert {path.name for path in result.artifacts.values()} == expected_names
    assert all(path.is_file() and path.stat().st_size > 0 for path in result.artifacts.values())
    assert result.validation["required_checks_passed"] is True
    assert result.validation["watertight"] is True
    assert result.validation["manifold"] is True
    assert result.validation["connected_components"] == 1
    assert result.validation["dimensions_mm"][0] == pytest.approx(180.0, abs=0.05)
    assert result.validation["dimensions_mm"][1] == pytest.approx(135.0, abs=0.05)
    assert result.validation["dimensions_mm"][2] <= 42.0 + 0.05
    assert result.validation["height_limit_passed"] is True
    assert result.validation["minimum_base_thickness_passed"] is True
    assert result.validation["triangle_budget_passed"] is True
    assert result.validation["bottom_planarity_error_mm"] <= 0.01
    dataset = result.provenance["dataset"]
    assert dataset["dataset_version"] == "fixture-v1"
    assert dataset["acquisition_period"] == "2026-07-31"
    assert dataset["download_time"] == "not-applicable-local-input"
    assert dataset["source_urls"] == ["https://example.test/synthetic-dem"]
    assert "nearest valid cells" in " ".join(result.provenance["processing"]["pipeline"])
    evidence = verify_artifact_bundle(output)
    assert evidence["required_checks_passed"] is True
    assert evidence["dataset_name"] == "Deterministic Gaussian hill"
    with pytest.raises(MeshValidationError, match="At least one manufacturing output format"):
        verify_artifact_bundle(output, required_formats=[])

    preview_path = result.artifacts["preview_png"]
    preview_bytes = preview_path.read_bytes()
    preview_path.write_bytes(preview_bytes + b"tamper")
    with pytest.raises(MeshValidationError, match="checksum mismatch"):
        verify_artifact_bundle(output)
    preview_path.write_bytes(preview_bytes)

    manifest_path = result.artifacts["manifest"]
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    del manifest["artifacts"]["processed_dem"]
    del manifest["sha256"]["processed_dem"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MeshValidationError, match="canonical artifact role processed_dem"):
        verify_artifact_bundle(output)
    manifest_path.write_bytes(manifest_bytes)

    mask_path = result.artifacts["original_nodata_mask"]
    mask_bytes = mask_path.read_bytes()
    with rasterio.open(mask_path, "r+") as mask_dataset:
        mask_dataset.transform = mask_dataset.transform * Affine.translation(1.0, 0.0)
    manifest = json.loads(manifest_bytes)
    manifest["sha256"]["original_nodata_mask"] = sha256_file(mask_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MeshValidationError, match="not aligned with processed_dem"):
        verify_artifact_bundle(output)
    mask_path.write_bytes(mask_bytes)
    manifest_path.write_bytes(manifest_bytes)

    with rasterio.open(mask_path) as mask_dataset:
        fractional_mask = mask_dataset.read(1).astype(np.float32)
        fractional_profile = mask_dataset.profile.copy()
    fractional_mask[0, 0] = 0.5
    fractional_profile["dtype"] = "float32"
    with rasterio.open(mask_path, "w", **fractional_profile) as mask_dataset:
        mask_dataset.write(fractional_mask, 1)
    manifest = json.loads(manifest_bytes)
    manifest["sha256"]["original_nodata_mask"] = sha256_file(mask_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MeshValidationError, match="not binary"):
        verify_artifact_bundle(output)
    mask_path.write_bytes(mask_bytes)
    manifest_path.write_bytes(manifest_bytes)

    source_acquisition_path = result.artifacts["source_acquisition"]
    source_acquisition_bytes = source_acquisition_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    del manifest["artifacts"]["source_acquisition"]
    del manifest["sha256"]["source_acquisition"]
    source_acquisition_path.unlink()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MeshValidationError, match="Artifact binding role inventory"):
        verify_artifact_bundle(output)
    source_acquisition_path.write_bytes(source_acquisition_bytes)
    manifest_path.write_bytes(manifest_bytes)

    record_slice_validation(
        output,
        {"status": "failed", "slicer": {"name": "PrusaSlicer", "version": "fixture"}},
    )
    manifest = json.loads(manifest_path.read_bytes())
    for role in ("slicer_validation", "bambu_studio_validation"):
        path = output / manifest["artifacts"].pop(role)
        del manifest["sha256"][role]
        path.unlink()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MeshValidationError, match="Slicer claims require canonical"):
        verify_artifact_bundle(output)


@pytest.mark.parametrize("output_format", ("stl", "3mf", "glb"))
def test_single_format_bundle_verification_and_slice_recording(
    tmp_path: Path,
    output_format: str,
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / f"{output_format}-source.tif",
        SyntheticTerrain.GAUSSIAN_HILL,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    output = tmp_path / f"{output_format}-build"
    build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=72.0,
            max_grid_cells=1_000,
            output_formats=[output_format],
        )
    )

    before = verify_artifact_bundle(output)
    assert set(before["format_measurements"]) == {output_format}
    record_slice_validation(
        output,
        {"status": "failed", "slicer": {"name": "PrusaSlicer", "version": "fixture"}},
    )
    after = verify_artifact_bundle(output)
    assert set(after["format_measurements"]) == {output_format}


def test_quality_mask_recorded_sha_is_rechecked_after_control_reseal(
    tmp_path: Path,
) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.GAUSSIAN_HILL,
        rows=10,
        columns=12,
        pixel_size_m=20.0,
    )
    with rasterio.open(source) as dataset:
        grid_shape = list(dataset.shape)
        transform = list(tuple(dataset.transform)[:6])
        crs = str(dataset.crs)
    acquisition_path = tmp_path / "source-acquisition.json"
    acquisition = {
        "quality_masks": [
            {
                "availability": "present",
                "role": "edm",
                "output": {
                    "path": str(source),
                    "sha256": sha256_file(source),
                    "grid_shape": grid_shape,
                    "transform": transform,
                    "crs": crs,
                },
            }
        ]
    }
    write_json(acquisition_path, acquisition)
    output = tmp_path / "build"
    build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=64.0,
            max_grid_cells=1_000,
            output_formats=["stl"],
            source_acquisition_manifest=acquisition_path,
        )
    )

    bundled_acquisition_path = output / "source_acquisition.json"
    bundled_acquisition = json.loads(bundled_acquisition_path.read_text(encoding="utf-8"))
    bundled_acquisition["quality_masks"][0]["output"]["sha256"] = "0" * 64
    write_json(bundled_acquisition_path, bundled_acquisition)
    source_acquisition_sha256 = sha256_file(bundled_acquisition_path)

    validation_path = output / "validation.json"
    provenance_path = output / "provenance.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["source_acquisition"] = bundled_acquisition
    validation["artifact_bindings"]["role_sha256"]["source_acquisition"] = source_acquisition_sha256
    provenance["artifact_bindings"]["role_sha256"]["source_acquisition"] = source_acquisition_sha256
    write_json(validation_path, validation)
    write_json(provenance_path, provenance)
    write_validation_html(output / "validation.html", validation)

    manifest_path = output / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"]["source_acquisition"] = source_acquisition_sha256
    for role in ("validation_json", "validation_html", "provenance"):
        manifest["sha256"][role] = sha256_file(output / manifest["artifacts"][role])
    write_json(manifest_path, manifest)

    with pytest.raises(MeshValidationError, match="checksum does not match acquisition evidence"):
        verify_artifact_bundle(output)
