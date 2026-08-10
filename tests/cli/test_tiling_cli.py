import json
from pathlib import Path

from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.engine import build_local_terrain
from topoforge.models import BuildConfig
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import (
    read_tile_layout,
    verify_print_tile_set,
    verify_tile_mesh_set,
    verify_tile_set,
)

runner = CliRunner()


def test_tile_plan_and_extract_cli_publish_verified_artifacts(tmp_path: Path) -> None:
    source = create_synthetic_geotiff(
        tmp_path / "source.tif",
        SyntheticTerrain.SADDLE,
        rows=24,
        columns=30,
        pixel_size_m=20.0,
    )
    bundle = tmp_path / "bundle"
    build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=bundle,
            model_width_mm=90.0,
            max_height_mm=30.0,
            max_grid_cells=20_000,
        )
    )
    layout_path = tmp_path / "tile-layout.json"

    result = runner.invoke(
        app,
        [
            "tile-plan",
            str(bundle),
            "--max-tile-size-mm",
            "60",
            "80",
            "--overlap-cells",
            "1",
            "--output",
            str(layout_path),
        ],
    )

    assert result.exit_code == 0, result.output
    layout = read_tile_layout(layout_path)
    assert layout.tile_grid_shape == (1, 2)
    assert layout.tile_count == 2
    planned = json.loads(result.output)
    assert planned["status"] == "planned"
    assert Path(planned["output"]) == layout_path.resolve()

    tile_output = tmp_path / "tile-set"
    extraction = runner.invoke(
        app,
        [
            "tile-extract",
            str(bundle),
            "--layout",
            str(layout_path),
            "--output",
            str(tile_output),
        ],
    )

    assert extraction.exit_code == 0, extraction.output
    evidence = verify_tile_set(tile_output, bundle)
    assert evidence["tile_count"] == 2
    assert evidence["required_checks_passed"] is True
    assert evidence["seam_count"] == 1
    assert evidence["terrain_seam_status"] == "passed"
    extracted = json.loads(extraction.output)
    assert extracted["status"] == "extracted"
    assert (
        Path(extracted["assembly_manifest"]) == (tile_output / "assembly_manifest.json").resolve()
    )
    assert Path(extracted["seam_report"]) == (tile_output / "seam_report.json").resolve()

    mesh_output = tmp_path / "tile-mesh-set"
    meshed = runner.invoke(
        app,
        [
            "tile-mesh",
            str(tile_output),
            "--source-bundle",
            str(bundle),
            "--output",
            str(mesh_output),
        ],
    )

    assert meshed.exit_code == 0, meshed.output
    mesh_evidence = verify_tile_mesh_set(mesh_output, tile_output, bundle)
    assert mesh_evidence["tile_count"] == 2
    assert mesh_evidence["mesh_seam_count"] == 1
    assert mesh_evidence["mesh_seam_status"] == "passed"
    assert mesh_evidence["required_checks_passed"] is True
    meshed_payload = json.loads(meshed.output)
    assert meshed_payload["status"] == "meshed"
    assert (
        Path(meshed_payload["assembly_manifest"])
        == (mesh_output / "tile-mesh-assembly-manifest.json").resolve()
    )
    assert Path(meshed_payload["coverage_image"]) == (mesh_output / "tile-coverage.png").resolve()

    print_output = tmp_path / "print-tile-set"
    connected = runner.invoke(
        app,
        [
            "tile-connect",
            str(mesh_output),
            "--tile-set",
            str(tile_output),
            "--source-bundle",
            str(bundle),
            "--output",
            str(print_output),
        ],
    )

    assert connected.exit_code == 0, connected.output
    print_evidence = verify_print_tile_set(print_output, mesh_output, tile_output, bundle)
    assert print_evidence["tile_count"] == 2
    assert print_evidence["seam_count"] == 1
    assert print_evidence["connector_fit_status"] == "passed"
    assert print_evidence["collision_status"] == "passed"
    assert print_evidence["required_checks_passed"] is True
    connected_payload = json.loads(connected.output)
    assert connected_payload["status"] == "connected"
    assert (
        Path(connected_payload["connector_plan"])
        == (print_output / "connector-plan.json").resolve()
    )
    assert (
        Path(connected_payload["connector_map"]) == (print_output / "connector-map.png").resolve()
    )
