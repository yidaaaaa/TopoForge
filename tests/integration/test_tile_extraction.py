from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.windows import Window

from topoforge.engine import build_local_terrain
from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import (
    AssemblyManifest,
    TileArtifactManifest,
    TileCoverageMap,
    TileLayoutConfig,
    extract_tile_set,
    plan_tile_layout,
    read_tile_layout,
    verify_tile_set,
    write_tile_layout,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _bundle_and_layout(tmp_path: Path) -> tuple[Path, Path]:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.NODATA_HOLE,
        rows=40,
        columns=50,
        pixel_size_m=20.0,
    )
    bundle = tmp_path / "bundle"
    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=bundle,
            model_width_mm=120.0,
            max_height_mm=35.0,
            max_grid_cells=50_000,
        )
    )
    with rasterio.open(result.artifacts["processed_dem"]) as dataset:
        shape = dataset.shape
    dimensions = result.validation["dimensions_mm"]
    layout = plan_tile_layout(
        TileLayoutConfig(
            source_grid_shape=shape,
            model_width_mm=float(dimensions[0]),
            model_depth_mm=float(dimensions[1]),
            maximum_tile_width_mm=70.0,
            maximum_tile_depth_mm=70.0,
            overlap_cells=1,
        )
    )
    layout_path = write_tile_layout(layout, tmp_path / "tile-layout.json")
    return bundle, layout_path


def test_extract_tile_set_preserves_windows_masks_hashes_and_assembly(tmp_path: Path) -> None:
    bundle, layout_path = _bundle_and_layout(tmp_path)
    layout = read_tile_layout(layout_path)
    result = extract_tile_set(bundle, layout_path, tmp_path / "tiles")

    assembly = AssemblyManifest.model_validate_json(
        result.assembly_manifest_path.read_text(encoding="utf-8")
    )
    coverage = TileCoverageMap.model_validate_json(
        result.coverage_map_path.read_text(encoding="utf-8")
    )
    source_manifest = json.loads((bundle / "build_manifest.json").read_text())
    assert assembly.layout_id == layout.layout_id
    assert assembly.tile_grid_shape == (2, 2)
    assert assembly.tile_count == 4
    assert coverage.rows == [
        ["tile-r0000-c0000", "tile-r0000-c0001"],
        ["tile-r0001-c0000", "tile-r0001-c0001"],
    ]
    assert assembly.raw_source_dem_sha256 == source_manifest["source_sha256"]
    assert assembly.processed_dem_sha256 == source_manifest["sha256"]["processed_dem"]
    assert assembly.layout_sha256 == _sha256(result.layout_path)
    assert assembly.coverage_map_sha256 == _sha256(result.coverage_map_path)

    saw_original_nodata = False
    with (
        rasterio.open(bundle / "processed_dem.tif") as source_dem,
        rasterio.open(bundle / "original_nodata_mask.tif") as source_mask,
    ):
        for tile, record, manifest_path in zip(
            layout.tiles, assembly.tiles, result.tile_manifest_paths, strict=True
        ):
            artifact = TileArtifactManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            assert artifact.validation.required_checks_passed is True
            assert artifact.tile_id == tile.tile_id == record.tile_id
            for role, relative_path in record.files.items():
                path = result.output_dir / relative_path
                assert _sha256(path) == record.sha256[role]
            assert _sha256(result.output_dir / record.tile_manifest) == (
                record.tile_manifest_sha256
            )

            window = Window(
                col_off=tile.sampling_window.column_start,
                row_off=tile.sampling_window.row_start,
                width=tile.sampling_window.column_stop - tile.sampling_window.column_start,
                height=tile.sampling_window.row_stop - tile.sampling_window.row_start,
            )
            expected_dem = source_dem.read(1, window=window)
            expected_mask = source_mask.read(1, window=window)
            with rasterio.open(result.output_dir / record.files["processed_dem"]) as tile_dem:
                np.testing.assert_array_equal(tile_dem.read(1), expected_dem)
                assert tile_dem.crs == source_dem.crs
            with rasterio.open(
                result.output_dir / record.files["original_nodata_mask"]
            ) as tile_mask:
                np.testing.assert_array_equal(tile_mask.read(1), expected_mask)
                assert tile_mask.crs == source_mask.crs
            saw_original_nodata = saw_original_nodata or bool(np.count_nonzero(expected_mask))
    assert saw_original_nodata is True


