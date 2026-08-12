from pathlib import Path
from typing import Any
from zipfile import ZipFile

import numpy as np
import pytest

from topoforge.exporters.three_mf import export_3mf, inspect_3mf
from topoforge.mesh import build_rectangular_terrain_mesh


def test_3mf_export_fsyncs_a_writable_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open
    temporary_modes: list[str] = []

    def tracking_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        if path.name.endswith(".tmp.3mf"):
            temporary_modes.append(mode)
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", tracking_open)
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )

    export_3mf(mesh, tmp_path / "model.3mf")

    assert temporary_modes == ["r+b"]


def test_3mf_round_trip_preserves_units_dimensions_and_topology(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    output = export_3mf(mesh, tmp_path / "model.3mf", metadata={"dataset_type": "dtm"})
    inspected = inspect_3mf(output)
    assert inspected.unit == "millimeter"
    assert inspected.object_count == 1
    assert inspected.object_names == ("TopoForge terrain",)
    assert inspected.strict_warning_count == 0
    assert inspected.lib3mf_version == (2, 5, 0)
    assert inspected.build_item_count == 1
    assert inspected.vertex_count == len(mesh.vertices)
    assert inspected.triangle_count == len(mesh.faces)
    assert inspected.dimensions_mm == pytest.approx(tuple(mesh.extents), abs=1e-9)
    assert inspected.bounds_mm[0] == pytest.approx(tuple(mesh.bounds[0]), abs=1e-9)
    assert inspected.bounds_mm[1] == pytest.approx(tuple(mesh.bounds[1]), abs=1e-9)


def test_repeated_3mf_exports_are_byte_identical(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    first = export_3mf(mesh, tmp_path / "first.3mf", metadata={"dataset_type": "dtm"})
    second = export_3mf(mesh, tmp_path / "second.3mf", metadata={"dataset_type": "dtm"})
    assert first.read_bytes() == second.read_bytes()


def test_3mf_inspection_rejects_duplicate_member_names(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    source_path = export_3mf(mesh, tmp_path / "source.3mf")
    attacked_path = tmp_path / "duplicate-member.3mf"
    with ZipFile(source_path) as source, ZipFile(attacked_path, "w") as attacked:
        for info in source.infolist():
            attacked.writestr(
                info.filename,
                source.read(info.filename),
                compress_type=info.compress_type,
            )
        duplicate_name = source.infolist()[0].filename
        with pytest.warns(UserWarning, match="Duplicate name"):
            attacked.writestr(duplicate_name, source.read(duplicate_name))

    with pytest.raises(ValueError, match="duplicate member names"):
        inspect_3mf(attacked_path)


def test_3mf_inspection_enforces_uncompressed_size_budget(tmp_path: Path) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    output = export_3mf(mesh, tmp_path / "model.3mf")

    with pytest.raises(ValueError, match="uncompressed-size limit"):
        inspect_3mf(output, max_uncompressed_bytes=1)


def test_3mf_export_does_not_follow_legacy_fixed_temporary_symlink(
    tmp_path: Path,
) -> None:
    mesh = build_rectangular_terrain_mesh(
        np.array([[3.0, 5.0], [4.0, 7.0]]),
        width_mm=80.0,
        depth_mm=60.0,
        base_thickness_mm=3.0,
    )
    destination = tmp_path / "model.3mf"
    external = tmp_path / "external.txt"
    external.write_bytes(b"preserve")
    legacy_temporary = tmp_path / ".model.tmp.3mf"
    try:
        legacy_temporary.symlink_to(external)
    except OSError:
        pytest.skip("host cannot create symlink fixture")

    output = export_3mf(mesh, destination)

    assert output == destination
    assert external.read_bytes() == b"preserve"
    assert legacy_temporary.is_symlink()
    assert not list(tmp_path.glob(".model.3mf.*"))
    inspected = inspect_3mf(output)
    assert inspected.strict_warning_count == 0
    assert inspected.triangle_count == len(mesh.faces)
