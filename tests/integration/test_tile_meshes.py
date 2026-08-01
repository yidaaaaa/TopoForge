from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import rasterio
from PIL import Image

from topoforge.engine import build_local_terrain
from topoforge.exceptions import ConfigurationError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.models import BuildConfig, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import (
    TileLayoutConfig,
    TileMeshArtifactManifest,
    TileMeshAssemblyManifest,
    TileMeshAssemblyValidation,
    extract_tile_set,
    generate_tile_mesh_set,
    plan_tile_layout,
    verify_tile_mesh_set,
    write_tile_layout,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _source_tile_set(tmp_path: Path) -> tuple[Path, Path]:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SLOPE,
        rows=16,
        columns=20,
        pixel_size_m=20.0,
    )
    bundle = tmp_path / "bundle"
    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=bundle,
            model_width_mm=90.0,
            max_height_mm=30.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=20_000,
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
            maximum_tile_width_mm=50.0,
            maximum_tile_depth_mm=40.0,
            overlap_cells=1,
        )
    )
    layout_path = write_tile_layout(layout, tmp_path / "tile-layout.json")
    tile_set = extract_tile_set(bundle, layout_path, tmp_path / "tile-set").output_dir
    return bundle, tile_set


def test_tile_meshes_preserve_global_frame_formats_and_assembly(tmp_path: Path) -> None:
    bundle, tile_set = _source_tile_set(tmp_path)
    result = generate_tile_mesh_set(tile_set, bundle, tmp_path / "mesh-set")
    evidence = verify_tile_mesh_set(result.output_dir, tile_set, bundle)

    assert evidence["tile_grid_shape"] == (2, 2)
    assert evidence["tile_count"] == 4
    assert evidence["mesh_seam_count"] == 4
    assert evidence["mesh_seam_status"] == "passed"
    assert evidence["maximum_top_seam_gap_mm"] <= 0.001
    assert evidence["required_checks_passed"] is True

    assembly = TileMeshAssemblyManifest.model_validate_json(
        result.assembly_manifest_path.read_text(encoding="utf-8")
    )
    validation = TileMeshAssemblyValidation.model_validate_json(
        result.assembly_validation_path.read_text(encoding="utf-8")
    )
    assert assembly.source_tile_set_seam_report_sha256 == _sha256(tile_set / "seam_report.json")
    assert validation.global_bounds_match is True
    assert validation.footprint_partition_passed is True
    assert validation.volume_match is True
    assert validation.total_top_seam_mismatch_count == 0
    assert validation.mesh_seam_status == "passed"

    with Image.open(result.coverage_image_path) as image:
        assert image.size == validation.coverage_image_size_px
        assert image.getbbox() is not None

    north_east = assembly.tiles[1]
    artifact = TileMeshArtifactManifest.model_validate_json(
        (result.output_dir / north_east.tile_mesh_manifest).read_text(encoding="utf-8")
    )
    assert artifact.validation.required_checks_passed is True
    assert artifact.validation.orientation_consistent is True
    assert artifact.validation.bounds_match is True
    assert artifact.validation.peak_coordinates_match is True
    inspection = inspect_3mf(result.output_dir / north_east.files["model_3mf"])
    assert inspection.strict_warning_count == 0
    assert inspection.bounds_mm[0][0] == pytest.approx(north_east.global_bounds_mm[0])
    assert inspection.bounds_mm[1][1] == pytest.approx(north_east.global_bounds_mm[4])
    assert inspection.metadata["customXMLNS0:tile_id"] == north_east.tile_id


def test_tile_mesh_set_is_byte_deterministic_and_detects_tamper(tmp_path: Path) -> None:
    bundle, tile_set = _source_tile_set(tmp_path)
    first = generate_tile_mesh_set(tile_set, bundle, tmp_path / "mesh-first")
    second = generate_tile_mesh_set(tile_set, bundle, tmp_path / "mesh-second")

    first_files = sorted(
        path.relative_to(first.output_dir) for path in first.output_dir.rglob("*") if path.is_file()
    )
    second_files = sorted(
        path.relative_to(second.output_dir)
        for path in second.output_dir.rglob("*")
        if path.is_file()
    )
    assert first_files == second_files
    for relative in first_files:
        assert (first.output_dir / relative).read_bytes() == (
            second.output_dir / relative
        ).read_bytes(), relative

    with pytest.raises(ConfigurationError, match="already exists"):
        generate_tile_mesh_set(tile_set, bundle, first.output_dir)

    tampered = first.output_dir / "tiles/tile-r0000-c0000/model.global.stl"
    tampered.write_bytes(tampered.read_bytes() + b"tampered")
    with pytest.raises(ConfigurationError, match="checksum mismatch"):
        verify_tile_mesh_set(first.output_dir, tile_set, bundle)
