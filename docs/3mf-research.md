# 3MF implementation research

Checked: **2026-07-31 UTC**.

## Decision

Use the 3MF Consortium's official **`lib3mf==2.5.0` Python package** as the
TopoForge 3MF writer and strict re-reader on supported platforms.

Why:

- lib3mf is the 3MF Consortium's reference open-source implementation and
  provides reading, writing, validation-related APIs, multiple objects, build
  items, transforms, units, metadata, colors/material resources, components,
  and current extensions.
- Version 2.5.0 was released 2026-02-24 and is published both on the official
  GitHub release and PyPI. The GitHub and PyPI manylinux wheel SHA-256 values
  matched in this check:
  `b4c00033c47cfeac93b7daa069fb46e8dea4391d5522b79cd6e9f6af75e33013`.
- The official wheel imported and executed under Python 3.12.3 on Linux x86-64.
- lib3mf is BSD-2-Clause, compatible with TopoForge's Apache-2.0 code license.
- The current published 3MF Core specification is **1.4.0** (2025-02-06), and
  the official repository explicitly recommends lib3mf as the open-source
  implementation.

Add the package through this project's PEP 621 dependency workflow:

```bash
uv add "lib3mf==2.5.0"
```

The resulting `pyproject.toml` entry belongs in the existing
`[project].dependencies` array, for example:

```toml
[project]
dependencies = [
  "lib3mf==2.5.0",
]
```

Pin the resolved wheel hash in `uv.lock`. Do not substitute the separately
maintained `py-lib3mf` PyPI project by name: it is a third-party repackaging
route and is not needed on the supported Linux x86-64 path.

## Platform status

Verified official 2.5.0 wheels:

- Linux: `py3-none-manylinux2014_x86_64`
- macOS: `py3-none-macosx_10_9_universal2`
- Windows: `py3-none-win_amd64`

Unresolved/platform caveat:

- The official release did not provide a Linux AArch64 wheel. Linux ARM64
  packaging needs a reproducible source build from the official SDK/source or
  a separately reviewed package. Do not silently use a third-party wheel in a
  release build.

Sources:

