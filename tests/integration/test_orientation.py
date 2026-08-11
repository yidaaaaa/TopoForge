from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
import rasterio
import trimesh
from affine import Affine
from rasterio.transform import from_origin

from topoforge.engine import build_local_terrain
from topoforge.exporters.three_mf import CORE_NS, MODEL_PATH, inspect_3mf
from topoforge.models import BuildConfig, SamplingMode, VerticalScaleMode


def _top_z(mesh: trimesh.Trimesh, x: float, y: float) -> float:
    vertices = np.asarray(mesh.vertices)
    matches = np.isclose(vertices[:, 0], x, atol=1e-5) & np.isclose(vertices[:, 1], y, atol=1e-5)
    return float(np.max(vertices[matches, 2]))


def _three_mf_mesh(path: Path) -> trimesh.Trimesh:
    with ZipFile(path) as package:
        root = ET.fromstring(package.read(MODEL_PATH))
    namespace = {"m": CORE_NS}
    vertices = np.asarray(
        [
            [float(item.attrib[axis]) for axis in ("x", "y", "z")]
            for item in root.findall(".//m:mesh/m:vertices/m:vertex", namespace)
        ]
    )
    faces = np.asarray(
        [
            [int(item.attrib[index]) for index in ("v1", "v2", "v3")]
            for item in root.findall(".//m:mesh/m:triangles/m:triangle", namespace)
        ]
    )
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def test_asymmetric_dem_keeps_all_corners_and_peak_in_stl_glb_and_3mf(tmp_path: Path) -> None:
    values = np.array(
        [
            [400.0, 80.0, 70.0, 60.0, 300.0],
            [50.0, 45.0, 40.0, 35.0, 30.0],
            [20.0, 18.0, 16.0, 14.0, 12.0],
            [200.0, 10.0, 8.0, 6.0, 100.0],
        ],
        dtype=np.float32,
    )
    source = tmp_path / "asymmetric.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:32647",
        transform=from_origin(500_000.0, 3_300_000.0, 30.0, 30.0),
    ) as dataset:
        dataset.write(values, 1)

    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=tmp_path / "build",
            model_width_mm=100.0,
            model_depth_mm=80.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
            max_height_mm=30.0,
        )
    )

    assert result.validation["orientation_consistent"] is True
    assert result.validation["orientation"]["east_axis"] == "+X = East"
    assert result.validation["orientation"]["north_axis"] == "+Y = North"
    assert result.validation["orientation"]["north_edge"] == "y=model_depth_mm"

    stl = trimesh.load(result.artifacts["model_stl"], force="mesh", process=True)
    glb = trimesh.load(result.artifacts["preview_glb"], force="mesh", process=False)
    three_mf = _three_mf_mesh(result.artifacts["model_3mf"])
    assert isinstance(stl, trimesh.Trimesh)
    assert isinstance(glb, trimesh.Trimesh)

    for mesh in (stl, glb, three_mf):
        northwest = _top_z(mesh, 0.0, 80.0)
        northeast = _top_z(mesh, 100.0, 80.0)
        southwest = _top_z(mesh, 0.0, 0.0)
        southeast = _top_z(mesh, 100.0, 0.0)
        assert northwest > northeast > southwest > southeast
        assert mesh.is_watertight
        assert mesh.is_winding_consistent
        assert mesh.volume > 0.0

    inspection = inspect_3mf(result.artifacts["model_3mf"])
    assert inspection.strict_warning_count == 0
    assert inspection.metadata["customXMLNS0:east_axis"] == "+X = East"
    assert inspection.metadata["customXMLNS0:north_axis"] == "+Y = North"
    assert inspection.metadata["customXMLNS0:north_edge"] == "y=model_depth_mm"


def test_west_up_metric_dem_is_normalized_before_mesh_construction(tmp_path: Path) -> None:
    values = np.array(
        [
            [300.0, 325.0, 350.0, 375.0, 400.0],
            [260.0, 280.0, 300.0, 320.0, 340.0],
            [180.0, 200.0, 220.0, 240.0, 260.0],
            [100.0, 125.0, 150.0, 175.0, 200.0],
        ],
        dtype=np.float32,
    )
    source = tmp_path / "west-up.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:32647",
        transform=Affine(-30.0, 0.0, 500_150.0, 0.0, -30.0, 3_300_000.0),
    ) as dataset:
        dataset.write(values, 1)

    result = build_local_terrain(
        BuildConfig(
            dem_path=source,
            output_dir=tmp_path / "west-up-build",
            model_width_mm=100.0,
            model_depth_mm=80.0,
            sampling_mode=SamplingMode.SOURCE_PRESERVING,
            vertical_scale_mode=VerticalScaleMode.FIT_HEIGHT,
            max_height_mm=30.0,
        )
    )

    mesh = trimesh.load(result.artifacts["model_stl"], force="mesh", process=True)
    assert isinstance(mesh, trimesh.Trimesh)
    northwest = _top_z(mesh, 0.0, 80.0)
    northeast = _top_z(mesh, 100.0, 80.0)
    southwest = _top_z(mesh, 0.0, 0.0)
    southeast = _top_z(mesh, 100.0, 0.0)
    assert northwest > northeast > southwest > southeast
    assert result.validation["orientation_consistent"] is True
    assert result.validation["orientation"]["east_axis"] == "+X = East"
    assert result.validation["orientation"]["north_axis"] == "+Y = North"
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.volume > 0.0
