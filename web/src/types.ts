import type { Geometry } from "geojson";

export type Language = "zh-CN" | "en";
export type SourceMode = "local" | "bbox" | "center-radius";
export type SamplingMode = "print-aware" | "source-preserving" | "custom";
export type ResourceBudgetMode = "adapt" | "strict";
export type TerrainMode = "best-available" | "dtm" | "dsm" | "bathymetry";
export type WorkspaceTab = "map" | "preview";
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