- [3MF Consortium lib3mf repository](https://github.com/3MFConsortium/lib3mf)
- [lib3mf 2.5.0 release](https://github.com/3MFConsortium/lib3mf/releases/tag/v2.5.0)
- [Official `lib3mf` PyPI record](https://pypi.org/project/lib3mf/2.5.0/)
- [lib3mf documentation](https://lib3mf.readthedocs.io/)
- [3MF Core specification repository, tag 1.4.0](https://github.com/3MFConsortium/spec_core/tree/1.4.0)
- [3MF conformance test suites](https://github.com/3MFConsortium/test_suites)
- [3MF sample files](https://github.com/3MFConsortium/3mf-samples)

## Required TopoForge package content

For a normal terrain build, the model must contain:

1. Root model unit explicitly set to `millimeter`.
2. One named mesh object per terrain tile or independent overlay.
3. Stable part numbers and object names.
4. One build item per top-level printable instance with an explicit
   transform. Child objects inside a components assembly are referenced by
   component instances; they do not each need a root build item.
5. Root metadata containing at least application/version, build id,
   configuration digest, provenance digest, dataset id/version, license id,
   CRS/vertical datum summary, and model dimensions.
6. Optional color/material resources only when they communicate an actual
   manufacturing intent; preview-only color belongs in GLB.

The Core 1.4.0 specification requires a ZIP/OPC package with a 3D Model part,
resources, and a build. It defines millimeter as the default, but TopoForge must
still write the unit explicitly. Mesh objects of type `model` must have
manifold edges, consistent triangle orientation, and outward-facing normals;
file-format validity does not repair invalid geometry.

Recommended mapping:

| TopoForge concept | 3MF representation |
| --- | --- |
| Single terrain | Named mesh object + build item |
| Tiled terrain | One named mesh object/build item per tile |
| Separate insert/overlay | Independent top-level object/build item, or a child component inside an assembly when relative placement must be fixed |
| Components assembly | Named mesh/component resources + deterministic component-instance UUIDs + one build item for each top-level assembly instance |
| Millimetre manufacturing coordinates | `model.SetUnit(ModelUnit.MilliMeter)` |
| Provenance summary | Namespaced root metadata with `preserve=true` |
| Human title/application/license | Core well-known metadata where applicable |
| Stable instance placement | Build-item transform and deterministic UUID |

Do not embed the full downloaded DEM in the manufacturing package. Put complete
provenance in the adjacent `provenance.json`; keep only a compact, digest-linked
summary in 3MF metadata.

## Reproducibility requirement

lib3mf automatically generates production-extension UUIDs for objects, build
items, component instances, and the build. If those UUIDs are left automatic,
two otherwise identical writes are not byte-identical. A file without a
components assembly has no component-instance UUIDs, but TopoForge must set them
as soon as it emits components.

Verified result:

```text
Automatic UUID run A:
edaa476fe12a1ceb4759db8449de4c09196b9f49eaa9fedeb3d4654d7e61e8d2  1898 bytes

Automatic UUID run B:
59061d065239d993becf8ea77c802c3f7687c454b593536f1efbcc4afd268112  1897 bytes

Unpacked diff: only object, build-item, and build p:UUID values differed.
```

TopoForge must derive UUIDv5 values from stable inputs, for example:

```text
namespace = a fixed TopoForge UUID
build UUID name = resolved-config SHA-256 + geometry SHA-256
object UUID name = build digest + object role + stable object/tile id
components-object UUID name = build digest + stable assembly id
component-instance UUID name = assembly UUID + child object UUID + stable child instance id/ordinal + transform
build-item UUID name = top-level object/assembly UUID + stable instance id/ordinal + transform
```

Set them before writing:

```python
model.SetBuildUUID(build_uuid)
mesh_object.SetUUID(object_uuid)
components_object.SetUUID(assembly_uuid)
component_instance.SetUUID(component_instance_uuid)
build_item.SetUUID(build_item_uuid)
```

With fixed UUIDs, two writes in the self-contained executed test below were
byte-identical:

```text
3f94ff8e44f640625f547072de1f85427e8fe11f6bf511ce42fb7c0f184fc670  a.3mf
3f94ff8e44f640625f547072de1f85427e8fe11f6bf511ce42fb7c0f184fc670  b.3mf
cmp_exit=0
```

A UUID name must include a stable logical instance id or canonical ordinal in
addition to the transform. Two intentional copies can share both an object and
a transform; deriving the UUID only from those values would collide.

The writer also used fixed ZIP entry timestamps and ordering in this test. Keep
a golden test because those implementation details are not a Core requirement
and can change in a future lib3mf release.

## Minimal implementation shape

The exporter should be a thin serializer over already validated mesh arrays:

```python
import lib3mf
from lib3mf import get_wrapper

wrapper = get_wrapper()
model = wrapper.CreateModel()
model.SetUnit(lib3mf.ModelUnit.MilliMeter)
model.SetLanguage("en-US")
model.SetBuildUUID(stable_build_uuid)

metadata = model.GetMetaDataGroup()
metadata.AddMetaData(
    "https://topoforge.dev/ns/3mf/1",
    "provenance-sha256",
    provenance_sha256,
    "xs:string",
    True,
)

obj = model.AddMeshObject()
obj.SetName(object_name)
obj.SetPartNumber(part_number)
obj.SetUUID(stable_object_uuid)
obj.SetGeometry(lib3mf_positions, lib3mf_triangles)

# Choose exactly one top-level representation for this logical instance.
if use_components:
    assembly = model.AddComponentsObject()
    assembly.SetName(assembly_name)
    assembly.SetUUID(stable_assembly_uuid)
    component = assembly.AddComponent(obj, child_transform)
    component.SetUUID(stable_component_instance_uuid)
    assembly_item = model.AddBuildItem(assembly, assembly_transform)
    assembly_item.SetUUID(stable_assembly_item_uuid)
else:
    item = model.AddBuildItem(obj, transform)
    item.SetUUID(stable_item_uuid)

model.QueryWriter("3mf").WriteToFile(output_path)
```

Implementation rules:

- Convert NumPy vertices/faces in bounded batches if large arrays make wrapper
  object creation expensive; benchmark representative terrain sizes before
  optimizing.
- Reject NaN/Inf, out-of-range triangle indices, empty objects, non-positive
  volume, and invalid topology before calling lib3mf.
- Use canonical object, component, and build-item ordering and canonical float
  preparation so stable UUIDs are not masking geometric nondeterminism.
- Assign and validate a unique stable logical id/ordinal for every top-level and
  component instance, even when multiple instances use the same object and
  transform.
- Use a TopoForge namespace for non-core metadata names. Core metadata without a
  namespace is restricted to names defined by the specification.
- Keep native lib3mf exceptions at the exporter boundary and translate them to
  a typed export error containing the object id and output path.

## Validation stack

Passing a lib3mf write call is necessary but not sufficient. A release build
must pass all of these layers:

1. **Geometry validation before export**: finite vertices, valid indices,
   manifold edges, consistent winding, outward normals, positive volume,
   expected components, no degenerates/duplicates, and dimension tolerance.
2. **Strict lib3mf re-read** in a fresh model:
   - `reader.SetStrictModeActive(True)`
   - `reader.ReadFromFile(path)`
   - assert `reader.GetWarningCount() == 0`, or record explicitly accepted
     warnings
   - assert root unit, metadata, object count/names, vertex/triangle counts,
     component graph, object/component/build/build-item UUID uniqueness,
     top-level build-item count, and transforms.
3. **Package inspection**: non-empty ZIP, expected OPC parts and relationships,
   no path traversal, no encrypted entries, supported compression, and no
   unexpected external references.
4. **Conformance regression** against relevant official 3MF Consortium test
   suites/samples.
5. **Independent slicer test** with OrcaSlicer or a compatible slicer, recording
   exact version, command, exit status, literal output, parsed dimensions, and
   whether G-code was produced.

Strict re-reading checks format semantics; it does not prove the terrain is
printable, and it does not replace a real slicer invocation.

## Alternatives considered

### Trimesh 3MF exporter

Current Trimesh has a native `export_3MF` implementation and can write multiple
scene objects, names, build transforms, and millimetre units. It remains useful
as the geometry library and as an additional independent loader.

It is not selected as the manufacturing-authoritative writer because the
reviewed exporter:

- generates random production UUIDs;
- writes geometry/build structure but does not expose the required TopoForge
  root provenance metadata path;
- does not serialize the richer color/material intent needed for later phases;
- is not the Consortium reference implementation used for strict re-read.

Source reviewed:
[Trimesh `threemf.py`](https://github.com/mikedh/trimesh/blob/main/trimesh/exchange/threemf.py).

### Hand-written ZIP/OPC/XML

A small deterministic Core-only writer is technically possible with `zipfile`
and a hardened XML library. It would give complete byte-level control and could
serve as a contingency for an unsupported platform.

It is not the primary route because TopoForge would then own namespace/XSD/OPC
conformance, extension behavior, security hardening, compatibility testing, and
future specification updates. If implemented as a fallback, its output must
still strict-read with lib3mf and pass the same slicer evidence.

### Third-party `py-lib3mf`

`py-lib3mf` 2.5.0 provides CPython-specific Linux AArch64 wheels and may be a
useful lead for ARM64 research. It is owned and packaged outside the 3MF
Consortium release path, so it is not the default dependency. Any future use
requires source, license, binary, and release-process review.

## Executed verification record

Environment:

```text
Python 3.12.3
Linux x86_64
glibc 2.35
lib3mf 2.5.0
```

The following commands are complete and independently runnable. They preserve
the wheel's official filename because `pip` rejects an arbitrarily renamed
wheel, verify its SHA-256 before installation, create two packages, strict-read
one in a fresh model, inspect the OPC members, and compare both files byte for
byte.

Dependency acquisition and API probe:

```bash
mkdir -p /tmp/topoforge-lib3mf-repro
curl -fsSL \
  "https://github.com/3MFConsortium/lib3mf/releases/download/v2.5.0/lib3mf-2.5.0-py3-none-manylinux2014_x86_64.whl" \
  -o /tmp/topoforge-lib3mf-repro/lib3mf-2.5.0-py3-none-manylinux2014_x86_64.whl
printf 'curl_exit=%s\n' "$?"
sha256sum \
  /tmp/topoforge-lib3mf-repro/lib3mf-2.5.0-py3-none-manylinux2014_x86_64.whl
python3.12 -m venv /tmp/topoforge-lib3mf-repro/venv
printf 'venv_exit=%s\n' "$?"
/tmp/topoforge-lib3mf-repro/venv/bin/python -m pip install \
  --disable-pip-version-check \
  /tmp/topoforge-lib3mf-repro/lib3mf-2.5.0-py3-none-manylinux2014_x86_64.whl \
  >/tmp/topoforge-lib3mf-repro/pip.log
printf 'pip_exit=%s\n' "$?"
/tmp/topoforge-lib3mf-repro/venv/bin/python - <<'PY'
from lib3mf import get_wrapper

wrapper = get_wrapper()
model = wrapper.CreateModel()
components_object = model.AddComponentsObject()
mesh = model.AddMeshObject()
component = components_object.AddComponent(mesh, wrapper.GetIdentityTransform())
print("wrapper library version", wrapper.GetLibraryVersion())
print("ComponentsObject UUID API", [name for name in dir(components_object) if "UUID" in name])
print("Component UUID API", [name for name in dir(component) if "UUID" in name])
PY
printf 'import_exit=%s\n' "$?"
```

Literal dependency/probe result:

```text
curl_exit=0
b4c00033c47cfeac93b7daa069fb46e8dea4391d5522b79cd6e9f6af75e33013  /tmp/topoforge-lib3mf-repro/lib3mf-2.5.0-py3-none-manylinux2014_x86_64.whl
venv_exit=0
pip_exit=0
wrapper library version (2, 5, 0)
ComponentsObject UUID API ['GetUUID', 'SetUUID']
Component UUID API ['GetUUID', 'SetUUID']
import_exit=0
```

The complete synthetic round-trip harness is preserved in this repository
record rather than only in `/tmp`:

```bash
cat > /tmp/topoforge-lib3mf-repro/repro.py <<'PY'
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import lib3mf
from lib3mf import get_wrapper

OUT = Path('/tmp/topoforge-lib3mf-repro')
OUT.mkdir(exist_ok=True)
A = OUT / 'a.3mf'
B = OUT / 'b.3mf'


def position(x: float, y: float, z: float) -> lib3mf.Position:
    value = lib3mf.Position()
    value.Coordinates[:] = (x, y, z)
    return value


def triangle(a: int, b: int, c: int) -> lib3mf.Triangle:
    value = lib3mf.Triangle()
    value.Indices[:] = (a, b, c)
    return value


def add_box(model, wrapper, *, name, size, tx, object_uuid, item_uuid):
    sx, sy, sz = size
    obj = model.AddMeshObject()
    obj.SetName(name)
    obj.SetPartNumber(name)
    obj.SetUUID(object_uuid)
    vertices = [
        position(0, 0, 0), position(sx, 0, 0),
        position(sx, sy, 0), position(0, sy, 0),
        position(0, 0, sz), position(sx, 0, sz),
        position(sx, sy, sz), position(0, sy, sz),
    ]
    faces = [
        triangle(*face) for face in (
            (2, 1, 0), (0, 3, 2), (4, 5, 6), (6, 7, 4),
            (0, 1, 5), (5, 4, 0), (2, 3, 7), (7, 6, 2),
            (1, 2, 6), (6, 5, 1), (3, 0, 4), (4, 7, 3),
        )
    ]
    obj.SetGeometry(vertices, faces)
    transform = wrapper.GetIdentityTransform()
    transform.Fields[3][0] = tx
    item = model.AddBuildItem(obj, transform)
    item.SetUUID(item_uuid)


def write(path: Path) -> None:
    wrapper = get_wrapper()
    model = wrapper.CreateModel()
    model.SetUnit(lib3mf.ModelUnit.MilliMeter)
    model.SetLanguage('en-US')
    model.SetBuildUUID('e0645b6b-7f04-5fa7-9e3f-d2b1ce1aec99')
    model.GetMetaDataGroup().AddMetaData(
        'https://topoforge.example/ns', 'dataset',
        'synthetic-test', 'xs:string', True,
    )
    add_box(
        model, wrapper, name='terrain', size=(10, 20, 3), tx=0,
        object_uuid='ecbf7953-fb2a-5b1c-89c8-73e3148b795d',
        item_uuid='85da81ff-b611-543f-96ac-237bf125d399',
    )
    add_box(
        model, wrapper, name='overlay', size=(2, 2, 1), tx=12,
        object_uuid='79d142f8-b65d-500c-942a-d99185b9f172',
        item_uuid='1baf9814-5a2e-58e3-8487-7aa82c827369',
    )
    model.QueryWriter('3mf').WriteToFile(str(path))


def strict_read(path: Path) -> None:
    wrapper = get_wrapper()
    model = wrapper.CreateModel()
    reader = model.QueryReader('3mf')
    reader.SetStrictModeActive(True)
    reader.ReadFromFile(str(path))
    meshes = []
    objects = model.GetMeshObjects()
    while objects.MoveNext():
        obj = objects.GetCurrentMeshObject()
        meshes.append((obj.GetName(), obj.GetVertexCount(), obj.GetTriangleCount()))
    build_items = 0
    items = model.GetBuildItems()
    while items.MoveNext():
        build_items += 1
    outbox = model.GetOutbox()
    minimum = list(outbox.MinCoordinate)
    maximum = list(outbox.MaxCoordinate)
    members = zipfile.ZipFile(path).namelist()
    print('strict', reader.GetStrictModeActive())
    print('warnings', reader.GetWarningCount())
    print('unit', model.GetUnit().name)
    print('meshes', meshes)
    print('build_items', build_items)
    print('outbox', minimum, '->', maximum)
    print('zip', members)
    assert reader.GetWarningCount() == 0
    assert model.GetUnit() == lib3mf.ModelUnit.MilliMeter
    assert meshes == [('terrain', 8, 12), ('overlay', 8, 12)]
    assert build_items == 2
    assert minimum == [0.0, 0.0, 0.0]
    assert maximum == [14.0, 20.0, 3.0]
    assert members == ['3D/3dmodel.model', '[Content_Types].xml', '_rels/.rels']


for output in (A, B):
    write(output)
strict_read(A)
for output in (A, B):
    print(hashlib.sha256(output.read_bytes()).hexdigest(), output)
assert A.read_bytes() == B.read_bytes()
PY
/tmp/topoforge-lib3mf-repro/venv/bin/python \
  /tmp/topoforge-lib3mf-repro/repro.py
py_exit=$?
printf 'python_exit=%s\n' "$py_exit"
cmp -s /tmp/topoforge-lib3mf-repro/a.3mf \
  /tmp/topoforge-lib3mf-repro/b.3mf
printf 'cmp_exit=%s\n' "$?"
```

Literal round-trip result:

```text
strict True
warnings 0
unit MilliMeter
meshes [('terrain', 8, 12), ('overlay', 8, 12)]
build_items 2
outbox [0.0, 0.0, 0.0] -> [14.0, 20.0, 3.0]
zip ['3D/3dmodel.model', '[Content_Types].xml', '_rels/.rels']
3f94ff8e44f640625f547072de1f85427e8fe11f6bf511ce42fb7c0f184fc670 /tmp/topoforge-lib3mf-repro/a.3mf
3f94ff8e44f640625f547072de1f85427e8fe11f6bf511ce42fb7c0f184fc670 /tmp/topoforge-lib3mf-repro/b.3mf
python_exit=0
cmp_exit=0
```

## Remaining work

- Benchmark lib3mf memory/time with a representative high-triangle terrain and
  multi-tile scene.
- Confirm Linux ARM64 packaging policy.
- Define the exact TopoForge metadata namespace and stable UUIDv5 namespace.
- Add material/color tests only when multi-material manufacturing behavior is
  implemented.
- Execute and record OrcaSlicer re-open/slice tests for the actual TopoForge
  terrain output; the temporary box round trip does not satisfy that gate.
