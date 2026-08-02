import type { Geometry } from "geojson";

export type Language = "zh-CN" | "en";
export type SourceMode = "local" | "bbox" | "center-radius";
export type SamplingMode = "print-aware" | "source-preserving" | "custom";
export type ResourceBudgetMode = "adapt" | "strict";
export type TerrainMode = "best-available" | "dtm" | "dsm" | "bathymetry";
export type WorkspaceTab = "map" | "preview" | "assembly";
export type MapTileStyle = "terrain" | "elevation" | "hillshade";
export type AssemblyMode = "2d" | "3d";
export type JobState =
  | "queued"
  | "running"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed";

export interface Health {
  status: string;
  version: string;
  loopback_only: boolean;
  languages: Language[];
  workspace_root: string;
  state_dir: string;
}

export interface AoiInput {
  bbox_wgs84?: [number, number, number, number];
  center_wgs84?: [number, number];
  radius_m?: number;
}

export interface NormalizedAoi {
  kind: string;
  user_input: Record<string, unknown>;
  normalized_geometry_geojson: Geometry;
  bounds_wgs84: [number, number, number, number];
  target_local_crs: string;
  crosses_antimeridian: boolean;
  area_m2: number;
  normalization_method: string;
}

export interface JobArtifact {
  artifact_id: string;
  relative_path: string;
  filename: string;
  kind: "file" | "directory";
  media_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  download_url: string | null;
}

export interface JobError {
  code: string;
  message: string;
  corrective_action: string;
  exception_type: string | null;
}

export interface WorkflowSummary {
  workflow_id: string;
  source_mode: "local" | "global";
  final_stage: string;
  ready_stages: string[];
  metrics: Record<string, unknown>;
  artifacts: Record<string, string>;
  required_checks_passed: boolean;
}

export interface JobMapManifest {
  schema_version: string;
  tilejson: string;
  job_id: string;
  source_sha256: string;
  cache_key: string;
  bounds_wgs84: [number, number, number, number];
  center_wgs84: [number, number];
  minzoom: number;
  maxzoom: number;
  tile_size: number;
  tile_url_template: string;
  styles: MapTileStyle[];
  default_style: MapTileStyle;
  elevation_min_m: number;
  elevation_max_m: number;
  layout_id: string;
  tile_grid_shape: [number, number];
  tile_count: number;
  tile_footprints_geojson: GeoJSON.FeatureCollection;
  attribution: string;
  crosses_antimeridian: boolean;
  web_mercator_latitude_clipped: boolean;
  generator: string;
  required_checks_passed: boolean;
}

export interface AssemblyTile {
  tile_id: string;
  row: number;
  column: number;
  physical_bounds_mm: [number, number, number, number];
  global_bounds_mm: [number, number, number, number, number, number];
  triangle_count: number;
  volume_mm3: number;
  male_connector_ids: string[];
  female_connector_ids: string[];
  glb_url: string;
  glb_sha256: string;
}

export interface AssemblyConnector {
  connector_id: string;
  seam_id: string;
  direction: string;
  male_tile_id: string;
  female_tile_id: string;
  seam_coordinate_mm: number;
  center_along_seam_mm: number;
  insertion_axis: string;
}

export interface JobAssemblyOverview {
  schema_version: string;
  job_id: string;
  layout_id: string;
  model_size_mm: [number, number];
  tile_grid_shape: [number, number];
  tile_count: number;
  seam_count: number;
  connector_count: number;
  row_origin: string;
  column_origin: string;
  east_axis: string;
  north_axis: string;
  up_axis: string;
  aggregate_glb_url: string;
  connector_map_url: string;
  tiles: AssemblyTile[];
  connectors: AssemblyConnector[];
  required_checks_passed: boolean;
}

export interface JobRecord {
  job_id: string;
  created_at: string;
  updated_at: string;
  state: JobState;
  workspace_dir: string;
  expected_stages: string[];
  progress_fraction: number;
  current_stage: string | null;
  ready_stages: string[];
  pid: number | null;
  exit_code: number | null;
  cancellation_requested: boolean;
  error: JobError | null;
  summary: WorkflowSummary | null;
  artifacts: JobArtifact[];
}

export interface WorkflowStorageEstimate {
  workspace: string;
  estimate_basis:
    | "configured_resource_ceilings"
    | "completed_workflow_measurements";
  current_workspace_bytes: number;
  estimated_peak_workspace_bytes: number;
  estimated_additional_bytes: number;
  available_bytes: number;
  estimated_headroom_bytes: number;
  sufficient_for_estimate: boolean;
  cleanup_reclaimable_bytes: number;
  backup_input_bytes: number;
}

export interface WorkflowCleanupCandidate {
  path: string;
  kind: "directory" | "file" | "symlink";
  size_bytes: number;
  reason: string;
}

export interface WorkflowCleanupPlan {
  workflow_id: string;
  workspace: string;
  current_workspace_bytes: number;
  reclaimable_bytes: number;
  candidates: WorkflowCleanupCandidate[];
  required_checks_passed: boolean;
}

export interface WorkflowBackupRecord {
  backup_id: string;
  workflow_id: string;
  original_workspace: string;
  archive_size_bytes: number;
  archive_sha256: string;
  file_count: number;
  download_url: string;
  required_checks_passed: boolean;
}

export interface JobMaintenanceOverview {
  job_id: string;
  storage: WorkflowStorageEstimate;
  cleanup: WorkflowCleanupPlan;
  backups: WorkflowBackupRecord[];
  required_checks_passed: boolean;
}

export interface WorkflowCleanupResult {
  workflow_id: string;
  workspace: string;
  removed_paths: string[];
  reclaimed_bytes: number;
  remaining_workspace_bytes: number;
  required_checks_passed: boolean;
}

export interface FileEntry {
  name: string;
  path: string;
  kind: "file" | "directory";
  size_bytes: number | null;
  selectable: boolean;
}

export interface FileListing {
  path: string | null;
  parent: string | null;
  roots: string[];
  entries: FileEntry[];
}

export interface FormState {
  workspaceName: string;
  sourceMode: SourceMode;
  sourcePath: string;
  bbox: [number, number, number, number];
  center: [number, number];
  radiusM: number;
  modelWidthMm: number;
  modelDepthMm: number | null;
  baseThicknessMm: number;
  maxHeightMm: number;
  samplingMode: SamplingMode;
  meshSamplingMm: number;
  maxGridCells: number;
  maxEstimatedTriangles: number;
  maxEstimatedMemoryMb: number;
  resourceBudgetMode: ResourceBudgetMode;
  maximumTileWidthMm: number;
  maximumTileDepthMm: number;
  overlapCells: number;
  slicingEnabled: boolean;
  slicerName: "bambu-studio" | "orca" | "prusa" | "auto";
  projectEvidenceEnabled: boolean;
  terrainMode: TerrainMode;
  overlayConfigPath: string;
}

export type JsonObject = Record<string, unknown>;

export interface JobCreateRequest {
  launch: JsonObject;
}
