import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Download,
  ExternalLink,
  HardDrive,
  ListX,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  Trash2,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { formatBytes } from "../config";
import {
  jobStateKey,
  stageKey,
  translate,
  type TranslationKey,
} from "../i18n";
import type { JobMaintenanceOverview, JobRecord, Language } from "../types";

interface ResultsPanelProps {
  language: Language;
  jobs: JobRecord[];
  selectedJob: JobRecord | null;
  maintenance: JobMaintenanceOverview | null;
  loading: boolean;
  maintenanceLoading: boolean;
  maintenanceBusy:
    | "backup"
    | "cleanup"
    | "restore"
    | "delete-record"
    | "delete-workspace"
    | null;
  onRefresh: () => void;
  onSelect: (jobId: string) => void;
  onCancel: (jobId: string) => void;
  onDelete: (jobId: string, workspaceName: string, deleteWorkspace: boolean) => void;
  onBackup: (jobId: string) => void;
  onCleanup: (jobId: string, workflowId: string) => void;
  onRestore: (backupId: string) => void;
}

function metricValue(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  return JSON.stringify(value);
}

function artifactLabel(role: string): string {
  return role.replaceAll("_", " ");
}


type JobStatusFilter = "all" | "active" | "completed" | "failed" | "cancelled";

function matchesStatus(job: JobRecord, filter: JobStatusFilter): boolean {
  if (filter === "all") {
    return true;
  }
  if (filter === "active") {
    return ["queued", "running", "cancelling"].includes(job.state);
  }
  return job.state === filter;
}

