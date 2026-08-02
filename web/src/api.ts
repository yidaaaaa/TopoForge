import type {
  AoiInput,
  FileListing,
  Health,
  JobAssemblyOverview,
  JobCreateRequest,
  JobDeleteResult,
  JobMaintenanceOverview,
  JobMapManifest,
  JobRecord,
  JsonObject,
  WorkflowBackupRecord,
  WorkflowCleanupResult,
  NormalizedAoi,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text();
    }
    const message =
      typeof detail === "object" &&
      detail !== null &&
      "detail" in detail &&
      typeof detail.detail === "string"
        ? detail.detail
        : response.statusText;
    throw new ApiError(response.status, message, detail);
  }
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<Health> {
  return request<Health>("/api/v1/health");
}

export function normalizeAoi(input: AoiInput): Promise<NormalizedAoi> {
  return request<NormalizedAoi>("/api/v1/aoi/normalize", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function validateJob(payload: JobCreateRequest): Promise<JsonObject> {
  return request<JsonObject>("/api/v1/jobs/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createJob(payload: JobCreateRequest): Promise<JobRecord> {
  return request<JobRecord>("/api/v1/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listJobs(): Promise<JobRecord[]> {
  return request<JobRecord[]>("/api/v1/jobs");
}

export function cancelJob(jobId: string): Promise<JobRecord> {
  return request<JobRecord>(`/api/v1/jobs/${jobId}/cancel`, {
    method: "POST",
  });
}

export function deleteJob(
  jobId: string,
  deleteWorkspace: boolean,
): Promise<JobDeleteResult> {
  return request<JobDeleteResult>(`/api/v1/jobs/${jobId}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm_job_id: jobId, delete_workspace: deleteWorkspace }),
  });
}

export function fetchJobMap(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobMapManifest> {
  return request<JobMapManifest>(`/api/v1/jobs/${jobId}/map/manifest`, { signal });
}

export function fetchJobAssembly(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobAssemblyOverview> {
  return request<JobAssemblyOverview>(`/api/v1/jobs/${jobId}/assembly`, {
    signal,
  });
}

export function fetchJobMaintenance(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobMaintenanceOverview> {
  return request<JobMaintenanceOverview>(`/api/v1/jobs/${jobId}/maintenance`, {
    signal,
  });
}

export function createJobBackup(jobId: string): Promise<WorkflowBackupRecord> {
  return request<WorkflowBackupRecord>(`/api/v1/jobs/${jobId}/backup`, {
    method: "POST",
  });
}

export function cleanupJob(
  jobId: string,
  workflowId: string,
): Promise<WorkflowCleanupResult> {
  return request<WorkflowCleanupResult>(`/api/v1/jobs/${jobId}/cleanup`, {
    method: "POST",
    body: JSON.stringify({ confirm_workflow_id: workflowId }),
  });
}

export function restoreBackup(backupId: string): Promise<JobRecord> {
  return request<JobRecord>(`/api/v1/backups/${backupId}/restore`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function listFiles(path?: string): Promise<FileListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return request<FileListing>(`/api/v1/files${query}`);
}

export function loadLocalConfig(
  kind: "overlay" | "launch",
  path: string,
): Promise<JsonObject> {
  return request<JsonObject>("/api/v1/config/load", {
    method: "POST",
    body: JSON.stringify({ kind, path }),
  });
}
