"""Publication and strict verification of local overlay artifact bundles."""

from __future__ import annotations

import html
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
import yaml
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel
from shapely.geometry import LineString, MultiLineString, Point, mapping
from shapely.geometry.base import BaseGeometry

from topoforge import __version__
from topoforge.engine import verify_artifact_bundle
from topoforge.exceptions import ConfigurationError
from topoforge.exporters.mesh import export_stl
from topoforge.exporters.three_mf import (
    ThreeMFObject,
    export_3mf_objects,
    inspect_3mf,
)
from topoforge.models import BuildConfig, ScalingResult
from topoforge.overlays.geometry import (
    ModelOverlayFeature,
    build_layer_mesh,
    build_terrain_surface,
    generate_contour_features,
    transform_features_to_model,
)
from topoforge.overlays.models import (
    OverlayConfig,
    OverlayFormat,
    OverlayKind,
    OverlayLayerRecord,
    OverlayManifest,
    OverlaySourceConfig,
    OverlaySourceRecord,
    OverlayValidation,
)
from topoforge.overlays.sources import parse_local_source
from topoforge.provenance import write_json
from topoforge.util import sha256_bytes, sha256_file


@dataclass(frozen=True, slots=True)
class OverlayBundleResult:
    """Published overlay bundle and its strict measurements."""

    output_dir: Path
    manifest_path: Path
    validation_path: Path
    model_3mf_path: Path
    preview_glb_path: Path
    preview_png_path: Path
    validation: OverlayValidation


def _canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def overlay_identity_payload(config: OverlayConfig) -> dict[str, Any]:
    """Return complete local source identities for workflow content addressing."""
    sources: list[dict[str, Any]] = []
    for source in config.sources:
        record: dict[str, Any] = source.model_dump(mode="json")
        if source.path is not None:
            path = source.path.expanduser().resolve()
            if not path.is_file():
                raise ConfigurationError(f"overlay source file does not exist: {path}")
            record["path"] = str(path)
            record["size_bytes"] = path.stat().st_size
            record["sha256"] = sha256_file(path)
        else:
            record["path"] = None
            record["size_bytes"] = 0
            record["sha256"] = None
        sources.append(record)
    return {
        "schema_version": "topoforge-overlay-request-v1",
        "config": config.model_dump(mode="json"),
        "sources": sources,
    }


def read_overlay_config(path: Path) -> OverlayConfig:
    """Read one strict local overlay YAML configuration."""
    resolved = path.expanduser().resolve()
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"overlay config is unreadable: {resolved}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("overlay config root must be a mapping")
    return OverlayConfig.model_validate(payload)


def write_overlay_config(config: OverlayConfig, path: Path) -> Path:
    """Write and strictly reopen a deterministic overlay YAML configuration."""
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(destination)
    if read_overlay_config(destination) != config:
        raise ConfigurationError("overlay config failed strict YAML reopen")
    return destination


def _resolved_config_payload(
    config: OverlayConfig,
    *,
    source_bundle: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "topoforge-overlay-config-v1",
        "source_bundle": str(source_bundle),
        "overlay": config.model_dump(mode="json"),
        "source_identities": overlay_identity_payload(config)["sources"],
    }


def _write_resolved_config(
    path: Path,
    config: OverlayConfig,
    *,
    source_bundle: Path,
) -> Path:
    payload = _resolved_config_payload(
        config,
        source_bundle=source_bundle,
    )
    path.write_text(yaml.safe_dump(payload, sort_keys=True, allow_unicode=True), encoding="utf-8")
    reopened = yaml.safe_load(path.read_text(encoding="utf-8"))
    if reopened != payload:
        raise ConfigurationError("resolved overlay config failed strict YAML reopen")
    return path


def _hex_rgba(value: str) -> tuple[int, int, int, int]:
    return (
        int(value[1:3], 16),
        int(value[3:5], 16),
        int(value[5:7], 16),
        255,
    )


def _atomic_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return path


