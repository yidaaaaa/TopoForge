from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from topoforge.validation.slicers import (
    BambuStudioAdapter,
    CommandExecution,
    SlicerProfile,
    SliceStatus,
    parse_gcode_generator,
    parse_gcode_metrics,
)

BAMBU_GCODE = """; HEADER_BLOCK_START
; BambuStudio 02.07.01.62
; model printing time: 6h 42m 13s; total estimated time: 6h 49m 15s
; total layer number: 224
; total filament length [mm] : 69624.45
; total filament volume [cm^3] : 167466.43
; total filament weight [g] : 211.01
; filament_density: 1.26
; filament_diameter: 1.75
; HEADER_BLOCK_END
; printer_model = Bambu Lab P2S
; printer_settings_id = Bambu Lab P2S 0.4 nozzle
; printer_variant = 0.4
; nozzle_diameter = 0.4
; printable_area = 0x0,256x0,256x256,0x256
; printable_height = 256
; print_settings_id = 0.20mm Standard @BBL P2S
; filament_settings_id = "Bambu PLA Basic @BBL P2S"
; filament_vendor = "Bambu Lab"
; filament_type = PLA
; filament_flow_ratio = 0.98
; filament_max_volumetric_speed = 21
; layer_height = 0.2
; initial_layer_print_height = 0.2
; wall_loops = 2
; top_shell_layers = 5
; bottom_shell_layers = 3
; sparse_infill_density = 15%
; sparse_infill_pattern = grid
; enable_support = 0
; support_type = tree(auto)
; brim_type = auto_brim
; brim_width = 5
; curr_bed_type = Textured PEI Plate
; textured_plate_temp = 55
; nozzle_temperature = 220
"""


class FakeBambuRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandExecution:
        del timeout_seconds, cwd
        normalized = tuple(command)
        self.calls.append((normalized, env))
        if "--help" in normalized:
            return CommandExecution(0, "BambuStudio-02.07.01.62:", "", 0.01)
        output_dir = Path(normalized[normalized.index("--outputdir") + 1])
        output_dir.joinpath("plate_1.gcode").write_text(BAMBU_GCODE, encoding="utf-8")
        output_dir.joinpath("result.json").write_text(
            json.dumps(
                {
                    "error_string": "Success.",
                    "return_code": 0,
                    "sliced_plates": [{"warning_message": ""}],
                }
            ),
            encoding="utf-8",
        )
        return CommandExecution(0, "official slice succeeded", "", 0.2)


def _executable(path: Path) -> Path:
    executable = path / "BambuStudio.AppImage"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_bambu_adapter_uses_normative_p2s_command_and_parses_settings(tmp_path: Path) -> None:
    runner = FakeBambuRunner()
    model = tmp_path / "terrain.3mf"
    model.write_bytes(b"3mf")
    machine = tmp_path / "machine.json"
    process = tmp_path / "process.json"
    filament = tmp_path / "filament.json"
    for path in (machine, process, filament):
        path.write_text("{}", encoding="utf-8")

    result = BambuStudioAdapter(_executable(tmp_path), runner=runner).slice(
        model,
        tmp_path / "terrain.gcode",
        profile=SlicerProfile(settings=(machine, process), filaments=(filament,)),
    )

    assert result.status is SliceStatus.SUCCEEDED
    assert result.slicer.name == "BambuStudio"
    assert result.slicer.version == "02.07.01.62"
    assert result.metrics.layer_count == 224
    assert result.metrics.filament_used_g == 211.01
    assert result.metrics.settings.printer_model == "Bambu Lab P2S"
    assert result.metrics.settings.printable_width_mm == 256.0
    assert result.metrics.settings.bed_type == "Textured PEI Plate"
    command, environment = runner.calls[-1]
    assert "--normative-check" in command
    assert "--ensure-on-bed" in command
    assert "--load-defaultfila" in command
    assert command[command.index("--curr-bed-type") + 1] == "Textured PEI Plate"
    assert environment is not None
    assert environment["APPIMAGE_EXTRACT_AND_RUN"] == "1"


def test_bambu_gcode_parser_captures_release_parameters() -> None:
    metrics = parse_gcode_metrics(BAMBU_GCODE)

    assert parse_gcode_generator(BAMBU_GCODE) == ("BambuStudio", "02.07.01.62")
    assert metrics.estimated_time_seconds == 24133
    assert metrics.filament_used_mm == 69624.45
    assert metrics.filament_used_cm3 == 167.46643
    assert metrics.settings.process_settings_id == "0.20mm Standard @BBL P2S"
    assert metrics.settings.filament_settings_ids == ("Bambu PLA Basic @BBL P2S",)
    assert metrics.settings.support_enabled is False
