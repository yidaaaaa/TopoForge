from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from topoforge.tiling.calibration import (
    CALIBRATION_ARTIFACT_NAME,
    CALIBRATION_CLEARANCES_MM,
    _engrave_label,
    _label_tool,
    _male_coupon,
    generate_connector_calibration,
)


def test_compact_connector_calibration_has_recessed_labels_and_small_footprint(
    tmp_path: Path,
) -> None:
    result = generate_connector_calibration(tmp_path / "calibration")
    inspection = result.inspection
    plan = json.loads(result.plan_path.read_text(encoding="utf-8"))
    validation = json.loads(result.validation_path.read_text(encoding="utf-8"))

    assert result.core_3mf_path.name == CALIBRATION_ARTIFACT_NAME
    assert inspection.strict_warning_count == 0
    assert inspection.lib3mf_version == (2, 5, 0)
    assert inspection.object_count == 12
    assert inspection.component_count == 12
    assert inspection.build_item_count == 1
    assert inspection.dimensions_mm == pytest.approx((87.0, 44.0, 4.6), abs=1e-6)
    assert inspection.bounds_mm[0] == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)

    expected_labels = {f".{round(value * 100):02d}" for value in CALIBRATION_CLEARANCES_MM}
    assert {sample["physical_label"] for sample in plan["samples"]} == expected_labels
    assert plan["physical_label_on_every_coupon"] is True
    assert plan["pair_layout"] == "2 columns x 3 rows; male left and female right"
    assert not [name for name in inspection.object_names if name.endswith("-label")]
    assert validation["physical_labels_present"] is True
    assert validation["physical_label_object_count"] == 0
    assert validation["labels_integrated_into_coupon_meshes"] is True
    assert validation["label_raised_height_mm"] == 0.0
    assert validation["label_recess_depth_mm"] == pytest.approx(0.4)
    assert validation["label_floor_height_mm"] == pytest.approx(2.6)
    assert validation["support_required"] is False
    assert validation["minimum_roof_thickness_mm"] == pytest.approx(1.0)
    assert validation["estimated_solid_mass_upper_bound_g"] < 15.0
    assert validation["footprint_reduction_fraction"] > 0.75
    assert validation["required_checks_passed"] is True


def test_recessed_label_cannot_protrude_into_the_pairing_clearance() -> None:
    raw = _male_coupon()
    engraved = _engrave_label(raw, _label_tool(".10"))
    label_region = engraved.vertices[:, 0] > 6.0

    assert engraved.volume < raw.volume
    assert engraved.bounds == pytest.approx(raw.bounds, abs=1e-9)
    assert np.max(engraved.vertices[label_region, 2]) == pytest.approx(3.0, abs=1e-9)
    assert np.any(np.isclose(engraved.vertices[label_region, 2], 2.6, atol=1e-9))
    assert engraved.is_watertight
    assert engraved.is_winding_consistent
    assert engraved.volume > 0.0


def test_compact_connector_calibration_core_bundle_is_byte_deterministic(
    tmp_path: Path,
) -> None:
    first = generate_connector_calibration(tmp_path / "first")
    second = generate_connector_calibration(tmp_path / "second")
    first_files = sorted(path.name for path in first.output_dir.iterdir() if path.is_file())
    second_files = sorted(path.name for path in second.output_dir.iterdir() if path.is_file())

    assert first_files == second_files
    for name in first_files:
        assert (first.output_dir / name).read_bytes() == (second.output_dir / name).read_bytes(), (
            name
        )