def _export_scene_glb(
    path: Path,
    terrain: trimesh.Trimesh,
    layers: tuple[tuple[OverlaySourceConfig, trimesh.Trimesh], ...],
) -> Path:
    scene = trimesh.Scene(base_frame="world")
    terrain_copy = terrain.copy()
    terrain_copy.visual.face_colors = np.tile(  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
        np.asarray((184, 190, 180, 255), dtype=np.uint8), (len(terrain_copy.faces), 1)
    )
    scene.add_geometry(terrain_copy, geom_name="terrain", node_name="terrain")
    for source, mesh in layers:
        colored = mesh.copy()
        colored.visual.face_colors = np.tile(  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
            np.asarray(_hex_rgba(source.resolved_color), dtype=np.uint8),
            (len(colored.faces), 1),
        )
        name = f"overlay-{source.source_id}"
        scene.add_geometry(colored, geom_name=name, node_name=name)
    payload = scene.export(file_type="glb")
    if not isinstance(payload, bytes | bytearray) or not payload:
        raise ConfigurationError("Trimesh returned an empty overlay GLB")
    return _atomic_bytes(path, bytes(payload))


def _draw_geometry(
    draw: ImageDraw.ImageDraw,
    geometry: BaseGeometry,
    *,
    width_px: int,
    height_px: int,
    model_width_mm: float,
    model_depth_mm: float,
    color: str,
    line_width_px: int,
) -> None:
    def convert(line: LineString) -> list[tuple[float, float]]:
        return [
            (
                float(x) / model_width_mm * (width_px - 1),
                (1.0 - float(y) / model_depth_mm) * (height_px - 1),
            )
            for x, y in line.coords
        ]

    if isinstance(geometry, LineString):
        draw.line(convert(geometry), fill=color, width=line_width_px, joint="curve")
    elif isinstance(geometry, MultiLineString):
        for line in geometry.geoms:
            draw.line(convert(line), fill=color, width=line_width_px, joint="curve")


