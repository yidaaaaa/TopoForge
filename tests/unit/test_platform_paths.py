"""Cross-platform path contracts."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

from topoforge.platform_paths import macos_application_data_root
from topoforge.platforms import (
    default_web_input_roots,
    default_web_state_dir,
    default_web_workspace_root,
    stat_result_is_link_like,
)


def test_stat_result_link_like_contract_covers_symlinks_and_windows_reparse_points() -> None:
    regular_directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o755,
        st_file_attributes=0,
    )
    posix_symlink = SimpleNamespace(
        st_mode=stat.S_IFLNK | 0o777,
        st_file_attributes=0,
    )
    windows_reparse_directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o755,
        st_file_attributes=0x0400,
    )

    assert stat_result_is_link_like(regular_directory) is False
    assert stat_result_is_link_like(posix_symlink) is True
    assert stat_result_is_link_like(windows_reparse_directory) is True


def test_macos_web_defaults_use_user_application_support() -> None:
    home = Path("/Users/topoforge tester")

    root = home / "Library" / "Application Support" / "TopoForge"
    assert macos_application_data_root(home=home) == root
    assert default_web_state_dir(system="Darwin", home=home) == root / "state"
    assert default_web_workspace_root(system="Darwin", home=home) == root / "workspaces"


def test_macos_input_browser_defaults_to_home_not_app_working_directory() -> None:
    home = Path("/Users/地形 maker")
    app_working_directory = Path("/Applications/TopoForge.app/Contents/MacOS")

    assert default_web_input_roots(system="Darwin", home=home, cwd=app_working_directory) == (home,)
