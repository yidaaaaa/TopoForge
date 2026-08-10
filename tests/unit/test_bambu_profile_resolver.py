from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_resolver() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "resolve_bambu_profiles.py"
    spec = importlib.util.spec_from_file_location("resolve_bambu_profiles", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_resolver_merges_parent_then_include_then_child(tmp_path: Path) -> None:
    root = tmp_path / "BBL"
    (root / "process").mkdir(parents=True)
    (root / "filament").mkdir()
    _write(root / "machine" / "parent.json", {"name": "parent", "a": 1, "shared": "parent"})
    _write(root / "machine" / "fragment.json", {"name": "fragment", "b": 2, "shared": "include"})
    _write(
        root / "machine" / "child.json",
        {
            "name": "child",
            "inherits": "parent",
            "include": ["fragment"],
            "shared": "child",
        },
    )
    module = _load_resolver()

    resolved = module.resolve("child", module.load_index(root))

    assert resolved["a"] == 1
    assert resolved["b"] == 2
    assert resolved["shared"] == "child"
