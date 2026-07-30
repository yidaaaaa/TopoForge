from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from topoforge.exporters import export_glb, export_stl
from topoforge.mesh import build_rectangular_terrain_mesh


def _sample_mesh() -> trimesh.Trimesh:
    x, y = np.meshgrid(np.linspace(-1.0, 1.0, 8), np.linspace(-1.0, 1.0, 6))
    elevation = 12.0 * np.exp(-2.0 * (x**2 + y**2))
    top_z_mm = 3.0 + elevation - float(np.min(elevation))
    return build_rectangular_terrain_mesh(
        top_z_mm,
        width_mm=90.0,
        depth_mm=55.0,
        base_thickness_mm=3.0,
    )


def test_stl_and_glb_exports_are_non_empty_and_reloadable(tmp_path: Path) -> None:
    mesh = _sample_mesh()
    stl_path = tmp_path / "nested" / "model.stl"
    glb_path = tmp_path / "nested" / "preview.glb"

    assert export_stl(mesh, stl_path) == stl_path
    assert export_glb(mesh, glb_path) == glb_path

    assert stl_path.stat().st_size > 84
    assert glb_path.stat().st_size > 20
    assert glb_path.read_bytes()[:4] == b"glTF"

    # STL triangle soup needs coordinate welding before topology checks.
    loaded_stl = trimesh.load_mesh(  # pyright: ignore[reportUnknownMemberType]
        stl_path, file_type="stl", process=True
    )
    assert isinstance(loaded_stl, trimesh.Trimesh)
    assert loaded_stl.is_watertight
    assert loaded_stl.is_winding_consistent
    assert loaded_stl.volume > 0.0
    np.testing.assert_allclose(loaded_stl.extents, mesh.extents, atol=1e-5)

    loaded_glb = trimesh.load_mesh(  # pyright: ignore[reportUnknownMemberType]
        glb_path, file_type="glb", force="mesh", process=False
    )
    assert isinstance(loaded_glb, trimesh.Trimesh)
    np.testing.assert_allclose(loaded_glb.extents, mesh.extents, atol=1e-5)


def test_exporters_enforce_format_suffixes(tmp_path: Path) -> None:
    mesh = _sample_mesh()

    with pytest.raises(ValueError, match=r"\.stl"):
        export_stl(mesh, tmp_path / "model.glb")
    with pytest.raises(ValueError, match=r"\.glb"):
        export_glb(mesh, tmp_path / "preview.stl")


def test_stl_export_rejects_open_mesh(tmp_path: Path) -> None:
    open_mesh = trimesh.Trimesh(
        vertices=np.array(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))),
        faces=np.array(((0, 1, 2),)),
        process=False,
    )

    with pytest.raises(ValueError, match="watertight"):
        export_stl(open_mesh, tmp_path / "open.stl")
