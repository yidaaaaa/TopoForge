import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  CheckSquare2,
  Download,
  ExternalLink,
  HardDrive,
  ListX,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  Square,
  Trash2,
  Undo2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { formatBytes } from "../config";
import {
  jobStateKey,
  stageKey,
  translate,
  type TranslationKey,
} from "../i18n";
import type {
  JobBatchDeleteMode,
  JobBatchDeletePlan,
  JobMaintenanceOverview,
  JobRecord,
  JobTrashRecord,
  Language,
} from "../types";

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
    | null;
  batchPlan: JobBatchDeletePlan | null;
  jobTrash: JobTrashRecord[];
  batchBusy: "plan" | "apply" | "restore" | "purge" | null;
  onRefresh: () => void;
  onSelect: (jobId: string | null) => void;
  onCancel: (jobId: string) => void;
  onPlanBatch: (jobIds: string[], mode: JobBatchDeleteMode) => void;
  onApplyBatch: () => void;
  onRestoreTrash: (batchId: string) => void;
  onPurgeTrash: (batchId: string) => void;
  onBackup: (jobId: string) => void;
  onCleanup: (jobId: string, workflowId: string, planId: string) => void;
  onRestore: (backupId: string) => void;
}

function metricValue(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  return JSON.stringify(value);
}

function artifactLabel(
  role: string,
  t: (key: TranslationKey) => string,
): string {
  if (role === "model_3mf") {
    return t("core3mfArtifact");
  }
  if (role.startsWith("bambu_project_3mf")) {
    const tile = role.slice("bambu_project_3mf".length).replace(/^_/, "");
    return tile
      ? `${t("bambuProject3mfArtifact")} · ${tile.replaceAll("_", "-")}`
      : t("bambuProject3mfArtifact");
  }
  if (role.startsWith("bambu_project_validation")) {
    const tile = role
      .slice("bambu_project_validation".length)
      .replace(/^_/, "");
    return tile
      ? `${t("bambuProjectValidationArtifact")} · ${tile.replaceAll("_", "-")}`
      : t("bambuProjectValidationArtifact");
  }
  return role.replaceAll("_", " ");
}


function workspaceBasename(path: string): string {
  const normalized = path.replaceAll("\\", "/").replace(/\/+$/, "");
  return normalized.split("/").at(-1) || path;
}

type JobStatusFilter = "all" | "active" | "completed" | "failed" | "cancelled";
type JobSort = "newest" | "oldest" | "name" | "status";

const terminalStates = new Set(["cancelled", "failed", "completed"]);
const statusOrder: Record<JobRecord["state"], number> = {
  running: 0,
  starting: 1,
  queued: 2,
  cancelling: 3,
  failed: 4,
  cancelled: 5,
  completed: 6,
};

