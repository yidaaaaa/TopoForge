"""Horizontal and vertical mapping from metres to printable millimetres."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from topoforge.exceptions import ConfigurationError
from topoforge.models import (
    BaselineMode,
    BuildConfig,
    RasterResult,
    ScalingResult,
    VerticalScaleMode,
)


def _baseline(elevations_m: npt.NDArray[np.float32], config: BuildConfig) -> float:
    if config.baseline_mode is BaselineMode.MINIMUM:
        return float(np.min(elevations_m))
    if config.baseline_mode is BaselineMode.SEA_LEVEL:
        return 0.0
    if config.baseline_mode is BaselineMode.CUSTOM:
        if config.baseline_elevation_m is None:  # protected by model validation
            raise ConfigurationError("A custom baseline requires baseline_elevation_m")
        return config.baseline_elevation_m
    return float(np.percentile(elevations_m, config.robust_low_percentile))


def resolve_scaling(
    elevations_m: npt.NDArray[np.float32],
    raster: RasterResult,
    config: BuildConfig,
) -> ScalingResult:
    """Resolve a single aspect-preserving horizontal scale and vertical policy."""
    scale = config.model_width_mm / raster.ground_width_m
    model_depth_mm = raster.ground_depth_m * scale
    if config.model_depth_mm is not None and abs(config.model_depth_mm - model_depth_mm) > 0.05:
        raise ConfigurationError(
            f"Requested depth {config.model_depth_mm:.3f} mm conflicts with the raster aspect "
            f"ratio; width {config.model_width_mm:.3f} mm requires {model_depth_mm:.3f} mm. "
            "Omit depth or crop the raster to the requested aspect ratio."
        )
    build_x, build_y, build_z = config.printer_profile.build_volume_mm
    if config.model_width_mm > build_x or model_depth_mm > build_y:
        raise ConfigurationError(
            f"Model footprint {config.model_width_mm:.2f} x {model_depth_mm:.2f} mm exceeds "
            f"printer profile {config.printer_profile.profile_id} "
            f"({build_x:.2f} x {build_y:.2f} mm)"
        )

    robust_low = float(np.percentile(elevations_m, config.robust_low_percentile))
    robust_high = float(np.percentile(elevations_m, config.robust_high_percentile))
    robust_relief_m = max(robust_high - robust_low, float(np.finfo(np.float32).eps))
    height_limit_mm = min(config.max_height_mm, build_z)
    budget_mm = height_limit_mm - config.base_thickness_mm
    natural_robust_relief_mm = robust_relief_m * scale
    if budget_mm <= 0:
        raise ConfigurationError("Height budget must exceed base thickness")

    if config.vertical_scale_mode is VerticalScaleMode.NATURAL:
        policy_exaggeration = 1.0
    elif config.vertical_scale_mode is VerticalScaleMode.CUSTOM:
        policy_exaggeration = config.vertical_exaggeration
    elif config.vertical_scale_mode is VerticalScaleMode.FIT_HEIGHT:
        policy_exaggeration = budget_mm / natural_robust_relief_mm
    else:
        minimum_visible_relief_mm = max(
            8.0 * config.printer_profile.layer_height_mm,
            4.0 * config.printer_profile.nozzle_diameter_mm,
            6.0,
        )
        target_relief_mm = min(
            budget_mm,
            max(minimum_visible_relief_mm, natural_robust_relief_mm),
        )
        policy_exaggeration = target_relief_mm / natural_robust_relief_mm
    policy_exaggeration = float(
        np.clip(
            policy_exaggeration,
            config.min_vertical_exaggeration,
            config.max_vertical_exaggeration,
        )
    )

    baseline = _baseline(elevations_m, config)
    maximum_delta_m = float(np.max(elevations_m.astype(np.float64) - baseline))
    exaggeration = policy_exaggeration
    height_limit_applied = False
    if maximum_delta_m > 0.0:
        maximum_exaggeration_for_height = budget_mm / (maximum_delta_m * scale)
        if policy_exaggeration > maximum_exaggeration_for_height:
            if config.vertical_scale_mode in {
                VerticalScaleMode.FIT_HEIGHT,
                VerticalScaleMode.AUTO_PERCEPTUAL,
            }:
                if maximum_exaggeration_for_height < config.min_vertical_exaggeration:
                    raise ConfigurationError(
                        f"Terrain requires vertical exaggeration "
                        f"{maximum_exaggeration_for_height:.6g} to fit the hard "
                        f"{height_limit_mm:.3f} mm height limit, below configured minimum "
                        f"{config.min_vertical_exaggeration:.6g}; increase --max-height-mm "
                        "or lower min_vertical_exaggeration"
                    )
                exaggeration = maximum_exaggeration_for_height
                height_limit_applied = True
            else:
                predicted = config.base_thickness_mm + maximum_delta_m * scale * policy_exaggeration
                raise ConfigurationError(
                    f"{config.vertical_scale_mode.value} scaling produces {predicted:.3f} mm "
                    f"height, above the hard {height_limit_mm:.3f} mm limit; increase "
                    "--max-height-mm or reduce vertical exaggeration"
                )

    z = (
        config.base_thickness_mm
        + (elevations_m.astype(np.float64) - baseline) * scale * exaggeration
    )
    min_z = float(np.min(z))
    max_z = float(np.max(z))
    minimum_required_mm = config.printer_profile.minimum_base_thickness_mm
    if min_z < minimum_required_mm:
        raise ConfigurationError(
            f"Baseline policy leaves {min_z:.3f} mm minimum material below the terrain, "
            f"below printer-profile minimum {minimum_required_mm:.3f} mm; increase base "
            "thickness, choose a lower baseline, or reduce exaggeration"
        )
    if max_z > height_limit_mm + 1e-9:
        raise ConfigurationError(
            f"Resolved terrain height {max_z:.6f} mm exceeds the hard "
            f"{height_limit_mm:.6f} mm limit"
        )
    return ScalingResult(
        horizontal_scale_mm_per_m=scale,
        model_width_mm=config.model_width_mm,
        model_depth_mm=model_depth_mm,
        base_thickness_mm=config.base_thickness_mm,
        baseline_elevation_m=baseline,
        robust_low_elevation_m=robust_low,
        robust_high_elevation_m=robust_high,
        policy_vertical_exaggeration=policy_exaggeration,
        vertical_exaggeration=float(exaggeration),
        height_limit_mm=height_limit_mm,
        height_limit_applied=height_limit_applied,
        predicted_min_z_mm=min_z,
        predicted_max_z_mm=max_z,
        scale_mode=config.vertical_scale_mode,
    )


def apply_vertical_scale(
    elevations_m: npt.NDArray[np.float32],
    scaling: ScalingResult,
) -> npt.NDArray[np.float64]:
    """Map elevation metres into model Z millimetres."""
    return (
        scaling.base_thickness_mm
        + (elevations_m.astype(np.float64) - scaling.baseline_elevation_m)
        * scaling.horizontal_scale_mm_per_m
        * scaling.vertical_exaggeration
    )
