import { defaultRouteEditorDraft } from "./routeEditor";
import { aoiInput, buildJobRequest, defaultFormState } from "./config";
import type { AoiInput, FormState, Health, JobCreateRequest, JsonObject } from "./types";

export interface ProjectReuseResponse {
  schema_version: "topoforge-project-reuse-v1";
  source_job_id: string;
  original_workspace_dir: string;
  suggested_workspace_name: string;
  request: JobCreateRequest;
  issues: { field: string; path: string | null; code: string; message: string }[];
}

export function jsonObject(value: unknown): JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as JsonObject : {};
}
const buildFields = {
  modelWidthMm: "model_width_mm", modelDepthMm: "model_depth_mm", baseThicknessMm: "base_thickness_mm",
  maxHeightMm: "max_height_mm", verticalScaleMode: "vertical_scale_mode", verticalExaggeration: "vertical_exaggeration",
  samplingMode: "sampling_mode", meshSamplingMm: "mesh_sampling_mm", maxGridCells: "max_grid_cells",
  maxEstimatedTriangles: "max_estimated_triangles", maxEstimatedMemoryMb: "max_estimated_memory_mb", resourceBudgetMode: "resource_budget_mode",
} as const;
const launchFields = {
  maximumTileWidthMm: "maximum_tile_width_mm", maximumTileDepthMm: "maximum_tile_depth_mm",
  overlapCells: "overlap_cells", slicingEnabled: "slicing_enabled", slicerName: "slicer_name", projectEvidenceEnabled: "project_evidence_enabled",
} as const;

/** Map visible controls while retaining the complete request separately. */
export function formFromProject(project: ProjectReuseResponse): FormState {
  const launch = project.request.launch;
  const build = jsonObject(launch.build);
  const global = jsonObject(launch.global_source);
  const form: FormState = { ...defaultFormState, bbox: [...defaultFormState.bbox], center: [...defaultFormState.center], workspaceName: project.suggested_workspace_name, reuseProjectId: project.source_job_id };
  for (const [key, field] of Object.entries({ ...buildFields, ...launchFields })) {
    const source = key in buildFields ? build : launch;
    if (field in source && !(key === "meshSamplingMm" && source[field] === null)) Object.assign(form, { [key]: source[field] });
  }
  form.connectorToleranceMm = Number(jsonObject(build.printer_profile).connector_tolerance_mm ?? defaultFormState.connectorToleranceMm);
  form.sourcePath = typeof build.dem_path === "string" ? build.dem_path : "";
  if (launch.global_source) {
    const aoi = jsonObject(global.aoi);
    if (Array.isArray(aoi.bbox_wgs84)) {
      form.sourceMode = "bbox";
      form.bbox = [...aoi.bbox_wgs84] as FormState["bbox"];
    } else if (Array.isArray(aoi.center_wgs84) && typeof aoi.radius_m === "number") {
      form.sourceMode = "center-radius";
      form.center = [...aoi.center_wgs84] as FormState["center"];
      form.radiusM = aoi.radius_m;
    } else throw new Error("reuse-unsupported-aoi");
  }
  form.terrainMode = (global.terrain_mode ?? build.terrain_mode ?? defaultFormState.terrainMode) as FormState["terrainMode"];
  const route = inheritedRoute(project);
  if (route) form.gpxRoute = route.draft;
  return form;
}

/** Retain a local raster's original clipping polygon until its source is changed. */
export function projectAoi(form: FormState, project: ProjectReuseResponse | null): AoiInput | null {
  const input = aoiInput(form);
  if (input || !project) return input;
  const original = formFromProject(project);
  if (original.sourceMode === "local" && original.sourcePath.trim() === form.sourcePath.trim()) {
    return (jsonObject(project.request.launch.build).aoi as AoiInput | null) ?? null;
  }
  return null;
}

