import type {
  AoiInput,
  FormState,
  Health,
  JobCreateRequest,
  JsonObject,
} from "./types";

export const defaultFormState: FormState = {
  workspaceName: "terrain-model",
  sourceMode: "local",
  sourcePath: "",
  bbox: [99.85, 29.45, 100.05, 29.65],
  center: [99.95, 29.55],
  radiusM: 10000,
  modelWidthMm: 180,
  modelDepthMm: null,
  baseThicknessMm: 3,
  maxHeightMm: 45,
  verticalScaleMode: "auto-perceptual",
  verticalExaggeration: 1,
  samplingMode: "print-aware",
  meshSamplingMm: 0.5,
  maxGridCells: 1_500_000,
  maxEstimatedTriangles: 3_000_000,
  maxEstimatedMemoryMb: 1024,
  resourceBudgetMode: "adapt",
  maximumTileWidthMm: 180,
  maximumTileDepthMm: 180,
  overlapCells: 1,
  slicingEnabled: false,
  slicerName: "bambu-studio",
  projectEvidenceEnabled: false,
  terrainMode: "best-available",
  overlayConfigPath: "",
};

export function sanitizeWorkspaceName(value: string): string {
  const trimmed = value.trim();
  if (!/^[A-Za-z0-9._-]+$/.test(trimmed) || trimmed === "." || trimmed === "..") {
    throw new Error("invalid-workspace");
  }
  return trimmed;
}

export function aoiInput(form: FormState): AoiInput | null {
  if (form.sourceMode === "bbox") {
    return { bbox_wgs84: form.bbox };
  }
  if (form.sourceMode === "center-radius") {
    return {
      center_wgs84: form.center,
      radius_m: form.radiusM,
    };
  }
  return null;
}

function joinPath(parent: string, child: string): string {
  return `${parent.replace(/\/$/, "")}/${child}`;
}

export function buildJobRequest(
  form: FormState,
  health: Health,
  overlay: JsonObject | null = null,
): JobCreateRequest {
  const workspaceName = sanitizeWorkspaceName(form.workspaceName);
  if (form.sourceMode === "local" && !form.sourcePath.trim()) {
    throw new Error("source-required");
  }
  const workspace = joinPath(health.workspace_root, workspaceName);
  const aoi = aoiInput(form);
  const demPath =
    form.sourceMode === "local"
      ? form.sourcePath.trim()
      : joinPath(workspace, "global-source-managed.tif");
  const build: JsonObject = {
    dem_path: demPath,
    output_dir: workspace,
    model_width_mm: form.modelWidthMm,
    model_depth_mm: form.modelDepthMm,
    base_thickness_mm: form.baseThicknessMm,
    max_height_mm: form.maxHeightMm,
    vertical_scale_mode: form.verticalScaleMode,
    vertical_exaggeration: form.verticalExaggeration,
    sampling_mode: form.samplingMode,
    mesh_sampling_mm:
      form.samplingMode === "custom" ? form.meshSamplingMm : null,
    max_grid_cells: form.maxGridCells,
    max_estimated_triangles: form.maxEstimatedTriangles,
    max_estimated_memory_mb: form.maxEstimatedMemoryMb,
    resource_budget_mode: form.resourceBudgetMode,
    aoi,
    output_formats: ["stl", "3mf", "glb"],
  };
  const globalSource =
    aoi === null
      ? null
      : {
          aoi,
          requested_provider_id: "auto",
          terrain_mode: form.terrainMode,
          allow_semantic_fallback: false,
          preferred_provider_ids: [],
          cache_dir: joinPath(health.state_dir, "provider-cache"),
          timeout_seconds: 30,
          max_attempts: 4,
          min_request_interval_seconds: 0.2,
        };
  return {
    launch: {
      workspace_dir: workspace,
      build,
      global_source: globalSource,
      overlay,
      maximum_tile_width_mm: form.maximumTileWidthMm,
      maximum_tile_depth_mm: form.maximumTileDepthMm,
      overlap_cells: form.overlapCells,
      slicing_enabled: form.slicingEnabled,
      slicer_name: form.slicerName,
      slicer_settings: [],
      slicer_filaments: [],
      slice_timeout_seconds: 1200,
      project_evidence_enabled:
        form.slicingEnabled &&
        form.slicerName === "bambu-studio" &&
        form.projectEvidenceEnabled,
      project_timeout_seconds: 1800,
    },
  };
}

export function formatBytes(value: number | null): string {
  if (value === null) {
    return "—";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KiB`;
  }
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}
