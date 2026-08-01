"""Local launch, resume, summary, and artifact-browsing contracts."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from topoforge.exceptions import ConfigurationError
from topoforge.models import BuildConfig
from topoforge.provenance import write_json
from topoforge.util import sha256_file
from topoforge.validation.slicers import (
    BambuStudioAdapter,
    OrcaSlicerAdapter,
    PrusaSlicerAdapter,
    SlicerAdapter,
    SlicerProfile,
    select_slicer,
)
from topoforge.workflow.acquisition import GlobalAcquisitionConfig
from topoforge.workflow.local import (
    LocalWorkflowConfig,
    LocalWorkflowManifest,
    LocalWorkflowResult,
    LocalWorkflowStatus,
    WorkflowStage,
    WorkflowState,
    run_local_workflow,
)

_LAUNCH_SCHEMA_VERSION = "topoforge-workflow-launch-v1"
_SUMMARY_SCHEMA_VERSION = "topoforge-workflow-summary-v1"
SlicerName = Literal["bambu-studio", "orca", "prusa", "auto"]


class WorkflowLaunchConfig(BaseModel):
    """Saved single-workstation inputs needed to execute or resume a workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _LAUNCH_SCHEMA_VERSION
    workspace_dir: Path
    build: BuildConfig
    global_source: GlobalAcquisitionConfig | None = None
    maximum_tile_width_mm: float = Field(default=180.0, gt=0)
    maximum_tile_depth_mm: float = Field(default=180.0, gt=0)
    overlap_cells: int = Field(default=1, ge=0)
    slicing_enabled: bool = False
    slicer_name: SlicerName = "bambu-studio"
    slicer_settings: tuple[Path, ...] = ()
    slicer_filaments: tuple[Path, ...] = ()
    slice_timeout_seconds: float = Field(default=1200.0, gt=0)
    project_evidence_enabled: bool = False
    project_timeout_seconds: float = Field(default=1800.0, gt=0)

    @model_validator(mode="after")
    def validate_launch(self) -> WorkflowLaunchConfig:
        """Reject launch combinations that cannot reach the shared workflow core."""
        if self.schema_version != _LAUNCH_SCHEMA_VERSION:
            raise ValueError(f"unsupported workflow launch schema: {self.schema_version}")
        if self.project_evidence_enabled and not self.slicing_enabled:
            raise ValueError("project_evidence_enabled requires slicing_enabled")
        if self.project_evidence_enabled and self.slicer_name != "bambu-studio":
            raise ValueError("project evidence requires slicer_name=bambu-studio")
        self.workflow_config()
        return self

    def workflow_config(self) -> LocalWorkflowConfig:
        """Return the existing validated orchestration configuration."""
        return LocalWorkflowConfig(
            workspace_dir=self.workspace_dir,
            build=self.build,
            global_source=self.global_source,
            maximum_tile_width_mm=self.maximum_tile_width_mm,
            maximum_tile_depth_mm=self.maximum_tile_depth_mm,
            overlap_cells=self.overlap_cells,
            slicing_enabled=self.slicing_enabled,
            slice_timeout_seconds=self.slice_timeout_seconds,
            project_evidence_enabled=self.project_evidence_enabled,
            project_timeout_seconds=self.project_timeout_seconds,
        )


class WorkflowRunSummary(BaseModel):
    """Concise measured status and artifact index for one local invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SUMMARY_SCHEMA_VERSION
    workflow_id: str
    state: WorkflowState
    source_mode: Literal["local", "global"]
    final_stage: WorkflowStage
    completed_stages: tuple[WorkflowStage, ...] = ()
    reused_stages: tuple[WorkflowStage, ...] = ()
    ready_stages: tuple[WorkflowStage, ...]
    metrics: dict[str, Any]
    artifacts: dict[str, str]
    required_checks_passed: bool


class WorkflowExecutionResult(BaseModel):
    """Workflow result paired with its saved launch and browser artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow: LocalWorkflowResult
    launch_config_path: Path
    summary: WorkflowRunSummary
    summary_path: Path
    report_path: Path