export function ResultsPanel({
  language,
  jobs,
  selectedJob,
  maintenance,
  loading,
  maintenanceLoading,
  maintenanceBusy,
  onRefresh,
  onSelect,
  onCancel,
  onDelete,
  onBackup,
  onCleanup,
  onRestore,
}: ResultsPanelProps) {
  const t = useCallback(
    (key: TranslationKey) => translate(language, key),
    [language],
  );
  const formatter = new Intl.DateTimeFormat(language, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const [jobQuery, setJobQuery] = useState("");
  const [jobStatusFilter, setJobStatusFilter] = useState<JobStatusFilter>("all");
  const filteredJobs = useMemo(() => {
    const query = jobQuery.trim().toLocaleLowerCase(language);
    return jobs.filter((job) => {
      if (!matchesStatus(job, jobStatusFilter)) {
        return false;
      }
      if (!query) {
        return true;
      }
      const workspaceName = job.workspace_dir.split("/").at(-1) ?? job.workspace_dir;
      return `${workspaceName} ${job.job_id}`.toLocaleLowerCase(language).includes(query);
    });
  }, [jobQuery, jobStatusFilter, jobs, language]);

  return (
    <aside className="results-panel" aria-label={t("tabResults")}>
      <div className="results-heading">
        <h2>{t("jobs")}</h2>
        <button
          type="button"
          className="icon-button"
          onClick={onRefresh}
          title={t("refresh")}
          aria-label={t("refresh")}
        >
          <RefreshCw size={17} className={loading ? "spin" : ""} />
        </button>
      </div>

      <div className="job-tools">
        <label className="job-search">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            value={jobQuery}
            onChange={(event) => setJobQuery(event.target.value)}
            aria-label={t("searchJobs")}
            placeholder={t("searchJobs")}
          />
        </label>
        <select
          value={jobStatusFilter}
          onChange={(event) => setJobStatusFilter(event.target.value as JobStatusFilter)}
          aria-label={t("filterJobs")}
        >
          <option value="all">{t("filterAll")}</option>
          <option value="active">{t("filterActive")}</option>
          <option value="completed">{t("filterCompleted")}</option>
          <option value="failed">{t("filterFailed")}</option>
          <option value="cancelled">{t("filterCancelled")}</option>
        </select>
        <span
          className="job-count"
          aria-label={t("visibleJobs")}
          title={t("visibleJobs")}
        >
          {filteredJobs.length}/{jobs.length}
        </span>
      </div>

      <div className="job-list">
        {jobs.length === 0 && <div className="empty-state">{t("noJobs")}</div>}
        {jobs.length > 0 && filteredJobs.length === 0 && (
          <div className="empty-state compact">{t("noMatchingJobs")}</div>
        )}
        {filteredJobs.map((job) => {
          const stageTranslation = stageKey(job.current_stage);
          return (
            <button
              type="button"
              className={selectedJob?.job_id === job.job_id ? "job-row active" : "job-row"}
              key={job.job_id}
              onClick={() => onSelect(job.job_id)}
            >
              <div className="job-row-head">
                <strong>{job.workspace_dir.split("/").at(-1)}</strong>
                <span className={`state-badge ${job.state}`}>
                  {t(jobStateKey(job.state))}
                </span>
              </div>
              <div className="progress-track">
                <span style={{ width: `${job.progress_fraction * 100}%` }} />
              </div>
              <small>
                {stageTranslation ? t(stageTranslation) : t("status")} ·{" "}
                {Math.round(job.progress_fraction * 100)}%
              </small>
            </button>
          );
        })}
      </div>

      {selectedJob ? (
        <div className="job-detail">
          <div className="detail-header">
            <div>
              <h3>{selectedJob.workspace_dir.split("/").at(-1)}</h3>
              <code>{selectedJob.job_id.slice(0, 12)}</code>
            </div>
            {["queued", "running", "cancelling"].includes(selectedJob.state) && (
              <button
                type="button"
                className="danger-button"
                onClick={() => onCancel(selectedJob.job_id)}
                disabled={selectedJob.state === "cancelling"}
              >
                <Square size={15} fill="currentColor" />
                {t("cancel")}
              </button>
            )}
          </div>
          <dl className="summary-list">
            <div>
              <dt>{t("status")}</dt>
              <dd>{t(jobStateKey(selectedJob.state))}</dd>
            </div>
            <div>
              <dt>{t("stage")}</dt>
              <dd>
                {stageKey(selectedJob.current_stage)
                  ? t(stageKey(selectedJob.current_stage) as TranslationKey)
                  : "—"}
              </dd>
            </div>
            <div>
              <dt>{t("created")}</dt>
              <dd>{formatter.format(new Date(selectedJob.created_at))}</dd>
            </div>
          </dl>

          {selectedJob.error && (
            <div className="error-block">
              <AlertTriangle size={18} />
              <div>
                <strong>{t("error")}</strong>
                <p>{selectedJob.error.message}</p>
                <small>
                  {t("correctiveAction")}: {selectedJob.error.corrective_action}
                </small>
              </div>
            </div>
          )}

          {selectedJob.summary && (
            <>
              <div className="subheading">
                <CheckCircle2 size={16} />
                <h3>{t("metrics")}</h3>
              </div>
              <dl className="metric-list">
                {Object.entries(selectedJob.summary.metrics).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key.replaceAll("_", " ")}</dt>
                    <dd>{metricValue(value)}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}

          {selectedJob.state === "completed" && (
            <>
              <div className="subheading">
                <HardDrive size={16} />
                <h3>{t("projectMaintenance")}</h3>
              </div>
              {maintenanceLoading && (
                <div className="empty-state compact">{t("maintenanceLoading")}</div>
              )}
              {maintenance && (
                <div className="maintenance-section">
                  <dl className="summary-list maintenance-summary">
                    <div>
                      <dt>{t("storageUsage")}</dt>
                      <dd>{formatBytes(maintenance.storage.current_workspace_bytes)}</dd>
                    </div>
                    <div>
                      <dt>{t("availableSpace")}</dt>
                      <dd>{formatBytes(maintenance.storage.available_bytes)}</dd>
                    </div>
                    <div>
                      <dt>{t("reclaimableSpace")}</dt>
                      <dd>{formatBytes(maintenance.cleanup.reclaimable_bytes)}</dd>
                    </div>
                    <div>
                      <dt>{t("backupCount")}</dt>
                      <dd>{maintenance.backups.length}</dd>
                    </div>
                  </dl>
                  <div className="maintenance-actions">
                    <button
                      type="button"
                      className="secondary"
                      disabled={maintenanceBusy !== null}
                      onClick={() => onBackup(selectedJob.job_id)}
                    >
                      <Archive size={14} />
                      {t("createBackup")}
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      disabled={
                        maintenanceBusy !== null ||
                        maintenance.cleanup.reclaimable_bytes === 0
                      }
                      onClick={() =>
                        onCleanup(selectedJob.job_id, maintenance.cleanup.workflow_id)
                      }
                    >
                      <Trash2 size={14} />
                      {t("cleanupWorkspace")}
                    </button>
                  </div>
                  <div className="backup-list">
                    {maintenance.backups.length === 0 && (
                      <div className="empty-state compact">{t("noBackups")}</div>
                    )}
                    {maintenance.backups.map((backup) => (
                      <div className="backup-row" key={backup.backup_id}>
                        <a
                          href={backup.download_url}
                          target="_blank"
                          rel="noreferrer"
                          title={t("downloadBackup")}
                        >
                          <Download size={14} />
                          <span>
                            <strong>{backup.backup_id.slice(0, 12)}</strong>
                            <small>
                              {formatBytes(backup.archive_size_bytes)} · {backup.file_count}{" "}
                              {t("backupFiles")}
                            </small>
                          </span>
                        </a>
                        <button
                          type="button"
                          className="icon-button"
                          disabled={maintenanceBusy !== null}
                          onClick={() => onRestore(backup.backup_id)}
                          title={t("restoreBackup")}
                          aria-label={t("restoreBackup")}
                        >
                          <RotateCcw size={15} />
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}

          {["cancelled", "failed", "completed"].includes(selectedJob.state) && (
            <>
              <div className="subheading">
                <ListX size={16} />
                <h3>{t("taskManagement")}</h3>
              </div>
              <div className="task-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={maintenanceBusy !== null}
                  onClick={() =>
                    onDelete(
                      selectedJob.job_id,
                      selectedJob.workspace_dir.split("/").at(-1) ?? selectedJob.workspace_dir,
                      false,
                    )
                  }
                >
                  <ListX size={14} />
                  {t("removeJobRecord")}
                </button>
                <button
                  type="button"
                  className="danger-button"
                  disabled={maintenanceBusy !== null}
                  onClick={() =>
                    onDelete(
                      selectedJob.job_id,
                      selectedJob.workspace_dir.split("/").at(-1) ?? selectedJob.workspace_dir,
                      true,
                    )
                  }
                >
                  <Trash2 size={14} />
                  {t("deleteProjectFiles")}
                </button>
              </div>
            </>
          )}

          <div className="subheading">
            <Download size={16} />
            <h3>{t("artifacts")}</h3>
          </div>
          <div className="artifact-list">
            {selectedJob.artifacts.filter((artifact) => artifact.kind === "file").length ===
              0 && <div className="empty-state compact">{t("noArtifacts")}</div>}
            {selectedJob.artifacts
              .filter((artifact) => artifact.kind === "file")
              .map((artifact) => (
                <a
                  key={artifact.artifact_id}
                  href={artifact.download_url ?? "#"}
                  target="_blank"
                  rel="noreferrer"
                  className="artifact-row"
                >
                  <span>
                    <strong>{artifactLabel(artifact.artifact_id)}</strong>
                    <small>{formatBytes(artifact.size_bytes)}</small>
                  </span>
                  <ExternalLink size={15} />
                </a>
              ))}
          </div>
        </div>
      ) : (
        <div className="empty-state">{t("selectJob")}</div>
      )}
    </aside>
  );
}
