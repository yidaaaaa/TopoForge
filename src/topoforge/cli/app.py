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
from rasterio.errors import RasterioError

from topoforge import __version__
from topoforge.config import DEFAULT_PRINTER_PROFILE_ID, get_printer_profile, load_build_config
from topoforge.engine import build_local_terrain, record_slice_validation, verify_artifact_bundle
from topoforge.exceptions import TopoForgeError
from topoforge.exporters.three_mf import inspect_3mf
from topoforge.models import (
    AreaOfInterestInput,
    BuildConfig,
    DatasetType,
    SamplingMode,
    VerticalScaleMode,
)
from topoforge.providers import (
    CachingHttpClient,
    ContentAddressedCache,
    CopernicusAwsProvider,
    HttpTransportConfig,
    list_provider_descriptors,
)
from topoforge.raster import SyntheticTerrain, create_synthetic_geotiff
from topoforge.util import sha256_file
from topoforge.validation import validate_mesh

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
                max_estimated_memory_mb=max_estimated_memory_mb,
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
    provider: Annotated[str, typer.Option("--provider")] = "copernicus-aws",
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("cache/providers"),
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds", min=0.1)] = 30.0,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 4,
    min_request_interval_seconds: Annotated[
        float, typer.Option("--min-request-interval-seconds", min=0.0)
    ] = 0.2,
) -> None:
    """Fetch, cache, verify, and AOI-crop a no-key global DEM."""
    try:
        if provider != "copernicus-aws":
            raise ValueError("--provider currently supports copernicus-aws")
        request = AreaOfInterestInput(
            bbox_wgs84=bbox,
            center_wgs84=center,
            radius_m=radius_m,
        )
        from topoforge.raster import normalize_area_of_interest

        normalized = normalize_area_of_interest(request)
        cache_store = ContentAddressedCache(cache_dir)
        client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                min_request_interval_seconds=min_request_interval_seconds,
            ),
        )
        acquisition = CopernicusAwsProvider(client).acquire(normalized, output)
        _emit(
            {
                "status": "completed",
                "provider": acquisition.provider_id,
                "dataset": acquisition.dataset.model_dump(mode="json"),
                "aoi": acquisition.aoi,
                "plan": acquisition.plan.model_dump(mode="json"),
                "raster": str(acquisition.raster_path),
                "source_acquisition": str(acquisition.acquisition_manifest_path),
                "cache": cache_store.summary().model_dump(mode="json"),
            }
        )
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
        request = AreaOfInterestInput(
            bbox_wgs84=bbox,
            center_wgs84=center,
            radius_m=radius_m,
        )
        from topoforge.raster import normalize_area_of_interest

        normalized = normalize_area_of_interest(request)
        source_root = (
            source_dir if source_dir is not None else output.parent / f"{output.name}-source"
        ).resolve()
        if source_root.exists():
            raise ValueError(
                f"source evidence directory already exists; choose a new path: {source_root}"
            )
        source_raster = source_root / "copernicus-aws-aoi.tif"
        cache_store = ContentAddressedCache(cache_dir)
        client = CachingHttpClient(
            cache_store,
            HttpTransportConfig(
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                min_request_interval_seconds=min_request_interval_seconds,
            ),
        )
        acquisition = CopernicusAwsProvider(client).acquire(normalized, source_raster)
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
            max_estimated_memory_mb=max_estimated_memory_mb,
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
