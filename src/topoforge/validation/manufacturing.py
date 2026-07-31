"""Release-gate evaluation for the default Bambu Lab P2S workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_RELEASE_PROFILE_ID = "bambu-p2s-0.4"


def evaluate_bambu_p2s_release_gate(
    result: Mapping[str, Any],
    *,
    printer_profile_id: str,
) -> dict[str, Any]:
    """Evaluate official Bambu Studio and resolved P2S parameter evidence."""
    metrics = _mapping(result.get("metrics"))
    settings = _mapping(metrics.get("settings"))
    slicer = _mapping(result.get("slicer"))
    checks: list[dict[str, Any]] = []

    def exact(name: str, actual: Any, expected: Any) -> None:
        checks.append(
            {"name": name, "expected": expected, "actual": actual, "passed": actual == expected}
        )

    def close(name: str, actual: Any, expected: float, tolerance: float = 1e-6) -> None:
        try:
            passed = abs(float(actual) - expected) <= tolerance
        except (TypeError, ValueError):
            passed = False
        checks.append({"name": name, "expected": expected, "actual": actual, "passed": passed})

    exact("slicer.name", slicer.get("name"), "BambuStudio")
    exact("slice.status", result.get("status"), "succeeded")
    exact("slice.exit_code", result.get("exit_code"), 0)
    exact("slice.gcode_generated", result.get("gcode_generated"), True)
    exact("machine.internal_profile_id", printer_profile_id, DEFAULT_RELEASE_PROFILE_ID)
    exact("machine.printer_model", settings.get("printer_model"), "Bambu Lab P2S")
    exact(
        "machine.printer_settings_id",
        settings.get("printer_settings_id"),
        "Bambu Lab P2S 0.4 nozzle",
    )
    exact("machine.printer_variant", settings.get("printer_variant"), "0.4")
    close("machine.nozzle_diameter_mm", settings.get("nozzle_diameter_mm"), 0.4)
    close("machine.printable_width_mm", settings.get("printable_width_mm"), 256.0)
    close("machine.printable_depth_mm", settings.get("printable_depth_mm"), 256.0)
    close("machine.printable_height_mm", settings.get("printable_height_mm"), 256.0)
    exact(
        "process.settings_id",
        settings.get("process_settings_id"),
        "0.20mm Standard @BBL P2S",
    )
    close("process.layer_height_mm", settings.get("layer_height_mm"), 0.2)
    close("process.initial_layer_height_mm", settings.get("initial_layer_height_mm"), 0.2)
    exact("process.wall_loops", settings.get("wall_loops"), 2)
    exact("process.top_shell_layers", settings.get("top_shell_layers"), 5)
    exact("process.bottom_shell_layers", settings.get("bottom_shell_layers"), 3)
    close(
        "process.sparse_infill_density_percent",
        settings.get("sparse_infill_density_percent"),
        15.0,
    )
    exact("process.sparse_infill_pattern", settings.get("sparse_infill_pattern"), "grid")
    exact("process.support_enabled", settings.get("support_enabled"), False)
    exact("process.brim_type", settings.get("brim_type"), "auto_brim")
    exact("machine.bed_type", settings.get("bed_type"), "Textured PEI Plate")
    close("machine.bed_temperature_c", settings.get("bed_temperature_c"), 55.0)
    close("machine.nozzle_temperature_c", settings.get("nozzle_temperature_c"), 220.0)
    exact(
        "filament.settings_id",
        settings.get("filament_settings_ids"),
        ["Bambu PLA Basic @BBL P2S"],
    )
    exact("filament.vendor", settings.get("filament_vendor"), "Bambu Lab")
    exact("filament.type", settings.get("filament_type"), "PLA")
    close("filament.density_g_cm3", settings.get("filament_density_g_cm3"), 1.26)
    close("filament.diameter_mm", settings.get("filament_diameter_mm"), 1.75)
    close("filament.flow_ratio", settings.get("filament_flow_ratio"), 0.98)
    exact("slice.out_of_bed", metrics.get("out_of_bed"), False)
    exact("slice.empty_layer_warning", metrics.get("empty_layer_warning"), False)
    exact("slice.floating_region_warning", metrics.get("floating_region_warning"), False)

    required = printer_profile_id == DEFAULT_RELEASE_PROFILE_ID
    parameter_checks_passed = all(check["passed"] for check in checks[4:])
    slice_checks_passed = all(check["passed"] for check in checks[:4]) and all(
        check["passed"] for check in checks[-3:]
    )
    release_gate_passed = required and all(check["passed"] for check in checks)
    return {
        "policy_id": "bambu-p2s-official-release-v1",
        "required": required,
        "required_validator": "BambuStudio",
        "printer_profile_id": printer_profile_id,
        "parameter_checks": checks,
        "parameter_checks_passed": parameter_checks_passed,
        "slice_checks_passed": slice_checks_passed,
        "release_gate_passed": release_gate_passed,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
