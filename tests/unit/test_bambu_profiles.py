from __future__ import annotations

import json
from pathlib import Path

import pytest

from topoforge.validation.slicers._bambu_profiles import (
    DEFAULT_FILAMENT_PROFILE,
    DEFAULT_MACHINE_PROFILE,
    DEFAULT_PROCESS_PROFILE,
    load_profile_index,
    prepare_bambu_profiles,
    resolve_profile,
)


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _profile_tree(root: Path) -> Path:
    _write(root / "machine" / "parent.json", {"name": "machine-parent", "a": 1})
    _write(
        root / "machine" / "fragment.json",
        {"name": "machine-fragment", "b": 2, "shared": "fragment"},
    )
    _write(
        root / "machine" / "p2s.json",
        {
            "name": DEFAULT_MACHINE_PROFILE,
            "inherits": "machine-parent",
            "include": ["machine-fragment"],
            "shared": "machine",
        },
    )
    _write(
        root / "process" / "standard.json",
        {"name": DEFAULT_PROCESS_PROFILE, "layer_height": "0.2"},
    )
    _write(
        root / "filament" / "pla.json",
        {"name": DEFAULT_FILAMENT_PROFILE, "filament_type": ["PLA"]},
    )
    _write(root / "filament" / "filament_id_map.json", {"GF001": "PLA"})
    return root


def _executable(path: Path) -> Path:
    executable = path / "Bambu Studio" / "bambu-studio.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic official executable")
    executable.chmod(0o755)
    return executable


def test_prepared_profiles_are_flattened_content_addressed_and_verified(
    tmp_path: Path,
) -> None:
    source = _profile_tree(tmp_path / "官方 profiles" / "BBL")
    executable = _executable(tmp_path)
    cache = tmp_path / "Local AppData" / "TopoForge" / "profiles"

    first = prepare_bambu_profiles(
        source,
        cache,
        executable=executable,
        executable_version="02.07.01.62",
    )
    second = prepare_bambu_profiles(
        source,
        cache,
        executable=executable,
        executable_version="02.07.01.62",
    )

    assert first == second
    machine = json.loads(first.machine_profile.read_text(encoding="utf-8"))
    assert machine["a"] == 1
    assert machine["b"] == 2
    assert machine["shared"] == "machine"
    assert "inherits" not in machine
    assert "include" not in machine
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert first.bundle_dir.name == manifest["bundle_id"]
    assert manifest["executable"]["path"] == str(executable.resolve())
    assert manifest["executable"]["version"] == "02.07.01.62"
    assert manifest["profiles"]["machine"]["name"] == DEFAULT_MACHINE_PROFILE
    assert [item["name"] for item in manifest["profiles"]["machine"]["sources"]] == [
        DEFAULT_MACHINE_PROFILE,
        "machine-parent",
        "machine-fragment",
    ]
    assert manifest["required_checks_passed"] is True

    first.machine_profile.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cached Bambu resolved profile changed"):
        prepare_bambu_profiles(
            source,
            cache,
            executable=executable,
            executable_version="02.07.01.62",
        )


def test_profile_resolver_rejects_cycles_missing_dependencies_and_duplicates(
    tmp_path: Path,
) -> None:
    cycle_root = _profile_tree(tmp_path / "cycle")
    _write(cycle_root / "machine" / "a.json", {"name": "cycle-a", "inherits": "cycle-b"})
    _write(cycle_root / "machine" / "b.json", {"name": "cycle-b", "inherits": "cycle-a"})
    cycle_index = load_profile_index(cycle_root)
    with pytest.raises(ValueError, match="dependency cycle"):
        resolve_profile("cycle-a", cycle_index)

    _write(
        cycle_root / "machine" / "missing.json",
        {"name": "missing-child", "include": ["not-installed"]},
    )
    with pytest.raises(ValueError, match="dependency was not found"):
        resolve_profile("missing-child", load_profile_index(cycle_root))

    duplicate_root = _profile_tree(tmp_path / "duplicate")
    _write(duplicate_root / "process" / "duplicate.json", {"name": DEFAULT_MACHINE_PROFILE})
    with pytest.raises(ValueError, match="duplicate Bambu preset name"):
        load_profile_index(duplicate_root)


def test_profile_resolver_rejects_invalid_dependency_shapes(tmp_path: Path) -> None:
    root = _profile_tree(tmp_path / "invalid")
    _write(
        root / "machine" / "bad.json",
        {"name": "bad-inherits", "inherits": ["machine-parent"]},
    )

    with pytest.raises(ValueError, match="invalid inherits"):
        resolve_profile("bad-inherits", load_profile_index(root))