function matchesStatus(job: JobRecord, filter: JobStatusFilter): boolean {
  if (filter === "all") {
    return true;
  }
  if (filter === "active") {
    return ["queued", "starting", "running", "cancelling"].includes(job.state);
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
  batchPlan,
  jobTrash,
  batchBusy,
  onRefresh,
  onSelect,
  onCancel,
  onPlanBatch,
  onApplyBatch,
  onRestoreTrash,
  onPurgeTrash,
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
  const [jobSort, setJobSort] = useState<JobSort>("newest");
  const [batchMode, setBatchMode] = useState<JobBatchDeleteMode>("record-only");
  const [selectedBatchJobIds, setSelectedBatchJobIds] = useState<Set<string>>(
    () => new Set(),
  );
  const filteredJobs = useMemo(() => {
    const query = jobQuery.trim().toLocaleLowerCase(language);
    const visible = jobs.filter((job) => {
      if (!matchesStatus(job, jobStatusFilter)) {
        return false;
      }
      if (!query) {
        return true;
      }
      const name = workspaceBasename(job.workspace_dir);
      return `${name} ${job.job_id}`.toLocaleLowerCase(language).includes(query);
    });
    return [...visible].sort((left, right) => {
      if (jobSort === "oldest") {
        return Date.parse(left.created_at) - Date.parse(right.created_at);
      }
      if (jobSort === "name") {
        const leftName = workspaceBasename(left.workspace_dir);
        const rightName = workspaceBasename(right.workspace_dir);
        return leftName.localeCompare(rightName, language) || left.job_id.localeCompare(right.job_id);
      }
      if (jobSort === "status") {
        return (
          statusOrder[left.state] - statusOrder[right.state] ||
          Date.parse(right.created_at) - Date.parse(left.created_at)
        );
      }
      return Date.parse(right.created_at) - Date.parse(left.created_at);
    });
  }, [jobQuery, jobSort, jobStatusFilter, jobs, language]);
  const visibleTerminalJobIds = useMemo(
    () => filteredJobs.filter((job) => terminalStates.has(job.state)).map((job) => job.job_id),
    [filteredJobs],
  );
  const allVisibleTerminalSelected =
    visibleTerminalJobIds.length > 0 &&
    visibleTerminalJobIds.every((jobId) => selectedBatchJobIds.has(jobId));

  useEffect(() => {
    const retained = new Set(
      [...selectedBatchJobIds].filter((jobId) => jobs.some((job) => job.job_id === jobId)),
    );
    if (
      retained.size !== selectedBatchJobIds.size ||
      [...retained].some((jobId) => !selectedBatchJobIds.has(jobId))
    ) {
      setSelectedBatchJobIds(retained);
    }
  }, [jobs, selectedBatchJobIds]);

  const toggleBatchJob = (jobId: string) => {
    setSelectedBatchJobIds((current) => {
      const next = new Set(current);
      if (next.has(jobId)) {
        next.delete(jobId);
      } else {
        next.add(jobId);
      }
      return next;
    });
  };

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
        <select
          value={jobSort}
          onChange={(event) => setJobSort(event.target.value as JobSort)}
          aria-label={t("sortJobs")}
        >
          <option value="newest">{t("sortNewest")}</option>
          <option value="oldest">{t("sortOldest")}</option>
          <option value="name">{t("sortName")}</option>
          <option value="status">{t("sortStatus")}</option>
        </select>
        <span
          className="job-count"
          aria-label={t("visibleJobs")}
          title={t("visibleJobs")}
        >
          {filteredJobs.length}/{jobs.length}
        </span>
      </div>

      {(visibleTerminalJobIds.length > 0 || selectedBatchJobIds.size > 0) && (
        <div className="batch-toolbar">
          <div className="batch-selection-row">
            <button
              type="button"
              className="icon-button"
              disabled={visibleTerminalJobIds.length === 0 || batchBusy !== null}
              onClick={() =>
                setSelectedBatchJobIds((current) => {
                  const next = new Set(current);
                  if (allVisibleTerminalSelected) {
                    visibleTerminalJobIds.forEach((jobId) => next.delete(jobId));
                  } else {
                    visibleTerminalJobIds.forEach((jobId) => next.add(jobId));
                  }
                  return next;
                })
              }
              title={t("selectVisibleTerminal")}
              aria-label={t("selectVisibleTerminal")}
            >
              <CheckSquare2 size={15} />
            </button>
            <span>
              {t("selectedJobs")}: <strong>{selectedBatchJobIds.size}</strong>
            </span>
            <button
              type="button"
              className="icon-button"
              disabled={selectedBatchJobIds.size === 0 || batchBusy !== null}
              onClick={() => setSelectedBatchJobIds(new Set())}
              title={t("clearBatchSelection")}
              aria-label={t("clearBatchSelection")}
            >
              <X size={15} />
            </button>
          </div>
          <div className="batch-action-row">
            <select
              value={batchMode}
              onChange={(event) =>
                setBatchMode(event.target.value as JobBatchDeleteMode)
              }
              aria-label={t("batchAction")}
            >
              <option value="record-only">{t("batchRecordOnly")}</option>
              <option value="quarantine-workspace">{t("batchQuarantine")}</option>
              <option value="backup-and-quarantine">
                {t("batchBackupQuarantine")}
              </option>
            </select>
            <button
              type="button"
              className="secondary"
              disabled={selectedBatchJobIds.size === 0 || batchBusy !== null}
              onClick={() => onPlanBatch([...selectedBatchJobIds], batchMode)}
            >
              <ShieldCheck size={14} />
              {t("reviewBatch")}
            </button>
          </div>
        </div>
      )}

      <div className="job-list">
        {jobs.length === 0 && <div className="empty-state">{t("noJobs")}</div>}
        {jobs.length > 0 && filteredJobs.length === 0 && (
          <div className="empty-state compact">{t("noMatchingJobs")}</div>
        )}
        {filteredJobs.map((job) => {
          const stageTranslation = stageKey(job.current_stage);
          const terminal = terminalStates.has(job.state);
          return (
            <div
              className={selectedJob?.job_id === job.job_id ? "job-row active" : "job-row"}
              key={job.job_id}
            >
              {terminal && (
                <label
                  className="job-batch-toggle"
                  title={t("selectJobForBatch")}
                >
                  <input
                    type="checkbox"
                    checked={selectedBatchJobIds.has(job.job_id)}
                    onChange={() => toggleBatchJob(job.job_id)}
                    aria-label={`${t("selectJobForBatch")}: ${workspaceBasename(job.workspace_dir)}`}
                  />
                </label>
              )}
              <button
                type="button"
                className="job-row-main"
                onClick={() =>
                  onSelect(selectedJob?.job_id === job.job_id ? null : job.job_id)
                }
                aria-pressed={selectedJob?.job_id === job.job_id}
                title={
                  selectedJob?.job_id === job.job_id
                    ? t("deselectJob")
                    : undefined
                }
              >
                <div className="job-row-head">
                  <strong>{workspaceBasename(job.workspace_dir)}</strong>
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
            </div>
          );
        })}
      </div>

      {batchPlan && (
        <section
          className={`batch-review${batchPlan.required_checks_passed ? "" : " blocked"}`}
          aria-label={t("batchReview")}
        >
          <div className="subheading">
            <ShieldCheck size={16} />
            <h3>{t("batchReview")}</h3>
            <code>{batchPlan.plan_id.slice(0, 12)}</code>
          </div>
          <dl className="summary-list">
            <div>
              <dt>{t("selectedJobs")}</dt>
              <dd>{batchPlan.selected_job_count}</dd>
            </div>
            <div>
              <dt>{t("batchEligible")}</dt>
              <dd>{batchPlan.eligible_job_count}</dd>
            </div>
            <div>
              <dt>{t("batchWorkspaces")}</dt>
              <dd>{batchPlan.unique_workspace_count}</dd>
            </div>
            <div>
              <dt>{t("batchTargetBytes")}</dt>
              <dd>{formatBytes(batchPlan.total_target_bytes)}</dd>
            </div>
            <div>
              <dt>{t("batchBackups")}</dt>
              <dd>{batchPlan.backup_job_ids.length}</dd>
            </div>
          </dl>
          {batchPlan.blockers.length > 0 && (
            <div className="batch-blockers">
              <strong>{t("batchBlocked")}</strong>
              {batchPlan.blockers.map((blocker) => (
                <code key={blocker}>{blocker}</code>
              ))}
            </div>
          )}
          <div className="batch-review-actions">
            <button
              type="button"
              className="danger-button"
              disabled={!batchPlan.required_checks_passed || batchBusy !== null}
              onClick={onApplyBatch}
            >
              <Trash2 size={14} />
              {t("applyBatch")}
            </button>
          </div>
        </section>
      )}

      <section className="trash-section" aria-label={t("trash")}>
        <div className="subheading">
          <Trash2 size={16} />
          <h3>{t("trash")}</h3>
          <span>{jobTrash.length}</span>
        </div>
        {jobTrash.length === 0 && (
          <div className="empty-state compact">{t("trashEmpty")}</div>
        )}
        {jobTrash.map((record) => (
          <div className="trash-row" key={record.batch_id}>
            <div>
              <strong>
                {t("trashBatch")} {record.batch_id.slice(0, 12)}
              </strong>
              <small>
                {record.job_ids.length} · {formatBytes(record.total_quarantined_bytes)}
              </small>
              <small>
                {t("recoveryUntil")}: {formatter.format(new Date(record.purge_after))}
              </small>
            </div>
            <div className="trash-actions">
              <button
                type="button"
                className="icon-button"
                disabled={batchBusy !== null}
                onClick={() => onRestoreTrash(record.batch_id)}
                title={t("restoreTrash")}
                aria-label={t("restoreTrash")}
              >
                <Undo2 size={15} />
              </button>
              <button
                type="button"
                className="icon-button danger-icon"
                disabled={batchBusy !== null}
                onClick={() => onPurgeTrash(record.batch_id)}
                title={t("purgeTrash")}
                aria-label={t("purgeTrash")}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </div>
        ))}
      </section>

      {selectedJob ? (
        <div className="job-detail">
          <div className="detail-header">
            <div>
              <h3>{workspaceBasename(selectedJob.workspace_dir)}</h3>
              <code>{selectedJob.job_id.slice(0, 12)}</code>
            </div>
            <div className="detail-header-actions">
              {["queued", "starting", "running", "cancelling"].includes(selectedJob.state) && (
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
              <button
                type="button"
                className="icon-button"
                onClick={() => onSelect(null)}
                title={t("clearJobSelection")}
                aria-label={t("clearJobSelection")}
              >
                <X size={16} />
              </button>
            </div>
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
                        onCleanup(
                          selectedJob.job_id,
                          maintenance.cleanup.workflow_id,
                          maintenance.cleanup.plan_id,
                        )
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
                  disabled={maintenanceBusy !== null || batchBusy !== null}
                  onClick={() =>
                    onPlanBatch([selectedJob.job_id], "record-only")
                  }
                >
                  <ListX size={14} />
                  {t("removeJobRecord")}
                </button>
                <button
                  type="button"
                  className="danger-button"
                  disabled={maintenanceBusy !== null || batchBusy !== null}
                  onClick={() =>
                    onPlanBatch([selectedJob.job_id], "quarantine-workspace")
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
                  className={`artifact-row${
                    artifact.artifact_id.startsWith("bambu_project_3mf")
                      ? " recommended"
                      : ""
                  }`}
                >
                  <span>
                    <strong>{artifactLabel(artifact.artifact_id, t)}</strong>
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
