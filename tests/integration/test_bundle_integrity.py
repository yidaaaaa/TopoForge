import json
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.shutil import copy as copy_raster

from topoforge.engine import build_local_terrain, verify_artifact_bundle
from topoforge.exceptions import MeshValidationError
from topoforge.models import BuildConfig, SamplingMode
from topoforge.provenance import write_json, write_validation_html
from topoforge.util import sha256_file


def _write_elevation_fixture(path: Path, elevations_m: np.ndarray) -> Path:
    transform = Affine.translation(500_000.0, 3_300_000.0) * Affine.scale(12.0, -12.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=elevations_m.shape[0],
        width=elevations_m.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:32648",
        transform=transform,
        nodata=-9999.0,
    ) as dataset:
        dataset.write(np.asarray(elevations_m, dtype=np.float32), 1)
    return path


@pytest.fixture(scope="module")
def valid_bundles(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("bundle-integrity")
    elevations_a = np.arange(18 * 24, dtype=np.float32).reshape(18, 24) + 100.0
    elevations_b = elevations_a.copy()
    elevations_b[5, 5], elevations_b[5, 6] = (
        elevations_a[5, 6],
        elevations_a[5, 5],
    )
    source_a = _write_elevation_fixture(
        root / "source-a.tif",
        elevations_a,
    )
    source_b = _write_elevation_fixture(
        root / "source-b.tif",
        elevations_b,
    )
    bundle_a = build_local_terrain(
        BuildConfig(
            dem_path=source_a,
            output_dir=root / "bundle-a",
            model_width_mm=64.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=5_000,
            dataset_name="Equal-stat integrity fixture",
        )
    ).output_dir
    bundle_b = build_local_terrain(
        BuildConfig(
            dem_path=source_b,
            output_dir=root / "bundle-b",
            model_width_mm=64.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=5_000,
            dataset_name="Equal-stat integrity fixture",
        )
    ).output_dir
    verify_artifact_bundle(bundle_a)
    verify_artifact_bundle(bundle_b)
    return bundle_a, bundle_b


def _attack_copy(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def _reseal_manifest(bundle: Path, roles: tuple[str, ...]) -> None:
    manifest_path = bundle / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for role in roles:
        artifact = bundle / manifest["artifacts"][role]
        manifest["sha256"][role] = sha256_file(artifact)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _reseal_bound_artifact(bundle: Path, role: str) -> None:
    manifest_path = bundle / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_sha256 = sha256_file(bundle / manifest["artifacts"][role])
    validation_path = bundle / "validation.json"
    provenance_path = bundle / "provenance.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    validation["artifact_bindings"]["role_sha256"][role] = artifact_sha256
    provenance["artifact_bindings"]["role_sha256"][role] = artifact_sha256
    write_json(validation_path, validation)
    write_json(provenance_path, provenance)
    write_validation_html(bundle / "validation.html", validation)
    manifest["sha256"][role] = artifact_sha256
    for control_role in ("validation_json", "validation_html", "provenance"):
        manifest["sha256"][control_role] = sha256_file(bundle / manifest["artifacts"][control_role])
    write_json(manifest_path, manifest)


def _add_external_glb_buffer_uri(path: Path) -> None:
    payload = path.read_bytes()
    json_length, json_type = struct.unpack_from("<II", payload, 12)
    assert json_type == 0x4E4F534A
    json_end = 20 + json_length
    header = json.loads(payload[20:json_end].rstrip(b" \t\r\n\x00"))
    header["buffers"][0]["uri"] = "../outside.bin"
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((-len(encoded)) % 4)
    remaining_chunks = payload[json_end:]
    total_length = 12 + 8 + len(encoded) + len(remaining_chunks)
    path.write_bytes(
        struct.pack("<III", 0x46546C67, 2, total_length)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
        + remaining_chunks
    )


def test_strict_bundle_reports_reopened_numeric_evidence(
    valid_bundles: tuple[Path, Path],
) -> None:
    bundle_a, _ = valid_bundles
    evidence = verify_artifact_bundle(bundle_a)

    assert evidence["raster_measurements"]["shape"] == (18, 24)
    assert evidence["raster_measurements"]["original_nodata_mask_fraction"] == 0.0
    assert set(evidence["format_measurements"]) == {"stl", "3mf", "glb"}
    assert {
        evidence["format_measurements"][name]["triangle_count"] for name in ("stl", "3mf", "glb")
    } == {4 * 18 * 24 - 4}
    assert {
        evidence["format_measurements"][name]["vertex_count"] for name in ("stl", "3mf", "glb")
    } == {2 * 18 * 24}


def test_equal_stat_fixtures_retain_the_scalar_collision_precondition(
    valid_bundles: tuple[Path, Path],
) -> None:
    bundle_a, bundle_b = valid_bundles
    validation_a = json.loads((bundle_a / "validation.json").read_text(encoding="utf-8"))
    validation_b = json.loads((bundle_b / "validation.json").read_text(encoding="utf-8"))
    assert validation_a["dimensions_mm"] == pytest.approx(validation_b["dimensions_mm"])
    assert validation_a["volume_mm3"] == pytest.approx(validation_b["volume_mm3"], abs=1e-6)
    assert validation_a["processed_peak_coordinate"] == validation_b["processed_peak_coordinate"]


@pytest.mark.parametrize(
    ("role", "filename"),
    (
        ("model_stl", "model.stl"),
        ("model_3mf", "model.3mf"),
        ("preview_glb", "preview.glb"),
    ),
)
def test_rejects_equal_stat_foreign_format_after_control_evidence_reseal(
    tmp_path: Path,
    valid_bundles: tuple[Path, Path],
    role: str,
    filename: str,
) -> None:
    bundle_a, bundle_b = valid_bundles
    attacked = _attack_copy(bundle_a, tmp_path / "attacked")
    shutil.copyfile(bundle_b / filename, attacked / filename)
    _reseal_bound_artifact(attacked, role)

    with pytest.raises(MeshValidationError, match="processed DEM terrain geometry") as raised:
        verify_artifact_bundle(attacked)
    assert "rebuild" in str(raised.value).lower()


def test_rejects_equal_stat_foreign_dem_mask_pair_after_control_evidence_reseal(
    tmp_path: Path,
    valid_bundles: tuple[Path, Path],
) -> None:
    bundle_a, bundle_b = valid_bundles
    attacked = _attack_copy(bundle_a, tmp_path / "attacked")
    shutil.copyfile(bundle_b / "processed_dem.tif", attacked / "processed_dem.tif")
    shutil.copyfile(
        bundle_b / "original_nodata_mask.tif",
        attacked / "original_nodata_mask.tif",
    )
    _reseal_bound_artifact(attacked, "processed_dem")
    _reseal_bound_artifact(attacked, "original_nodata_mask")

    with pytest.raises(MeshValidationError, match="processed_dem_semantic_sha256"):
        verify_artifact_bundle(attacked)


def test_rejects_vrt_masquerading_as_processed_geotiff(
    tmp_path: Path,
    valid_bundles: tuple[Path, Path],
) -> None:
    bundle_a, _ = valid_bundles
    attacked = _attack_copy(bundle_a, tmp_path / "vrt-attacked")
    external_dem = tmp_path / "external-dem.tif"
    shutil.copyfile(attacked / "processed_dem.tif", external_dem)
    (attacked / "processed_dem.tif").unlink()
    copy_raster(external_dem, attacked / "processed_dem.tif", driver="VRT")
    _reseal_manifest(attacked, ("processed_dem",))

    with pytest.raises(MeshValidationError, match="self-contained GeoTIFF"):
        verify_artifact_bundle(attacked)


def test_rejects_external_glb_uri_even_after_control_evidence_is_resealed(
    tmp_path: Path,
    valid_bundles: tuple[Path, Path],
) -> None:
    bundle_a, _ = valid_bundles
    attacked = _attack_copy(bundle_a, tmp_path / "glb-attacked")
    _add_external_glb_buffer_uri(attacked / "preview.glb")
    _reseal_bound_artifact(attacked, "preview_glb")

    with pytest.raises(MeshValidationError, match="external buffer URI"):
        verify_artifact_bundle(attacked)
