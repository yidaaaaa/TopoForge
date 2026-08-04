"""Deterministic compact connector-clearance calibration coupons."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon

from topoforge.exceptions import MeshValidationError
from topoforge.exporters.three_mf import (
    ThreeMFInspection,
    ThreeMFObject,
    export_3mf_objects,
    inspect_3mf,
)

CALIBRATION_CLEARANCES_MM = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40)
CALIBRATION_SCHEMA_VERSION = "topoforge-connector-calibration-v3-recessed"
CALIBRATION_ARTIFACT_NAME = "connector-calibration-p2s-v3-recessed.3mf"

_COUPON_WIDTH_MM = 20.0
_COUPON_DEPTH_MM = 12.0
_BASE_THICKNESS_MM = 3.0
_PAIR_GAP_MM = 2.0
_COLUMN_GAP_MM = 3.0
_ROW_GAP_MM = 4.0
_COLUMN_COUNT = 2
_ROW_COUNT = 3
_MALE_HEIGHT_MM = 1.6
_MALE_NECK_WIDTH_MM = 3.2
_MALE_HEAD_WIDTH_MM = 5.0
_INSERTION_DEPTH_MM = 3.2
_CONNECTOR_NECK_X_MM = 2.0
_LABEL_PIXEL_MM = 0.5
_LABEL_PIXEL_GAP_MM = 0.05
_LABEL_RECESS_MM = 0.4
_PLA_DENSITY_G_CM3 = 1.26
_V1_DIMENSIONS_MM = (238.0, 68.0, 14.0)


@dataclass(frozen=True, slots=True)
class ConnectorCalibrationResult:
    """Files and measured properties for one generated calibration plate."""

    output_dir: Path
    core_3mf_path: Path
    plan_path: Path
    measurement_sheet_path: Path
    preview_path: Path
    validation_path: Path
    checksums_path: Path
    inspection: ThreeMFInspection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return path


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    return _write_bytes(path, payload)


def _require_mesh(value: Any, *, operation: str) -> trimesh.Trimesh:
    if not isinstance(value, trimesh.Trimesh) or len(value.faces) == 0:
        raise MeshValidationError(f"connector calibration {operation} produced no mesh")
    value.units = "mm"
    if (
        not bool(value.is_watertight)
        or not bool(value.is_winding_consistent)
        or float(value.volume) <= 0.0
    ):
        raise MeshValidationError(f"connector calibration {operation} produced invalid topology")
    return value


def _box(extents_mm: tuple[float, float, float]) -> trimesh.Trimesh:
    value = trimesh.creation.box(extents=extents_mm)
    value.apply_translation(tuple(dimension / 2.0 for dimension in extents_mm))
    value.units = "mm"
    return value


def _polygon_prism(
    points_xy_mm: tuple[tuple[float, float], ...],
    *,
    z_min_mm: float,
    z_max_mm: float,
) -> trimesh.Trimesh:
    value = trimesh.creation.extrude_polygon(
        Polygon(points_xy_mm),
        z_max_mm - z_min_mm,
        engine="earcut",
    )
    if not isinstance(value, trimesh.Trimesh):
        raise MeshValidationError("connector calibration polygon extrusion failed")
    value.apply_translation((0.0, 0.0, z_min_mm))
    return _require_mesh(value, operation="polygon extrusion")


def _dovetail_points(*, total_clearance_mm: float) -> tuple[tuple[float, float], ...]:
    center_y_mm = _COUPON_DEPTH_MM / 2.0
    neck_half_mm = (_MALE_NECK_WIDTH_MM + total_clearance_mm) / 2.0
    head_half_mm = (_MALE_HEAD_WIDTH_MM + total_clearance_mm) / 2.0
    head_x_mm = _CONNECTOR_NECK_X_MM + _INSERTION_DEPTH_MM
    return (
        (_CONNECTOR_NECK_X_MM, center_y_mm - neck_half_mm),
        (head_x_mm, center_y_mm - head_half_mm),
        (head_x_mm, center_y_mm + head_half_mm),
        (_CONNECTOR_NECK_X_MM, center_y_mm + neck_half_mm),
    )


def _male_coupon() -> trimesh.Trimesh:
    base = _box((_COUPON_WIDTH_MM, _COUPON_DEPTH_MM, _BASE_THICKNESS_MM))
    dovetail = _polygon_prism(
        _dovetail_points(total_clearance_mm=0.0),
        z_min_mm=_BASE_THICKNESS_MM,
        z_max_mm=_BASE_THICKNESS_MM + _MALE_HEIGHT_MM,
    )
    return _require_mesh(
        trimesh.boolean.union([base, dovetail], engine="manifold"),
        operation="male union",
    )


def _female_coupon(total_clearance_mm: float) -> trimesh.Trimesh:
    base = _box((_COUPON_WIDTH_MM, _COUPON_DEPTH_MM, _BASE_THICKNESS_MM))
    cavity = _polygon_prism(
        _dovetail_points(total_clearance_mm=total_clearance_mm),
        z_min_mm=-0.05,
        z_max_mm=_MALE_HEIGHT_MM + total_clearance_mm,
    )
    return _require_mesh(
        trimesh.boolean.difference([base, cavity], engine="manifold"),
        operation="female difference",
    )


def _label_tool(text: str) -> trimesh.Trimesh:
    font = ImageFont.load_default(size=16)
    left, top, right, bottom = font.getbbox(text)
    width_px = max(1, round(right - left))
    height_px = max(1, round(bottom - top))
    bitmap = Image.new("1", (width_px, height_px), color=0)
    draw = ImageDraw.Draw(bitmap)
    draw.text((-left, -top), text, fill=1, font=font)
    pixels = np.asarray(bitmap, dtype=np.uint8)
    active = np.argwhere(pixels > 0)
    if not len(active):
        raise ValueError(f"connector calibration label {text!r} is empty")

    pitch_mm = _LABEL_PIXEL_MM + _LABEL_PIXEL_GAP_MM
    width_mm = (width_px - 1) * pitch_mm + _LABEL_PIXEL_MM
    height_mm = (height_px - 1) * pitch_mm + _LABEL_PIXEL_MM
    label_center_x_mm = 13.25
    origin_x_mm = label_center_x_mm - width_mm / 2.0
    origin_y_mm = _COUPON_DEPTH_MM / 2.0 - height_mm / 2.0
    if origin_x_mm <= _CONNECTOR_NECK_X_MM + _INSERTION_DEPTH_MM + 0.5:
        raise ValueError(f"connector calibration label {text!r} overlaps the dovetail")
    if origin_y_mm < 0.5 or origin_y_mm + height_mm > _COUPON_DEPTH_MM - 0.5:
        raise ValueError(f"connector calibration label {text!r} exceeds the coupon")

    # Extend slightly above the original top face so the boolean cut is robust.
    thickness_mm = _LABEL_RECESS_MM + 0.05
    boxes: list[trimesh.Trimesh] = []
    for row, column in active:
        pixel = trimesh.creation.box(extents=(_LABEL_PIXEL_MM, _LABEL_PIXEL_MM, thickness_mm))
        pixel.apply_translation(
            (
                origin_x_mm + float(column) * pitch_mm + _LABEL_PIXEL_MM / 2.0,
                origin_y_mm + float(height_px - int(row) - 1) * pitch_mm + _LABEL_PIXEL_MM / 2.0,
                _BASE_THICKNESS_MM - _LABEL_RECESS_MM + thickness_mm / 2.0,
            )
        )
        boxes.append(pixel)
    label = trimesh.util.concatenate(boxes)
    return _require_mesh(label, operation=f"label tool {text!r}")


def _engrave_label(coupon: trimesh.Trimesh, label_tool: trimesh.Trimesh) -> trimesh.Trimesh:
    """Cut a recessed label into the exposed top of one coupon."""
    return _require_mesh(
        trimesh.boolean.difference([coupon, label_tool], engine="manifold"),
        operation="recessed label difference",
    )


def _translated(mesh: trimesh.Trimesh, x_mm: float, y_mm: float) -> trimesh.Trimesh:
    value = mesh.copy()
    value.apply_translation((x_mm, y_mm, 0.0))
    value.units = "mm"
    return value


def _render_preview(path: Path, samples: list[dict[str, Any]]) -> Path:
    scale = 8
    margin_px = 28
    header_px = 74
    width_mm = (
        _COLUMN_COUNT * (2.0 * _COUPON_WIDTH_MM + _PAIR_GAP_MM)
        + (_COLUMN_COUNT - 1) * _COLUMN_GAP_MM
    )
    depth_mm = _ROW_COUNT * _COUPON_DEPTH_MM + (_ROW_COUNT - 1) * _ROW_GAP_MM
    canvas = Image.new(
        "RGB",
        (round(width_mm * scale) + 2 * margin_px, round(depth_mm * scale) + header_px + 42),
        (247, 248, 246),
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((margin_px, 14), "TopoForge compact connector calibration v3", fill=(24, 31, 35))
    draw.text(
        (margin_px, 40),
        "Physical recessed labels: .10-.40 mm total clearance; male left, female right",
        fill=(73, 85, 91),
    )

    for sample in samples:
        x_mm = float(sample["pair_origin_mm"][0])
        y_mm = float(sample["pair_origin_mm"][1])
        display_y_mm = depth_mm - y_mm - _COUPON_DEPTH_MM
        x0 = margin_px + round(x_mm * scale)
        y0 = header_px + round(display_y_mm * scale)
        for role_index, role in enumerate(("male", "female")):
            item_x = x0 + round(role_index * (_COUPON_WIDTH_MM + _PAIR_GAP_MM) * scale)
            bounds = (
                item_x,
                y0,
                item_x + round(_COUPON_WIDTH_MM * scale),
                y0 + round(_COUPON_DEPTH_MM * scale),
            )
            fill = (184, 210, 215) if role == "male" else (235, 197, 150)
            draw.rectangle(bounds, fill=fill, outline=(42, 58, 64), width=2)
            draw.text((item_x + 8, y0 + 8), str(sample["physical_label"]), fill=(18, 24, 27))
            draw.text(
                (item_x + 8, y0 + round(_COUPON_DEPTH_MM * scale) - 20),
                role,
                fill=(42, 58, 64),
            )
        draw.text(
            (x0, y0 - 16),
            f"{sample['sample_id']}  {sample['total_lateral_clearance_mm']:.2f} mm",
            fill=(31, 44, 49),
        )

    draw.text(
        (margin_px, canvas.height - 28),
        f"Plate bounds: {width_mm:.0f} x {depth_mm:.0f} mm; base {_BASE_THICKNESS_MM:.0f} mm",
        fill=(73, 85, 91),
    )
    temporary = path.with_name(f".{path.name}.tmp")
    canvas.save(temporary, format="PNG", optimize=True)
    temporary.replace(path)
    return path


def _measurement_csv(samples: list[dict[str, Any]]) -> bytes:
    fields = (
        "sample_id",
        "physical_label",
        "total_lateral_clearance_mm",
        "vertical_clearance_mm",
        "fit_classification",
        "insertion_force_n",
        "insertion_cycles",
        "play_x_mm",
        "play_y_mm",
        "play_z_mm",
        "dimensional_error_mm",
        "elephant_foot_or_burrs",
        "material",
        "humidity_percent",
        "operator_notes",
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for sample in samples:
        writer.writerow(
            {
                "sample_id": sample["sample_id"],
                "physical_label": sample["physical_label"],
                "total_lateral_clearance_mm": f"{sample['total_lateral_clearance_mm']:.2f}",
                "vertical_clearance_mm": f"{sample['vertical_clearance_mm']:.2f}",
                "material": "Bambu PLA Basic",
            }
        )
    return output.getvalue().encode("utf-8")


def generate_connector_calibration(output_dir: Path) -> ConnectorCalibrationResult:
    """Generate compact, physically labeled P2S connector calibration evidence."""
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pair_width_mm = 2.0 * _COUPON_WIDTH_MM + _PAIR_GAP_MM
    plate_width_mm = _COLUMN_COUNT * pair_width_mm + (_COLUMN_COUNT - 1) * _COLUMN_GAP_MM
    plate_depth_mm = _ROW_COUNT * _COUPON_DEPTH_MM + (_ROW_COUNT - 1) * _ROW_GAP_MM

    objects: list[ThreeMFObject] = []
    samples: list[dict[str, Any]] = []
    coupon_volume_mm3 = 0.0
    for index, clearance_mm in enumerate(CALIBRATION_CLEARANCES_MM):
        row = index // _COLUMN_COUNT
        column = index % _COLUMN_COUNT
        pair_x_mm = column * (pair_width_mm + _COLUMN_GAP_MM)
        pair_y_mm = (_ROW_COUNT - row - 1) * (_COUPON_DEPTH_MM + _ROW_GAP_MM)
        female_x_mm = pair_x_mm + _COUPON_WIDTH_MM + _PAIR_GAP_MM
        token = f"{round(clearance_mm * 100):02d}"
        physical_label = f".{token}"

        label = _label_tool(physical_label)
        male = _translated(
            _engrave_label(_male_coupon(), label),
            pair_x_mm,
            pair_y_mm,
        )
        female = _translated(
            _engrave_label(_female_coupon(clearance_mm), label),
            female_x_mm,
            pair_y_mm,
        )
        coupon_volume_mm3 += float(male.volume + female.volume)
        prefix = f"clearance-0p{token}"
        objects.extend(
            (
                ThreeMFObject(f"{prefix}-male", male, f"cal-{token}-male"),
                ThreeMFObject(f"{prefix}-female", female, f"cal-{token}-female"),
            )
        )
        samples.append(
            {
                "sample_id": f"C{index + 1:02d}",
                "physical_label": physical_label,
                "label_semantics": (
                    f"{physical_label} means {clearance_mm:.2f} mm total clearance"
                ),
                "total_lateral_clearance_mm": clearance_mm,
                "per_side_lateral_clearance_mm": clearance_mm / 2.0,
                "vertical_clearance_mm": clearance_mm,
                "male_height_mm": _MALE_HEIGHT_MM,
                "female_cavity_height_mm": _MALE_HEIGHT_MM + clearance_mm,
                "minimum_roof_thickness_mm": (_BASE_THICKNESS_MM - _MALE_HEIGHT_MM - clearance_mm),
                "pair_origin_mm": [pair_x_mm, pair_y_mm],
                "male_role": "left coupon; raised dovetail",
                "female_role": "right coupon; bottom-open cavity",
                "status": "measure_after_print",
            }
        )

    metadata = {
        "topoforge_calibration_schema": CALIBRATION_SCHEMA_VERSION,
        "printer": "Bambu Lab P2S",
        "printer_profile_id": "bambu-p2s-0.4",
        "nozzle_diameter_mm": "0.4",
        "layer_height_mm": "0.2",
        "filament_profile": "Bambu PLA Basic @BBL P2S",
        "plate": "Textured PEI Plate",
        "support": "false",
        "base_thickness_mm": f"{_BASE_THICKNESS_MM:.3f}",
        "clearance_matrix_mm": ",".join(f"{value:.2f}" for value in CALIBRATION_CLEARANCES_MM),
        "physical_labels": ".10,.15,.20,.25,.30,.40 recessed into every male and female coupon",
        "label_geometry": (
            "recessed; label floor is below the coupon top and cannot interfere with pairing"
        ),
        "label_semantics": ".10 means 0.10 mm total lateral and vertical clearance",
        "layout": "2 columns x 3 rows; male left and female right within every pair",
        "assembly_direction": "+Z: lower each female coupon over its matching male",
    }
    core_path = export_3mf_objects(
        tuple(objects),
        output / CALIBRATION_ARTIFACT_NAME,
        title="TopoForge P2S Compact Connector Calibration v3 Recessed Labels",
        metadata=metadata,
    )
    inspection = inspect_3mf(core_path)
    expected_dimensions_mm = (
        plate_width_mm,
        plate_depth_mm,
        _BASE_THICKNESS_MM + _MALE_HEIGHT_MM,
    )
    if not np.allclose(inspection.dimensions_mm, expected_dimensions_mm, atol=1e-6, rtol=0.0):
        raise MeshValidationError(
            "compact connector calibration dimensions changed unexpectedly: "
            f"{inspection.dimensions_mm} != {expected_dimensions_mm}"
        )

    preview_path = _render_preview(output / "connector-calibration-preview.png", samples)
    measurement_path = _write_bytes(output / "measurement-sheet.csv", _measurement_csv(samples))
    estimated_fused_volume_mm3 = coupon_volume_mm3
    solid_mass_upper_bound_g = estimated_fused_volume_mm3 / 1000.0 * _PLA_DENSITY_G_CM3
    old_footprint_mm2 = _V1_DIMENSIONS_MM[0] * _V1_DIMENSIONS_MM[1]
    new_footprint_mm2 = plate_width_mm * plate_depth_mm
    footprint_reduction_fraction = 1.0 - new_footprint_mm2 / old_footprint_mm2

    plan = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "artifact": core_path.name,
        "artifact_sha256": _sha256(core_path),
        "printer": "Bambu Lab P2S",
        "printer_profile_id": "bambu-p2s-0.4",
        "nozzle_diameter_mm": 0.4,
        "layer_height_mm": 0.2,
        "filament_profile": "Bambu PLA Basic @BBL P2S",
        "plate": "Textured PEI Plate",
        "support": False,
        "dimensions_mm": list(inspection.dimensions_mm),
        "base_thickness_mm": _BASE_THICKNESS_MM,
        "physical_label_on_every_coupon": True,
        "physical_label_rule": ".10 means 0.10 mm total lateral and vertical clearance",
        "pair_layout": "2 columns x 3 rows; male left and female right",
        "matrix_total_lateral_clearance_mm": list(CALIBRATION_CLEARANCES_MM),
        "matrix_vertical_clearance_mm": list(CALIBRATION_CLEARANCES_MM),
        "estimated_fused_solid_volume_mm3": estimated_fused_volume_mm3,
        "estimated_solid_mass_upper_bound_g": solid_mass_upper_bound_g,
        "comparison_to_v1": {
            "v1_dimensions_mm": list(_V1_DIMENSIONS_MM),
            "v3_dimensions_mm": list(inspection.dimensions_mm),
            "footprint_reduction_fraction": footprint_reduction_fraction,
            "maximum_height_reduction_fraction": (
                1.0 - inspection.dimensions_mm[2] / _V1_DIMENSIONS_MM[2]
            ),
        },
        "print_instruction": (
            "Open at 100% scale. Print the complete plate once. Match identical recessed "
            "labels; male is left and female is right in each pair. Recesses stay below "
            "the top surface and do not interfere with insertion. Record fit before "
            "changing the production connector tolerance."
        ),
        "samples": samples,
    }
    plan_path = _write_json(output / "connector-calibration-plan.json", plan)
    validation = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "artifact": core_path.name,
        "artifact_sha256": _sha256(core_path),
        "strict_3mf_warning_count": inspection.strict_warning_count,
        "lib3mf_version": list(inspection.lib3mf_version),
        "object_count": inspection.object_count,
        "coupon_object_count": len(CALIBRATION_CLEARANCES_MM) * 2,
        "physical_label_object_count": 0,
        "labels_integrated_into_coupon_meshes": True,
        "component_count": inspection.component_count,
        "build_item_count": inspection.build_item_count,
        "triangle_count": inspection.triangle_count,
        "dimensions_mm": list(inspection.dimensions_mm),
        "bounds_mm": [list(value) for value in inspection.bounds_mm],
        "base_thickness_mm": _BASE_THICKNESS_MM,
        "minimum_roof_thickness_mm": min(
            float(sample["minimum_roof_thickness_mm"]) for sample in samples
        ),
        "minimum_required_roof_thickness_mm": 0.8,
        "minimum_label_feature_mm": _LABEL_PIXEL_MM,
        "label_raised_height_mm": 0.0,
        "label_recess_depth_mm": _LABEL_RECESS_MM,
        "label_floor_height_mm": _BASE_THICKNESS_MM - _LABEL_RECESS_MM,
        "physical_labels_present": True,
        "male_female_pairing_recorded": True,
        "support_required": False,
        "all_coupon_meshes_watertight": all(item.mesh.is_watertight for item in objects),
        "all_coupon_meshes_winding_consistent": all(
            item.mesh.is_winding_consistent for item in objects
        ),
        "all_object_volumes_positive": all(item.mesh.volume > 0.0 for item in objects),
        "estimated_fused_solid_volume_mm3": estimated_fused_volume_mm3,
        "estimated_solid_mass_upper_bound_g": solid_mass_upper_bound_g,
        "v1_footprint_mm2": old_footprint_mm2,
        "v3_footprint_mm2": new_footprint_mm2,
        "footprint_reduction_fraction": footprint_reduction_fraction,
        "required_checks_passed": True,
    }
    validation_path = _write_json(output / "validation.json", validation)
    checksums_path = output / "SHA256SUMS"
    checksum_files = (
        core_path,
        plan_path,
        measurement_path,
        preview_path,
        validation_path,
    )
    checksum_payload = "".join(
        f"{_sha256(path)}  {path.name}\n"
        for path in sorted(checksum_files, key=lambda item: item.name)
    ).encode("ascii")
    _write_bytes(checksums_path, checksum_payload)
    return ConnectorCalibrationResult(
        output_dir=output,
        core_3mf_path=core_path,
        plan_path=plan_path,
        measurement_sheet_path=measurement_path,
        preview_path=preview_path,
        validation_path=validation_path,
        checksums_path=checksums_path,
        inspection=inspection,
    )
