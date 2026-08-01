from pathlib import Path

from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.engine import build_local_terrain
from topoforge.models import BuildConfig
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import read_tile_layout, verify_tile_mesh_set, verify_tile_set

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
    assert '"status": "planned"' in result.output
    assert str(layout_path.resolve()) in result.output

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
    assert '"status": "extracted"' in extraction.output
    assert str((tile_output / "assembly_manifest.json").resolve()) in extraction.output
    assert str((tile_output / "seam_report.json").resolve()) in extraction.output

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
    assert '"status": "meshed"' in meshed.output
    assert str((mesh_output / "tile-mesh-assembly-manifest.json").resolve()) in meshed.output
    assert str((mesh_output / "tile-coverage.png").resolve()) in meshed.output
