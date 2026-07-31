"""Atomic end-to-end local raster terrain builds."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import trimesh
import yaml
from PIL import Image
from rasterio.crs import CRS as RasterioCRS

from topoforge import __version__
from topoforge.config import dump_resolved_config
from topoforge.engine.preflight import (
    ManufacturingPreflightReport,
    evaluate_manufacturing_preflight,
)
from topoforge.exceptions import ConfigurationError, MeshValidationError
from topoforge.exporters import export_glb, export_stl
from topoforge.exporters.three_mf import ThreeMFInspection, export_3mf, inspect_3mf
from topoforge.mesh import build_rectangular_terrain_mesh
from topoforge.models import BuildConfig
from topoforge.provenance import write_json, write_validation_html
from topoforge.raster import process_local_raster
from topoforge.rendering import render_elevation_preview
from topoforge.scaling import apply_vertical_scale, resolve_scaling
from topoforge.util import sha256_file
from topoforge.validation import evaluate_bambu_p2s_release_gate, validate_mesh


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Completed build paths and measured reports."""

    output_dir: Path
    artifacts: dict[str, Path]
    validation: dict[str, Any]
    provenance: dict[str, Any]


def _require_empty_destination(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ConfigurationError(f"Output path exists and is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ConfigurationError(
                f"Output directory is not empty: {output_dir}. "
                "Choose a new directory to preserve prior evidence."
            )
        output_dir.rmdir()
    output_dir.parent.mkdir(parents=True, exist_ok=True)


def _load_stl(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="stl", force="mesh", process=True)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"Reopened STL is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _load_glb(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, file_type="glb", force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh):
        raise MeshValidationError(f"Reopened GLB is not a triangle mesh: {path}")
    loaded.units = "mm"
    return loaded


def _peak_coordinate_mm(
    mesh: trimesh.Trimesh, *, expected_xy_mm: tuple[float, float]
) -> tuple[float, float, float]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    maximum_z = float(np.max(vertices[:, 2]))
    candidate_indices = np.flatnonzero(np.isclose(vertices[:, 2], maximum_z, atol=1e-6, rtol=0.0))
    candidates = vertices[candidate_indices]
    distances = np.square(candidates[:, 0] - expected_xy_mm[0]) + np.square(
        candidates[:, 1] - expected_xy_mm[1]
    )
    peak = candidates[int(np.argmin(distances))]
    return float(peak[0]), float(peak[1]), float(peak[2])


def _closest_peak_coordinate_mm(
    candidates: tuple[tuple[float, float, float], ...],
    *,
    expected_xy_mm: tuple[float, float],
) -> tuple[float, float, float]:
    return min(
        candidates,
        key=lambda value: (value[0] - expected_xy_mm[0]) ** 2 + (value[1] - expected_xy_mm[1]) ** 2,
    )


def _expected_model_peak_mm(
    processed_elevations_m: np.ndarray,
    processed_peak_coordinate: dict[str, object],
    top_z_mm: np.ndarray,
    *,
    width_mm: float,
    depth_mm: float,
) -> tuple[float, float, float]:
    rows, columns = processed_elevations_m.shape
    row_value = processed_peak_coordinate.get("row")
    column_value = processed_peak_coordinate.get("column")
    if not isinstance(row_value, int) or not isinstance(column_value, int):
        raise MeshValidationError("Processed peak raster indices are not integers")
    row = row_value
    column = column_value
    x_mm = width_mm * column / max(columns - 1, 1)
    y_mm = depth_mm * (rows - 1 - row) / max(rows - 1, 1)
    return x_mm, y_mm, float(top_z_mm[row, column])


def _required_validation_passed(report: dict[str, Any]) -> bool:
    true_checks = (
        "finite_vertices",
        "finite_face_normals",
        "watertight",
        "winding_consistent",
        "manifold",
        "positive_volume",
        "flat_bottom",
        "dimensions_within_tolerance",
        "height_limit_passed",
        "minimum_base_thickness_passed",
        "triangle_budget_passed",
        "terrain_fidelity_passed",
        "orientation_consistent",
    )
    if not all(report.get(key) is True for key in true_checks):
        return False
    return (
        report.get("connected_components") == 1
        and report.get("degenerate_faces") == 0
        and report.get("duplicate_faces") == 0
        and float(report.get("bottom_planarity_error_mm") or 0.0) <= 0.01
    )


def _artifact_map(stage: Path, formats: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {
        "processed_dem": stage / "processed_dem.tif",
        "original_nodata_mask": stage / "original_nodata_mask.tif",
        "manufacturing_preflight": stage / "manufacturing_preflight.json",
        "provenance": stage / "provenance.json",
        "validation_json": stage / "validation.json",
        "validation_html": stage / "validation.html",
        "resolved_config": stage / "build_config.resolved.yaml",
        "preview_png": stage / "preview.png",
        "manifest": stage / "build_manifest.json",
    }
    if "stl" in formats:
        artifacts["model_stl"] = stage / "model.stl"
    if "3mf" in formats:
        artifacts["model_3mf"] = stage / "model.3mf"
    if "glb" in formats:
        artifacts["preview_glb"] = stage / "preview.glb"
    return artifacts


def build_local_terrain(config: BuildConfig) -> BuildResult:
    """Run one local GeoTIFF build atomically and independently reopen every artifact."""
    source = config.dem_path.expanduser().resolve()
    output = config.output_dir.expanduser().resolve()
    _require_empty_destination(output)
    stage = output.parent / f".{output.name}.topoforge-stage-{uuid.uuid4().hex}"
    stage.mkdir(parents=False)
    stage_config = config.model_copy(update={"dem_path": source, "output_dir": stage})
    resolved_config = config.model_copy(update={"dem_path": source, "output_dir": output})
    artifacts = _artifact_map(stage, config.output_formats)
    source_acquisition: dict[str, Any] | None = None
    source_quality_inputs: dict[str, tuple[Path, str]] = {}
    if resolved_config.source_acquisition_manifest is not None:
        source_acquisition_path = resolved_config.source_acquisition_manifest.expanduser().resolve()
        if not source_acquisition_path.is_file():
            raise ConfigurationError(
                f"Source acquisition manifest does not exist: {source_acquisition_path}"
            )
        try:
            loaded_acquisition = json.loads(source_acquisition_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Source acquisition manifest is not valid JSON: {source_acquisition_path}"
            ) from exc
        if not isinstance(loaded_acquisition, dict):
            raise ConfigurationError("Source acquisition manifest root must be a JSON object")
        source_acquisition = loaded_acquisition
        artifacts["source_acquisition"] = stage / "source_acquisition.json"
        quality_masks = source_acquisition.get("quality_masks", [])
        if not isinstance(quality_masks, list):
            raise ConfigurationError("Source acquisition quality_masks must be a list")
        for record in quality_masks:
            if not isinstance(record, dict) or record.get("availability") != "present":
                continue
            role = record.get("role")
            output_record = record.get("output")
            if role not in {"edm", "flm", "hem", "wbm"} or not isinstance(output_record, dict):
                raise ConfigurationError("Present source quality mask has invalid metadata")
            raw_path = output_record.get("path")
            expected_sha256 = output_record.get("sha256")
            if not isinstance(raw_path, str) or not isinstance(expected_sha256, str):
                raise ConfigurationError(
                    f"Present source quality mask {role} is missing path/SHA-256"
                )
            source_quality_path = Path(raw_path).expanduser().resolve()
            if not source_quality_path.is_file():
                raise ConfigurationError(
                    f"Source quality mask does not exist: {source_quality_path}"
                )
            if sha256_file(source_quality_path) != expected_sha256:
                raise ConfigurationError(
                    f"Source quality mask checksum changed before build: {source_quality_path}"
                )
            artifact_role = f"source_quality_{role}"
            artifact_name = f"source_quality_{role}.tif"
            artifacts[artifact_role] = stage / artifact_name
            source_quality_inputs[artifact_role] = (source_quality_path, expected_sha256)
            output_record["bundled_artifact"] = artifact_name
    try:
        for artifact_role, (source_quality_path, expected_sha256) in source_quality_inputs.items():
            shutil.copyfile(source_quality_path, artifacts[artifact_role])
            if sha256_file(artifacts[artifact_role]) != expected_sha256:
                raise ConfigurationError(
                    f"Bundled source quality mask checksum changed: {artifact_role}"
                )
            with rasterio.open(artifacts[artifact_role]) as quality_dataset:
                if quality_dataset.count != 1 or quality_dataset.crs is None:
                    raise ConfigurationError(
                        f"Bundled source quality mask failed raster reopen: {artifact_role}"
                    )
        if source_acquisition is not None:
            write_json(artifacts["source_acquisition"], source_acquisition)
        processed = process_local_raster(stage_config)
        scaling = resolve_scaling(processed.elevations_m, processed.report, resolved_config)
        manufacturing_preflight = evaluate_manufacturing_preflight(
            processed.report, scaling, resolved_config
        )
        write_json(
            artifacts["manufacturing_preflight"],
            manufacturing_preflight.model_dump(mode="json"),
        )
        top_z_mm = apply_vertical_scale(processed.elevations_m, scaling)
        expected_peak_mm = _expected_model_peak_mm(
            processed.elevations_m,
            processed.report.processed_peak_coordinate,
            top_z_mm,
            width_mm=scaling.model_width_mm,
            depth_mm=scaling.model_depth_mm,
        )
        # Raster row 0 is north.  Flip rows so mesh row 0 is south/y=0 and +Y is North.
        oriented_top_z_mm = np.flipud(top_z_mm)
        mesh = build_rectangular_terrain_mesh(
            oriented_top_z_mm,
            width_mm=scaling.model_width_mm,
            depth_mm=scaling.model_depth_mm,
            base_thickness_mm=scaling.base_thickness_mm,
        )
        if "stl" in config.output_formats:
            export_stl(mesh, artifacts["model_stl"])
        if "glb" in config.output_formats:
            export_glb(mesh, artifacts["preview_glb"])
        three_mf: ThreeMFInspection | None = None
        if "3mf" in config.output_formats:
            export_3mf(
                mesh,
                artifacts["model_3mf"],
                metadata={
                    "dataset_name": processed.report.metadata.dataset_name,
                    "dataset_type": processed.report.metadata.dataset_type.value,
                    "horizontal_crs": processed.report.metadata.horizontal_crs,
                    "vertical_datum": processed.report.metadata.vertical_datum,
                    "vertical_exaggeration": format(scaling.vertical_exaggeration, ".9g"),
                    "east_axis": "+X = East",
                    "north_axis": "+Y = North",
                    "north_edge": "y=model_depth_mm",
                    "source_bounds": json.dumps(
                        processed.report.source_bounds,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "orientation_transform": (
                        "mesh_heightfield=flipud(processed_dem); "
                        "source row 0 north maps to y=model_depth_mm"
                    ),
                },
            )
            three_mf = inspect_3mf(artifacts["model_3mf"])

        validation_mesh = (
            _load_stl(artifacts["model_stl"]) if "stl" in config.output_formats else mesh
        )
        mesh_extents = np.asarray(mesh.extents, dtype=np.float64)
        expected_dimensions = (
            float(mesh_extents[0]),
            float(mesh_extents[1]),
            float(mesh_extents[2]),
        )
        geometry_report = validate_mesh(
            validation_mesh,
            expected_dimensions_mm=expected_dimensions,
            dimension_tolerance_mm=0.05,
            flat_bottom_tolerance_mm=0.01,
        )
        validation = geometry_report.model_dump(mode="json")
        validation["validation_source"] = (
            "reopened model.stl" if "stl" in config.output_formats else "in-memory mesh"
        )
        validation["mesh_units"] = "millimetres by TopoForge coordinate contract"
        validation["estimated_triangle_count"] = processed.report.estimated_triangle_count
        validation["triangle_budget"] = config.max_estimated_triangles or (
            config.max_grid_cells * 4 - 4
        )
        validation["triangle_budget_passed"] = int(validation["triangle_count"]) <= int(
            validation["triangle_budget"]
        )
        validation["manufacturing_preflight"] = manufacturing_preflight.model_dump(mode="json")
        validation["height_limit_mm"] = scaling.height_limit_mm
        validation["height_limit_passed"] = (
            float(validation["dimensions_mm"][2]) <= scaling.height_limit_mm + 0.05
        )
        validation["minimum_base_thickness_required_mm"] = (
            config.printer_profile.minimum_base_thickness_mm
        )
        measured_base = validation.get("minimum_base_thickness_mm")
        validation["minimum_base_thickness_passed"] = (
            measured_base is not None
            and float(measured_base) >= config.printer_profile.minimum_base_thickness_mm - 0.01
        )
        if three_mf is not None:
            validation["three_mf_round_trip"] = {
                "unit": three_mf.unit,
                "object_count": three_mf.object_count,
                "build_item_count": three_mf.build_item_count,
                "vertex_count": three_mf.vertex_count,
                "triangle_count": three_mf.triangle_count,
                "dimensions_mm": three_mf.dimensions_mm,
                "object_names": three_mf.object_names,
                "strict_warning_count": three_mf.strict_warning_count,
                "lib3mf_version": three_mf.lib3mf_version,
                "peak_coordinate_mm": three_mf.peak_coordinate_mm,
                "peak_coordinates_mm": three_mf.peak_coordinates_mm,
                "metadata": three_mf.metadata,
            }
            validation["three_mf_dimensions_match"] = bool(
                np.allclose(three_mf.dimensions_mm, expected_dimensions, atol=0.05, rtol=0.0)
            )

        expected_xy_mm = (expected_peak_mm[0], expected_peak_mm[1])
        format_peaks: dict[str, tuple[float, float, float]] = {
            "in_memory": _peak_coordinate_mm(mesh, expected_xy_mm=expected_xy_mm)
        }
        if "stl" in config.output_formats:
            format_peaks["stl"] = _peak_coordinate_mm(
                validation_mesh, expected_xy_mm=expected_xy_mm
            )
        if "glb" in config.output_formats:
            format_peaks["glb"] = _peak_coordinate_mm(
                _load_glb(artifacts["preview_glb"]), expected_xy_mm=expected_xy_mm
            )
        if three_mf is not None:
            format_peaks["3mf"] = _closest_peak_coordinate_mm(
                three_mf.peak_coordinates_mm,
                expected_xy_mm=expected_xy_mm,
            )
        orientation_consistent = all(
            np.allclose(value, expected_peak_mm, atol=0.01, rtol=0.0)
            for value in format_peaks.values()
        )
        reference_peak_difference_m = (
            None
            if config.reference_peak_elevation_m is None
            else config.reference_peak_elevation_m - processed.report.raw_elevation_max_m
        )
        validation.update(
            {
                "reference_peak_elevation_m": config.reference_peak_elevation_m,
                "reference_peak_difference_m": reference_peak_difference_m,
                "reference_peak_adjustment_applied": False,
                "source_horizontal_resolution_m": processed.report.source_horizontal_resolution_m,
                "processed_horizontal_resolution_m": (
                    processed.report.processed_horizontal_resolution_m
                ),
                "source_grid_shape": processed.report.source_grid_shape,
                "processed_grid_shape": processed.report.processed_grid_shape,
                "downsampling_factor": processed.report.downsampling_factor,
                "physical_sample_spacing_mm": processed.report.physical_sample_spacing_mm,
                "estimated_memory_mb": processed.report.estimated_memory_mb,
                "raw_elevation_min_m": processed.report.raw_elevation_min_m,
                "raw_elevation_max_m": processed.report.raw_elevation_max_m,
                "processed_elevation_min_m": processed.report.processed_elevation_min_m,
                "processed_elevation_max_m": processed.report.processed_elevation_max_m,
                "peak_elevation_loss_m": processed.report.peak_elevation_loss_m,
                "raw_peak_coordinate": processed.report.raw_peak_coordinate,
                "processed_peak_coordinate": processed.report.processed_peak_coordinate,
                "peak_horizontal_shift_m": processed.report.peak_horizontal_shift_m,
                "sampling_decision_reasons": processed.report.sampling_decision_reasons,
                "sampling_warnings": processed.report.sampling_warnings,
                "terrain_fidelity_status": processed.report.terrain_fidelity_status,
                "terrain_fidelity_passed": processed.report.terrain_fidelity_passed,
                "peak_elevation_loss_threshold_m": (
                    processed.report.peak_elevation_loss_threshold_m
                ),
                "peak_horizontal_shift_threshold_m": (
                    processed.report.peak_horizontal_shift_threshold_m
                ),
                "orientation_consistent": orientation_consistent,
                "orientation": {
                    "east_axis": "+X = East",
                    "north_axis": "+Y = North",
                    "up_axis": "+Z = Up",
                    "north_edge": "y=model_depth_mm",
                    "orientation_transform": (
                        "mesh_heightfield=flipud(processed_dem); "
                        "source row 0 north maps to y=model_depth_mm"
                    ),
                    "expected_processed_peak_mm": expected_peak_mm,
                    "reopened_format_peak_coordinates_mm": format_peaks,
                    "coordinate_correction_only": True,
                },
            }
        )
        validation["required_checks_passed"] = _required_validation_passed(validation)
        if not validation["required_checks_passed"]:
            raise MeshValidationError(
                "Generated STL failed a required geometry invariant; "
                "inspect the retained command log"
            )
        if three_mf is not None and not validation.get("three_mf_dimensions_match"):
            raise MeshValidationError("Reopened 3MF dimensions differ from the manufacturing mesh")

        render_elevation_preview(
            processed.elevations_m,
            artifacts["preview_png"],
            title=processed.report.metadata.dataset_name,
        )
        dump_resolved_config(resolved_config, artifacts["resolved_config"])
        generated_at = datetime.now(UTC).isoformat()
        recorded_provider_selection = (
            (source_acquisition or {}).get("provider_selection")
            if source_acquisition is not None
            else None
        )
        if not isinstance(recorded_provider_selection, dict):
            recorded_provider_selection = {
                "selected": processed.report.metadata.provider,
                "reason": (
                    [
                        "user supplied a local elevation raster",
                        "local inputs have priority over network providers",
                        "no provider fallback was required",
                    ]
                    if processed.report.metadata.provider == "local"
                    else list(
                        (source_acquisition or {})
                        .get("plan", {})
                        .get(
                            "decisions",
                            ["provider acquisition manifest selected this cached raster"],
                        )
                    )
                ),
                "attempted_providers": [
                    {
                        "provider": processed.report.metadata.provider,
                        "status": "selected",
                        "dataset": processed.report.metadata.dataset_name,
                    }
                ],
                "source_acquisition_artifact": (
                    "source_acquisition.json" if source_acquisition is not None else None
                ),
            }
        provenance: dict[str, Any] = {
            "topoforge_version": __version__,
            "generated_at": generated_at,
            "provider_selection": recorded_provider_selection,
            "source_acquisition": source_acquisition,
            "dataset": processed.report.metadata.model_dump(mode="json"),
            "source_file_checksums": processed.report.metadata.checksums,
            "manufacturing_preflight": manufacturing_preflight.model_dump(mode="json"),
            "elevation_reference_comparison": {
                "raw_dem_peak_elevation_m": processed.report.raw_elevation_max_m,
                "published_reference_peak_elevation_m": config.reference_peak_elevation_m,
                "reference_minus_raw_dem_m": reference_peak_difference_m,
                "note": config.reference_peak_elevation_note,
                "dataset_semantics": processed.report.metadata.dataset_type.value,
                "vertical_datum": processed.report.metadata.vertical_datum,
                "terrain_adjustment_applied": False,
                "interpretation": (
                    "The published nominal elevation is contextual provenance only; "
                    "the model retains measured source raster elevations without "
                    "an artificial peak."
                ),
            },
            "processing": {
                "pipeline": [
                    "read metadata",
                    "normalize to a north-up metric CRS when required",
                    "resolve print-aware/source-preserving/custom sampling",
                    (
                        "enforce deterministic cell, triangle, and memory budgets "
                        "using average resampling"
                    ),
                    "preserve original NoData mask",
                    "interpolate bounded interior holes from nearest valid cells",
                    "write processed DEM",
                ],
                "processed_crs": processed.report.crs,
                "automatic_projection_choice": processed.report.crs,
                "array_shape": processed.report.array_shape,
                "source_grid_shape": processed.report.source_grid_shape,
                "processed_grid_shape": processed.report.processed_grid_shape,
                "source_horizontal_resolution_m": (processed.report.source_horizontal_resolution_m),
                "processed_horizontal_resolution_m": (
                    processed.report.processed_horizontal_resolution_m
                ),
                "downsampling_factor": processed.report.downsampling_factor,
                "physical_sample_spacing_mm": processed.report.physical_sample_spacing_mm,
                "physical_sample_spacing_xy_mm": (processed.report.physical_sample_spacing_xy_mm),
                "estimated_triangle_count": processed.report.estimated_triangle_count,
                "estimated_memory_mb": processed.report.estimated_memory_mb,
                "sampling_mode": config.sampling_mode.value,
                "mesh_sampling_mm": config.mesh_sampling_mm,
                "max_grid_cells": config.max_grid_cells,
                "max_estimated_triangles": config.max_estimated_triangles,
                "max_estimated_memory_mb": config.max_estimated_memory_mb,
                "resource_budget_mode": config.resource_budget_mode.value,
                "sampling_decision_reasons": processed.report.sampling_decision_reasons,
                "sampling_warnings": processed.report.sampling_warnings,
                "horizontal_resolution_m": (processed.report.processed_horizontal_resolution_m),
                "nodata_percentage": processed.report.original_nodata_fraction * 100.0,
                "interpolated_percentage": processed.report.interpolated_fraction * 100.0,
                "original_nodata_mask": "original_nodata_mask.tif",
                "elevation_min_m": processed.report.elevation_min_m,
                "elevation_max_m": processed.report.elevation_max_m,
                "raw_elevation_min_m": processed.report.raw_elevation_min_m,
                "raw_elevation_max_m": processed.report.raw_elevation_max_m,
                "processed_elevation_min_m": processed.report.processed_elevation_min_m,
                "processed_elevation_max_m": processed.report.processed_elevation_max_m,
                "peak_elevation_loss_m": processed.report.peak_elevation_loss_m,
                "raw_peak_coordinate": processed.report.raw_peak_coordinate,
                "processed_peak_coordinate": processed.report.processed_peak_coordinate,
                "peak_horizontal_shift_m": processed.report.peak_horizontal_shift_m,
                "peak_elevation_loss_threshold_m": (
                    processed.report.peak_elevation_loss_threshold_m
                ),
                "peak_horizontal_shift_threshold_m": (
                    processed.report.peak_horizontal_shift_threshold_m
                ),
                "terrain_fidelity_status": processed.report.terrain_fidelity_status,
                "terrain_fidelity_passed": processed.report.terrain_fidelity_passed,
                "source_bounds": processed.report.source_bounds,
                "aoi": processed.report.aoi,
            },
            "orientation": {
                "east_axis": "+X = East",
                "north_axis": "+Y = North",
                "up_axis": "+Z = Up",
                "north_edge": "y=model_depth_mm",
                "source_bounds": processed.report.source_bounds,
                "orientation_transform": (
                    "mesh_heightfield=flipud(processed_dem); "
                    "source row 0 north maps to y=model_depth_mm"
                ),
                "coordinate_correction_only": True,
            },
            "scaling": scaling.model_dump(mode="json"),
            "geometry": {
                "construction": "explicit regular-grid top, four boundary walls, and flat bottom",
                "manufacturing_units": "millimetres",
                "self_intersection_status": validation["self_intersection_status"],
            },
            "formats": {
                "stl": {"units_contract": "millimetres"},
                "3mf": (
                    {
                        "writer": "lib3mf",
                        "lib3mf_version": three_mf.lib3mf_version,
                        "strict_warning_count": three_mf.strict_warning_count,
                    }
                    if three_mf is not None
                    else None
                ),
                "glb": {"role": "preview only"},
            },
            "license_note": (
                "Dataset terms are separate from the Apache-2.0 TopoForge code license."
            ),
        }
        provenance.update(
            {
                "source_horizontal_resolution_m": (processed.report.source_horizontal_resolution_m),
                "processed_horizontal_resolution_m": (
                    processed.report.processed_horizontal_resolution_m
                ),
                "source_grid_shape": processed.report.source_grid_shape,
                "processed_grid_shape": processed.report.processed_grid_shape,
                "downsampling_factor": processed.report.downsampling_factor,
                "physical_sample_spacing_mm": processed.report.physical_sample_spacing_mm,
                "estimated_triangle_count": processed.report.estimated_triangle_count,
                "raw_elevation_min_m": processed.report.raw_elevation_min_m,
                "raw_elevation_max_m": processed.report.raw_elevation_max_m,
                "processed_elevation_min_m": processed.report.processed_elevation_min_m,
                "processed_elevation_max_m": processed.report.processed_elevation_max_m,
                "peak_elevation_loss_m": processed.report.peak_elevation_loss_m,
                "raw_peak_coordinate": processed.report.raw_peak_coordinate,
                "processed_peak_coordinate": processed.report.processed_peak_coordinate,
                "peak_horizontal_shift_m": processed.report.peak_horizontal_shift_m,
                "sampling_decision_reasons": processed.report.sampling_decision_reasons,
                "terrain_fidelity_status": processed.report.terrain_fidelity_status,
            }
        )
        write_json(artifacts["validation_json"], validation)
        write_validation_html(artifacts["validation_html"], validation)
        write_json(artifacts["provenance"], provenance)

        checksums = {
            key: sha256_file(path)
            for key, path in sorted(artifacts.items())
            if key != "manifest" and path.is_file()
        }
        write_json(
            artifacts["manifest"],
            {
                "topoforge_version": __version__,
                "generated_at": generated_at,
                "source_sha256": sha256_file(source),
                "resolved_config_sha256": sha256_file(artifacts["resolved_config"]),
                "artifacts": {key: path.name for key, path in sorted(artifacts.items())},
                "sha256": checksums,
            },
        )
        verify_artifact_bundle(stage, required_formats=config.output_formats)
        stage.replace(output)
        final_artifacts = {key: output / path.name for key, path in artifacts.items()}
        return BuildResult(output, final_artifacts, validation, provenance)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_artifact_bundle(
    output_dir: Path, required_formats: list[str] | None = None
) -> dict[str, Any]:
    """Reopen every required output role and return literal verification measurements."""
    formats = required_formats or ["stl", "3mf", "glb"]
    required = [
        "processed_dem.tif",
        "original_nodata_mask.tif",
        "manufacturing_preflight.json",
        "provenance.json",
        "validation.json",
        "validation.html",
        "build_config.resolved.yaml",
        "preview.png",
        "build_manifest.json",
    ]
    required.extend(
        {"stl": "model.stl", "3mf": "model.3mf", "glb": "preview.glb"}[item] for item in formats
    )
    missing = [
        name
        for name in required
        if not (output_dir / name).is_file() or (output_dir / name).stat().st_size == 0
    ]
    if missing:
        raise MeshValidationError(
            f"Artifact bundle is missing non-empty files: {', '.join(missing)}"
        )

    with rasterio.open(output_dir / "processed_dem.tif") as dataset:
        raster_shape = (dataset.height, dataset.width)
        raster_crs = str(dataset.crs)
        if dataset.count != 1 or dataset.crs is None or not np.all(np.isfinite(dataset.read(1))):
            raise MeshValidationError(
                "Reopened processed_dem.tif failed finite single-band CRS checks"
            )
    with rasterio.open(output_dir / "original_nodata_mask.tif") as dataset:
        mask_values = np.unique(dataset.read(1))
        if not set(int(value) for value in mask_values).issubset({0, 1}):
            raise MeshValidationError("Reopened original_nodata_mask.tif is not binary")
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    validation = json.loads((output_dir / "validation.json").read_text(encoding="utf-8"))
    preflight = ManufacturingPreflightReport.model_validate_json(
        (output_dir / "manufacturing_preflight.json").read_text(encoding="utf-8")
    )
    if preflight.status not in {"passed", "passed-with-warnings"}:
        raise MeshValidationError("manufacturing_preflight.json did not pass")
    preflight_payload = preflight.model_dump(mode="json")
    if validation.get("manufacturing_preflight") != preflight_payload:
        raise MeshValidationError("validation.json manufacturing preflight does not match artifact")
    if provenance.get("manufacturing_preflight") != preflight_payload:
        raise MeshValidationError("provenance.json manufacturing preflight does not match artifact")
    manifest = json.loads((output_dir / "build_manifest.json").read_text(encoding="utf-8"))
    manifest_artifacts = manifest.get("artifacts", {})
    manifest_checksums = manifest.get("sha256", {})
    if not isinstance(manifest_artifacts, dict) or not isinstance(manifest_checksums, dict):
        raise MeshValidationError("build_manifest.json artifact/checksum maps are invalid")
    for role, expected_sha256 in manifest_checksums.items():
        artifact_name = manifest_artifacts.get(role)
        if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
            raise MeshValidationError(f"Manifest role {role!r} has an unsafe artifact path")
        artifact_path = output_dir / artifact_name
        if not artifact_path.is_file():
            raise MeshValidationError(f"Manifest role {role!r} points to a missing artifact")
        actual_sha256 = sha256_file(artifact_path)
        if actual_sha256 != expected_sha256:
            raise MeshValidationError(
                f"Manifest checksum mismatch for {role}: expected {expected_sha256}, "
                f"found {actual_sha256}"
            )
    source_acquisition_artifact = output_dir / "source_acquisition.json"
    if source_acquisition_artifact.is_file():
        source_acquisition = json.loads(source_acquisition_artifact.read_text(encoding="utf-8"))
        quality_masks = source_acquisition.get("quality_masks", [])
        if not isinstance(quality_masks, list):
            raise MeshValidationError("source_acquisition quality_masks is not a list")
        for record in quality_masks:
            if not isinstance(record, dict) or record.get("availability") != "present":
                continue
            role = record.get("role")
            output_record = record.get("output")
            if not isinstance(role, str) or not isinstance(output_record, dict):
                raise MeshValidationError("Present source quality mask metadata is invalid")
            artifact_role = f"source_quality_{role}"
            bundled_artifact = output_record.get("bundled_artifact")
            if manifest_artifacts.get(artifact_role) != bundled_artifact:
                raise MeshValidationError(
                    f"Source quality mask role {role} is not bound to its bundle artifact"
                )
            quality_path = output_dir / str(bundled_artifact)
            with rasterio.open(quality_path) as quality_dataset:
                expected_shape = tuple(output_record.get("grid_shape", []))
                expected_transform = tuple(output_record.get("transform", []))
                expected_crs = output_record.get("crs")
                if (
                    quality_dataset.count != 1
                    or quality_dataset.shape != expected_shape
                    or RasterioCRS.from_user_input(expected_crs) != quality_dataset.crs
                    or len(expected_transform) != 6
                    or not np.allclose(
                        tuple(quality_dataset.transform)[:6],
                        expected_transform,
                        atol=1e-12,
                        rtol=0.0,
                    )
                ):
                    raise MeshValidationError(
                        f"Bundled source quality mask is not aligned as recorded: {role}"
                    )
    resolved = yaml.safe_load(
        (output_dir / "build_config.resolved.yaml").read_text(encoding="utf-8")
    )
    with Image.open(output_dir / "preview.png") as preview:
        preview.verify()
    if "stl" in formats:
        reopened_stl = _load_stl(output_dir / "model.stl")
        if not reopened_stl.is_watertight:
            raise MeshValidationError("Reopened model.stl is not watertight")
    if "3mf" in formats:
        three_mf = inspect_3mf(output_dir / "model.3mf")
        metadata_names = set(three_mf.metadata)
        for required_suffix in (
            "east_axis",
            "north_axis",
            "north_edge",
            "source_bounds",
            "orientation_transform",
        ):
            if not any(name.endswith(required_suffix) for name in metadata_names):
                raise MeshValidationError(
                    f"Reopened model.3mf is missing orientation metadata {required_suffix}"
                )
    if "glb" in formats:
        glb = trimesh.load(output_dir / "preview.glb", force="scene")
        if not isinstance(glb, trimesh.Scene) or len(glb.geometry) == 0:
            raise MeshValidationError("Reopened preview.glb has no geometry")
    return {
        "raster_shape": raster_shape,
        "raster_crs": raster_crs,
        "required_checks_passed": validation.get("required_checks_passed"),
        "dataset_name": provenance.get("dataset", {}).get("dataset_name"),
        "resolved_output_dir": resolved.get("output_dir"),
        "manifest_artifact_count": len(manifest.get("artifacts", {})),
    }


def record_slice_validation(build_dir: Path, result: dict[str, Any]) -> Path:
    """Attach one actually executed slicer result to an existing verified build."""
    build_dir = build_dir.expanduser().resolve()
    validation_path = build_dir / "validation.json"
    provenance_path = build_dir / "provenance.json"
    manifest_path = build_dir / "build_manifest.json"
    if not all(path.is_file() for path in (validation_path, provenance_path, manifest_path)):
        raise MeshValidationError(f"{build_dir} is not a completed TopoForge artifact bundle")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved_config = yaml.safe_load(
        (build_dir / "build_config.resolved.yaml").read_text(encoding="utf-8")
    )
    printer_profile_id = str(resolved_config["printer_profile"]["profile_id"])
    manufacturing_validation = evaluate_bambu_p2s_release_gate(
        result, printer_profile_id=printer_profile_id
    )
    slicer_path = build_dir / "slicer_validation.json"
    write_json(slicer_path, result)
    bambu_path = build_dir / "bambu_studio_validation.json"
    write_json(
        bambu_path,
        {
            "manufacturing_validation": manufacturing_validation,
            "slicer_result": result,
        },
    )
    validation["slicer_result"] = result
    validation["manufacturing_validation"] = manufacturing_validation
    slicer_record = result.get("slicer")
    slicer_name = slicer_record.get("name") if isinstance(slicer_record, dict) else None
    release_role = "official-p2s-release" if slicer_name == "BambuStudio" else "diagnostic"
    provenance["slicer_validation"] = {
        "slicer": slicer_record,
        "release_role": release_role,
        "profile": result.get("profile"),
        "success": result.get("status") == "succeeded",
        "gcode_generated": result.get("gcode_generated"),
        "gcode_size_bytes": result.get("gcode_size_bytes"),
        "metrics": result.get("metrics"),
        "manufacturing_validation": manufacturing_validation,
        "report": slicer_path.name,
        "bambu_report": bambu_path.name,
    }
    write_json(validation_path, validation)
    write_validation_html(build_dir / "validation.html", validation)
    write_json(provenance_path, provenance)
    manifest.setdefault("artifacts", {})["slicer_validation"] = slicer_path.name
    manifest.setdefault("artifacts", {})["bambu_studio_validation"] = bambu_path.name
    manifest.setdefault("sha256", {})["slicer_validation"] = sha256_file(slicer_path)
    manifest.setdefault("sha256", {})["bambu_studio_validation"] = sha256_file(bambu_path)
    manifest["sha256"]["validation_json"] = sha256_file(validation_path)
    manifest["sha256"]["validation_html"] = sha256_file(build_dir / "validation.html")
    manifest["sha256"]["provenance"] = sha256_file(provenance_path)
    write_json(manifest_path, manifest)
    verify_artifact_bundle(build_dir)
    if (
        manufacturing_validation["required"]
        and slicer_name == "BambuStudio"
        and not manufacturing_validation["release_gate_passed"]
    ):
        raise MeshValidationError(
            "Bambu Lab P2S release gate failed; inspect bambu_studio_validation.json"
        )
    return slicer_path
