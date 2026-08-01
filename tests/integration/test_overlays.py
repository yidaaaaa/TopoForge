from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from typer.testing import CliRunner

from topoforge.cli.app import app
from topoforge.engine import build_local_terrain
from topoforge.exceptions import ConfigurationError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.models import BuildConfig, SamplingMode, VerticalScaleMode
from topoforge.overlays import (
    OverlayConfig,
    OverlayFormat,
    OverlayKind,
    OverlaySourceConfig,
    OverlayStyle,
    generate_overlay_bundle,
    verify_overlay_bundle,
    write_overlay_config,
)
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.validation.slicers import (
    SlicerAvailability,
    SliceResult,
    SlicerInfo,
    SliceStatus,
    parse_gcode_metrics,
)
from topoforge.workflow import LocalWorkflowConfig, WorkflowStage, run_local_workflow

runner = CliRunner()


def _build_bundle(tmp_path: Path, terrain: SyntheticTerrain) -> Path:
    source = create_synthetic_geotiff(
        tmp_path / f"{terrain.value}.tif",
        terrain,
        rows=15,
        columns=20,
        pixel_size_m=20.0,
    )
    output = tmp_path / f"{terrain.value}-build"
    build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=60.0,
            max_height_mm=25.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
            vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
            dataset_name="synthetic overlay fixture",
            data_license="Apache-2.0 synthetic fixture",
            attribution="TopoForge tests",
            nodata_max_fraction=0.1,
        )
    )
    return output


def _build_projected_bundle(tmp_path: Path, *, name: str, crs: str) -> Path:
    source = tmp_path / f"{name}.tif"
    elevations = np.linspace(100.0, 300.0, 12 * 16, dtype=np.float32).reshape(12, 16)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=16,
        height=12,
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_origin(-20_000.0, 15_000.0, 2_500.0, 2_500.0),
        nodata=np.nan,
    ) as dataset:
        dataset.write(elevations, 1)
    output = tmp_path / f"{name}-build"
    build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=output,
            model_width_mm=60.0,
            max_height_mm=25.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
            dataset_name=f"{name} projected fixture",
        )
    )
    return output


def _write_line_geojson(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "diagonal",
                        "properties": {"name": "Diagonal"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[500020.0, 3299980.0], [500360.0, 3299740.0]],
                        },
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_label_geojson(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "label",
                        "properties": {"name": "Peak"},
                        "geometry": {
                            "type": "Point",
                            "coordinates": [500200.0, 3299860.0],
                        },
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_gpx(path: Path) -> Path:
    to_wgs84 = Transformer.from_crs("EPSG:32648", "EPSG:4326", always_xy=True)
    first = to_wgs84.transform(500030.0, 3299760.0)
    second = to_wgs84.transform(500350.0, 3299960.0)
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="TopoForge tests" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        "  <trk><name>Route</name><trkseg>\n"
        f'    <trkpt lat="{first[1]:.12f}" lon="{first[0]:.12f}"><ele>100</ele></trkpt>\n'
        f'    <trkpt lat="{second[1]:.12f}" lon="{second[0]:.12f}"><ele>120</ele></trkpt>\n'
        "  </trkseg></trk>\n"
        "</gpx>\n",
        encoding="utf-8",
    )
    return path


def _source(
    source_id: str,
    kind: OverlayKind,
    path: Path,
    *,
    color: str,
) -> OverlaySourceConfig:
    return OverlaySourceConfig(
        source_id=source_id,
        kind=kind,
        format=OverlayFormat.GEOJSON,
        path=path,
        source_crs="EPSG:32648",
        dataset_name=f"{source_id} fixture",
        license="CC0-1.0",
        attribution="TopoForge tests",
        style=OverlayStyle(color=color, line_width_mm=0.8),
    )


