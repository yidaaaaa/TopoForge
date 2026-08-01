"""Verify the retained-data Phase 7 overlay release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from topoforge.exporters.three_mf import inspect_3mf
from topoforge.overlays import verify_overlay_bundle


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _diagnostic_count(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def _artifact_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--slice-json", type=Path, required=True)
    parser.add_argument("--machine-profile", type=Path, required=True)
    parser.add_argument("--process-profile", type=Path, required=True)
    parser.add_argument("--filament-profile", type=Path, required=True)
    parser.add_argument("--bambu-executable", type=Path, required=True)
    parser.add_argument("--gongga-build-log", type=Path, required=True)
    parser.add_argument("--gongga-reopen-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, action="append", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    primary = args.primary.resolve()
    repeat = args.repeat.resolve()
    source_bundle = args.source_bundle.resolve()
    slice_json_path = args.slice_json.resolve()
    slice_result = _load_json(slice_json_path)
    primary_manifest = _load_json(primary / "overlay_manifest.json")
    repeat_manifest = _load_json(repeat / "overlay_manifest.json")
    primary_validation = _load_json(primary / "validation.json")

    primary_summary = verify_overlay_bundle(primary, source_bundle)
    repeat_summary = verify_overlay_bundle(repeat, source_bundle)
    _require(primary_summary == repeat_summary, "primary and repeat verification summaries differ")
    _require(
        primary_manifest["sha256"] == repeat_manifest["sha256"],
        "primary and repeat overlay artifact hashes differ",
    )
    _require(
        (primary / "overlay_manifest.json").read_bytes()
        == (repeat / "overlay_manifest.json").read_bytes(),
        "primary and repeat manifests are not byte-identical",
    )

    model_path = primary / str(primary_manifest["artifacts"]["model_3mf"])
    inspection = inspect_3mf(model_path)
    expected_object_count = len(primary_validation["layers"]) + 1
    _require(inspection.object_count == expected_object_count, "unexpected 3MF mesh count")
    _require(inspection.build_item_count == 1, "3MF must have one top-level build item")
    _require(inspection.components_object_count == 1, "3MF must have one components object")
    _require(inspection.component_count == expected_object_count, "3MF component count changed")
    _require(inspection.base_material_group_count == 1, "3MF material group count changed")
    _require(
        inspection.material_assigned_object_count == expected_object_count,
        "not every 3MF mesh has a material assignment",
    )
    _require(inspection.strict_warning_count == 0, "strict 3MF reader emitted warnings")

    metrics = slice_result["metrics"]
    settings = metrics["settings"]
    _require(slice_result["status"] == "succeeded", "official Bambu slice did not succeed")
    _require(slice_result["exit_code"] == 0, "official Bambu slice exit code is not zero")
    _require(slice_result["gcode_generated"] is True, "official Bambu slice made no G-code")
    _require(metrics["floating_region_warning"] is False, "slice reports a floating region")
    _require(metrics["empty_layer_warning"] is False, "slice reports an empty layer")
    _require(metrics["out_of_bed"] is False, "slice reports out-of-bed geometry")
    _require(metrics["support_material"] is False, "slice unexpectedly enables support")
    _require(settings["printer_model"] == "Bambu Lab P2S", "wrong printer model")
    _require(settings["printer_variant"] == "0.4", "wrong nozzle variant")
    _require(settings["nozzle_diameter_mm"] == 0.4, "wrong nozzle diameter")
    _require(settings["printable_width_mm"] == 256, "wrong printable width")
    _require(settings["printable_depth_mm"] == 256, "wrong printable depth")
    _require(settings["printable_height_mm"] == 256, "wrong printable height")
    _require(settings["support_enabled"] is False, "resolved P2S support setting is enabled")

    gcode_path = Path(str(slice_result["output_gcode"])).resolve()
    _require(gcode_path.is_file(), "slice JSON references missing G-code")
    _require(
        gcode_path.stat().st_size == slice_result["gcode_size_bytes"],
        "G-code size differs from the slice report",
    )
    gcode_text = gcode_path.read_text(encoding="utf-8", errors="replace")
    slice_stdout = str(slice_result["stdout"])
    machine_profile = _load_json(args.machine_profile)
    machine_end_gcode = str(machine_profile.get("machine_end_gcode", ""))
    t65535_sequence = "M620 S65535\nT65535\n"
    _require(
        t65535_sequence in machine_end_gcode,
        "official P2S machine profile no longer contains the reviewed AMS unload sentinel",
    )
    _require(
        re.search(r"(?m)^T65535\r?$", gcode_text) is not None,
        "generated G-code no longer contains the reviewed AMS unload sentinel",
    )
    _require(
        "Invalid T command (T65535)" in slice_stdout,
        "expected Bambu Studio sentinel diagnostic is absent; review baseline classification",
    )

    gongga_build_text = args.gongga_build_log.read_text(encoding="utf-8", errors="replace")
    gongga_reopen_text = args.gongga_reopen_log.read_text(encoding="utf-8", errors="replace")
    _require(
        "Invalid T command (T65535)" in gongga_build_text
        and "Invalid T command (T65535)" in gongga_reopen_text,
        "retained official P2S baseline no longer contains the sentinel diagnostic",
    )
    _require(
        "floating" not in slice_stdout.casefold(),
        "raw Bambu output contains a floating diagnostic",
    )

    terrain_roles = primary_validation["source_terrain_sha256_before"]
    for name, expected in terrain_roles.items():
        _require(_sha256(source_bundle / name) == expected, f"source terrain changed: {name}")
    _require(
        primary_validation["source_terrain_sha256_before"]
        == primary_validation["source_terrain_sha256_after"],
        "overlay report records a terrain mutation",
    )

    report = {
        "schema_version": "topoforge-phase7-overlay-release-verification-v1",
        "status": "passed",
        "topoforge_version": primary_manifest["topoforge_version"],
        "source_bundle": str(source_bundle),
        "primary_overlay_bundle": str(primary),
        "repeat_overlay_bundle": str(repeat),
        "overlay_summary": primary_summary,
        "determinism": {
            "manifest_byte_identical": True,
            "artifact_role_count": len(primary_manifest["sha256"]),
            "artifact_hashes_byte_identical": True,
        },
        "three_mf": {
            "path": str(model_path),
            "mesh_object_count": inspection.object_count,
            "components_object_count": inspection.components_object_count,
            "component_count": inspection.component_count,
            "top_level_build_item_count": inspection.build_item_count,
            "base_material_group_count": inspection.base_material_group_count,
            "material_assigned_object_count": inspection.material_assigned_object_count,
            "triangle_count": inspection.triangle_count,
            "strict_warning_count": inspection.strict_warning_count,
            "object_names": inspection.object_names,
        },
        "official_bambu_slice": {
            "command": slice_result["command"],
            "process_exit_status": slice_result["exit_code"],
            "status": slice_result["status"],
            "slicer": slice_result["slicer"],
            "profile": slice_result["profile"],
            "metrics": metrics,
            "gcode": _artifact_record(gcode_path),
            "slice_json": _artifact_record(slice_json_path),
            "stdout_log": _artifact_record(primary / "bambu-p2s-slice.stdout.log"),
            "stderr_log": _artifact_record(primary / "bambu-p2s-slice.stderr.log"),
        },
        "diagnostic_classification": {
            "floating_diagnostic_present": False,
            "empty_layer_diagnostic_present": False,
            "out_of_bed_diagnostic_present": False,
            "support_required": False,
            "t65535_diagnostic_present": True,
            "t65535_classification": (
                "official P2S machine_end_gcode AMS unload sentinel; identical diagnostic is "
                "retained in the accepted Gongga build and project-reopen baselines"
            ),
            "invalid_object_material_assignment": False,
            "zfiller_diagnostic_count": _diagnostic_count(slice_stdout, r"ZFiller"),
            "gongga_build_zfiller_diagnostic_count": _diagnostic_count(
                gongga_build_text, r"ZFiller"
            ),
            "gongga_reopen_zfiller_diagnostic_count": _diagnostic_count(
                gongga_reopen_text, r"ZFiller"
            ),
            "zfiller_classification": (
                "non-gating Bambu Studio polygon diagnostic also present in accepted single-"
                "terrain baselines; result.json warning_message is empty and required slice "
                "metrics pass"
            ),
        },
        "source_terrain_sha256": terrain_roles,
        "inputs": {
            "bambu_executable": _artifact_record(args.bambu_executable.resolve()),
            "machine_profile": _artifact_record(args.machine_profile.resolve()),
            "process_profile": _artifact_record(args.process_profile.resolve()),
            "filament_profile": _artifact_record(args.filament_profile.resolve()),
            "gongga_build_log": _artifact_record(args.gongga_build_log.resolve()),
            "gongga_reopen_log": _artifact_record(args.gongga_reopen_log.resolve()),
        },
    }
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    for output in args.output:
        destination = output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        _require(_load_json(destination)["status"] == "passed", "verification reread failed")
        print(destination)


if __name__ == "__main__":
    main()
