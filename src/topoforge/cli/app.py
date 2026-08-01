"""Thin Typer CLI over the unified TopoForge engine."""

from __future__ import annotations

import json
import platform
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import rasterio
import trimesh
import typer
import yaml
from rasterio.errors import RasterioError

from topoforge import __version__
from topoforge.config import DEFAULT_PRINTER_PROFILE_ID, get_printer_profile, load_build_config
from topoforge.engine import (
    build_local_terrain,
    preflight_local_terrain,
    record_slice_validation,
    verify_artifact_bundle,
)
from topoforge.exceptions import TopoForgeError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.geocoding import (
    NominatimConfig,
    NominatimGeocoder,
    PlaceCandidateSelectionError,
    PlaceSearchResult,
    place_candidate_aoi_input,
    select_place_candidate,
)
from topoforge.models import (
    AreaOfInterestInput,
    BuildConfig,
    DatasetType,
    ResourceBudgetMode,
    SamplingMode,
    TerrainMode,
    VerticalScaleMode,
)
from topoforge.provenance import write_json
from topoforge.providers import (
    CachingHttpClient,
    ContentAddressedCache,
    CopernicusAwsProvider,
    HttpTransportConfig,
    ProviderAcquisition,
    ProviderSelectionError,
    ProviderSelectionPolicy,
    fetch_with_provider_selection,
    list_provider_descriptors,
)
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.tiling import (
    TileLayoutConfig,
    extract_tile_set,
    generate_print_tile_set,
    generate_tile_mesh_set,
    plan_tile_layout,
    slice_print_tile_set,
    verify_print_tile_set,
    verify_tile_mesh_set,
    verify_tile_set,
    verify_tile_slice_set,
    write_tile_layout,
)
from topoforge.util import sha256_file
from topoforge.validation import validate_mesh
from topoforge.workflow import (
    GlobalAcquisitionConfig,
    WorkflowExecutionResult,
    WorkflowLaunchConfig,
    apply_workflow_cleanup,
    create_workflow_backup,
    estimate_workflow_storage,
    execute_workflow_launch,
    inspect_workflow_workspace,
    plan_workflow_cleanup,
    read_workflow_launch_config,
    restore_workflow_backup,
    write_workflow_launch_config,
    write_workflow_report,
    write_workflow_storage_estimate,
)