def test_all_local_overlay_kinds_are_strict_and_deterministic(tmp_path: Path) -> None:
    build = _build_bundle(tmp_path, SyntheticTerrain.SADDLE)
    lines = _write_line_geojson(tmp_path / "lines.geojson")
    labels = _write_label_geojson(tmp_path / "labels.geojson")
    gpx = _write_gpx(tmp_path / "route.gpx")
    config = OverlayConfig(
        sources=(
            OverlaySourceConfig(
                source_id="route",
                kind=OverlayKind.GPX,
                format=OverlayFormat.GPX,
                path=gpx,
                dataset_name="GPX fixture",
                license="CC0-1.0",
                attribution="TopoForge tests",
                style=OverlayStyle(color="#d1495b", line_width_mm=0.8),
            ),
            _source("roads", OverlayKind.ROAD, lines, color="#e9c46a"),
            _source("rivers", OverlayKind.RIVER, lines, color="#277da1"),
            _source("coast", OverlayKind.COAST, lines, color="#0096c7"),
            OverlaySourceConfig(
                source_id="labels",
                kind=OverlayKind.LABEL,
                format=OverlayFormat.GEOJSON,
                path=labels,
                source_crs="EPSG:32648",
                dataset_name="label fixture",
                license="CC0-1.0",
                attribution="TopoForge tests",
                style=OverlayStyle(color="#111111", label_font_height_mm=4.0),
            ),
            OverlaySourceConfig(
                source_id="contours",
                kind=OverlayKind.CONTOUR,
                format=OverlayFormat.GENERATED_CONTOURS,
                dataset_name="derived synthetic contours",
                license="derived from Apache-2.0 fixture",
                attribution="TopoForge threshold-cell-boundary contours",
                contour_interval_m=60.0,
                style=OverlayStyle(color="#7f5539", line_width_mm=0.6),
            ),
        ),
        max_triangles=300_000,
        preview_width_px=640,
    )

    first = generate_overlay_bundle(build, config, tmp_path / "overlays-first")
    second = generate_overlay_bundle(build, config, tmp_path / "overlays-second")
    first_verification = verify_overlay_bundle(first.output_dir, build)
    second_verification = verify_overlay_bundle(second.output_dir, build)

    assert first_verification == second_verification
    assert first.validation.required_checks_passed is True
    assert first.validation.format_reopen_checks_passed is True
    assert first.validation.terrain_artifacts_unchanged is True
    assert first.validation.total_feature_count >= 6
    inspection = inspect_3mf(first.model_3mf_path)
    assert inspection.object_count == 7
    assert inspection.build_item_count == 1
    assert inspection.components_object_count == 1
    assert inspection.component_count == 7
    assert inspection.base_material_group_count == 1
    assert inspection.material_assigned_object_count == 7
    assert inspection.strict_warning_count == 0
    assert first.validation.combined_3mf_build_item_count == 1
    assert first.validation.combined_3mf_components_object_count == 1
    assert first.validation.combined_3mf_component_count == 7
    assert first.validation.combined_3mf_base_material_group_count == 1
    assert first.validation.combined_3mf_material_assigned_object_count == 7
    plan = json.loads((first.output_dir / "overlay-plan.geojson").read_text(encoding="utf-8"))
    road = next(
        feature for feature in plan["features"] if feature["properties"]["source_id"] == "roads"
    )
    first_point, second_point = road["geometry"]["coordinates"]
    assert first_point[0] < second_point[0]
    assert first_point[1] > second_point[1]
    first_files = {
        path.relative_to(first.output_dir).as_posix(): path.read_bytes()
        for path in first.output_dir.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second.output_dir).as_posix(): path.read_bytes()
        for path in second.output_dir.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    tampered_layer = second.output_dir / "layers" / "roads.stl"
    tampered_layer.write_bytes(tampered_layer.read_bytes() + b"tamper")
    with pytest.raises(ConfigurationError, match="checksum mismatch"):
        verify_overlay_bundle(second.output_dir, build)


def test_original_nodata_intersection_requires_explicit_opt_in(tmp_path: Path) -> None:
    build = _build_bundle(tmp_path, SyntheticTerrain.NODATA_HOLE)
    lines = _write_line_geojson(tmp_path / "nodata-line.geojson")
    source = _source("road", OverlayKind.ROAD, lines, color="#e9c46a")
    with pytest.raises(ConfigurationError, match="original NoData"):
        generate_overlay_bundle(
            build,
            OverlayConfig(sources=(source,)),
            tmp_path / "nodata-rejected",
        )

    allowed = generate_overlay_bundle(
        build,
        OverlayConfig(sources=(source,), allow_original_nodata=True),
        tmp_path / "nodata-allowed",
    )
    assert allowed.validation.required_checks_passed is True
    assert allowed.validation.layers[0].original_nodata_overlap_mm2 > 0


def test_overlay_stage_is_content_addressed_and_reused(tmp_path: Path) -> None:
    source_dem = create_synthetic_geotiff(
        tmp_path / "workflow-source.tif",
        SyntheticTerrain.SLOPE,
        rows=12,
        columns=16,
        pixel_size_m=20.0,
    )
    lines = _write_line_geojson(tmp_path / "workflow-road.geojson")
    overlay = OverlayConfig(
        sources=(_source("road", OverlayKind.ROAD, lines, color="#e9c46a"),),
        preview_width_px=480,
    )
    workspace = tmp_path / "overlay-workflow"
    workflow = LocalWorkflowConfig(
        workspace_dir=workspace,
        build=BuildConfig(
            dem_path=source_dem,
            output_dir=workspace,
            model_width_mm=60.0,
            max_height_mm=25.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            max_grid_cells=10_000,
        ),
        overlay=overlay,
        maximum_tile_width_mm=60.0,
        maximum_tile_depth_mm=60.0,
        slicing_enabled=False,
    )

    first = run_local_workflow(workflow)
    assert WorkflowStage.OVERLAY in first.completed_stages
    overlay_output = first.stage_outputs[WorkflowStage.OVERLAY]
    assert (
        verify_overlay_bundle(overlay_output, first.stage_outputs[WorkflowStage.BUILD])[
            "required_checks_passed"
        ]
        is True
    )

    repeated = run_local_workflow(workflow)
    assert repeated.completed_stages == ()
    assert WorkflowStage.OVERLAY in repeated.reused_stages
    assert repeated.stage_outputs[WorkflowStage.OVERLAY] == overlay_output


