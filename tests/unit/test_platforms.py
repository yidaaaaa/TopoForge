from pathlib import Path

from topoforge.platforms import (
    default_web_input_roots,
    default_web_state_dir,
    default_web_workspace_root,
    windows_application_data_root,
)


def test_windows_web_defaults_use_local_application_data(tmp_path: Path) -> None:
    local_app_data = tmp_path / "Local AppData" / "制作者"
    environment = {"LOCALAPPDATA": str(local_app_data)}

    assert windows_application_data_root(environ=environment) == local_app_data / "TopoForge"
    assert default_web_state_dir(system="Windows", environ=environment) == (
        local_app_data / "TopoForge" / "state"
    )
    assert default_web_workspace_root(system="Windows", environ=environment) == (
        local_app_data / "TopoForge" / "workspaces"
    )


def test_windows_web_defaults_have_a_home_fallback(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "Maker"

    assert windows_application_data_root(environ={}, home=home) == (
        home / "AppData" / "Local" / "TopoForge"
    )
    assert default_web_input_roots(system="Windows", home=home) == (home,)


def test_posix_web_defaults_remain_backward_compatible(tmp_path: Path) -> None:
    assert default_web_state_dir(system="Linux") == Path("~/.topoforge/web")
    assert default_web_workspace_root(system="Linux") == Path("topoforge-workspaces")
    assert default_web_input_roots(system="Linux", cwd=tmp_path) == (tmp_path,)
