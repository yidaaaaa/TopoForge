"""FastAPI loopback adapter for TopoForge local workflows."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi import Path as PathParameter
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from topoforge import __version__
from topoforge.exceptions import ConfigurationError
from topoforge.models import AreaOfInterest, AreaOfInterestInput
from topoforge.raster import normalize_area_of_interest
from topoforge.util import sha256_file
from topoforge.web.configuration import LocalConfigLoadRequest, load_local_config
from topoforge.web.jobs import LocalJobManager, expected_workflow_stages
from topoforge.web.map_tiles import (
    JobAssemblyOverview,
    JobMapManifest,
    MapTileNotFoundError,
    MapTileStyle,
    WebVisualizationService,
)
from topoforge.web.models import (
    FileListing,
    JobCreateRequest,
    JobMaintenanceOverview,
    JobRecord,
    WebAppConfig,
    WorkflowBackupRecord,
    WorkflowCleanupRequest,
    WorkflowRestoreRequest,
)
from topoforge.workflow import WorkflowCleanupResult


def bundled_static_dir() -> Path:
    """Return the installed React bundle directory."""
    return Path(str(files("topoforge.web").joinpath("static"))).resolve()


def verify_static_assets(static_dir: Path | None = None) -> dict[str, Any]:
    """Strictly verify the minimum packaged Web application roles."""
    root = (static_dir or bundled_static_dir()).resolve()
    index = root / "index.html"
    manifest = root / "asset-manifest.json"
    if not index.is_file() or not manifest.is_file():
        raise ConfigurationError(f"Web assets are incomplete in {root}")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Web asset manifest is unreadable: {manifest}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "topoforge-web-assets-v1":
        raise ConfigurationError("Web asset manifest schema is invalid")
    raw_assets = payload.get("assets")
    raw_hashes = payload.get("sha256")
    raw_sizes = payload.get("sizes")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ConfigurationError("Web asset manifest has no assets")
    if not isinstance(raw_hashes, dict) or not isinstance(raw_sizes, dict):
        raise ConfigurationError("Web asset manifest has no checksum or size map")
    for raw in raw_assets:
        if not isinstance(raw, str):
            raise ConfigurationError("Web asset manifest contains a non-string path")
        path = (root / raw).resolve()
        if root not in path.parents or not path.is_file():
            raise ConfigurationError(f"Web asset is missing or unsafe: {raw}")
        expected_sha256 = raw_hashes.get(raw)
        expected_size = raw_sizes.get(raw)
        if not isinstance(expected_sha256, str):
            raise ConfigurationError(f"Web asset SHA-256 is missing: {raw}")
        if not isinstance(expected_size, int):
            raise ConfigurationError(f"Web asset byte count is missing: {raw}")
        if sha256_file(path) != expected_sha256:
            raise ConfigurationError(f"Web asset checksum changed: {raw}")
        if path.stat().st_size != expected_size:
            raise ConfigurationError(f"Web asset byte count changed: {raw}")
    return {
        "static_dir": str(root),
        "asset_count": len(raw_assets),
        "languages": payload.get("languages"),
        "frameworks": payload.get("frameworks"),
        "required_checks_passed": True,
    }


def create_app(
    config: WebAppConfig | None = None,
    *,
    manager: LocalJobManager | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Create the loopback application without duplicating workflow algorithms."""
    resolved = (config or WebAppConfig()).resolved()
    jobs = manager or LocalJobManager(resolved)
    assets = (static_dir or bundled_static_dir()).resolve()
    verify_static_assets(assets)

    visualization = WebVisualizationService(jobs)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        jobs.start()
        try:
            yield
        finally:
            jobs.close()

    app = FastAPI(
        title="TopoForge Local API",
        version=__version__,
        docs_url=None,
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.job_manager = jobs
    app.state.web_config = resolved
    app.state.visualization_service = visualization
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "::1", "testserver"],
    )

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob: https://tile.openstreetmap.org; "
            "style-src 'self' 'unsafe-inline'; "
            "worker-src 'self' blob:; "
            "connect-src 'self' https://tile.openstreetmap.org; "
            "script-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'"
        )
        return response

    @app.exception_handler(ConfigurationError)
    async def configuration_error(_: Request, exc: ConfigurationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": {
                    "code": "configuration-error",
                    "message": str(exc),
                }
            },
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "loopback_only": True,
            "languages": ["zh-CN", "en"],
            "workspace_root": str(resolved.workspace_root),
            "state_dir": str(resolved.state_dir),
        }

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "sampling_modes": ["print-aware", "source-preserving", "custom"],
            "resource_budget_modes": ["adapt", "strict"],
            "source_modes": ["local", "bbox", "center-radius"],
            "terrain_modes": ["best-available", "dtm", "dsm", "bathymetry"],
            "slicers": ["bambu-studio", "orca", "prusa", "auto"],
            "maximum_parallel_jobs": resolved.max_concurrent_jobs,
            "map": {
                "library": "MapLibre",
                "offline_background": True,
                "local_xyz_tiles": True,
                "styles": [style.value for style in MapTileStyle],
            },
            "preview": {
                "library": "Three.js",
                "formats": ["glb"],
                "assembly_tiles": True,
            },
        }

    @app.post("/api/v1/aoi/normalize", response_model=AreaOfInterest)
    def normalize_aoi(request: AreaOfInterestInput) -> AreaOfInterest:
        try:
            return normalize_area_of_interest(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/jobs/validate")
    def validate_job(request: JobCreateRequest) -> dict[str, Any]:
        normalized, slicer = jobs.validate_request(request)
        workspace = normalized.launch.workspace_dir
        if workspace == resolved.workspace_root or resolved.workspace_root not in workspace.parents:
            raise HTTPException(
                status_code=422,
                detail=f"workspace must be a child of {resolved.workspace_root}",
            )
        return {
            "valid": True,
            "workspace": str(workspace),
            "expected_stages": [stage.value for stage in expected_workflow_stages(normalized)],
            "slicer": None if slicer is None else slicer.model_dump(mode="json"),
            "normalized_aoi": (
                normalized.launch.global_source.normalized_aoi().model_dump(mode="json")
                if normalized.launch.global_source is not None
                else None
            ),
        }

    @app.post("/api/v1/jobs", response_model=JobRecord, status_code=201)
    def create_job(request: JobCreateRequest) -> JobRecord:
        return jobs.submit(request)

    @app.get("/api/v1/jobs", response_model=tuple[JobRecord, ...])
    def list_jobs() -> tuple[JobRecord, ...]:
        return jobs.list()

    @app.get("/api/v1/jobs/{job_id}", response_model=JobRecord)
    def get_job(job_id: str) -> JobRecord:
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobRecord)
    def cancel_job(job_id: str) -> JobRecord:
        try:
            return jobs.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get(
        "/api/v1/jobs/{job_id}/maintenance",
        response_model=JobMaintenanceOverview,
    )
    def get_job_maintenance(job_id: str) -> JobMaintenanceOverview:
        try:
            return jobs.maintenance(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post(
        "/api/v1/jobs/{job_id}/backup",
        response_model=WorkflowBackupRecord,
        status_code=201,
    )
    def create_job_backup(job_id: str) -> WorkflowBackupRecord:
        try:
            return jobs.create_backup(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post(
        "/api/v1/jobs/{job_id}/cleanup",
        response_model=WorkflowCleanupResult,
    )
    def cleanup_job(
        job_id: str,
        request: WorkflowCleanupRequest,
    ) -> WorkflowCleanupResult:
        try:
            return jobs.cleanup(
                job_id,
                confirm_workflow_id=request.confirm_workflow_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/api/v1/backups", response_model=tuple[WorkflowBackupRecord, ...])
    def list_backups() -> tuple[WorkflowBackupRecord, ...]:
        return jobs.list_backups()

    @app.get("/api/v1/backups/{backup_id}")
    def get_backup(backup_id: str) -> FileResponse:
        try:
            path, backup = jobs.backup_archive_path(backup_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="backup not found") from exc
        return FileResponse(
            path,
            media_type="application/zip",
            filename=(f"topoforge-{backup.workflow_id[:12]}-{backup.backup_id[:12]}.zip"),
            headers={
                "ETag": f'"{backup.archive_sha256}"',
                "Cache-Control": "private, no-cache",
                "X-TopoForge-Backup-SHA256": backup.archive_sha256,
            },
        )

    @app.post(
        "/api/v1/backups/{backup_id}/restore",
        response_model=JobRecord,
        status_code=201,
    )
    def restore_backup(
        backup_id: str,
        request: WorkflowRestoreRequest,
    ) -> JobRecord:
        try:
            return jobs.restore_backup(
                backup_id,
                workspace_name=request.workspace_name,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="backup not found") from exc

    @app.get("/api/v1/jobs/{job_id}/events")
    async def stream_events(
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        try:
            jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

        async def generate() -> AsyncIterator[str]:
            sequence = after
            terminal = {"completed", "failed", "cancelled"}
            while True:
                events = jobs.read_events(job_id, after=sequence)
                for event in events:
                    sequence = event.sequence
                    yield (f"id: {event.sequence}\nevent: job\ndata: {event.model_dump_json()}\n\n")
                record = jobs.get(job_id)
                if record.state.value in terminal and not events:
                    break
                await asyncio.sleep(resolved.poll_interval_seconds)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_id}")
    def get_artifact(job_id: str, artifact_id: str) -> FileResponse:
        try:
            path, artifact = jobs.artifact_path(job_id, artifact_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        return FileResponse(
            path,
            media_type=artifact.media_type,
            filename=artifact.filename,
        )

    @app.get("/api/v1/jobs/{job_id}/map/manifest", response_model=JobMapManifest)
    def get_map_manifest(job_id: str) -> JobMapManifest:
        try:
            return visualization.manifest(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job map source not found") from exc

    @app.get("/api/v1/jobs/{job_id}/map/tiles/{style}/{z}/{x}/{y}.png")
    def get_map_tile(
        job_id: str,
        style: MapTileStyle,
        z: Annotated[int, PathParameter(ge=0, le=22)],
        x: Annotated[int, PathParameter(ge=0)],
        y: Annotated[int, PathParameter(ge=0)],
    ) -> FileResponse:
        try:
            tile_path, record, cache_state = visualization.tile(job_id, style, z, x, y)
        except (KeyError, MapTileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="map tile not found") from exc
        return FileResponse(
            tile_path,
            media_type="image/png",
            headers={
                "ETag": f'"{record.png_sha256}"',
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-TopoForge-Cache": cache_state,
                "X-TopoForge-Tile-SHA256": record.png_sha256,
            },
        )

    @app.get("/api/v1/jobs/{job_id}/assembly", response_model=JobAssemblyOverview)
    def get_assembly(job_id: str) -> JobAssemblyOverview:
        try:
            return visualization.assembly(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job assembly not found") from exc

    @app.get("/api/v1/jobs/{job_id}/assembly/tiles/{tile_id}.glb")
    def get_assembly_tile(job_id: str, tile_id: str) -> FileResponse:
        try:
            tile_path, expected_sha256 = visualization.assembly_tile_path(job_id, tile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="assembly tile not found") from exc
        return FileResponse(
            tile_path,
            media_type="model/gltf-binary",
            filename=tile_path.name,
            headers={
                "ETag": f'"{expected_sha256}"',
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @app.post("/api/v1/config/load")
    def load_config(request: LocalConfigLoadRequest) -> dict[str, Any]:
        return load_local_config(jobs, request)

    @app.get("/api/v1/files", response_model=FileListing)
    def list_files(path: str | None = None) -> FileListing:
        return jobs.list_files(Path(path) if path is not None else None)

    app.mount("/assets", StaticFiles(directory=assets / "assets"), name="web-assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(assets / "index.html", media_type="text/html")

    @app.get("/{path:path}", include_in_schema=False)
    def spa_fallback(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        return FileResponse(assets / "index.html", media_type="text/html")

    return app