def test_overlay_cli_and_complete_outside_rejection(tmp_path: Path) -> None:
    build = _build_bundle(tmp_path, SyntheticTerrain.SLOPE)
    lines = _write_line_geojson(tmp_path / "cli-road.geojson")
    config = OverlayConfig(
        sources=(_source("road", OverlayKind.ROAD, lines, color="#e9c46a"),),
        preview_width_px=480,
    )
    config_path = write_overlay_config(config, tmp_path / "overlay.yaml")
    output = tmp_path / "cli-overlay-output"
    result = runner.invoke(
        app,
        ["overlay", str(build), "--config", str(config_path), "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["required_checks_passed"] is True
    assert payload["layer_count"] == 1

    outside_path = tmp_path / "outside.geojson"
    outside_path.write_text(
        json.dumps(
            {
                "type": "LineString",
                "coordinates": [[600000.0, 3400000.0], [600100.0, 3400100.0]],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    outside = OverlayConfig(
        sources=(_source("outside", OverlayKind.ROAD, outside_path, color="#e9c46a"),)
    )
    with pytest.raises(ConfigurationError, match="does not intersect"):
        generate_overlay_bundle(build, outside, tmp_path / "outside-output")


def test_overlay_cli_slice_does_not_rewrite_terrain_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import topoforge.cli.app as cli_module
    import topoforge.validation.slicers as slicers_module

    build = _build_bundle(tmp_path, SyntheticTerrain.SLOPE)
    lines = _write_line_geojson(tmp_path / "slice-road.geojson")
    overlay = generate_overlay_bundle(
        build,
        OverlayConfig(sources=(_source("road", OverlayKind.ROAD, lines, color="#e9c46a"),)),
        tmp_path / "slice-overlay",
    )
    validation_path = overlay.output_dir / "validation.json"
    validation_before = validation_path.read_bytes()

    class SuccessfulOverlaySlicer:
        def slice(self, input_model: Path, output_gcode: Path, **_: object) -> SliceResult:
            output_gcode.write_text(
                "; generated by BambuStudio 02.07.01.62\n"
                "; total layers count = 12\n"
                "; support_material = 0\n"
                "G28\nG1 X1 Y1 Z0.2\n",
                encoding="utf-8",
            )
            return SliceResult(
                status=SliceStatus.SUCCEEDED,
                slicer=SlicerInfo(
                    name="BambuStudio",
                    version="02.07.01.62",
                    executable=tmp_path / "bambu-studio",
                    status=SlicerAvailability.AVAILABLE,
                ),
                profile="overlay regression",
                input_model=input_model,
                output_gcode=output_gcode,
                exit_code=0,
                gcode_generated=True,
                gcode_size_bytes=output_gcode.stat().st_size,
                metrics=parse_gcode_metrics(output_gcode.read_text(encoding="utf-8")),
            )

    def unexpected_record(*_: object, **__: object) -> Path:
        raise AssertionError("overlay slicing must not write a terrain bundle slice report")

    monkeypatch.setattr(slicers_module, "BambuStudioAdapter", SuccessfulOverlaySlicer)
    monkeypatch.setattr(cli_module, "record_slice_validation", unexpected_record)
    gcode = tmp_path / "overlay.gcode"
    result = runner.invoke(
        app,
        ["slice", str(overlay.model_3mf_path), "--output", str(gcode)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["exit_code"] == 0
    assert validation_path.read_bytes() == validation_before
    assert not (overlay.output_dir / "slice_validation.json").exists()


@pytest.mark.parametrize(
    ("name", "crs", "coordinates"),
    (
        (
            "antimeridian",
            "+proj=aeqd +lat_0=0 +lon_0=180 +datum=WGS84 +units=m +no_defs",
            [[179.9, 0.0], [-179.9, 0.0]],
        ),
        (
            "high-latitude",
            "+proj=aeqd +lat_0=85 +lon_0=0 +datum=WGS84 +units=m +no_defs",
            [[-1.0, 85.0], [1.0, 85.0]],
        ),
    ),
)
def test_overlay_projection_edges_preserve_east_order(
    tmp_path: Path,
    name: str,
    crs: str,
    coordinates: list[list[float]],
) -> None:
    build = _build_projected_bundle(tmp_path, name=name, crs=crs)
    path = tmp_path / f"{name}.geojson"
    path.write_text(
        json.dumps({"type": "LineString", "coordinates": coordinates}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config = OverlayConfig(
        sources=(
            OverlaySourceConfig(
                source_id=name,
                kind=OverlayKind.ROAD,
                format=OverlayFormat.GEOJSON,
                path=path,
                source_crs="EPSG:4326",
                dataset_name=f"{name} vector fixture",
                license="CC0-1.0",
                attribution="TopoForge tests",
            ),
        ),
        preview_width_px=480,
    )
    result = generate_overlay_bundle(build, config, tmp_path / f"{name}-overlay")
    plan = json.loads((result.output_dir / "overlay-plan.geojson").read_text(encoding="utf-8"))
    first, second = plan["features"][0]["geometry"]["coordinates"]
    assert 0.0 <= first[0] < second[0] <= 60.0
    assert result.validation.required_checks_passed is True
