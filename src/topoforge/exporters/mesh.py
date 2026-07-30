"""STL and GLB exporters backed by Trimesh."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import trimesh


def export_stl(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """Export a manufacturing mesh as binary STL.

    STL has no reliable internal unit metadata.  TopoForge therefore preserves
    the mesh's millimetre-valued coordinates and rejects open, inconsistently
    wound, or non-positive solids before serializing them.
    """

    destination = _destination(path, ".stl")
    _require_finite_mesh(mesh)
    if not bool(mesh.is_watertight):
        msg = "STL export requires a watertight mesh"
        raise ValueError(msg)
    if not bool(mesh.is_winding_consistent):
        msg = "STL export requires consistent face winding"
        raise ValueError(msg)
    if float(mesh.volume) <= 0.0:
        msg = "STL export requires a positive-volume mesh"
        raise ValueError(msg)
    return _write_trimesh_export(mesh, destination, file_type="stl")


def export_glb(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """Export a finite terrain preview as binary glTF (GLB)."""

    destination = _destination(path, ".glb")
    _require_finite_mesh(mesh)
    return _write_trimesh_export(mesh, destination, file_type="glb")


def _destination(path: str | Path, required_suffix: str) -> Path:
    destination = Path(path)
    if destination.suffix.lower() != required_suffix:
        msg = f"expected a {required_suffix} output path, got {destination}"
        raise ValueError(msg)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _require_finite_mesh(mesh: trimesh.Trimesh) -> None:
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        msg = "mesh must contain vertices and faces"
        raise ValueError(msg)
    if not bool(np.all(np.isfinite(np.asarray(mesh.vertices)))):
        msg = "mesh vertices must be finite"
        raise ValueError(msg)


def _write_trimesh_export(
    mesh: trimesh.Trimesh,
    destination: Path,
    *,
    file_type: Literal["stl", "glb"],
) -> Path:
    exported: bytes | str | dict[object, object] = mesh.export(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        file_type=file_type
    )
    if isinstance(exported, str):
        payload = exported.encode("utf-8")
    elif isinstance(exported, bytes | bytearray):
        payload = bytes(exported)
    else:
        msg = f"Trimesh returned unsupported {file_type} payload type: {type(exported).__name__}"
        raise TypeError(msg)
    if not payload:
        msg = f"Trimesh returned an empty {file_type} payload"
        raise ValueError(msg)

    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    if not destination.is_file() or destination.stat().st_size == 0:
        msg = f"failed to create non-empty export: {destination}"
        raise OSError(msg)
    return destination
