import pytest

from topoforge.config import DEFAULT_PRINTER_PROFILE_ID, get_printer_profile
from topoforge.models import BuildConfig, PrinterProfile


def test_default_printer_is_bambu_p2s(tmp_path) -> None:
    profile = PrinterProfile()
    config = BuildConfig(dem_path=tmp_path / "dem.tif", output_dir=tmp_path / "out")

    assert DEFAULT_PRINTER_PROFILE_ID == "bambu-p2s-0.4"
    assert profile.profile_id == DEFAULT_PRINTER_PROFILE_ID
    assert profile.build_volume_mm == (256.0, 256.0, 256.0)
    assert config.printer_profile.profile_id == DEFAULT_PRINTER_PROFILE_ID


def test_generic_profile_remains_explicitly_generic() -> None:
    profile = get_printer_profile("generic-fdm-0.4")

    assert profile.profile_id == "generic-fdm-0.4"
    assert profile.build_volume_mm == (220.0, 220.0, 250.0)


def test_build_config_rejects_zero_output_formats(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        BuildConfig(
            dem_path=tmp_path / "dem.tif",
            output_dir=tmp_path / "out",
            output_formats=[],
        )
