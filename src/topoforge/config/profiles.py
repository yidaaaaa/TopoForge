"""Built-in printer profiles; no sampling magic constants outside this module."""

from topoforge.models import PrinterProfile

_PROFILES: dict[str, PrinterProfile] = {
    "generic-fdm-0.4": PrinterProfile(),
    "bambu-p2s-0.4": PrinterProfile(
        profile_id="bambu-p2s-0.4",
        build_volume_mm=(256.0, 256.0, 256.0),
        nozzle_diameter_mm=0.4,
        layer_height_mm=0.2,
        minimum_feature_mm=0.5,
        preferred_mesh_sampling_mm=0.5,
        minimum_base_thickness_mm=2.0,
        connector_tolerance_mm=0.2,
    ),
    "bambu-p2s-0.2": PrinterProfile(
        profile_id="bambu-p2s-0.2",
        build_volume_mm=(256.0, 256.0, 256.0),
        nozzle_diameter_mm=0.2,
        layer_height_mm=0.1,
        minimum_feature_mm=0.3,
        preferred_mesh_sampling_mm=0.35,
        minimum_base_thickness_mm=2.0,
        connector_tolerance_mm=0.15,
    ),
    "resin-generic": PrinterProfile(
        profile_id="resin-generic",
        build_volume_mm=(145.0, 89.0, 175.0),
        nozzle_diameter_mm=0.05,
        layer_height_mm=0.05,
        minimum_feature_mm=0.15,
        preferred_mesh_sampling_mm=0.2,
        minimum_base_thickness_mm=1.5,
        connector_tolerance_mm=0.1,
    ),
}


def get_printer_profile(profile_id: str) -> PrinterProfile:
    """Return a defensive copy of a built-in profile."""
    try:
        return _PROFILES[profile_id].model_copy(deep=True)
    except KeyError as exc:
        choices = ", ".join(sorted(_PROFILES))
        msg = f"Unknown printer profile '{profile_id}'. Available profiles: {choices}"
        raise ValueError(msg) from exc


def list_printer_profiles() -> list[PrinterProfile]:
    """List built-in printer profiles in stable identifier order."""
    return [_PROFILES[key].model_copy(deep=True) for key in sorted(_PROFILES)]