def _atomic_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(value, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_workflow_launch_config(
    config: WorkflowLaunchConfig,
    path: Path | None = None,
) -> Path:
    """Write and strictly reopen a stable workflow launch YAML file."""
    destination = (
        (path if path is not None else config.workspace_dir / "workflow-launch.yaml")
        .expanduser()
        .resolve()
    )
    _atomic_yaml(destination, config.model_dump(mode="json"))
    reopened = read_workflow_launch_config(destination)
    if reopened != config:
        raise ConfigurationError("workflow launch config failed strict YAML reopen")
    return destination


def read_workflow_launch_config(path: Path) -> WorkflowLaunchConfig:
    """Read one saved launch file with strict field validation."""
    resolved = path.expanduser().resolve()
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"workflow launch config is unreadable: {resolved}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("workflow launch config root is not a mapping")
    return WorkflowLaunchConfig.model_validate(raw)


def _slicer_context(
    config: WorkflowLaunchConfig,
) -> tuple[SlicerAdapter | None, SlicerProfile | None]:
    if not config.slicing_enabled:
        return None, None
    if config.slicer_name == "auto":
        adapter: SlicerAdapter = select_slicer()
    elif config.slicer_name == "bambu-studio":
        adapter = BambuStudioAdapter()
    elif config.slicer_name == "orca":
        adapter = OrcaSlicerAdapter()
    else:
        adapter = PrusaSlicerAdapter()
    profile = SlicerProfile(
        name=(
            "Bambu Lab P2S 0.4 / 0.20mm Standard / Bambu PLA Basic"
            if config.slicer_name == "bambu-studio"
            else None
        ),
        settings=config.slicer_settings,
        filaments=config.slicer_filaments,
    )
    return adapter, profile


def _within(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise ConfigurationError(f"workflow artifact escapes workspace: {path}")
    return path


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"workflow JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"workflow JSON root is not an object: {path}")
    return value


def _existing_artifacts(root: Path, candidates: dict[str, Path]) -> dict[str, str]:
    artifacts = {"workspace": str(root)}
    for role, path in candidates.items():
        resolved = path.resolve()
        if resolved.exists():
            if resolved != root and root not in resolved.parents:
                raise ConfigurationError(f"workflow artifact escapes workspace: {resolved}")
            artifacts[role] = str(resolved)
    return artifacts


def inspect_workflow_workspace(
    workspace_dir: Path,
    *,
    completed_stages: tuple[WorkflowStage, ...] = (),
    reused_stages: tuple[WorkflowStage, ...] = (),
) -> WorkflowRunSummary:
    """Strictly reopen workflow records and build a measured artifact index."""
    root = workspace_dir.expanduser().resolve()
    manifest_path = root / "workflow-manifest.json"
    status_path = root / "workflow-status.json"
    try:
        manifest = LocalWorkflowManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        status = LocalWorkflowStatus.model_validate_json(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"workflow manifest/status is unreadable in {root}") from exc
    if manifest.workflow_id != status.workflow_id:
        raise ConfigurationError("workflow manifest and status ids do not match")
    if status.state is not WorkflowState.COMPLETED or status.current_stage is not None:
        raise ConfigurationError("artifact browsing requires a completed workflow")
    if not manifest.required_checks_passed:
        raise ConfigurationError("workflow manifest does not pass required checks")

    stage_paths: dict[WorkflowStage, Path] = {}
    for record in manifest.stages:
        output = _within(root, record.output_path)
        stage_manifest = _within(root, record.manifest_path)
        if not output.is_dir() or not stage_manifest.is_file():
            raise ConfigurationError(f"workflow stage artifact is missing: {record.name.value}")
        if sha256_file(stage_manifest) != record.manifest_sha256:
            raise ConfigurationError(
                f"workflow stage manifest checksum changed: {record.name.value}"
            )
        if not record.required_checks_passed:
            raise ConfigurationError(f"workflow stage is not validated: {record.name.value}")
        stage_paths[record.name] = output
    if tuple(stage_paths) != status.ready_stages:
        raise ConfigurationError("workflow ready stage order does not match the final manifest")

    build_dir = stage_paths.get(WorkflowStage.BUILD)
    if build_dir is None:
        raise ConfigurationError("completed workflow has no build stage")
    validation = _json_object(build_dir / "validation.json")
    metrics: dict[str, Any] = {}
    for key in (
        "dimensions_mm",
        "source_grid_shape",
        "processed_grid_shape",
        "source_horizontal_resolution_m",
        "processed_horizontal_resolution_m",
        "physical_sample_spacing_mm",
        "estimated_triangle_count",
        "peak_elevation_loss_m",
        "peak_horizontal_shift_m",
        "terrain_fidelity_status",
    ):
        if key in validation:
            metrics[key] = validation[key]
    for stage, keys in (
        (WorkflowStage.LAYOUT, ("tile_count", "tile_grid_shape")),
        (WorkflowStage.CONNECT, ("connector_count", "connector_fit_status")),
        (
            WorkflowStage.SLICE,
            (
                "maximum_layer_count",
                "total_gcode_size_bytes",
                "total_estimated_time_seconds",
                "total_filament_used_g",
                "all_exit_codes_zero",
            ),
        ),
    ):
        record = next((item for item in manifest.stages if item.name is stage), None)
        if record is not None:
            for key in keys:
                if key in record.verification:
                    metrics[key] = record.verification[key]

    connect_dir = stage_paths.get(WorkflowStage.CONNECT)
    slice_dir = stage_paths.get(WorkflowStage.SLICE)
    project_dir = stage_paths.get(WorkflowStage.PROJECT)
    candidates = {
        "workflow_launch": root / "workflow-launch.yaml",
        "workflow_summary": root / "workflow-summary.json",
        "workflow_report": root / "workflow-report.html",
        "workflow_storage": root / "workflow-storage.json",
        "workflow_restore": root / "workflow-restore.json",
        "workflow_request": root / "workflow-request.json",
        "workflow_manifest": manifest_path,
        "workflow_status": status_path,
        "build_directory": build_dir,
        "validation_html": build_dir / "validation.html",
        "validation_json": build_dir / "validation.json",
        "provenance": build_dir / "provenance.json",
        "manufacturing_preflight": build_dir / "manufacturing_preflight.json",
        "preview_png": build_dir / "preview.png",
        "preview_glb": build_dir / "preview.glb",
        "model_stl": build_dir / "model.stl",
        "model_3mf": build_dir / "model.3mf",
        "processed_dem": build_dir / "processed_dem.tif",
    }
    if connect_dir is not None:
        candidates.update(
            {
                "print_tiles_directory": connect_dir,
                "connector_map": connect_dir / "connector-map.png",
                "connector_assembly_glb": connect_dir / "connector-assembly.global.glb",
                "connector_validation": connect_dir / "print-tile-assembly-validation.json",
            }
        )
    if slice_dir is not None:
        candidates.update(
            {
                "slice_directory": slice_dir,
                "slice_manifest": slice_dir / "tile-slice-manifest.json",
            }
        )
    if project_dir is not None:
        candidates.update(
            {
                "project_directory": project_dir,
                "project_manifest": project_dir / "bambu-tile-project-manifest.json",
            }
        )
    return WorkflowRunSummary(
        workflow_id=manifest.workflow_id,
        state=status.state,
        source_mode=("global" if WorkflowStage.ACQUIRE in stage_paths else "local"),
        final_stage=manifest.final_stage,
        completed_stages=completed_stages,
        reused_stages=reused_stages,
        ready_stages=status.ready_stages,
        metrics=metrics,
        artifacts=_existing_artifacts(root, candidates),
        required_checks_passed=True,
    )


def _relative_link(report_path: Path, artifact_path: str) -> str:
    target = Path(artifact_path).resolve()
    relative = target.relative_to(report_path.parent.resolve()).as_posix()
    return quote(relative, safe="/.-_") + ("/" if target.is_dir() else "")


def write_workflow_report(path: Path, summary: WorkflowRunSummary) -> Path:
    """Write a dependency-free local artifact browser as one static HTML file."""
    destination = path.expanduser().resolve()
    metric_rows = "\n".join(
        "<tr>"
        f"<th>{html.escape(key)}</th>"
        f"<td><code>{html.escape(json.dumps(value, ensure_ascii=False, default=str))}</code></td>"
        "</tr>"
        for key, value in summary.metrics.items()
    )
    completed = set(summary.completed_stages)
    reused = set(summary.reused_stages)
    stage_states = {
        stage: "completed" if stage in completed else "reused" if stage in reused else "ready"
        for stage in summary.ready_stages
    }
    stage_rows = "\n".join(
        f"<tr><td>{html.escape(stage.value)}</td><td>{stage_states[stage]}</td></tr>"
        for stage in summary.ready_stages
    )
    artifact_rows = "\n".join(
        "<tr>"
        f"<th>{html.escape(role)}</th>"
        f'<td><a href="{html.escape(_relative_link(destination, value), quote=True)}">'
        f"{html.escape(Path(value).name or value)}</a></td>"
        "</tr>"
        for role, value in summary.artifacts.items()
    )
    images: list[str] = []
    for title, value in (
        ("Terrain preview", summary.artifacts.get("preview_png")),
        ("Connector map", summary.artifacts.get("connector_map")),
    ):
        if value is not None:
            link = html.escape(_relative_link(destination, value), quote=True)
            images.append(
                f"<figure><figcaption>{title}</figcaption>"
                f'<a href="{link}"><img src="{link}" alt="{title}"></a></figure>'
            )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TopoForge {html.escape(summary.workflow_id)}</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin: 0; font: 14px system-ui, sans-serif; color: #182127; background: #f6f7f8; }}
header {{ background: #1f2a30; color: white; padding: 20px max(24px, calc((100vw - 1180px) / 2)); }}
header h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }}
header p {{ margin: 0; color: #d7e0e4; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
section {{ margin: 0 0 28px; }}
h2 {{ font-size: 16px; margin: 0 0 10px; letter-spacing: 0; }}
.status {{ color: #86efac; font-weight: 700; }}
table {{ border-collapse: collapse; width: 100%; background: white; }}
th, td {{ border: 1px solid #cfd6da; padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ width: 30%; background: #eef1f2; }}
code {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
a {{ color: #075985; }}
.visuals {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 18px;
}}
figure {{ margin: 0; background: white; border: 1px solid #cfd6da; padding: 10px; }}
figcaption {{ font-weight: 700; margin-bottom: 8px; }}
img {{
  display: block;
  width: 100%;
  height: auto;
  max-height: 560px;
  object-fit: contain;
  background: #e9edef;
}}
</style>
</head>
<body>
<header>
<h1>TopoForge workflow</h1>
<p>{html.escape(summary.workflow_id)} · {html.escape(summary.source_mode)} source ·
<span class="status">PASS</span></p>
</header>
<main>
<section><h2>Measured result</h2><table>{metric_rows}</table></section>
<section><h2>Stages</h2><table>
<tr><th>Stage</th><th>Latest invocation</th></tr>{stage_rows}
</table></section>
<section><h2>Artifacts</h2><table>{artifact_rows}</table></section>
<section class="visuals">{"".join(images)}</section>
</main>
</body>
</html>
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(destination)
    return destination


def publish_workflow_summary(result: LocalWorkflowResult) -> tuple[WorkflowRunSummary, Path, Path]:
    """Strictly inspect a completed result and publish concise JSON/HTML views."""
    from topoforge.workflow.maintenance import (
        estimate_workflow_storage,
        write_workflow_storage_estimate,
    )

    root = result.workspace_dir.resolve()
    summary_path = root / "workflow-summary.json"
    report_path = root / "workflow-report.html"
    storage_path = root / "workflow-storage.json"
    summary = inspect_workflow_workspace(
        root,
        completed_stages=result.completed_stages,
        reused_stages=result.reused_stages,
    )
    launch = read_workflow_launch_config(root / "workflow-launch.yaml")
    storage = estimate_workflow_storage(launch, summary=summary)
    write_workflow_storage_estimate(storage, storage_path)
    summary = summary.model_copy(
        update={
            "metrics": {
                **summary.metrics,
                "storage": {
                    "estimate_basis": storage.estimate_basis,
                    "current_workspace_bytes": storage.current_workspace_bytes,
                    "estimated_peak_workspace_bytes": storage.estimated_peak_workspace_bytes,
                    "estimated_additional_bytes": storage.estimated_additional_bytes,
                    "available_bytes": storage.available_bytes,
                    "sufficient_for_estimate": storage.sufficient_for_estimate,
                    "cleanup_reclaimable_bytes": storage.cleanup_reclaimable_bytes,
                    "backup_input_bytes": storage.backup_input_bytes,
                },
            },
            "artifacts": {
                **summary.artifacts,
                "workflow_summary": str(summary_path),
                "workflow_report": str(report_path),
                "workflow_storage": str(storage_path),
            },
        }
    )
    write_json(summary_path, summary.model_dump(mode="json"))
    write_workflow_report(report_path, summary)
    reopened = WorkflowRunSummary.model_validate_json(summary_path.read_text(encoding="utf-8"))
    if reopened != summary:
        raise ConfigurationError("workflow summary failed strict JSON reopen")
    return summary, summary_path, report_path


def execute_workflow_launch(config: WorkflowLaunchConfig) -> WorkflowExecutionResult:
    """Save one launch request, execute the shared core, and publish local views."""
    workspace = config.workspace_dir.expanduser().resolve()
    normalized = config.model_copy(
        update={
            "workspace_dir": workspace,
            "build": config.build.model_copy(update={"output_dir": workspace}),
        }
    )
    launch_path = write_workflow_launch_config(normalized)
    adapter, profile = _slicer_context(normalized)
    result = run_local_workflow(
        normalized.workflow_config(),
        adapter=adapter,
        profile=profile,
    )
    summary, summary_path, report_path = publish_workflow_summary(result)
    return WorkflowExecutionResult(
        workflow=result,
        launch_config_path=launch_path,
        summary=summary,
        summary_path=summary_path,
        report_path=report_path,
    )
