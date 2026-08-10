"""Platform-path contracts introduced for Phase 13A."""

from __future__ import annotations

from pathlib import Path

from topoforge.platform_paths import macos_application_data_root
from topoforge.platforms import default_web_state_dir, default_web_workspace_root


def test_macos_web_defaults_use_user_application_support() -> None:
    home = Path("/Users/topoforge tester")

    root = home / "Library" / "Application Support" / "TopoForge"
    assert macos_application_data_root(home=home) == root
    assert default_web_state_dir(system="Darwin", home=home) == root / "state"
    assert default_web_workspace_root(system="Darwin", home=home) == root / "workspaces"
