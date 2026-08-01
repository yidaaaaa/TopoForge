"""Deterministic 3MF export and strict reference-library round-trip inspection."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import lib3mf
import numpy as np
import trimesh
from lib3mf import get_wrapper

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_PATH = "3D/3dmodel.model"
METADATA_NS = "https://topoforge.dev/ns/3mf/1"
_UUID_NAMESPACE = uuid.UUID("d70c7f97-1a8c-5b66-8e4d-fde90514c885")


@dataclass(frozen=True, slots=True)
class ThreeMFInspection:
    """Measurements obtained by strictly reopening the serialized 3MF package."""

    unit: str
    object_count: int
    build_item_count: int
    vertex_count: int
    triangle_count: int
    dimensions_mm: tuple[float, float, float]
    bounds_mm: tuple[tuple[float, float, float], tuple[float, float, float]]
    peak_coordinate_mm: tuple[float, float, float]
    peak_coordinates_mm: tuple[tuple[float, float, float], ...]
    metadata: dict[str, str]
    object_names: tuple[str, ...]
    strict_warning_count: int
    lib3mf_version: tuple[int, int, int]


def _position(x: float, y: float, z: float) -> lib3mf.Position:
    value = lib3mf.Position()
    value.Coordinates[0] = float(x)
    value.Coordinates[1] = float(y)
    value.Coordinates[2] = float(z)
    return value


def _triangle(a: int, b: int, c: int) -> lib3mf.Triangle:
    value = lib3mf.Triangle()
    value.Indices[0] = int(a)
    value.Indices[1] = int(b)
    value.Indices[2] = int(c)
    return value


def _stable_uuids(
    vertices: np.ndarray,
    faces: np.ndarray,
    object_name: str,
    metadata: dict[str, str],
) -> tuple[str, str, str]:
    digest = hashlib.sha256()
    digest.update(np.asarray(vertices, dtype="<f8").tobytes(order="C"))
    digest.update(np.asarray(faces, dtype="<u4").tobytes(order="C"))
    digest.update(object_name.encode("utf-8"))
    digest.update(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    geometry_name = digest.hexdigest()
    build_uuid = uuid.uuid5(_UUID_NAMESPACE, f"build:{geometry_name}")
    object_uuid = uuid.uuid5(_UUID_NAMESPACE, f"object:{geometry_name}:terrain")
    item_uuid = uuid.uuid5(_UUID_NAMESPACE, f"item:{object_uuid}:identity")
    return str(build_uuid), str(object_uuid), str(item_uuid)


def export_3mf(
    mesh: trimesh.Trimesh,
    path: str | Path,
    *,
    object_name: str = "TopoForge terrain",
    metadata: dict[str, str] | None = None,
) -> Path:
    """Export one validated terrain through the official lib3mf reference writer."""
    destination = Path(path)
    if destination.suffix.lower() != ".3mf":
        raise ValueError(f"expected a .3mf output path, got {destination}")
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if vertices.size == 0 or faces.size == 0 or not bool(np.all(np.isfinite(vertices))):
        raise ValueError("3MF export requires a finite non-empty triangle mesh")
    if (
        not bool(mesh.is_watertight)
        or not bool(mesh.is_winding_consistent)
        or float(mesh.volume) <= 0
    ):
        raise ValueError(
            "3MF export requires a watertight, consistently wound, positive-volume mesh"
        )
    if int(np.min(faces)) < 0 or int(np.max(faces)) >= len(vertices):
        raise ValueError("3MF export found an out-of-range triangle index")

    normalized_metadata = {str(key): str(value) for key, value in (metadata or {}).items()}
    build_uuid, object_uuid, item_uuid = _stable_uuids(
        vertices,
        faces,
        object_name,
        normalized_metadata,
    )
    wrapper = get_wrapper()
    model = wrapper.CreateModel()
    if model is None:
        raise RuntimeError("lib3mf did not create a model")
    model.SetUnit(lib3mf.ModelUnit.MilliMeter)
    model.SetLanguage("en-US")
    model.SetBuildUUID(build_uuid)
    metadata_group = model.GetMetaDataGroup()
    metadata_group.AddMetaData("", "Title", object_name, "xs:string", False)
    metadata_group.AddMetaData("", "Application", "TopoForge", "xs:string", False)
    for key, value in sorted(normalized_metadata.items()):
        metadata_group.AddMetaData(METADATA_NS, key, value, "xs:string", True)

    mesh_object = model.AddMeshObject()
    mesh_object.SetName(object_name)
    mesh_object.SetPartNumber("topoforge-terrain")
    mesh_object.SetUUID(object_uuid)
    lib_vertices = [_position(*vertex) for vertex in vertices]
    lib_triangles = [_triangle(*face) for face in faces]
    mesh_object.SetGeometry(lib_vertices, lib_triangles)
    build_item = model.AddBuildItem(mesh_object, wrapper.GetIdentityTransform())
    build_item.SetPartNumber("topoforge-terrain-instance")
    build_item.SetUUID(item_uuid)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp.3mf")
    writer = model.QueryWriter("3mf")
    writer.SetStrictModeActive(True)
    writer.WriteToFile(str(temporary))
    if writer.GetWarningCount() != 0:
        warnings = [writer.GetWarning(index) for index in range(writer.GetWarningCount())]
        temporary.unlink(missing_ok=True)
        raise ValueError(f"lib3mf writer emitted strict warnings: {warnings}")
    temporary.replace(destination)
    inspect_3mf(destination)
    return destination


def _inspect_package(path: Path) -> ET.Element:
    with ZipFile(path, "r") as package:
        names = set(package.namelist())
        required = {"[Content_Types].xml", "_rels/.rels", MODEL_PATH}
        if not required.issubset(names):
            missing = ", ".join(sorted(required - names))
            raise ValueError(f"3MF package is missing required parts: {missing}")
        for info in package.infolist():
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"3MF package contains an unsafe path: {info.filename}")
            if info.flag_bits & 0x1:
                raise ValueError(f"3MF package contains an encrypted part: {info.filename}")
            if info.compress_type not in {0, 8}:
                raise ValueError(
                    "3MF package uses unsupported compression "
                    f"{info.compress_type}: {info.filename}"
                )
            if info.filename.endswith(".rels"):
                relationships = ET.fromstring(package.read(info.filename))
                for relationship in relationships:
                    if relationship.attrib.get("TargetMode", "Internal") == "External":
                        raise ValueError("3MF package contains an external relationship")
        return ET.fromstring(package.read(MODEL_PATH))


def inspect_3mf(path: str | Path) -> ThreeMFInspection:
    """Strict-read with lib3mf, harden the OPC package, and independently measure XML."""
    source = Path(path)
    root = _inspect_package(source)
    wrapper = get_wrapper()
    model = wrapper.CreateModel()
    if model is None:
        raise RuntimeError("lib3mf did not create a read model")
    reader = model.QueryReader("3mf")
    reader.SetStrictModeActive(True)
    reader.ReadFromFile(str(source))
    warning_count = int(reader.GetWarningCount())
    if warning_count:
        warnings = [reader.GetWarning(index) for index in range(warning_count)]
        raise ValueError(f"lib3mf strict reader emitted warnings: {warnings}")
    if model.GetUnit() != lib3mf.ModelUnit.MilliMeter:
        raise ValueError(f"3MF unit is not millimetres: {model.GetUnit().name}")

    object_names: list[str] = []
    strict_vertex_count = 0
    strict_triangle_count = 0
    mesh_iterator = model.GetMeshObjects()
    while mesh_iterator.MoveNext():
        mesh_object = mesh_iterator.GetCurrentMeshObject()
        object_names.append(mesh_object.GetName())
        strict_vertex_count += int(mesh_object.GetVertexCount())
        strict_triangle_count += int(mesh_object.GetTriangleCount())
    build_item_count = 0
    build_iterator = model.GetBuildItems()
    while build_iterator.MoveNext():
        build_item_count += 1

    namespace = {"m": CORE_NS}
    vertices = root.findall(".//m:mesh/m:vertices/m:vertex", namespace)
    triangles = root.findall(".//m:mesh/m:triangles/m:triangle", namespace)
    if not vertices or not triangles:
        raise ValueError("3MF model has no triangle mesh")
    coordinates = np.asarray(
        [[float(vertex.attrib[axis]) for axis in ("x", "y", "z")] for vertex in vertices],
        dtype=np.float64,
    )
    if not bool(np.all(np.isfinite(coordinates))):
        raise ValueError("3MF contains non-finite vertices")
    if strict_vertex_count != len(vertices) or strict_triangle_count != len(triangles):
        raise ValueError("lib3mf and independent XML topology counts disagree")
    dimensions = np.ptp(coordinates, axis=0)
    minimum = np.min(coordinates, axis=0)
    maximum = np.max(coordinates, axis=0)
    peak_index = int(np.argmax(coordinates[:, 2]))
    peak = coordinates[peak_index]
    maximum_z = float(peak[2])
    peak_candidates = coordinates[np.isclose(coordinates[:, 2], maximum_z, atol=1e-6, rtol=0.0)]
    metadata = {
        str(element.attrib.get("name", "")): str(element.text or "")
        for element in root.findall("m:metadata", namespace)
    }
    return ThreeMFInspection(
        unit="millimeter",
        object_count=len(object_names),
        build_item_count=build_item_count,
        vertex_count=len(vertices),
        triangle_count=len(triangles),
        dimensions_mm=(float(dimensions[0]), float(dimensions[1]), float(dimensions[2])),
        bounds_mm=(
            (float(minimum[0]), float(minimum[1]), float(minimum[2])),
            (float(maximum[0]), float(maximum[1]), float(maximum[2])),
        ),
        peak_coordinate_mm=(float(peak[0]), float(peak[1]), float(peak[2])),
        peak_coordinates_mm=tuple(
            (float(candidate[0]), float(candidate[1]), float(candidate[2]))
            for candidate in peak_candidates
        ),
        metadata=metadata,
        object_names=tuple(object_names),
        strict_warning_count=warning_count,
        lib3mf_version=(
            int(wrapper.GetLibraryVersion()[0]),
            int(wrapper.GetLibraryVersion()[1]),
            int(wrapper.GetLibraryVersion()[2]),
        ),
    )
