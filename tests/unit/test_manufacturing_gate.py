from topoforge.validation.manufacturing import evaluate_bambu_p2s_release_gate


def _result() -> dict:
    return {
        "status": "succeeded",
        "slicer": {"name": "BambuStudio", "version": "02.07.01.62"},
        "exit_code": 0,
        "gcode_generated": True,
        "metrics": {
            "out_of_bed": False,
            "empty_layer_warning": False,
            "floating_region_warning": False,
            "settings": {
                "printer_model": "Bambu Lab P2S",
                "printer_settings_id": "Bambu Lab P2S 0.4 nozzle",
                "printer_variant": "0.4",
                "nozzle_diameter_mm": 0.4,
                "printable_width_mm": 256.0,
                "printable_depth_mm": 256.0,
                "printable_height_mm": 256.0,
                "process_settings_id": "0.20mm Standard @BBL P2S",
                "filament_settings_ids": ["Bambu PLA Basic @BBL P2S"],
                "layer_height_mm": 0.2,
                "initial_layer_height_mm": 0.2,
                "wall_loops": 2,
                "top_shell_layers": 5,
                "bottom_shell_layers": 3,
                "sparse_infill_density_percent": 15.0,
                "sparse_infill_pattern": "grid",
                "support_enabled": False,
                "brim_type": "auto_brim",
                "bed_type": "Textured PEI Plate",
                "bed_temperature_c": 55.0,
                "nozzle_temperature_c": 220.0,
                "filament_vendor": "Bambu Lab",
                "filament_type": "PLA",
                "filament_density_g_cm3": 1.26,
                "filament_diameter_mm": 1.75,
                "filament_flow_ratio": 0.98,
            },
        },
    }


def test_official_bambu_p2s_evidence_passes_release_gate() -> None:
    gate = evaluate_bambu_p2s_release_gate(_result(), printer_profile_id="bambu-p2s-0.4")

    assert gate["parameter_checks_passed"] is True
    assert gate["slice_checks_passed"] is True
    assert gate["release_gate_passed"] is True


def test_false_200_mm_p2s_and_prusa_results_are_rejected() -> None:
    wrong_bed = _result()
    wrong_bed["metrics"]["settings"]["printable_width_mm"] = 200.0
    prusa = _result()
    prusa["slicer"]["name"] = "PrusaSlicer"

    assert not evaluate_bambu_p2s_release_gate(wrong_bed, printer_profile_id="bambu-p2s-0.4")[
        "release_gate_passed"
    ]
    assert not evaluate_bambu_p2s_release_gate(prusa, printer_profile_id="bambu-p2s-0.4")[
        "release_gate_passed"
    ]