def test_tile_extraction_is_byte_deterministic_and_refuses_overwrite(tmp_path: Path) -> None:
    bundle, layout_path = _bundle_and_layout(tmp_path)
    first = extract_tile_set(bundle, layout_path, tmp_path / "first")
    second = extract_tile_set(bundle, layout_path, tmp_path / "second")

    first_files = sorted(path.relative_to(first.output_dir) for path in first.output_dir.rglob("*"))
    second_files = sorted(
        path.relative_to(second.output_dir) for path in second.output_dir.rglob("*")
    )
    assert first_files == second_files
    for relative in first_files:
        first_path = first.output_dir / relative
        second_path = second.output_dir / relative
        if first_path.is_file():
            assert first_path.read_bytes() == second_path.read_bytes(), relative
    with pytest.raises(ConfigurationError, match="already exists"):
        extract_tile_set(bundle, layout_path, first.output_dir)


def test_tile_extraction_rejects_layout_grid_mismatch_without_partial_output(
    tmp_path: Path,
) -> None:
    bundle, valid_layout_path = _bundle_and_layout(tmp_path)
    valid = read_tile_layout(valid_layout_path)
    mismatch = plan_tile_layout(
        TileLayoutConfig(
            source_grid_shape=(valid.source_grid_shape[0] - 1, valid.source_grid_shape[1]),
            model_width_mm=valid.model_size_mm[0],
            model_depth_mm=valid.model_size_mm[1],
            maximum_tile_width_mm=valid.maximum_tile_size_mm[0],
            maximum_tile_depth_mm=valid.maximum_tile_size_mm[1],
            overlap_cells=valid.overlap_cells,
        )
    )
    mismatch_path = write_tile_layout(mismatch, tmp_path / "mismatch-layout.json")
    output = tmp_path / "mismatch-output"

    with pytest.raises(ConfigurationError, match="does not match bundle raster"):
        extract_tile_set(bundle, mismatch_path, output)
    assert not output.exists()


def test_tile_extraction_rejects_any_source_bundle_checksum_mismatch(
    tmp_path: Path,
) -> None:
    bundle, layout_path = _bundle_and_layout(tmp_path)
    preview = bundle / "preview.png"
    preview.write_bytes(preview.read_bytes() + b"tampered")
    output = tmp_path / "checksum-failure"

    with pytest.raises(ConfigurationError, match="preview_png"):
        extract_tile_set(bundle, layout_path, output)
    assert not output.exists()


def test_verify_tile_set_detects_tampered_tile_artifact(tmp_path: Path) -> None:
    bundle, layout_path = _bundle_and_layout(tmp_path)
    result = extract_tile_set(bundle, layout_path, tmp_path / "tiles")
    tile_dem = result.output_dir / "tiles/tile-r0000-c0000/processed_dem.tif"
    tile_dem.write_bytes(tile_dem.read_bytes() + b"tampered")

    with pytest.raises(ConfigurationError, match="checksum mismatch"):
        verify_tile_set(result.output_dir, bundle)


def test_tile_extraction_rejects_layout_model_size_mismatch_without_output(
    tmp_path: Path,
) -> None:
    bundle, valid_layout_path = _bundle_and_layout(tmp_path)
    valid = read_tile_layout(valid_layout_path)
    mismatch = plan_tile_layout(
        TileLayoutConfig(
            source_grid_shape=valid.source_grid_shape,
            model_width_mm=valid.model_size_mm[0] + 1.0,
            model_depth_mm=valid.model_size_mm[1],
            maximum_tile_width_mm=valid.maximum_tile_size_mm[0],
            maximum_tile_depth_mm=valid.maximum_tile_size_mm[1],
            overlap_cells=valid.overlap_cells,
        )
    )
    mismatch_path = write_tile_layout(mismatch, tmp_path / "size-mismatch-layout.json")
    output = tmp_path / "size-mismatch-output"

    with pytest.raises(ConfigurationError, match="model_size_mm"):
        extract_tile_set(bundle, mismatch_path, output)
    assert not output.exists()
