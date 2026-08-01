from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import rasterio

from topoforge.engine import build_local_terrain
from topoforge.exceptions import ConfigurationError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.models import BuildConfig, PrinterProfile, SamplingMode
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import (
    ConnectorPlan,
    PrintTileArtifactManifest,
    PrintTileAssemblyManifest,
    PrintTileAssemblyValidation,
    TileLayoutConfig,
    derive_connector_policy,
    extract_tile_set,
    generate_print_tile_set,
    generate_tile_mesh_set,
    plan_tile_layout,
    verify_print_tile_set,
    write_tile_layout,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _source_mesh_set(tmp_path: Path) -> tuple[Path, Path, Path]:
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
    mesh_set = generate_tile_mesh_set(tile_set, bundle, tmp_path / "mesh-set").output_dir
    return bundle, tile_set, mesh_set


def test_connector_tiles_preserve_terrain_and_publish_reversible_print_frames(
    tmp_path: Path,
) -> None:
    bundle, tile_set, mesh_set = _source_mesh_set(tmp_path)
    source_manifest = mesh_set / "tile-mesh-assembly-manifest.json"
    source_hash = _sha256(source_manifest)

    result = generate_print_tile_set(
        mesh_set,
        tile_set,
        bundle,
        tmp_path / "print-set",
    )
    evidence = verify_print_tile_set(result.output_dir, mesh_set, tile_set, bundle)

    assert _sha256(source_manifest) == source_hash
    assert evidence["tile_grid_shape"] == (2, 2)
    assert evidence["tile_count"] == 4
    assert evidence["seam_count"] == 4
    assert evidence["connector_count"] == 4
    assert evidence["connector_fit_status"] == "passed"
    assert evidence["collision_status"] == "passed"
    assert evidence["maximum_top_surface_deviation_mm"] == pytest.approx(0.0)
    assert evidence["minimum_lateral_clearance_per_side_mm"] == pytest.approx(0.1)
    assert evidence["minimum_vertical_clearance_mm"] == pytest.approx(0.2)
    assert evidence["required_checks_passed"] is True

    plan = ConnectorPlan.model_validate_json(result.connector_plan_path.read_text(encoding="utf-8"))
    assert plan.policy.tolerance_definition.startswith(
        "connector_tolerance_mm is total lateral clearance"
    )
    assert plan.policy.fit_classification == "verified-clearance-fit"
    assert plan.policy.remaining_roof_thickness_mm >= plan.policy.minimum_wall_thickness_mm
    assert all(item.male_tile_id < item.female_tile_id for item in plan.connectors)
    assert {item.insertion_axis for item in plan.connectors} == {"+X East", "-Y South"}

    assembly = PrintTileAssemblyManifest.model_validate_json(
        result.assembly_manifest_path.read_text(encoding="utf-8")
    )
    validation = PrintTileAssemblyValidation.model_validate_json(
        result.assembly_validation_path.read_text(encoding="utf-8")
    )
    assert validation.all_top_surfaces_preserved is True
    assert validation.all_bed_contacts_flat is True
    assert validation.all_thin_wall_checks_passed is True
    assert validation.all_build_volume_checks_passed is True
    assert validation.global_bounds_match is True
    assert validation.maximum_collision_volume_mm3 <= max(
        item.volume_tolerance_mm3 for item in validation.connectors
    )

    north_west = assembly.tiles[0]
    south_east = assembly.tiles[-1]
    assert len(north_west.male_connector_ids) == 2
    assert north_west.female_connector_ids == ()
    assert south_east.male_connector_ids == ()
    assert len(south_east.female_connector_ids) == 2

    artifact = PrintTileArtifactManifest.model_validate_json(
        (result.output_dir / south_east.tile_manifest).read_text(encoding="utf-8")
    )
    assert artifact.validation.top_surface_preserved is True
    assert artifact.validation.bed_contact_flat is True
    assert artifact.validation.bottom_recesses_expected is True
    assert artifact.validation.global_geometry.watertight is True
    assert artifact.validation.local_geometry.watertight is True
    translation = artifact.validation.global_to_print_local_translation_mm
    inverse = artifact.validation.print_local_to_global_translation_mm
    assert tuple(a + b for a, b in zip(translation, inverse, strict=True)) == pytest.approx(
        (0.0, 0.0, 0.0)
    )
    local_3mf = inspect_3mf(result.output_dir / south_east.files["print_local_3mf"])
    assert local_3mf.strict_warning_count == 0
    assert local_3mf.bounds_mm[0] == pytest.approx((0.0, 0.0, 0.0))
    assert local_3mf.metadata["customXMLNS0:coordinate_frame"] == "print-local"
    assert local_3mf.metadata["customXMLNS0:east_axis"] == "+X = East"
    assert local_3mf.metadata["customXMLNS0:north_axis"] == "+Y = North"


def test_connector_tile_set_is_byte_deterministic_and_detects_tamper(tmp_path: Path) -> None:
    bundle, tile_set, mesh_set = _source_mesh_set(tmp_path)
    first = generate_print_tile_set(mesh_set, tile_set, bundle, tmp_path / "print-first")
    second = generate_print_tile_set(mesh_set, tile_set, bundle, tmp_path / "print-second")

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
        generate_print_tile_set(mesh_set, tile_set, bundle, first.output_dir)

    tampered = first.output_dir / "tiles/tile-r0000-c0000/model.print-local.stl"
    tampered.write_bytes(tampered.read_bytes() + b"tampered")
    with pytest.raises(ConfigurationError, match="checksum mismatch"):
        verify_print_tile_set(first.output_dir, mesh_set, tile_set, bundle)


def test_connector_policy_rejects_unprintable_base_and_clearance() -> None:
    with pytest.raises(ConfigurationError, match="base thickness cannot contain"):
        derive_connector_policy(PrinterProfile(), base_thickness_mm=1.5)
    with pytest.raises(ConfigurationError, match="outside the verified clearance range"):
        derive_connector_policy(
            PrinterProfile(connector_tolerance_mm=0.01),
            base_thickness_mm=3.0,
        )