app = typer.Typer(
    name="topoforge",
    help="Generate provenance-aware, dimensionally controlled terrain models.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2) from exc


def _emit_workflow_execution(execution: WorkflowExecutionResult) -> None:
    result = execution.workflow
    summary = execution.summary
    _emit(
        {
            "status": summary.state.value,
            "workflow_id": summary.workflow_id,
            "source_mode": summary.source_mode,
            "workspace": str(result.workspace_dir),
            "manifest": str(result.manifest_path),
            "workflow_status": str(result.status_path),
            "launch_config": str(execution.launch_config_path),
            "summary": str(execution.summary_path),
            "report": str(execution.report_path),
            "completed_stages": [stage.value for stage in summary.completed_stages],
            "reused_stages": [stage.value for stage in summary.reused_stages],
            "metrics": summary.metrics,
            "required_checks_passed": summary.required_checks_passed,
        }
    )


def _prompt_float_tuple(label: str, count: int) -> tuple[float, ...]:
    raw = typer.prompt(label)
    try:
        values = tuple(float(item) for item in raw.replace(",", " ").split())
    except ValueError as exc:
        raise ValueError(f"{label} requires {count} numeric values") from exc
    if len(values) != count:
        raise ValueError(f"{label} requires exactly {count} numeric values")
    return values


def _fail_provider_selection(exc: ProviderSelectionError) -> None:
    typer.echo(
        json.dumps(
            {
                "status": "failed",
                "error": str(exc),
                "provider_selection": exc.trace.model_dump(mode="json"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        err=True,
    )
    raise typer.Exit(code=2) from exc


def _global_provider_descriptors() -> list[Any]:
    return [item for item in list_provider_descriptors() if item.provider_id != "local"]


def _global_provider_instances(client: CachingHttpClient) -> dict[str, Any]:
    return {"copernicus-aws": CopernicusAwsProvider(client)}


def _fail_place_candidates(exc: PlaceCandidateSelectionError) -> None:
    typer.echo(
        json.dumps(
            {
                "status": "candidate-selection-required",
                "error": str(exc),
                "place_search": exc.result.model_dump(mode="json"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        err=True,
    )
    raise typer.Exit(code=2) from exc


def _resolve_global_aoi(
    *,
    bbox: tuple[float, float, float, float] | None,
    center: tuple[float, float] | None,
    radius_m: float | None,
    place: str | None,
    place_candidate_id: str | None,
    geocoder_url: str,
    cache_store: ContentAddressedCache,
    timeout_seconds: float,
    max_attempts: int,
) -> tuple[AreaOfInterestInput, Any, PlaceSearchResult | None]:
    from topoforge.raster import normalize_area_of_interest

    if place is None and place_candidate_id is not None:
        raise ValueError("--place-candidate-id requires --place")
    if place is not None:
        if bbox is not None or center is not None or radius_m is not None:
            raise ValueError("--place cannot be combined with --bbox or --center/--radius-m")
        geocoder_client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                min_request_interval_seconds=1.0,
            ),
        )
        search = NominatimGeocoder(
            geocoder_client,
            NominatimConfig(base_url=geocoder_url),
        ).search(place)
        selected = select_place_candidate(search, candidate_id=place_candidate_id)
        request = place_candidate_aoi_input(search, selected)
        return request, normalize_area_of_interest(request), search
    request = AreaOfInterestInput(
        bbox_wgs84=bbox,
        center_wgs84=center,
        radius_m=radius_m,
    )
    return request, normalize_area_of_interest(request), None


def _record_geocoding_manifest(
    acquisition: ProviderAcquisition, search: PlaceSearchResult | None
) -> None:
    if search is None:
        return
    path = acquisition.acquisition_manifest_path.resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("source acquisition manifest root is not an object")
    payload["geocoding"] = search.model_dump(mode="json")
    temporary = path.with_name(f".{path.name}.geocoding.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        json.loads(temporary.read_text(encoding="utf-8"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _was_provided(ctx: typer.Context, parameter_name: str) -> bool:
    """Return whether Click received a value explicitly on the command line."""
    source = ctx.get_parameter_source(parameter_name)
    return source is not None and source.name == "COMMANDLINE"


@app.command()
def build(
    ctx: typer.Context,
    dem: Annotated[Path | None, typer.Option("--dem", help="Local GeoTIFF/DEM path.")] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New empty output directory.")
    ] = Path("outputs/build"),
    size_mm: Annotated[
        tuple[float, float],
        typer.Option(
            "--size-mm",
            metavar="WIDTH DEPTH",
            help="Model footprint; use depth 0 to preserve source aspect.",
        ),
    ] = (180.0, 0.0),
    base_mm: Annotated[float, typer.Option("--base-mm", min=0.01)] = 3.0,
    max_height_mm: Annotated[float, typer.Option("--max-height-mm", min=0.01)] = 45.0,
    vertical_scale: Annotated[
        VerticalScaleMode, typer.Option("--vertical-scale")
    ] = VerticalScaleMode.AUTO_PERCEPTUAL,
    vertical_exaggeration: Annotated[
        float, typer.Option("--vertical-exaggeration", min=0.01)
    ] = 1.0,
    printer_profile: Annotated[
        str,
        typer.Option(
            "--printer-profile",
            help="Manufacturing profile; defaults to Bambu Lab P2S with a 0.4 mm nozzle.",
        ),
    ] = DEFAULT_PRINTER_PROFILE_ID,
    sampling_mode: Annotated[
        SamplingMode,
        typer.Option("--sampling-mode", help="print-aware, source-preserving, or custom."),
    ] = SamplingMode.PRINT_AWARE,
    mesh_sampling_mm: Annotated[
        float | None,
        typer.Option("--mesh-sampling-mm", min=0.001, help="Custom physical mesh spacing."),
    ] = None,
    max_grid_cells: Annotated[
        int, typer.Option("--max-grid-cells", min=16, help="Hard processed-grid cell budget.")
    ] = 1_500_000,
    max_estimated_triangles: Annotated[
        int | None,
        typer.Option("--max-estimated-triangles", min=12, help="Hard triangle budget."),
    ] = None,
    resource_budget_mode: Annotated[
        ResourceBudgetMode,
        typer.Option("--resource-budget-mode", help="adapt or strict."),
    ] = ResourceBudgetMode.ADAPT,
    max_estimated_memory_mb: Annotated[
        float,
        typer.Option("--max-estimated-memory-mb", min=1.0, help="Estimated mesh memory budget."),
    ] = 1024.0,
    bbox: Annotated[
        tuple[float, float, float, float] | None,
        typer.Option("--bbox", metavar="WEST SOUTH EAST NORTH", help="WGS84 AOI bbox."),
    ] = None,
    center: Annotated[
        tuple[float, float] | None,
        typer.Option("--center", metavar="LON LAT", help="WGS84 AOI center."),
    ] = None,
    radius_m: Annotated[
        float | None,
        typer.Option("--radius-m", min=0.001, help="Geodesic center AOI radius."),
    ] = None,
    dataset_type: Annotated[DatasetType, typer.Option("--dataset-type")] = DatasetType.UNKNOWN,
    dataset_name: Annotated[str | None, typer.Option("--dataset-name")] = None,
    dataset_version: Annotated[str, typer.Option("--dataset-version")] = "unknown",
    acquisition_period: Annotated[str, typer.Option("--acquisition-period")] = "unknown",
    source_url: Annotated[
        list[str] | None,
        typer.Option("--source-url", help="Repeatable original dataset/source URL."),
    ] = None,
    vertical_crs: Annotated[str, typer.Option("--vertical-crs")] = "unknown",
    vertical_datum: Annotated[str, typer.Option("--vertical-datum")] = "unknown",
    data_license: Annotated[
        str, typer.Option("--data-license")
    ] = "user-supplied; verify source terms",
    attribution: Annotated[str, typer.Option("--attribution")] = "Provided by the user",
    config: Annotated[
        Path | None, typer.Option("--config", help="YAML config; explicit CLI options override it.")
    ] = None,
) -> None:
    """Build STL, 3MF, GLB, provenance, validation, and preview from a local DEM."""
    try:
        if config is not None:
            overrides: dict[str, Any] = {}
            scalar_overrides = {
                "dem": ("dem_path", dem),
                "output": ("output_dir", output),
                "base_mm": ("base_thickness_mm", base_mm),
                "max_height_mm": ("max_height_mm", max_height_mm),
                "vertical_scale": ("vertical_scale_mode", vertical_scale),
                "vertical_exaggeration": ("vertical_exaggeration", vertical_exaggeration),
                "sampling_mode": ("sampling_mode", sampling_mode),
                "mesh_sampling_mm": ("mesh_sampling_mm", mesh_sampling_mm),
                "max_grid_cells": ("max_grid_cells", max_grid_cells),
                "max_estimated_triangles": ("max_estimated_triangles", max_estimated_triangles),
                "resource_budget_mode": ("resource_budget_mode", resource_budget_mode),
                "max_estimated_memory_mb": (
                    "max_estimated_memory_mb",
                    max_estimated_memory_mb,
                ),
                "dataset_type": ("dataset_type", dataset_type),
                "dataset_name": ("dataset_name", dataset_name),
                "dataset_version": ("dataset_version", dataset_version),
                "acquisition_period": ("acquisition_period", acquisition_period),
                "source_url": ("source_urls", source_url),
                "vertical_crs": ("vertical_crs", vertical_crs),
                "vertical_datum": ("vertical_datum", vertical_datum),
                "data_license": ("data_license", data_license),
                "attribution": ("attribution", attribution),
            }
            for parameter_name, (field_name, value) in scalar_overrides.items():
                if _was_provided(ctx, parameter_name):
                    overrides[field_name] = value
            if _was_provided(ctx, "size_mm"):
                overrides["model_width_mm"] = size_mm[0]
                overrides["model_depth_mm"] = size_mm[1] if size_mm[1] > 0 else None
            if _was_provided(ctx, "printer_profile"):
                overrides["printer_profile"] = get_printer_profile(printer_profile)
            if any(_was_provided(ctx, name) for name in ("bbox", "center", "radius_m")):
                overrides["aoi"] = AreaOfInterestInput(
                    bbox_wgs84=bbox,
                    center_wgs84=center,
                    radius_m=radius_m,
                )
            resolved = load_build_config(config, overrides)
        else:
            if dem is None:
                raise ValueError("--dem is required unless --config supplies dem_path")
            profile = get_printer_profile(printer_profile)
            resolved = BuildConfig(
                dem_path=dem,
                output_dir=output,
                model_width_mm=size_mm[0],
                model_depth_mm=size_mm[1] if size_mm[1] > 0 else None,
                base_thickness_mm=base_mm,
                max_height_mm=max_height_mm,
                vertical_scale_mode=vertical_scale,
                vertical_exaggeration=vertical_exaggeration,
                printer_profile=profile,
                sampling_mode=sampling_mode,
                mesh_sampling_mm=mesh_sampling_mm,
                max_grid_cells=max_grid_cells,
                max_estimated_triangles=max_estimated_triangles,
                max_estimated_memory_mb=max_estimated_memory_mb,
                resource_budget_mode=resource_budget_mode,
                aoi=(
                    AreaOfInterestInput(
                        bbox_wgs84=bbox,
                        center_wgs84=center,
                        radius_m=radius_m,
                    )
                    if bbox is not None or center is not None or radius_m is not None
                    else None
                ),
                dataset_type=dataset_type,
                dataset_name=dataset_name,
                dataset_version=dataset_version,
                acquisition_period=acquisition_period,
                source_urls=source_url or [],
                vertical_crs=vertical_crs,
                vertical_datum=vertical_datum,
                data_license=data_license,
                attribution=attribution,
            )
        result = build_local_terrain(resolved)
        _emit(
            {
                "status": "completed",
                "output_dir": str(result.output_dir),
                "vertical_exaggeration": result.provenance["scaling"]["vertical_exaggeration"],
                "dimensions_mm": result.validation["dimensions_mm"],
                "sampling_mode": resolved.sampling_mode.value,
                "source_horizontal_resolution_m": result.validation.get(
                    "source_horizontal_resolution_m"
                ),
                "processed_horizontal_resolution_m": result.validation.get(
                    "processed_horizontal_resolution_m"
                ),
                "orientation": result.validation.get("orientation"),
                "watertight": result.validation["watertight"],
                "manifold": result.validation["manifold"],
                "required_checks_passed": result.validation["required_checks_passed"],
                "artifacts": {key: str(path) for key, path in result.artifacts.items()},
            }
        )
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("preflight")
def preflight(
    dem: Annotated[Path | None, typer.Option("--dem")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    size_mm: Annotated[tuple[float, float], typer.Option("--size-mm")] = (180.0, 0.0),
    base_mm: Annotated[float, typer.Option("--base-mm", min=0.01)] = 3.0,
    max_height_mm: Annotated[float, typer.Option("--max-height-mm", min=0.01)] = 45.0,
    printer_profile: Annotated[str, typer.Option("--printer-profile")] = DEFAULT_PRINTER_PROFILE_ID,
    vertical_scale: Annotated[
        VerticalScaleMode, typer.Option("--vertical-scale")
    ] = VerticalScaleMode.AUTO_PERCEPTUAL,
    vertical_exaggeration: Annotated[
        float, typer.Option("--vertical-exaggeration", min=0.01)
    ] = 1.0,
    sampling_mode: Annotated[
        SamplingMode, typer.Option("--sampling-mode")
    ] = SamplingMode.PRINT_AWARE,
    mesh_sampling_mm: Annotated[float | None, typer.Option("--mesh-sampling-mm")] = None,
    max_grid_cells: Annotated[int, typer.Option("--max-grid-cells", min=16)] = 1_500_000,
    max_estimated_triangles: Annotated[
        int | None, typer.Option("--max-estimated-triangles", min=12)
    ] = None,
    max_estimated_memory_mb: Annotated[
        float, typer.Option("--max-estimated-memory-mb", min=1.0)
    ] = 1024.0,
    resource_budget_mode: Annotated[
        ResourceBudgetMode, typer.Option("--resource-budget-mode")
    ] = ResourceBudgetMode.ADAPT,
    bbox: Annotated[tuple[float, float, float, float] | None, typer.Option("--bbox")] = None,
    center: Annotated[tuple[float, float] | None, typer.Option("--center")] = None,
    radius_m: Annotated[float | None, typer.Option("--radius-m", min=0.001)] = None,
) -> None:
    """Resolve printer fit, sampling, triangles, memory, and vertical scale without a build."""
    try:
        if config is not None:
            if dem is not None:
                raise ValueError("use either --config or --dem for preflight")
            resolved = load_build_config(config)
        else:
            if dem is None:
                raise ValueError("--dem is required unless --config is supplied")
            resolved = BuildConfig(
                dem_path=dem,
                output_dir=Path("outputs/preflight-not-published"),
                model_width_mm=size_mm[0],
                model_depth_mm=size_mm[1] if size_mm[1] > 0 else None,
                base_thickness_mm=base_mm,
                max_height_mm=max_height_mm,
                printer_profile=get_printer_profile(printer_profile),
                vertical_scale_mode=vertical_scale,
                vertical_exaggeration=vertical_exaggeration,
                sampling_mode=sampling_mode,
                mesh_sampling_mm=mesh_sampling_mm,
                max_grid_cells=max_grid_cells,
                max_estimated_triangles=max_estimated_triangles,
                max_estimated_memory_mb=max_estimated_memory_mb,
                resource_budget_mode=resource_budget_mode,
                aoi=(
                    AreaOfInterestInput(bbox_wgs84=bbox, center_wgs84=center, radius_m=radius_m)
                    if bbox is not None or center is not None or radius_m is not None
                    else None
                ),
            )
        _emit(preflight_local_terrain(resolved).model_dump(mode="json"))
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("synthetic")
def synthetic_command(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path(
        "examples/synthetic/gaussian-hill.tif"
    ),
    terrain: Annotated[
        SyntheticTerrain, typer.Option("--terrain")
    ] = SyntheticTerrain.GAUSSIAN_HILL,
    rows: Annotated[int, typer.Option("--rows", min=2)] = 64,
    columns: Annotated[int, typer.Option("--columns", min=2)] = 80,
    pixel_size_m: Annotated[float, typer.Option("--pixel-size-m", min=0.01)] = 10.0,
) -> None:
    """Create a deterministic analytic GeoTIFF test fixture."""
    try:
        path = create_synthetic_geotiff(output, terrain, rows, columns, pixel_size_m)
        _emit({"path": str(path.resolve()), "terrain": terrain.value, "sha256": sha256_file(path)})
    except (ValueError, OSError) as exc:
        _fail(exc)


@app.command()
def inspect(path: Annotated[Path, typer.Argument(help="Raster, STL, GLB, or 3MF file.")]) -> None:
    """Measure an input file without changing it."""
    try:
        suffix = path.suffix.lower()
        if suffix in {".tif", ".tiff"}:
            with rasterio.open(path) as dataset:
                band = dataset.read(1, masked=True)
                finite = np.asarray(band.compressed(), dtype=np.float64)
                value = {
                    "path": str(path.resolve()),
                    "driver": dataset.driver,
                    "shape": [dataset.height, dataset.width],
                    "crs": str(dataset.crs) if dataset.crs else None,
                    "transform": tuple(dataset.transform)[:6],
                    "nodata": dataset.nodata,
                    "valid_fraction": float(finite.size / band.size),
                    "elevation_min": float(np.min(finite)) if finite.size else None,
                    "elevation_max": float(np.max(finite)) if finite.size else None,
                    "tags": dataset.tags(),
                    "sha256": sha256_file(path),
                }
        elif suffix == ".3mf":
            value = asdict(inspect_3mf(path))
        elif suffix in {".stl", ".glb"}:
            mesh = trimesh.load(path, force="mesh", process=True)
            if not isinstance(mesh, trimesh.Trimesh):
                raise ValueError(f"{path} did not reopen as one mesh")
            value = validate_mesh(mesh).model_dump(mode="json")
            value["sha256"] = sha256_file(path)
        else:
            raise ValueError(f"Unsupported inspection suffix: {suffix}")
        _emit(value)
    except (ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command()
def validate(
    model: Annotated[Path, typer.Argument(help="STL model to validate.")],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Reopen and measure a manufacturing STL."""
    try:
        mesh = trimesh.load(model, file_type="stl", force="mesh", process=True)
        if not isinstance(mesh, trimesh.Trimesh):
            raise ValueError(f"{model} did not reopen as one mesh")
        report = validate_mesh(mesh).model_dump(mode="json")
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        _emit(report)
        if not all(
            report.get(key) is True
            for key in ("watertight", "winding_consistent", "manifold", "positive_volume")
        ):
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (ValueError, OSError) as exc:
        _fail(exc)


@app.command()
def preview(
    build_dir: Annotated[Path, typer.Argument(help="Completed TopoForge artifact directory.")],
) -> None:
    """Verify and print the preview artifact paths for a completed build."""
    try:
        evidence = verify_artifact_bundle(build_dir)
        evidence["preview_png"] = str((build_dir / "preview.png").resolve())
        evidence["preview_glb"] = str((build_dir / "preview.glb").resolve())
        _emit(evidence)
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("run")
def run_local(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option("--config", help="Resolved local build YAML used by the workflow."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Workflow workspace; defaults to config output_dir."),
    ] = None,
    bbox: Annotated[
        tuple[float, float, float, float] | None,
        typer.Option("--bbox", metavar="WEST SOUTH EAST NORTH", help="WGS84 global AOI bbox."),
    ] = None,
    center: Annotated[
        tuple[float, float] | None,
        typer.Option("--center", metavar="LON LAT", help="WGS84 global AOI center."),
    ] = None,
    radius_m: Annotated[
        float | None, typer.Option("--radius-m", min=0.001, help="Geodesic AOI radius.")
    ] = None,
    provider: Annotated[str, typer.Option("--provider")] = "auto",
    terrain_mode: Annotated[TerrainMode, typer.Option("--terrain-mode")] = (
        TerrainMode.BEST_AVAILABLE
    ),
    allow_semantic_fallback: Annotated[
        bool,
        typer.Option(
            "--allow-semantic-fallback",
            help="Permit an explicitly recorded DTM/DSM/mixed semantic downgrade.",
        ),
    ] = False,
    preferred_provider: Annotated[
        list[str] | None,
        typer.Option("--preferred-provider", help="Repeat in deterministic preference order."),
    ] = None,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
    acquisition_timeout_seconds: Annotated[
        float, typer.Option("--acquisition-timeout-seconds", min=0.1)
    ] = 30.0,
    acquisition_max_attempts: Annotated[
        int, typer.Option("--acquisition-max-attempts", min=1, max=10)
    ] = 4,
    acquisition_min_request_interval_seconds: Annotated[
        float, typer.Option("--acquisition-min-request-interval-seconds", min=0.0)
    ] = 0.2,
    max_tile_size_mm: Annotated[
        tuple[float, float],
        typer.Option("--max-tile-size-mm", metavar="WIDTH DEPTH"),
    ] = (180.0, 180.0),
    overlap_cells: Annotated[int, typer.Option("--overlap-cells", min=0)] = 1,
    slicing: Annotated[
        bool,
        typer.Option("--slice/--no-slice", help="Run actual per-tile software slicing."),
    ] = True,
    profile: Annotated[
        Path | None, typer.Option("--profile", help="Legacy process/machine settings file.")
    ] = None,
    machine_profile: Annotated[
        Path | None, typer.Option("--machine-profile", help="Resolved machine preset JSON.")
    ] = None,
    process_profile: Annotated[
        Path | None, typer.Option("--process-profile", help="Resolved process preset JSON.")
    ] = None,
    filament_profile: Annotated[
        Path | None, typer.Option("--filament-profile", help="Resolved filament preset JSON.")
    ] = None,
    slicer: Annotated[
        str,
        typer.Option(
            "--slicer",
            help="bambu-studio (release gate), orca, prusa, or auto (diagnostic fallback).",
        ),
    ] = "bambu-studio",
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=1.0)] = 1200.0,
    project_evidence: Annotated[
        bool,
        typer.Option(
            "--project-evidence/--no-project-evidence",
            help="Export and independently reopen per-tile Bambu project 3MF evidence.",
        ),
    ] = False,
    project_timeout_seconds: Annotated[
        float, typer.Option("--project-timeout-seconds", min=1.0)
    ] = 1800.0,
) -> None:
    """Run or resume local/global acquisition and manufacturing stages."""
    try:
        resolved = load_build_config(config)
        workspace = (output or resolved.output_dir).expanduser().resolve()
        has_global_aoi = any(value is not None for value in (bbox, center, radius_m))
        global_control_names = (
            "provider",
            "terrain_mode",
            "allow_semantic_fallback",
            "preferred_provider",
            "cache_dir",
            "acquisition_timeout_seconds",
            "acquisition_max_attempts",
            "acquisition_min_request_interval_seconds",
        )
        global_controls_provided = any(_was_provided(ctx, name) for name in global_control_names)
        if not has_global_aoi and global_controls_provided:
            raise ValueError(
                "global acquisition options require --bbox or --center with --radius-m"
            )
        global_source = (
            GlobalAcquisitionConfig(
                aoi=AreaOfInterestInput(
                    bbox_wgs84=bbox,
                    center_wgs84=center,
                    radius_m=radius_m,
                ),
                requested_provider_id=provider,
                terrain_mode=terrain_mode,
                allow_semantic_fallback=allow_semantic_fallback,
                preferred_provider_ids=tuple(preferred_provider or ()),
                cache_dir=cache_dir,
                timeout_seconds=acquisition_timeout_seconds,
                max_attempts=acquisition_max_attempts,
                min_request_interval_seconds=(acquisition_min_request_interval_seconds),
            )
            if has_global_aoi
            else None
        )
        if slicer not in {"bambu-studio", "orca", "prusa", "auto"}:
            raise ValueError("--slicer must be bambu-studio, orca, prusa, or auto")
        settings = tuple(
            path for path in (machine_profile, process_profile, profile) if path is not None
        )
        filaments = () if filament_profile is None else (filament_profile,)
        launch = WorkflowLaunchConfig.model_validate(
            {
                "workspace_dir": workspace,
                "build": resolved,
                "global_source": global_source,
                "maximum_tile_width_mm": max_tile_size_mm[0],
                "maximum_tile_depth_mm": max_tile_size_mm[1],
                "overlap_cells": overlap_cells,
                "slicing_enabled": slicing,
                "slicer_name": slicer,
                "slicer_settings": settings,
                "slicer_filaments": filaments,
                "slice_timeout_seconds": timeout_seconds,
                "project_evidence_enabled": project_evidence,
                "project_timeout_seconds": project_timeout_seconds,
            }
        )
        _emit_workflow_execution(execute_workflow_launch(launch))
    except ProviderSelectionError as exc:
        _fail_provider_selection(exc)
    except (ImportError, TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("resume")
def resume_workflow(
    launch: Annotated[
        Path,
        typer.Argument(help="workflow-launch.yaml or its workspace directory."),
    ],
) -> None:
    """Resume a saved local workflow without reconstructing its command line."""
    try:
        path = launch.expanduser().resolve()
        if path.is_dir():
            path = path / "workflow-launch.yaml"
        _emit_workflow_execution(execute_workflow_launch(read_workflow_launch_config(path)))
    except ProviderSelectionError as exc:
        _fail_provider_selection(exc)
    except (ImportError, TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("browse")
def browse_workflow(
    workspace: Annotated[Path, typer.Argument(help="Completed workflow workspace.")],
    open_report: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the static report in the desktop browser."),
    ] = False,
) -> None:
    """Verify and index reports, previews, maps, models, and output directories."""
    try:
        root = workspace.expanduser().resolve()
        report_path = root / "workflow-report.html"
        summary = inspect_workflow_workspace(root)
        summary = summary.model_copy(
            update={
                "artifacts": {
                    **summary.artifacts,
                    "workflow_report": str(report_path),
                }
            }
        )
        write_workflow_report(report_path, summary)
        opened = False
        if open_report:
            import webbrowser

            opened = webbrowser.open(report_path.as_uri())
        _emit(
            {
                "status": "ready",
                "workflow_id": summary.workflow_id,
                "report": str(report_path),
                "opened": opened,
                "metrics": summary.metrics,
                "artifacts": summary.artifacts,
                "required_checks_passed": summary.required_checks_passed,
            }
        )
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("storage")
def workflow_storage(
    launch: Annotated[
        Path,
        typer.Argument(help="workflow-launch.yaml or its workflow workspace."),
    ],
) -> None:
    """Estimate disk headroom using configured ceilings or completed measurements."""
    try:
        path = launch.expanduser().resolve()
        if path.is_dir():
            path = path / "workflow-launch.yaml"
        config = read_workflow_launch_config(path)
        root = config.workspace_dir.expanduser().resolve()
        summary = None
        if (root / "workflow-manifest.json").is_file() or (root / "workflow-status.json").is_file():
            summary = inspect_workflow_workspace(root)
        estimate = estimate_workflow_storage(config, summary=summary)
        report_path = write_workflow_storage_estimate(estimate)
        _emit(
            {
                "status": "estimated",
                "report": str(report_path),
                **estimate.model_dump(mode="json"),
            }
        )
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("cleanup")
def cleanup_workflow(
    workspace: Annotated[Path, typer.Argument(help="Completed workflow workspace.")],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Delete the reviewed unreferenced stage paths."),
    ] = False,
    confirm_workflow_id: Annotated[
        str | None,
        typer.Option(
            "--confirm-workflow-id",
            help="Exact workflow id required with --apply.",
        ),
    ] = None,
) -> None:
    """Review or explicitly apply cleanup of unreferenced content-addressed stages."""
    try:
        root = workspace.expanduser().resolve()
        plan = plan_workflow_cleanup(root)
        plan_path = root / "workflow-cleanup-plan.json"
        write_json(plan_path, plan.model_dump(mode="json"))
        if not apply:
            _emit({"status": "review", "plan": str(plan_path), **plan.model_dump(mode="json")})
            return
        if confirm_workflow_id is None:
            raise ValueError("--apply requires --confirm-workflow-id from the reviewed plan")
        result = apply_workflow_cleanup(
            root,
            confirm_workflow_id=confirm_workflow_id,
        )
        result_path = root / "workflow-cleanup-result.json"
        write_json(result_path, result.model_dump(mode="json"))
        _emit({"status": "applied", "result": str(result_path), **result.model_dump(mode="json")})
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("backup")
def backup_workflow(
    workspace: Annotated[Path, typer.Argument(help="Completed workflow workspace.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="New deterministic ZIP.")],
) -> None:
    """Create and strictly verify a portable workflow backup archive."""
    try:
        result = create_workflow_backup(workspace, output)
        manifest = result.manifest
        _emit(
            {
                "status": "verified",
                "archive_path": str(result.archive_path),
                "archive_sha256": result.archive_sha256,
                "archive_size_bytes": result.archive_size_bytes,
                "backup_id": manifest.backup_id,
                "workflow_id": manifest.workflow_id,
                "file_count": len(manifest.files),
                "external_file_count": sum(item.kind == "external" for item in manifest.files),
                "required_checks_passed": manifest.required_checks_passed,
            }
        )
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("restore")
def restore_workflow(
    archive: Annotated[Path, typer.Argument(help="Verified TopoForge workflow backup ZIP.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="New workspace path.")],
) -> None:
    """Atomically restore, remap, and strictly reopen a workflow backup."""
    try:
        result = restore_workflow_backup(archive, output)
        _emit({"status": "restored", **result.model_dump(mode="json")})
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("wizard")
def workflow_wizard(
    ctx: typer.Context,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    source_mode: Annotated[
        str | None,
        typer.Option("--source", help="local, bbox, or center."),
    ] = None,
    dem: Annotated[Path | None, typer.Option("--dem", help="Local GeoTIFF source.")] = None,
    bbox: Annotated[
        tuple[float, float, float, float] | None,
        typer.Option("--bbox", metavar="WEST SOUTH EAST NORTH"),
    ] = None,
    center: Annotated[
        tuple[float, float] | None,
        typer.Option("--center", metavar="LON LAT"),
    ] = None,
    radius_m: Annotated[float | None, typer.Option("--radius-m", min=0.001)] = None,
    build_template: Annotated[
        Path | None,
        typer.Option("--build-template", help="Existing BuildConfig YAML to use as defaults."),
    ] = None,
    model_width_mm: Annotated[float | None, typer.Option("--model-width-mm", min=0.1)] = None,
    model_depth_mm: Annotated[float | None, typer.Option("--model-depth-mm", min=0.1)] = None,
    base_mm: Annotated[float | None, typer.Option("--base-mm", min=0.01)] = None,
    max_height_mm: Annotated[float | None, typer.Option("--max-height-mm", min=0.1)] = None,
    printer_profile: Annotated[str, typer.Option("--printer-profile")] = (
        DEFAULT_PRINTER_PROFILE_ID
    ),
    max_tile_size_mm: Annotated[
        tuple[float, float] | None,
        typer.Option("--max-tile-size-mm", metavar="WIDTH DEPTH"),
    ] = None,
    overlap_cells: Annotated[int, typer.Option("--overlap-cells", min=0)] = 1,
    sampling_mode: Annotated[
        SamplingMode | None,
        typer.Option("--sampling-mode"),
    ] = None,
    mesh_sampling_mm: Annotated[
        float | None,
        typer.Option("--mesh-sampling-mm", min=0.001),
    ] = None,
    slicing: Annotated[bool, typer.Option("--slice/--no-slice")] = False,
    slicer: Annotated[str, typer.Option("--slicer")] = "bambu-studio",
    machine_profile: Annotated[Path | None, typer.Option("--machine-profile")] = None,
    process_profile: Annotated[Path | None, typer.Option("--process-profile")] = None,
    filament_profile: Annotated[Path | None, typer.Option("--filament-profile")] = None,
    project_evidence: Annotated[
        bool,
        typer.Option("--project-evidence/--no-project-evidence"),
    ] = False,
    provider: Annotated[str, typer.Option("--provider")] = "auto",
    terrain_mode: Annotated[TerrainMode, typer.Option("--terrain-mode")] = (
        TerrainMode.BEST_AVAILABLE
    ),
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
    run_now: Annotated[bool, typer.Option("--run/--no-run")] = True,
    yes: Annotated[bool, typer.Option("--yes", help="Accept the resolved review.")] = False,
) -> None:
    """Create a reviewed local launch config and optionally execute it immediately."""
    try:
        workspace = (
            (output if output is not None else Path(typer.prompt("Workflow workspace")))
            .expanduser()
            .resolve()
        )
        mode = source_mode
        if mode is None:
            if dem is not None:
                mode = "local"
            elif bbox is not None:
                mode = "bbox"
            elif center is not None or radius_m is not None:
                mode = "center"
            else:
                mode = typer.prompt("Source mode", default="local")
        mode = mode.strip().lower()
        if mode not in {"local", "bbox", "center"}:
            raise ValueError("--source must be local, bbox, or center")

        request: AreaOfInterestInput | None = None
        source_path: Path
        if mode == "local":
            if any(value is not None for value in (bbox, center, radius_m)):
                raise ValueError("local source cannot be combined with global AOI options")
            source_path = (
                (dem if dem is not None else Path(typer.prompt("Local DEM GeoTIFF")))
                .expanduser()
                .resolve()
            )
            if not source_path.is_file():
                raise ValueError(f"local DEM does not exist: {source_path}")
        elif mode == "bbox":
            if dem is not None or center is not None or radius_m is not None:
                raise ValueError("bbox source accepts only --bbox")
            if bbox is None:
                prompted_bbox = _prompt_float_tuple("WGS84 west south east north", 4)
                bbox = (
                    prompted_bbox[0],
                    prompted_bbox[1],
                    prompted_bbox[2],
                    prompted_bbox[3],
                )
            request = AreaOfInterestInput(bbox_wgs84=bbox)
            source_path = workspace / "global-source-managed.tif"
        else:
            if dem is not None or bbox is not None:
                raise ValueError("center source accepts only --center and --radius-m")
            if center is None:
                prompted_center = _prompt_float_tuple("WGS84 longitude latitude", 2)
                center = (prompted_center[0], prompted_center[1])
            radius_value = radius_m or typer.prompt("Radius metres", type=float)
            request = AreaOfInterestInput(
                center_wgs84=center,
                radius_m=radius_value,
            )
            source_path = workspace / "global-source-managed.tif"

        template = load_build_config(build_template) if build_template is not None else None
        width_default = template.model_width_mm if template is not None else 180.0
        base_default = template.base_thickness_mm if template is not None else 3.0
        height_default = template.max_height_mm if template is not None else 45.0
        width = model_width_mm
        if width is None:
            width = (
                width_default
                if yes
                else typer.prompt("Model width mm", default=width_default, type=float)
            )
        base = base_mm
        if base is None:
            base = (
                base_default
                if yes
                else typer.prompt("Base thickness mm", default=base_default, type=float)
            )
        height = max_height_mm
        if height is None:
            height = (
                height_default
                if yes
                else typer.prompt("Maximum model height mm", default=height_default, type=float)
            )
        selected_printer = (
            template.printer_profile
            if template is not None and not _was_provided(ctx, "printer_profile")
            else get_printer_profile(printer_profile)
        )
        selected_depth = (
            model_depth_mm
            if model_depth_mm is not None
            else template.model_depth_mm
            if template is not None
            else None
        )
        tile_size = max_tile_size_mm
        if tile_size is None:
            default_tile = (180.0, 180.0)
            if yes:
                tile_size = default_tile
            else:
                tile_size = _prompt_float_tuple("Maximum tile width depth mm", 2)
        selected_sampling = sampling_mode
        if selected_sampling is None:
            default_sampling = (
                template.sampling_mode if template is not None else SamplingMode.PRINT_AWARE
            )
            if yes:
                selected_sampling = default_sampling
            else:
                selected_sampling = SamplingMode(
                    typer.prompt("Sampling mode", default=default_sampling.value)
                )
        selected_mesh_spacing = mesh_sampling_mm
        if selected_sampling is SamplingMode.CUSTOM and selected_mesh_spacing is None:
            default_spacing = template.mesh_sampling_mm if template is not None else 0.5
            selected_mesh_spacing = (
                default_spacing
                if yes
                else typer.prompt("Mesh sample spacing mm", default=default_spacing, type=float)
            )
        if selected_sampling is not SamplingMode.CUSTOM:
            selected_mesh_spacing = None

        if template is None:
            build = BuildConfig(
                dem_path=source_path,
                output_dir=workspace,
                model_width_mm=width,
                model_depth_mm=selected_depth,
                base_thickness_mm=base,
                max_height_mm=height,
                printer_profile=selected_printer,
                sampling_mode=selected_sampling,
                mesh_sampling_mm=selected_mesh_spacing,
                aoi=request,
            )
        else:
            updates: dict[str, Any] = {
                "dem_path": source_path,
                "output_dir": workspace,
                "model_width_mm": width,
                "model_depth_mm": selected_depth,
                "base_thickness_mm": base,
                "max_height_mm": height,
                "printer_profile": selected_printer,
                "sampling_mode": selected_sampling,
                "mesh_sampling_mm": selected_mesh_spacing,
                "aoi": request,
            }
            if mode == "local":
                updates.update(
                    {
                        "dataset_type": DatasetType.UNKNOWN,
                        "dataset_name": None,
                        "dataset_version": "unknown",
                        "acquisition_period": "unknown",
                        "source_urls": [],
                        "vertical_crs": "unknown",
                        "vertical_datum": "unknown",
                        "data_license": "user-supplied; verify source terms",
                        "attribution": "Provided by the user",
                        "source_provider": "local",
                        "source_download_time": "not-applicable-local-input",
                        "source_checksums": {},
                        "source_acquisition_manifest": None,
                    }
                )
            build = BuildConfig.model_validate({**template.model_dump(mode="json"), **updates})
        global_source = (
            GlobalAcquisitionConfig(
                aoi=request,
                requested_provider_id=provider,
                terrain_mode=terrain_mode,
                cache_dir=cache_dir,
            )
            if request is not None
            else None
        )
        if slicer not in {"bambu-studio", "orca", "prusa", "auto"}:
            raise ValueError("--slicer must be bambu-studio, orca, prusa, or auto")
        settings = tuple(path for path in (machine_profile, process_profile) if path is not None)
        filaments = () if filament_profile is None else (filament_profile,)
        launch = WorkflowLaunchConfig.model_validate(
            {
                "workspace_dir": workspace,
                "build": build,
                "global_source": global_source,
                "maximum_tile_width_mm": tile_size[0],
                "maximum_tile_depth_mm": tile_size[1],
                "overlap_cells": overlap_cells,
                "slicing_enabled": slicing,
                "slicer_name": slicer,
                "slicer_settings": settings,
                "slicer_filaments": filaments,
                "project_evidence_enabled": project_evidence,
            }
        )
        typer.echo(
            yaml.safe_dump(
                launch.model_dump(mode="json"),
                sort_keys=True,
                allow_unicode=True,
            )
        )
        if not yes and not typer.confirm("Use this configuration?", default=True):
            raise typer.Abort()
        launch_path = write_workflow_launch_config(launch)
        if run_now:
            _emit_workflow_execution(execute_workflow_launch(launch))
        else:
            _emit(
                {
                    "status": "configured",
                    "launch_config": str(launch_path),
                    "workspace": str(workspace),
                    "source_mode": mode,
                }
            )
    except typer.Abort:
        raise
    except ProviderSelectionError as exc:
        _fail_provider_selection(exc)
    except (ImportError, TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("tile-plan")
def tile_plan(
    build_dir: Annotated[Path, typer.Argument(help="Completed TopoForge artifact directory.")],
    max_tile_size_mm: Annotated[
        tuple[float, float],
        typer.Option("--max-tile-size-mm", metavar="WIDTH DEPTH"),
    ] = (180.0, 180.0),
    overlap_cells: Annotated[int, typer.Option("--overlap-cells", min=0)] = 1,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Plan deterministic north-to-south/west-to-east terrain tiles from a bundle."""
    try:
        bundle = build_dir.expanduser().resolve()
        verify_artifact_bundle(bundle)
        with rasterio.open(bundle / "processed_dem.tif") as dataset:
            source_grid_shape = (dataset.height, dataset.width)
        validation = json.loads((bundle / "validation.json").read_text(encoding="utf-8"))
        dimensions = validation.get("dimensions_mm")
        if not isinstance(dimensions, list) or len(dimensions) < 2:
            raise ValueError("validation.json does not contain model dimensions")
        config = TileLayoutConfig(
            source_grid_shape=source_grid_shape,
            model_width_mm=float(dimensions[0]),
            model_depth_mm=float(dimensions[1]),
            maximum_tile_width_mm=max_tile_size_mm[0],
            maximum_tile_depth_mm=max_tile_size_mm[1],
            overlap_cells=overlap_cells,
        )
        layout = plan_tile_layout(config)
        published = write_tile_layout(layout, output) if output is not None else None
        result: dict[str, Any] = {
            "status": "planned",
            "bundle": str(bundle),
            "layout_id": layout.layout_id,
            "tile_grid_shape": layout.tile_grid_shape,
            "tile_count": layout.tile_count,
            "overlap_cells": layout.overlap_cells,
            "row_origin": layout.row_origin,
            "column_origin": layout.column_origin,
        }
        if published is not None:
            result["output"] = str(published)
        else:
            result["layout"] = layout.model_dump(mode="json")
        _emit(result)
    except (TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("tile-extract")
def tile_extract(
    build_dir: Annotated[Path, typer.Argument(help="Completed TopoForge artifact directory.")],
    layout: Annotated[
        Path,
        typer.Option("--layout", file_okay=True, dir_okay=False, readable=True),
    ],
    output: Annotated[Path, typer.Option("--output", "-o")],
) -> None:
    """Extract deterministic overlapped tile rasters and publish assembly evidence."""
    try:
        bundle = build_dir.expanduser().resolve()
        result = extract_tile_set(bundle, layout, output)
        verification = verify_tile_set(result.output_dir, bundle)
        _emit(
            {
                "status": "extracted",
                "bundle": str(bundle),
                "layout": str(result.layout_path),
                "output": str(result.output_dir),
                "assembly_manifest": str(result.assembly_manifest_path),
                "coverage_map": str(result.coverage_map_path),
                "seam_report": str(result.seam_report_path),
                "tile_manifest_count": len(result.tile_manifest_paths),
                "verification": verification,
            }
        )
    except (TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("tile-mesh")
def tile_mesh(
    tile_set_dir: Annotated[
        Path, typer.Argument(help="Verified TopoForge raster tile-set directory.")
    ],
    source_bundle_dir: Annotated[
        Path, typer.Option("--source-bundle", help="Completed source build bundle.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o")],
) -> None:
    """Generate global-frame tile meshes and verify their complete assembly."""
    try:
        result = generate_tile_mesh_set(tile_set_dir, source_bundle_dir, output)
        verification = verify_tile_mesh_set(
            result.output_dir,
            tile_set_dir,
            source_bundle_dir,
        )
        _emit(
            {
                "status": "meshed",
                "tile_set": str(tile_set_dir.expanduser().resolve()),
                "source_bundle": str(source_bundle_dir.expanduser().resolve()),
                "output": str(result.output_dir),
                "assembly_manifest": str(result.assembly_manifest_path),
                "assembly_validation": str(result.assembly_validation_path),
                "coverage_image": str(result.coverage_image_path),
                "tile_mesh_manifest_count": len(result.tile_mesh_manifest_paths),
                "verification": verification,
            }
        )
    except (TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("tile-connect")
def tile_connect(
    mesh_set_dir: Annotated[
        Path, typer.Argument(help="Verified TopoForge global-frame tile mesh-set directory.")
    ],
    source_tile_set_dir: Annotated[
        Path, typer.Option("--tile-set", help="Verified source raster tile-set directory.")
    ],
    source_bundle_dir: Annotated[
        Path, typer.Option("--source-bundle", help="Completed source build bundle.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o")],
) -> None:
    """Generate verified connectors and reversible print-local tile files."""
    try:
        result = generate_print_tile_set(
            mesh_set_dir,
            source_tile_set_dir,
            source_bundle_dir,
            output,
        )
        verification = verify_print_tile_set(
            result.output_dir,
            mesh_set_dir,
            source_tile_set_dir,
            source_bundle_dir,
        )
        _emit(
            {
                "status": "connected",
                "mesh_set": str(mesh_set_dir.expanduser().resolve()),
                "tile_set": str(source_tile_set_dir.expanduser().resolve()),
                "source_bundle": str(source_bundle_dir.expanduser().resolve()),
                "output": str(result.output_dir),
                "connector_plan": str(result.connector_plan_path),
                "assembly_manifest": str(result.assembly_manifest_path),
                "assembly_validation": str(result.assembly_validation_path),
                "connector_map": str(result.connector_map_path),
                "assembly_preview": str(result.assembly_preview_path),
                "tile_manifest_count": len(result.tile_manifest_paths),
                "verification": verification,
            }
        )
    except (TopoForgeError, ValueError, OSError, RasterioError) as exc:
        _fail(exc)


@app.command("tile-slice")
def tile_slice(
    print_set_dir: Annotated[
        Path, typer.Argument(help="Verified connector-bearing print tile-set directory.")
    ],
    source_mesh_set_dir: Annotated[
        Path, typer.Option("--mesh-set", help="Verified global-frame tile mesh set.")
    ],
    source_tile_set_dir: Annotated[
        Path, typer.Option("--tile-set", help="Verified source raster tile set.")
    ],
    source_bundle_dir: Annotated[
        Path, typer.Option("--source-bundle", help="Completed source build bundle.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o")],
    profile: Annotated[
        Path | None, typer.Option("--profile", help="Legacy process/machine settings file.")
    ] = None,
    machine_profile: Annotated[
        Path | None, typer.Option("--machine-profile", help="Resolved machine preset JSON.")
    ] = None,
    process_profile: Annotated[
        Path | None, typer.Option("--process-profile", help="Resolved process preset JSON.")
    ] = None,
    filament_profile: Annotated[
        Path | None, typer.Option("--filament-profile", help="Resolved filament preset JSON.")
    ] = None,
    slicer: Annotated[
        str,
        typer.Option(
            "--slicer",
            help="bambu-studio (release gate), orca, prusa, or auto (diagnostic fallback).",
        ),
    ] = "bambu-studio",
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=1.0)] = 1200.0,
) -> None:
    """Actually slice every print-local tile and publish strict G-code evidence."""
    try:
        from topoforge.validation.slicers import (
            BambuStudioAdapter,
            OrcaSlicerAdapter,
            PrusaSlicerAdapter,
            SlicerProfile,
            select_slicer,
        )

        settings = tuple(
            path for path in (machine_profile, process_profile, profile) if path is not None
        )
        filaments = () if filament_profile is None else (filament_profile,)
        slicer_profile = SlicerProfile(
            name=(
                "Bambu Lab P2S 0.4 / 0.20mm Standard / Bambu PLA Basic"
                if slicer == "bambu-studio"
                else None
            ),
            settings=settings,
            filaments=filaments,
        )
        adapters = {
            "bambu-studio": BambuStudioAdapter,
            "orca": OrcaSlicerAdapter,
            "prusa": PrusaSlicerAdapter,
        }
        if slicer == "auto":
            adapter = select_slicer()
        elif slicer in adapters:
            adapter = adapters[slicer]()
        else:
            raise ValueError("--slicer must be bambu-studio, orca, prusa, or auto")
        result = slice_print_tile_set(
            print_set_dir,
            source_mesh_set_dir,
            source_tile_set_dir,
            source_bundle_dir,
            output,
            adapter=adapter,
            profile=slicer_profile,
            timeout_seconds=timeout_seconds,
        )
        verification = verify_tile_slice_set(
            result.output_dir,
            print_set_dir,
            source_mesh_set_dir,
            source_tile_set_dir,
            source_bundle_dir,
        )
        _emit(
            {
                "status": "sliced",
                "print_set": str(print_set_dir.expanduser().resolve()),
                "output": str(result.output_dir),
                "manifest": str(result.manifest_path),
                "report_count": len(result.report_paths),
                "gcode_count": len(result.gcode_paths),
                "verification": verification,
            }
        )
    except (ImportError, TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command()
def geocode(
    query: Annotated[str, typer.Argument(help="Place name to search without autocomplete.")],
    candidate_id: Annotated[str | None, typer.Option("--candidate-id")] = None,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
    geocoder_url: Annotated[str, typer.Option("--geocoder-url")] = (
        "https://nominatim.openstreetmap.org"
    ),
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.1)] = 30.0,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 4,
) -> None:
    """Return Nominatim-compatible candidates; ambiguous names require an explicit id."""
    try:
        cache_store = ContentAddressedCache(cache_dir)
        client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                min_request_interval_seconds=1.0,
            ),
        )
        result = NominatimGeocoder(
            client,
            NominatimConfig(base_url=geocoder_url),
        ).search(query)
        payload = result.model_dump(mode="json")
        if candidate_id is not None or result.candidate_status == "unique":
            selected = select_place_candidate(result, candidate_id=candidate_id)
            request = place_candidate_aoi_input(result, selected)
            from topoforge.raster import normalize_area_of_interest

            payload["selected_candidate"] = selected.model_dump(mode="json")
            payload["normalized_aoi"] = normalize_area_of_interest(request).model_dump(mode="json")
        _emit(payload)
    except PlaceCandidateSelectionError as exc:
        _fail_place_candidates(exc)
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command()
def providers() -> None:
    """List provider semantics, key requirements, and implementation state."""
    _emit([item.model_dump(mode="json") for item in list_provider_descriptors()])


@app.command("fetch-dem")
def fetch_dem(
    output: Annotated[Path, typer.Option("--output", "-o", help="New provider GeoTIFF path.")],
    bbox: Annotated[
        tuple[float, float, float, float] | None,
        typer.Option("--bbox", metavar="WEST SOUTH EAST NORTH", help="WGS84 AOI bbox."),
    ] = None,
    center: Annotated[
        tuple[float, float] | None,
        typer.Option("--center", metavar="LON LAT", help="WGS84 AOI center."),
    ] = None,
    radius_m: Annotated[
        float | None, typer.Option("--radius-m", min=0.001, help="Geodesic AOI radius.")
    ] = None,
    place: Annotated[str | None, typer.Option("--place")] = None,
    place_candidate_id: Annotated[str | None, typer.Option("--place-candidate-id")] = None,
    geocoder_url: Annotated[str, typer.Option("--geocoder-url")] = (
        "https://nominatim.openstreetmap.org"
    ),
    provider: Annotated[str, typer.Option("--provider")] = "auto",
    terrain_mode: Annotated[TerrainMode, typer.Option("--terrain-mode")] = (
        TerrainMode.BEST_AVAILABLE
    ),
    allow_semantic_fallback: Annotated[
        bool,
        typer.Option(
            "--allow-semantic-fallback",
            help="Permit an explicitly recorded DTM/DSM/mixed semantic downgrade.",
        ),
    ] = False,
    preferred_provider: Annotated[
        list[str] | None,
        typer.Option("--preferred-provider", help="Repeat in deterministic preference order."),
    ] = None,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.1)] = 30.0,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 4,
    min_request_interval_seconds: Annotated[
        float, typer.Option("--min-request-interval-seconds", min=0.0)
    ] = 0.2,
) -> None:
    """Fetch, cache, verify, and AOI-crop a no-key global DEM."""
    try:
        cache_store = ContentAddressedCache(cache_dir)
        _request, normalized, place_search = _resolve_global_aoi(
            bbox=bbox,
            center=center,
            radius_m=radius_m,
            place=place,
            place_candidate_id=place_candidate_id,
            geocoder_url=geocoder_url,
            cache_store=cache_store,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                min_request_interval_seconds=min_request_interval_seconds,
            ),
        )
        selection = fetch_with_provider_selection(
            aoi=normalized,
            destination=output,
            providers=_global_provider_instances(client),
            descriptors=_global_provider_descriptors(),
            policy=ProviderSelectionPolicy(
                requested_provider_id=provider,
                requested_terrain_mode=terrain_mode,
                allow_semantic_fallback=allow_semantic_fallback,
                preferred_provider_ids=preferred_provider or [],
            ),
        )
        if not isinstance(selection.acquisition, ProviderAcquisition):
            raise ValueError("selected provider returned an unsupported acquisition result")
        acquisition = selection.acquisition
        _record_geocoding_manifest(acquisition, place_search)
        _emit(
            {
                "status": "completed",
                "provider": acquisition.provider_id,
                "provider_selection": selection.trace.model_dump(mode="json"),
                "geocoding": (
                    place_search.model_dump(mode="json") if place_search is not None else None
                ),
                "dataset": acquisition.dataset.model_dump(mode="json"),
                "aoi": acquisition.aoi,
                "plan": acquisition.plan.model_dump(mode="json"),
                "raster": str(acquisition.raster_path),
                "source_acquisition": str(acquisition.acquisition_manifest_path),
                "cache": cache_store.summary().model_dump(mode="json"),
            }
        )
    except PlaceCandidateSelectionError as exc:
        _fail_place_candidates(exc)
    except ProviderSelectionError as exc:
        _fail_provider_selection(exc)
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command("build-global")
def build_global(
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New complete terrain bundle directory.")
    ],
    bbox: Annotated[
        tuple[float, float, float, float] | None,
        typer.Option("--bbox", metavar="WEST SOUTH EAST NORTH", help="WGS84 AOI bbox."),
    ] = None,
    center: Annotated[
        tuple[float, float] | None,
        typer.Option("--center", metavar="LON LAT", help="WGS84 AOI center."),
    ] = None,
    radius_m: Annotated[
        float | None, typer.Option("--radius-m", min=0.001, help="Geodesic AOI radius.")
    ] = None,
    place: Annotated[str | None, typer.Option("--place")] = None,
    place_candidate_id: Annotated[str | None, typer.Option("--place-candidate-id")] = None,
    geocoder_url: Annotated[str, typer.Option("--geocoder-url")] = (
        "https://nominatim.openstreetmap.org"
    ),
    provider: Annotated[str, typer.Option("--provider")] = "auto",
    terrain_mode: Annotated[TerrainMode, typer.Option("--terrain-mode")] = (
        TerrainMode.BEST_AVAILABLE
    ),
    allow_semantic_fallback: Annotated[
        bool,
        typer.Option(
            "--allow-semantic-fallback",
            help="Permit an explicitly recorded DTM/DSM/mixed semantic downgrade.",
        ),
    ] = False,
    preferred_provider: Annotated[
        list[str] | None,
        typer.Option("--preferred-provider", help="Repeat in deterministic preference order."),
    ] = None,
    source_dir: Annotated[
        Path | None,
        typer.Option("--source-dir", help="New directory for acquired source evidence."),
    ] = None,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
    size_mm: Annotated[tuple[float, float], typer.Option("--size-mm", metavar="WIDTH DEPTH")] = (
        180.0,
        0.0,
    ),
    base_mm: Annotated[float, typer.Option("--base-mm", min=0.01)] = 3.0,
    max_height_mm: Annotated[float, typer.Option("--max-height-mm", min=0.01)] = 45.0,
    printer_profile: Annotated[str, typer.Option("--printer-profile")] = (
        DEFAULT_PRINTER_PROFILE_ID
    ),
    sampling_mode: Annotated[SamplingMode, typer.Option("--sampling-mode")] = (
        SamplingMode.PRINT_AWARE
    ),
    mesh_sampling_mm: Annotated[float | None, typer.Option("--mesh-sampling-mm", min=0.001)] = None,
    max_grid_cells: Annotated[int, typer.Option("--max-grid-cells", min=16)] = 1_500_000,
    max_estimated_triangles: Annotated[
        int | None, typer.Option("--max-estimated-triangles", min=12)
    ] = None,
    resource_budget_mode: Annotated[
        ResourceBudgetMode, typer.Option("--resource-budget-mode")
    ] = ResourceBudgetMode.ADAPT,
    max_estimated_memory_mb: Annotated[
        float, typer.Option("--max-estimated-memory-mb", min=1.0)
    ] = 1024.0,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.1)] = 30.0,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 4,
    min_request_interval_seconds: Annotated[
        float, typer.Option("--min-request-interval-seconds", min=0.0)
    ] = 0.2,
) -> None:
    """Acquire a no-key global DSM and run the existing validated build pipeline."""
    try:
        if output.exists():
            raise ValueError(f"output already exists; preserve it and choose a new path: {output}")
        cache_store = ContentAddressedCache(cache_dir)
        request, normalized, place_search = _resolve_global_aoi(
            bbox=bbox,
            center=center,
            radius_m=radius_m,
            place=place,
            place_candidate_id=place_candidate_id,
            geocoder_url=geocoder_url,
            cache_store=cache_store,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        source_root = (
            source_dir if source_dir is not None else output.parent / f"{output.name}-source"
        ).resolve()
        if source_root.exists():
            raise ValueError(
                f"source evidence directory already exists; choose a new path: {source_root}"
            )
        source_raster = source_root / "global-aoi.tif"
        client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                min_request_interval_seconds=min_request_interval_seconds,
            ),
        )
        selection = fetch_with_provider_selection(
            aoi=normalized,
            destination=source_raster,
            providers=_global_provider_instances(client),
            descriptors=_global_provider_descriptors(),
            policy=ProviderSelectionPolicy(
                requested_provider_id=provider,
                requested_terrain_mode=terrain_mode,
                allow_semantic_fallback=allow_semantic_fallback,
                preferred_provider_ids=preferred_provider or [],
            ),
        )
        if not isinstance(selection.acquisition, ProviderAcquisition):
            raise ValueError("selected provider returned an unsupported acquisition result")
        acquisition = selection.acquisition
        _record_geocoding_manifest(acquisition, place_search)
        dataset = acquisition.dataset
        resolved = BuildConfig(
            dem_path=acquisition.raster_path,
            output_dir=output,
            model_width_mm=size_mm[0],
            model_depth_mm=size_mm[1] if size_mm[1] > 0 else None,
            base_thickness_mm=base_mm,
            max_height_mm=max_height_mm,
            printer_profile=get_printer_profile(printer_profile),
            sampling_mode=sampling_mode,
            mesh_sampling_mm=mesh_sampling_mm,
            max_grid_cells=max_grid_cells,
            max_estimated_triangles=max_estimated_triangles,
            max_estimated_memory_mb=max_estimated_memory_mb,
            resource_budget_mode=resource_budget_mode,
            aoi=request,
            dataset_type=dataset.dataset_type,
            dataset_name=dataset.dataset_name,
            dataset_version=dataset.dataset_version,
            acquisition_period=dataset.acquisition_period,
            source_urls=dataset.source_urls,
            vertical_crs=dataset.vertical_crs,
            vertical_datum=dataset.vertical_datum,
            data_license=dataset.license,
            attribution=dataset.attribution,
            source_provider=dataset.provider,
            source_download_time=dataset.download_time,
            source_checksums=dataset.checksums,
            source_acquisition_manifest=acquisition.acquisition_manifest_path,
        )
        result = build_local_terrain(resolved)
        _emit(
            {
                "status": "completed",
                "provider": dataset.provider,
                "provider_selection": selection.trace.model_dump(mode="json"),
                "geocoding": (
                    place_search.model_dump(mode="json") if place_search is not None else None
                ),
                "dataset": dataset.model_dump(mode="json"),
                "source_raster": str(acquisition.raster_path),
                "source_acquisition": str(acquisition.acquisition_manifest_path),
                "output_dir": str(result.output_dir),
                "required_checks_passed": result.validation["required_checks_passed"],
                "dimensions_mm": result.validation["dimensions_mm"],
                "artifacts": {key: str(path) for key, path in result.artifacts.items()},
                "cache": cache_store.summary().model_dump(mode="json"),
            }
        )
    except PlaceCandidateSelectionError as exc:
        _fail_place_candidates(exc)
    except ProviderSelectionError as exc:
        _fail_provider_selection(exc)
    except (TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


@app.command()
def doctor() -> None:
    """Report the exact local runtime and external manufacturing tools."""
    slicers = {
        name: shutil.which(name)
        for name in (
            "BambuStudio",
            "bambu-studio",
            "OrcaSlicer",
            "orca-slicer",
            "prusa-slicer",
            "PrusaSlicer",
        )
        if shutil.which(name)
    }
    _emit(
        {
            "topoforge": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "gdal": rasterio.__gdal_version__,
            "proj": rasterio.__proj_version__,
            "slicers": slicers,
        }
    )


@app.command()
def cache(
    action: Annotated[str, typer.Argument(help="Supported action: status")] = "status",
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
) -> None:
    """Inspect the content-addressed provider cache state."""
    if action != "status":
        _fail(ValueError("cache action must be status"))
    _emit(ContentAddressedCache(cache_dir).summary().model_dump(mode="json"))


@app.command()
def slice(
    model: Annotated[Path, typer.Argument(help="STL or 3MF model.")],
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("outputs/sliced/model.gcode"),
    profile: Annotated[
        Path | None,
        typer.Option("--profile", help="Legacy process/machine settings file."),
    ] = None,
    machine_profile: Annotated[
        Path | None, typer.Option("--machine-profile", help="Resolved machine preset JSON.")
    ] = None,
    process_profile: Annotated[
        Path | None, typer.Option("--process-profile", help="Resolved process preset JSON.")
    ] = None,
    filament_profile: Annotated[
        Path | None, typer.Option("--filament-profile", help="Resolved filament preset JSON.")
    ] = None,
    slicer: Annotated[
        str,
        typer.Option(
            "--slicer",
            help="bambu-studio (release gate), orca, prusa, or auto (diagnostic fallback).",
        ),
    ] = "bambu-studio",
) -> None:
    """Invoke the installed headless slicer adapter and emit measured results."""
    try:
        from topoforge.validation.slicers import (
            BambuStudioAdapter,
            OrcaSlicerAdapter,
            PrusaSlicerAdapter,
            SlicerProfile,
            SliceStatus,
            select_slicer,
        )

        settings = tuple(
            path for path in (machine_profile, process_profile, profile) if path is not None
        )
        filaments = () if filament_profile is None else (filament_profile,)
        slicer_profile = SlicerProfile(
            name=(
                "Bambu Lab P2S 0.4 / 0.20mm Standard / Bambu PLA Basic"
                if slicer == "bambu-studio"
                else None
            ),
            settings=settings,
            filaments=filaments,
        )
        adapters = {
            "bambu-studio": BambuStudioAdapter,
            "orca": OrcaSlicerAdapter,
            "prusa": PrusaSlicerAdapter,
        }
        if slicer == "auto":
            adapter = select_slicer()
        elif slicer in adapters:
            adapter = adapters[slicer]()
        else:
            raise ValueError("--slicer must be bambu-studio, orca, prusa, or auto")
        result = adapter.slice(model, output, profile=slicer_profile)
        serialized_result = result.model_dump(mode="json")
        if result.status is SliceStatus.SUCCEEDED and (model.parent / "validation.json").is_file():
            report_path = record_slice_validation(model.parent, serialized_result)
            serialized_result["bundle_report"] = str(report_path)
        _emit(serialized_result)
        if result.status is not SliceStatus.SUCCEEDED:
            raise typer.Exit(code=1)
    except (ImportError, TopoForgeError, ValueError, OSError) as exc:
        _fail(exc)


if __name__ == "__main__":  # pragma: no cover
    app()