def _render_preview(
    path: Path,
    surface: Any,
    source_features: tuple[tuple[OverlaySourceConfig, tuple[ModelOverlayFeature, ...]], ...],
    preview_width_px: int,
) -> tuple[int, int]:
    elevations = surface.elevations_m_north.astype(np.float64)
    low, high = np.percentile(elevations, (1.0, 99.0))
    span = max(float(high - low), np.finfo(np.float64).eps)
    normalized = np.clip((elevations - low) / span, 0.0, 1.0)
    gradient_y, gradient_x = np.gradient(elevations)
    slope = np.hypot(gradient_x, gradient_y)
    slope /= max(float(np.percentile(slope, 99.0)), np.finfo(np.float64).eps)
    luminance = np.clip(225.0 - normalized * 105.0 - slope * 45.0, 55.0, 235.0)
    base = Image.fromarray(luminance.astype(np.uint8)).convert("RGB")
    preview_height_px = max(
        320,
        round(preview_width_px * surface.model_depth_mm / surface.model_width_mm),
    )
    base = base.resize((preview_width_px, preview_height_px), resample=Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(base)
    font = ImageFont.load_default(size=16)
    for source, features in source_features:
        line_width_px = max(
            2,
            round(source.style.line_width_mm / surface.model_width_mm * preview_width_px),
        )
        for feature in features:
            if source.kind is OverlayKind.LABEL and isinstance(feature.geometry, Point):
                if feature.label is None:
                    continue
                x = float(feature.geometry.x) / surface.model_width_mm * (preview_width_px - 1)
                y = (1.0 - float(feature.geometry.y) / surface.model_depth_mm) * (
                    preview_height_px - 1
                )
                draw.text((x, y), feature.label, fill=source.resolved_color, font=font, anchor="mm")
            else:
                _draw_geometry(
                    draw,
                    feature.geometry,
                    width_px=preview_width_px,
                    height_px=preview_height_px,
                    model_width_mm=surface.model_width_mm,
                    model_depth_mm=surface.model_depth_mm,
                    color=source.resolved_color,
                    line_width_px=line_width_px,
                )
    margin = 24
    arrow_bottom = 82
    draw.line((margin, arrow_bottom, margin, margin), fill="#111111", width=4)
    draw.polygon(
        ((margin, margin), (margin - 8, margin + 16), (margin + 8, margin + 16)),
        fill="#111111",
    )
    draw.text((margin + 12, margin - 6), "N", fill="#111111", font=font)
    draw.rectangle((0, 0, preview_width_px - 1, preview_height_px - 1), outline="#111111", width=2)
    temporary = path.with_name(f".{path.name}.tmp")
    base.save(temporary, format="PNG", optimize=False, compress_level=9)
    temporary.replace(path)
    return preview_width_px, preview_height_px


def _write_overlay_html(path: Path, validation: OverlayValidation) -> Path:
    rows = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td><code>"
        f"{html.escape(json.dumps(value, ensure_ascii=False, default=str))}</code></td></tr>"
        for key, value in sorted(validation.model_dump(mode="json").items())
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>TopoForge overlay validation</title>
<style>body{{font:15px system-ui;margin:2rem;max-width:1100px;color:#1d252b}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;vertical-align:top;
border:1px solid #ccd4d9;padding:.55rem}}th{{width:32%;background:#f2f5f6}}
code{{white-space:pre-wrap}}</style></head><body><h1>TopoForge overlay validation</h1>
<p><strong>Required checks: {"PASS" if validation.required_checks_passed else "FAIL"}</strong></p>
<p>Overlay objects are independent; the source terrain surface is not rewritten.</p>
<table>{rows}</table></body></html>"""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)
    return path


def _source_terrain_hashes(bundle: Path) -> dict[str, str]:
    names = (
        "processed_dem.tif",
        "original_nodata_mask.tif",
        "model.stl",
        "model.3mf",
        "preview.glb",
    )
    return {name: sha256_file(bundle / name) for name in names}


def _safe_artifact_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise ConfigurationError(f"overlay artifact escapes bundle: {path}")
    return path


def generate_overlay_bundle(
    source_bundle_dir: Path,
    config: OverlayConfig,
    output_dir: Path,
) -> OverlayBundleResult:
    """Generate a deterministic overlay bundle without modifying source terrain artifacts."""
    source_bundle = source_bundle_dir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if output.exists():
        raise ConfigurationError(f"overlay output already exists: {output}")
    verify_artifact_bundle(source_bundle)
    build_manifest_path = source_bundle / "build_manifest.json"
    source_build_manifest_sha256 = sha256_file(build_manifest_path)
    source_hashes_before = _source_terrain_hashes(source_bundle)
    provenance = json.loads((source_bundle / "provenance.json").read_text(encoding="utf-8"))
    scaling = ScalingResult.model_validate(provenance.get("scaling"))
    build_config = BuildConfig.model_validate(
        yaml.safe_load((source_bundle / "build_config.resolved.yaml").read_text(encoding="utf-8"))
    )
    surface = build_terrain_surface(
        source_bundle / "processed_dem.tif",
        source_bundle / "original_nodata_mask.tif",
        scaling,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    try:
        layers_directory = staging / "layers"
        layers_directory.mkdir(parents=True)
        resolved_config_path = _write_resolved_config(
            staging / "overlay_config.resolved.yaml",
            config,
            source_bundle=source_bundle,
        )
        source_records: list[OverlaySourceRecord] = []
        layer_records: list[OverlayLayerRecord] = []
        layer_meshes: list[tuple[OverlaySourceConfig, trimesh.Trimesh]] = []
        source_features: list[tuple[OverlaySourceConfig, tuple[ModelOverlayFeature, ...]]] = []
        plan_features: list[dict[str, Any]] = []
        total_input_features = 0
        total_triangles = 0
        for source in config.sources:
            contour_levels: tuple[float, ...] = ()
            if source.format is OverlayFormat.GENERATED_CONTOURS:
                parsed, contour_levels = generate_contour_features(surface, source)
                effective_source = source.model_copy(update={"source_crs": surface.processed_crs})
                source_path = source_bundle / "processed_dem.tif"
            else:
                parsed = parse_local_source(source)
                effective_source = source
                if source.path is None:
                    raise AssertionError("validated local overlay path disappeared")
                source_path = source.path.expanduser().resolve()
            total_input_features += len(parsed)
            if total_input_features > config.max_features:
                raise ConfigurationError(
                    f"overlay request exceeds max_features={config.max_features}; "
                    "simplify or split the local sources"
                )
            transformed = transform_features_to_model(
                surface,
                effective_source,
                parsed,
                clip_to_model=config.clip_to_model,
            )
            if not transformed.features:
                raise ConfigurationError(
                    f"overlay source {source.source_id} does not intersect the model"
                )
            mesh, surface_error, nodata_overlap, _ = build_layer_mesh(
                surface,
                source,
                transformed.features,
                minimum_feature_mm=build_config.printer_profile.minimum_feature_mm,
                allow_original_nodata=config.allow_original_nodata,
            )
            total_triangles += len(mesh.faces)
            if total_triangles > config.max_triangles:
                raise ConfigurationError(
                    f"overlay request exceeds max_triangles={config.max_triangles}; "
                    "increase line width/simplification or reduce feature count"
                )
            layer_path = layers_directory / f"{source.source_id}.stl"
            export_stl(mesh, layer_path)
            reopened = trimesh.load_mesh(layer_path, process=True)
            if not isinstance(reopened, trimesh.Trimesh):
                raise ConfigurationError(f"overlay STL did not reopen as a mesh: {layer_path}")
            components = reopened.split(only_watertight=False)
            layer_record = OverlayLayerRecord(
                source_id=source.source_id,
                kind=source.kind,
                object_name=f"TopoForge overlay {source.source_id}",
                stl_path=layer_path.relative_to(staging).as_posix(),
                stl_sha256=sha256_file(layer_path),
                feature_count=len(transformed.features),
                vertex_count=len(reopened.vertices),
                triangle_count=len(reopened.faces),
                connected_components=len(components),
                volume_mm3=float(reopened.volume),
                bounds_mm=(
                    (
                        float(reopened.bounds[0, 0]),
                        float(reopened.bounds[0, 1]),
                        float(reopened.bounds[0, 2]),
                    ),
                    (
                        float(reopened.bounds[1, 0]),
                        float(reopened.bounds[1, 1]),
                        float(reopened.bounds[1, 2]),
                    ),
                ),
                watertight=bool(reopened.is_watertight),
                winding_consistent=bool(reopened.is_winding_consistent),
                positive_volume=float(reopened.volume) > 0,
                maximum_surface_mapping_error_mm=surface_error,
                original_nodata_overlap_mm2=nodata_overlap,
                color=source.resolved_color,
            )
            layer_records.append(layer_record)
            layer_meshes.append((source, mesh))
            source_features.append((source, transformed.features))
            source_records.append(
                OverlaySourceRecord(
                    source_id=source.source_id,
                    kind=source.kind,
                    format=source.format,
                    path=str(source_path),
                    sha256=sha256_file(source_path),
                    size_bytes=source_path.stat().st_size,
                    source_crs=effective_source.source_crs,
                    processed_crs=surface.processed_crs,
                    dataset_name=source.dataset_name,
                    dataset_version=source.dataset_version,
                    license=source.license,
                    attribution=source.attribution,
                    source_urls=source.source_urls,
                    acquisition_period=source.acquisition_period,
                    input_feature_count=transformed.input_feature_count,
                    output_feature_count=len(transformed.features),
                    clipped_feature_count=transformed.clipped_feature_count,
                    dropped_feature_count=transformed.dropped_feature_count,
                    input_length_m=transformed.input_length_m,
                    clipped_length_m=transformed.clipped_length_m,
                    contour_levels_m=contour_levels,
                )
            )
            for feature in transformed.features:
                properties = {
                    "source_id": source.source_id,
                    "feature_id": feature.feature_id,
                    "kind": source.kind.value,
                    "color": source.resolved_color,
                    **feature.properties,
                }
                if feature.label is not None:
                    properties["label"] = feature.label
                plan_features.append(
                    {
                        "type": "Feature",
                        "geometry": mapping(feature.geometry),
                        "properties": properties,
                    }
                )

        terrain = trimesh.load_mesh(source_bundle / "model.stl", process=True)
        if not isinstance(terrain, trimesh.Trimesh):
            raise ConfigurationError("source terrain STL did not reopen as one mesh")
        config_digest = sha256_bytes(_canonical_bytes(overlay_identity_payload(config)))
        objects = (
            ThreeMFObject(
                name="TopoForge terrain",
                mesh=terrain,
                part_number="topoforge-terrain",
            ),
            *(
                ThreeMFObject(
                    name=f"TopoForge overlay {source.source_id}",
                    mesh=mesh,
                    part_number=f"topoforge-overlay-{source.source_id}",
                )
                for source, mesh in layer_meshes
            ),
        )
        model_3mf = export_3mf_objects(
            objects,
            staging / "model-with-overlays.3mf",
            title="TopoForge terrain with local overlays",
            metadata={
                "topoforge_version": __version__,
                "source_build_manifest_sha256": source_build_manifest_sha256,
                "overlay_config_sha256": config_digest,
                "east_axis": "+X = East",
                "north_axis": "+Y = North",
                "up_axis": "+Z = Up",
                "north_edge": "y=model_depth_mm",
                "terrain_surface_modified": "false",
                "overlay_object_count": str(len(layer_meshes)),
            },
        )
        preview_glb = _export_scene_glb(
            staging / "preview-with-overlays.glb",
            terrain,
            tuple(layer_meshes),
        )
        plan_path = write_json(
            staging / "overlay-plan.geojson",
            {
                "type": "FeatureCollection",
                "coordinate_system": "+X East, +Y North, millimetres",
                "terrain_surface_modified": False,
                "features": plan_features,
            },
        )
        preview_size = _render_preview(
            staging / "overlay-preview.png",
            surface,
            tuple(source_features),
            config.preview_width_px,
        )
        source_hashes_after = _source_terrain_hashes(source_bundle)
        three_mf = inspect_3mf(model_3mf)
        reopened_scene = trimesh.load(preview_glb, force="scene")
        if not isinstance(reopened_scene, trimesh.Scene):
            raise ConfigurationError("overlay preview GLB did not reopen as a scene")
        layer_checks = all(
            layer.watertight and layer.winding_consistent and layer.positive_volume
            for layer in layer_records
        )
        bounds_passed = all(
            layer.bounds_mm[0][0] >= -1e-6
            and layer.bounds_mm[0][1] >= -1e-6
            and layer.bounds_mm[0][2] >= -1e-6
            and layer.bounds_mm[1][0] <= surface.model_width_mm + 1e-6
            and layer.bounds_mm[1][1] <= surface.model_depth_mm + 1e-6
            and layer.bounds_mm[1][2] <= build_config.printer_profile.build_volume_mm[2] + 1e-6
            for layer in layer_records
        )
        nodata_passed = config.allow_original_nodata or all(
            layer.original_nodata_overlap_mm2 <= 1e-9 for layer in layer_records
        )
        format_reopen_passed = (
            three_mf.object_count == len(layer_records) + 1
            and three_mf.build_item_count == 1
            and three_mf.components_object_count == 1
            and three_mf.component_count == len(layer_records) + 1
            and three_mf.base_material_group_count == 1
            and three_mf.material_assigned_object_count == len(layer_records) + 1
            and three_mf.strict_warning_count == 0
            and len(reopened_scene.geometry) == len(layer_records) + 1
        )
        validation = OverlayValidation(
            source_bundle=str(source_bundle),
            source_build_manifest_sha256=source_build_manifest_sha256,
            source_terrain_sha256_before=source_hashes_before,
            source_terrain_sha256_after=source_hashes_after,
            terrain_artifacts_unchanged=source_hashes_before == source_hashes_after,
            coordinate_system="+X = East, +Y = North, +Z = Up",
            orientation_transform=(
                "source row 0 north maps to y=model_depth_mm; vector CRS transforms to the "
                "processed metric CRS and then the exact sample-centre model frame"
            ),
            source_records=tuple(source_records),
            layers=tuple(layer_records),
            total_feature_count=sum(layer.feature_count for layer in layer_records),
            total_triangle_count=sum(layer.triangle_count for layer in layer_records),
            combined_3mf_object_count=three_mf.object_count,
            combined_3mf_build_item_count=three_mf.build_item_count,
            combined_3mf_components_object_count=three_mf.components_object_count,
            combined_3mf_component_count=three_mf.component_count,
            combined_3mf_base_material_group_count=three_mf.base_material_group_count,
            combined_3mf_material_assigned_object_count=(three_mf.material_assigned_object_count),
            combined_3mf_triangle_count=three_mf.triangle_count,
            combined_3mf_strict_warning_count=three_mf.strict_warning_count,
            combined_glb_geometry_count=len(reopened_scene.geometry),
            preview_size_px=preview_size,
            minimum_feature_mm=build_config.printer_profile.minimum_feature_mm,
            minimum_feature_checks_passed=True,
            original_nodata_check_passed=nodata_passed,
            bounds_check_passed=bounds_passed,
            layer_geometry_checks_passed=layer_checks,
            format_reopen_checks_passed=format_reopen_passed,
            deterministic_contract=(
                "sorted source order, exact source SHA-256, fixed CRS/sample mapping, "
                "stable mesh topology, stable 3MF UUIDs, canonical JSON/YAML"
            ),
            terrain_surface_modified=False,
            required_checks_passed=(
                source_hashes_before == source_hashes_after
                and layer_checks
                and bounds_passed
                and nodata_passed
                and format_reopen_passed
            ),
        )
        if not validation.required_checks_passed:
            failed_layers = [
                f"{layer.source_id}(watertight={layer.watertight}, "
                f"winding={layer.winding_consistent}, positive={layer.positive_volume})"
                for layer in validation.layers
                if not (layer.watertight and layer.winding_consistent and layer.positive_volume)
            ]
            raise ConfigurationError(
                "overlay validation failed: "
                f"terrain_unchanged={validation.terrain_artifacts_unchanged}, "
                f"layer_geometry={validation.layer_geometry_checks_passed}, "
                f"bounds={validation.bounds_check_passed}, "
                f"nodata={validation.original_nodata_check_passed}, "
                f"format_reopen={validation.format_reopen_checks_passed}, "
                f"failed_layers={failed_layers}"
            )
        validation_path = write_json(
            staging / "validation.json", validation.model_dump(mode="json")
        )
        validation_html = _write_overlay_html(staging / "validation.html", validation)
        provenance_path = write_json(
            staging / "provenance.json",
            {
                "schema_version": "topoforge-overlay-provenance-v1",
                "topoforge_version": __version__,
                "source_bundle": str(source_bundle),
                "source_build_manifest_sha256": source_build_manifest_sha256,
                "source_terrain_sha256": source_hashes_before,
                "coordinate_system": validation.coordinate_system,
                "orientation_transform": validation.orientation_transform,
                "surface_mapping": "exact fixed-diagonal terrain triangle interpolation",
                "contour_algorithm": "threshold-cell-boundary without fabricated elevation",
                "terrain_surface_modified": False,
                "three_mf_assembly": {
                    "mesh_object_count": three_mf.object_count,
                    "components_object_count": three_mf.components_object_count,
                    "component_count": three_mf.component_count,
                    "top_level_build_item_count": three_mf.build_item_count,
                    "single_material_group_count": three_mf.base_material_group_count,
                    "material_assigned_object_count": three_mf.material_assigned_object_count,
                    "contract": (
                        "named terrain/overlay mesh resources in one identity-transform "
                        "components assembly with one top-level build item"
                    ),
                },
                "sources": [record.model_dump(mode="json") for record in source_records],
                "layers": [record.model_dump(mode="json") for record in layer_records],
            },
        )
        artifacts = {
            "resolved_config": resolved_config_path.name,
            "model_3mf": model_3mf.name,
            "preview_glb": preview_glb.name,
            "preview_png": "overlay-preview.png",
            "plan_geojson": plan_path.name,
            "provenance": provenance_path.name,
            "validation_json": validation_path.name,
            "validation_html": validation_html.name,
        }
        layer_artifacts = {
            source.source_id: f"layers/{source.source_id}.stl" for source in config.sources
        }
        for source_id, relative in layer_artifacts.items():
            artifacts[f"layer_{source_id}_stl"] = relative
        checksums = {
            role: sha256_file(_safe_artifact_path(staging, relative))
            for role, relative in sorted(artifacts.items())
        }
        manifest = OverlayManifest(
            topoforge_version=__version__,
            source_bundle=str(source_bundle),
            source_build_manifest_sha256=source_build_manifest_sha256,
            overlay_config_sha256=sha256_file(resolved_config_path),
            source_identities=tuple(overlay_identity_payload(config)["sources"]),
            artifacts=artifacts,
            sha256=checksums,
            layer_artifacts=layer_artifacts,
            required_checks_passed=validation.required_checks_passed,
        )
        write_json(staging / "overlay_manifest.json", manifest.model_dump(mode="json"))
        verify_overlay_bundle(staging, source_bundle)
        staging.replace(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return OverlayBundleResult(
        output_dir=output,
        manifest_path=output / "overlay_manifest.json",
        validation_path=output / "validation.json",
        model_3mf_path=output / "model-with-overlays.3mf",
        preview_glb_path=output / "preview-with-overlays.glb",
        preview_png_path=output / "overlay-preview.png",
        validation=validation,
    )


def verify_overlay_bundle(
    output_dir: Path,
    source_bundle_dir: Path | None = None,
) -> dict[str, Any]:
    """Strictly reopen every overlay role and remeasure source and format bindings."""
    root = output_dir.expanduser().resolve()
    manifest_path = root / "overlay_manifest.json"
    validation_path = root / "validation.json"
    try:
        manifest = OverlayManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        validation = OverlayValidation.model_validate_json(
            validation_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"overlay manifest/validation is unreadable in {root}") from exc
    if not manifest.required_checks_passed or not validation.required_checks_passed:
        raise ConfigurationError("overlay bundle required checks did not pass")
    for role, expected in manifest.sha256.items():
        relative = manifest.artifacts.get(role)
        if relative is None:
            raise ConfigurationError(f"overlay manifest checksum role has no artifact: {role}")
        path = _safe_artifact_path(root, relative)
        if not path.is_file() or sha256_file(path) != expected:
            raise ConfigurationError(f"overlay artifact checksum mismatch: {role}")
    source_bundle = (
        source_bundle_dir.expanduser().resolve()
        if source_bundle_dir is not None
        else Path(manifest.source_bundle).expanduser().resolve()
    )
    verify_artifact_bundle(source_bundle)
    if sha256_file(source_bundle / "build_manifest.json") != manifest.source_build_manifest_sha256:
        raise ConfigurationError("overlay source build manifest changed")
    if _source_terrain_hashes(source_bundle) != validation.source_terrain_sha256_before:
        raise ConfigurationError("overlay source terrain artifacts changed")
    if validation.source_terrain_sha256_before != validation.source_terrain_sha256_after:
        raise ConfigurationError("overlay validation reports changed source terrain artifacts")
    for identity in manifest.source_identities:
        path_value = identity.get("path")
        digest = identity.get("sha256")
        if path_value is None:
            continue
        path = Path(str(path_value)).expanduser().resolve()
        if not path.is_file() or sha256_file(path) != digest:
            raise ConfigurationError(f"overlay input source changed or is missing: {path}")
    for layer in validation.layers:
        layer_path = _safe_artifact_path(root, layer.stl_path)
        reopened = trimesh.load_mesh(layer_path, process=True)
        if not isinstance(reopened, trimesh.Trimesh):
            raise ConfigurationError(f"overlay layer did not reopen as a mesh: {layer.source_id}")
        if (
            len(reopened.vertices) != layer.vertex_count
            or len(reopened.faces) != layer.triangle_count
            or not bool(reopened.is_watertight)
            or not bool(reopened.is_winding_consistent)
            or float(reopened.volume) <= 0
        ):
            raise ConfigurationError(f"overlay layer geometry changed: {layer.source_id}")
    inspection = inspect_3mf(_safe_artifact_path(root, manifest.artifacts["model_3mf"]))
    if (
        inspection.object_count != validation.combined_3mf_object_count
        or inspection.build_item_count != validation.combined_3mf_build_item_count
        or inspection.components_object_count != validation.combined_3mf_components_object_count
        or inspection.component_count != validation.combined_3mf_component_count
        or inspection.base_material_group_count != validation.combined_3mf_base_material_group_count
        or inspection.material_assigned_object_count
        != validation.combined_3mf_material_assigned_object_count
        or inspection.triangle_count != validation.combined_3mf_triangle_count
        or inspection.strict_warning_count != 0
    ):
        raise ConfigurationError("combined overlay 3MF changed after publication")
    scene = trimesh.load(
        _safe_artifact_path(root, manifest.artifacts["preview_glb"]), force="scene"
    )
    if (
        not isinstance(scene, trimesh.Scene)
        or len(scene.geometry) != validation.combined_glb_geometry_count
    ):
        raise ConfigurationError("combined overlay GLB changed after publication")
    with Image.open(_safe_artifact_path(root, manifest.artifacts["preview_png"])) as preview:
        preview.verify()
    plan = json.loads(
        _safe_artifact_path(root, manifest.artifacts["plan_geojson"]).read_text(encoding="utf-8")
    )
    if not isinstance(plan, dict) or plan.get("type") != "FeatureCollection":
        raise ConfigurationError("overlay plan GeoJSON is invalid")
    return {
        "source_count": len(validation.source_records),
        "layer_count": len(validation.layers),
        "feature_count": validation.total_feature_count,
        "triangle_count": validation.total_triangle_count,
        "combined_3mf_object_count": validation.combined_3mf_object_count,
        "combined_3mf_build_item_count": validation.combined_3mf_build_item_count,
        "combined_3mf_component_count": validation.combined_3mf_component_count,
        "combined_glb_geometry_count": validation.combined_glb_geometry_count,
        "terrain_artifacts_unchanged": validation.terrain_artifacts_unchanged,
        "required_checks_passed": True,
    }
