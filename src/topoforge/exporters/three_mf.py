"""Deterministic 3MF export and strict reference-library round-trip inspection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
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
_MAX_3MF_MEMBERS = 256
_MAX_3MF_UNCOMPRESSED_BYTES = 1_610_612_736
_MAX_3MF_MEMBER_BYTES = 1_073_741_824
_MAX_3MF_COMPRESSION_RATIO = 2_000.0


def _write_lib3mf_model(model: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = model.QueryWriter("3mf")
    writer.SetStrictModeActive(True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    ) as staging_directory:
        temporary = Path(staging_directory) / f"{destination.stem}.tmp.3mf"
        writer.WriteToFile(str(temporary))
        if writer.GetWarningCount() != 0:
            warnings = [writer.GetWarning(index) for index in range(writer.GetWarningCount())]
            raise ValueError(f"lib3mf writer emitted strict warnings: {warnings}")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    if os.name != "nt":
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


@dataclass(frozen=True, slots=True)
class ThreeMFObject:
    """One named, independently printable mesh in a multi-object package."""

    name: str
    mesh: trimesh.Trimesh
    part_number: str


@dataclass(frozen=True, slots=True)
class ThreeMFObjectInspection:
    """Independent topology and bounds for one reopened 3MF mesh object."""

    name: str
    vertex_count: int
    triangle_count: int
    bounds_mm: tuple[tuple[float, float, float], tuple[float, float, float]]
    geometry_sha256: str


@dataclass(frozen=True, slots=True)
class ThreeMFInspection:
    """Measurements obtained by strictly reopening the serialized 3MF package."""

    unit: str
    object_count: int
    build_item_count: int
    components_object_count: int
    component_count: int
    base_material_group_count: int
    material_assigned_object_count: int
    vertex_count: int
    triangle_count: int
    dimensions_mm: tuple[float, float, float]
    bounds_mm: tuple[tuple[float, float, float], tuple[float, float, float]]
    peak_coordinate_mm: tuple[float, float, float]
    peak_coordinates_mm: tuple[tuple[float, float, float], ...]
    metadata: dict[str, str]
    object_names: tuple[str, ...]
    objects: tuple[ThreeMFObjectInspection, ...]
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

    _write_lib3mf_model(model, destination)
    inspect_3mf(destination)
    return destination


def export_3mf_objects(
    objects: tuple[ThreeMFObject, ...],
    path: str | Path,
    *,
    title: str,
    metadata: dict[str, str] | None = None,
) -> Path:
    """Export stable independent mesh objects through the official lib3mf writer."""
    destination = Path(path)
    if destination.suffix.lower() != ".3mf":
        raise ValueError(f"expected a .3mf output path, got {destination}")
    if not objects:
        raise ValueError("multi-object 3MF export requires at least one object")
    names = [item.name for item in objects]
    if len(names) != len(set(names)):
        raise ValueError("multi-object 3MF object names must be unique")
    normalized_metadata = {str(key): str(value) for key, value in (metadata or {}).items()}
    digest = hashlib.sha256()
    digest.update(title.encode("utf-8"))
    digest.update(json.dumps(normalized_metadata, sort_keys=True).encode("utf-8"))
    prepared: list[tuple[ThreeMFObject, np.ndarray, np.ndarray]] = []
    for item in objects:
        vertices = np.asarray(item.mesh.vertices, dtype=np.float64)
        faces = np.asarray(item.mesh.faces, dtype=np.int64)
        if vertices.size == 0 or faces.size == 0 or not bool(np.all(np.isfinite(vertices))):
            raise ValueError(f"3MF object {item.name!r} requires a finite non-empty mesh")
        if (
            not bool(item.mesh.is_watertight)
            or not bool(item.mesh.is_winding_consistent)
            or float(item.mesh.volume) <= 0
        ):
            raise ValueError(
                f"3MF object {item.name!r} must be watertight, consistently wound, "
                "and positive-volume"
            )
        if int(np.min(faces)) < 0 or int(np.max(faces)) >= len(vertices):
            raise ValueError(f"3MF object {item.name!r} has an out-of-range triangle index")
        digest.update(item.name.encode("utf-8"))
        digest.update(item.part_number.encode("utf-8"))
        digest.update(np.asarray(vertices, dtype="<f8").tobytes(order="C"))
        digest.update(np.asarray(faces, dtype="<u4").tobytes(order="C"))
        prepared.append((item, vertices, faces))

    identity = digest.hexdigest()
    wrapper = get_wrapper()
    model = wrapper.CreateModel()
    if model is None:
        raise RuntimeError("lib3mf did not create a model")
    model.SetUnit(lib3mf.ModelUnit.MilliMeter)
    model.SetLanguage("en-US")
    model.SetBuildUUID(str(uuid.uuid5(_UUID_NAMESPACE, f"multi-build:{identity}")))
    metadata_group = model.GetMetaDataGroup()
    metadata_group.AddMetaData("", "Title", title, "xs:string", False)
    metadata_group.AddMetaData("", "Application", "TopoForge", "xs:string", False)
    for key, value in sorted(normalized_metadata.items()):
        metadata_group.AddMetaData(METADATA_NS, key, value, "xs:string", True)

    material_group = model.AddBaseMaterialGroup()
    material_property_id = material_group.AddMaterial(
        "TopoForge single material",
        wrapper.RGBAToColor(255, 255, 255, 255),
    )
    material_resource_id = material_group.GetUniqueResourceID()
    mesh_resources: list[tuple[lib3mf.MeshObject, uuid.UUID]] = []
    for index, (item, vertices, faces) in enumerate(prepared):
        object_uuid = uuid.uuid5(_UUID_NAMESPACE, f"multi-object:{identity}:{index}:{item.name}")
        mesh_object = model.AddMeshObject()
        mesh_object.SetName(item.name)
        mesh_object.SetPartNumber(item.part_number)
        mesh_object.SetUUID(str(object_uuid))
        mesh_object.SetGeometry(
            [_position(*vertex) for vertex in vertices],
            [_triangle(*face) for face in faces],
        )
        mesh_object.SetObjectLevelProperty(material_resource_id, material_property_id)
        mesh_resources.append((mesh_object, object_uuid))

    assembly_uuid = uuid.uuid5(_UUID_NAMESPACE, f"multi-assembly:{identity}")
    assembly = model.AddComponentsObject()
    assembly.SetName(f"{title} assembly")
    assembly.SetPartNumber("topoforge-assembly")
    assembly.SetUUID(str(assembly_uuid))
    for index, (mesh_object, object_uuid) in enumerate(mesh_resources):
        component_uuid = uuid.uuid5(
            _UUID_NAMESPACE,
            f"multi-component:{assembly_uuid}:{index}:{object_uuid}:identity",
        )
        component = assembly.AddComponent(mesh_object, wrapper.GetIdentityTransform())
        component.SetUUID(str(component_uuid))
    build_item_uuid = uuid.uuid5(_UUID_NAMESPACE, f"multi-assembly-item:{assembly_uuid}:identity")
    build_item = model.AddBuildItem(assembly, wrapper.GetIdentityTransform())
    build_item.SetPartNumber("topoforge-assembly-instance")
    build_item.SetUUID(str(build_item_uuid))

    _write_lib3mf_model(model, destination)
    inspection = inspect_3mf(destination)
    if (
        inspection.object_names != tuple(names)
        or inspection.build_item_count != 1
        or inspection.components_object_count != 1
        or inspection.component_count != len(objects)
        or inspection.base_material_group_count != 1
        or inspection.material_assigned_object_count != len(objects)
    ):
        destination.unlink(missing_ok=True)
        raise ValueError("multi-object 3MF failed strict component-assembly reopen")
    return destination


def _inspect_package(
    path: Path,
    *,
    max_uncompressed_bytes: int = _MAX_3MF_UNCOMPRESSED_BYTES,
) -> ET.Element:
    if max_uncompressed_bytes <= 0:
        raise ValueError("3MF uncompressed byte limit must be positive")
    with ZipFile(path, "r") as package:
        infos = package.infolist()
        if len(infos) > _MAX_3MF_MEMBERS:
            raise ValueError(
                f"3MF package has {len(infos)} members, above the safe limit {_MAX_3MF_MEMBERS}"
            )
        member_names = [info.filename for info in infos]
        if len(member_names) != len(set(member_names)):
            raise ValueError("3MF package contains duplicate member names")
        names = set(member_names)
        required = {"[Content_Types].xml", "_rels/.rels", MODEL_PATH}
        if not required.issubset(names):
            missing = ", ".join(sorted(required - names))
            raise ValueError(f"3MF package is missing required parts: {missing}")
        total_uncompressed_bytes = 0
        for info in infos:
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
            if info.file_size > _MAX_3MF_MEMBER_BYTES:
                raise ValueError(f"3MF member is too large after decompression: {info.filename}")
            total_uncompressed_bytes += info.file_size
            if total_uncompressed_bytes > max_uncompressed_bytes:
                raise ValueError(
                    "3MF package exceeds the bounded uncompressed-size limit; "
                    "rebuild with a smaller grid"
                )
            if info.file_size and info.compress_size == 0:
                raise ValueError(f"3MF member has an invalid compressed size: {info.filename}")
            if (
                info.compress_size > 0
                and info.file_size / info.compress_size > _MAX_3MF_COMPRESSION_RATIO
            ):
                raise ValueError(f"3MF member has an unsafe compression ratio: {info.filename}")
            if info.filename.endswith(".rels"):
                relationships = ET.fromstring(package.read(info.filename))
                for relationship in relationships:
                    if relationship.attrib.get("TargetMode", "Internal") == "External":
                        raise ValueError("3MF package contains an external relationship")
        return ET.fromstring(package.read(MODEL_PATH))


def inspect_3mf(
    path: str | Path,
    *,
    max_uncompressed_bytes: int = _MAX_3MF_UNCOMPRESSED_BYTES,
) -> ThreeMFInspection:
    """Strict-read with lib3mf, harden the OPC package, and independently measure XML."""
    source = Path(path)
    root = _inspect_package(source, max_uncompressed_bytes=max_uncompressed_bytes)
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
    components_elements = root.findall("m:resources/m:object/m:components", namespace)
    component_count = sum(
        len(components.findall("m:component", namespace)) for components in components_elements
    )
    base_material_groups = root.findall("m:resources/m:basematerials", namespace)
    material_assigned_object_count = sum(
        "pid" in object_element.attrib and "pindex" in object_element.attrib
        for object_element in root.findall("m:resources/m:object", namespace)
    )
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
    object_inspections: list[ThreeMFObjectInspection] = []
    for object_element in root.findall("m:resources/m:object", namespace):
        mesh_element = object_element.find("m:mesh", namespace)
        if mesh_element is None:
            continue
        object_vertices = mesh_element.findall("m:vertices/m:vertex", namespace)
        object_triangles = mesh_element.findall("m:triangles/m:triangle", namespace)
        if not object_vertices or not object_triangles:
            continue
        object_coordinates = np.asarray(
            [
                [float(vertex.attrib[axis]) for axis in ("x", "y", "z")]
                for vertex in object_vertices
            ],
            dtype=np.float64,
        )
        object_faces = np.asarray(
            [
                [int(triangle.attrib[axis]) for axis in ("v1", "v2", "v3")]
                for triangle in object_triangles
            ],
            dtype=np.uint32,
        )
        object_digest = hashlib.sha256()
        object_digest.update(np.asarray(object_coordinates, dtype="<f8").tobytes(order="C"))
        object_digest.update(np.asarray(object_faces, dtype="<u4").tobytes(order="C"))
        object_minimum = np.min(object_coordinates, axis=0)
        object_maximum = np.max(object_coordinates, axis=0)
        object_inspections.append(
            ThreeMFObjectInspection(
                name=object_element.attrib.get("name", ""),
                vertex_count=len(object_vertices),
                triangle_count=len(object_triangles),
                bounds_mm=(
                    (
                        float(object_minimum[0]),
                        float(object_minimum[1]),
                        float(object_minimum[2]),
                    ),
                    (
                        float(object_maximum[0]),
                        float(object_maximum[1]),
                        float(object_maximum[2]),
                    ),
                ),
                geometry_sha256=object_digest.hexdigest(),
            )
        )
    return ThreeMFInspection(
        unit="millimeter",
        object_count=len(object_names),
        build_item_count=build_item_count,
        components_object_count=len(components_elements),
        component_count=component_count,
        base_material_group_count=len(base_material_groups),
        material_assigned_object_count=material_assigned_object_count,
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
        objects=tuple(object_inspections),
        strict_warning_count=warning_count,
        lib3mf_version=(
            int(wrapper.GetLibraryVersion()[0]),
            int(wrapper.GetLibraryVersion()[1]),
            int(wrapper.GetLibraryVersion()[2]),
        ),
    )
