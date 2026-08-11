from __future__ import annotations

import stat
from types import SimpleNamespace

from topoforge.platforms import stat_result_is_link_like


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