/** Patch only edited controls; all unexposed provenance/engine options survive. */
export function buildReusedRequest(form: FormState, health: Health, project: ProjectReuseResponse, overlay: JsonObject | null): JobCreateRequest {
  if (form.reuseProjectId !== project.source_job_id) throw new Error("reuse-not-ready");
  const baseline = formFromProject(project);
  const fresh = buildJobRequest(form, health, overlay).launch;
  const launch = structuredClone(project.request.launch);
  const build = jsonObject(launch.build);
  const freshBuild = jsonObject(fresh.build);
  const changed = (key: keyof FormState) => JSON.stringify(form[key]) !== JSON.stringify(baseline[key]);
  launch.workspace_dir = fresh.workspace_dir;
  build.output_dir = freshBuild.output_dir;
  for (const [key, field] of Object.entries(buildFields)) if (changed(key as keyof FormState)) build[field] = freshBuild[field];
  for (const [key, field] of Object.entries(launchFields)) if (changed(key as keyof FormState)) launch[field] = fresh[field];
  if (changed("samplingMode") || changed("meshSamplingMm")) build.mesh_sampling_mm = freshBuild.mesh_sampling_mm;
  if (changed("slicingEnabled") || changed("slicerName")) launch.project_evidence_enabled = fresh.project_evidence_enabled;
  if (changed("connectorToleranceMm")) build.printer_profile = { ...jsonObject(build.printer_profile), connector_tolerance_mm: form.connectorToleranceMm };
  const sourceChanged = (form.sourceMode === "local") !== (baseline.sourceMode === "local") || (form.sourceMode === "local" && form.sourcePath.trim() !== baseline.sourcePath.trim());
  const areaChanged = JSON.stringify(aoiInput(form)) !== JSON.stringify(aoiInput(baseline));
  if (sourceChanged) {
    build.dem_path = freshBuild.dem_path;
    build.aoi = freshBuild.aoi;
    launch.global_source = fresh.global_source;
    // These describe the original source and must not be attached to a different raster.
    for (const field of ["dataset_type", "dataset_name", "dataset_version", "acquisition_period", "source_urls", "vertical_crs", "vertical_datum", "data_license", "attribution", "source_provider", "source_download_time", "source_checksums", "source_acquisition_manifest", "reference_peak_elevation_m", "reference_peak_elevation_note"]) delete build[field];
  } else if (launch.global_source) {
    build.dem_path = freshBuild.dem_path;
    if (areaChanged) {
      build.aoi = freshBuild.aoi;
      launch.global_source = { ...jsonObject(launch.global_source), aoi: freshBuild.aoi };
    }
  }
  if (changed("terrainMode")) {
    build.terrain_mode = form.terrainMode;
    if (launch.global_source) launch.global_source = { ...jsonObject(launch.global_source), terrain_mode: form.terrainMode };
  }
  if (launch.global_source) {
    const global = jsonObject(launch.global_source);
    const oldWorkspace = String(project.request.launch.workspace_dir).replaceAll("\\", "/").replace(/\/$/, "");
    const cache = typeof global.cache_dir === "string" ? global.cache_dir.replaceAll("\\", "/") : "";
    if (cache === oldWorkspace || cache.startsWith(`${oldWorkspace}/`)) global.cache_dir = String(launch.workspace_dir) + cache.slice(oldWorkspace.length);
  }
  launch.build = build;
  launch.overlay = overlay;
  return { launch, reuse_source_job_id: project.source_job_id };
}

export function mergeRouteOverlay(base: JsonObject | null, route: JsonObject): JsonObject {
  const existing = base?.sources;
  if (existing !== undefined && !Array.isArray(existing)) throw new Error("invalid-overlay");
  const sources = [...(Array.isArray(existing) ? existing : [])];
  const routeSource = jsonObject((route.sources as unknown[])[0]);
  const ids = new Set(sources.map(source => jsonObject(source).source_id));
  let id = "gpx-route";
  for (let i = 2; ids.has(id); i++) id = `gpx-route-${i}`;
  return { ...(base ?? route), sources: [...sources, { ...routeSource, source_id: id }] };
}

/** Expose the first GPX layer for editing; retain other layers and source metadata. */
export function inheritedRoute(project: ProjectReuseResponse) {
  const sources = jsonObject(project.request.launch.overlay).sources;
  const source = Array.isArray(sources) ? sources.map(jsonObject).find(item => item.kind === "gpx" && item.format === "gpx") : undefined;
  if (!source) return null;
  const style = jsonObject(source.style);
  const defaults = defaultRouteEditorDraft();
  return { source, draft: {
    path: String(source.path ?? ""), datasetName: String(source.dataset_name ?? ""),
    license: String(source.license ?? ""), attribution: String(source.attribution ?? ""),
    color: typeof style.color === "string" ? style.color : defaults.color,
    lineWidthMm: Number(style.line_width_mm ?? defaults.lineWidthMm), raisedHeightMm: Number(style.raised_height_mm ?? defaults.raisedHeightMm), embedDepthMm: Number(style.embed_depth_mm ?? defaults.embedDepthMm),
  } };
}

export function projectOverlay(project: ProjectReuseResponse, route: JsonObject | null): JsonObject | null {
  const overlay = project.request.launch.overlay;
  if (!overlay) return route;
  const base = structuredClone(jsonObject(overlay));
  const inherited = inheritedRoute(project);
  if (!inherited) return route ? mergeRouteOverlay(base, route) : base;
  const replacement = route ? jsonObject((route.sources as unknown[])[0]) : null;
  const sources = (base.sources as unknown[]).flatMap(source => {
    const item = jsonObject(source);
    if (item.source_id !== inherited.source.source_id) return [item];
    if (!replacement) return [];
    if (replacement.path !== item.path) return [{ ...replacement, source_id: item.source_id, style: { ...jsonObject(item.style), ...jsonObject(replacement.style) } }];
    return [{ ...item, ...replacement, source_id: item.source_id, source_crs: item.source_crs, style: { ...jsonObject(item.style), ...jsonObject(replacement.style) } }];
  });
  return sources.length ? { ...base, sources } : null;
}
