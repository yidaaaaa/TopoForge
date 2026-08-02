import {
  AlertTriangle,
  CheckCircle2,
  Download,
  ExternalLink,
  RefreshCw,
  Square,
} from "lucide-react";
import { useCallback } from "react";

import { formatBytes } from "../config";
import {
  jobStateKey,
  stageKey,
  translate,
  type TranslationKey,
} from "../i18n";
import type { JobRecord, Language } from "../types";

interface ResultsPanelProps {
  language: Language;
  jobs: JobRecord[];
  selectedJob: JobRecord | null;
  loading: boolean;
  onRefresh: () => void;
  onSelect: (jobId: string) => void;
  onCancel: (jobId: string) => void;
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

export function ResultsPanel({
  language,
  jobs,
  selectedJob,
  loading,
  onRefresh,
  onSelect,
  onCancel,
}: ResultsPanelProps) {
  const t = useCallback(
    (key: TranslationKey) => translate(language, key),
    [language],
  );
  const formatter = new Intl.DateTimeFormat(language, {
    dateStyle: "medium",
    timeStyle: "short",
  });

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

      <div className="job-list">
        {jobs.length === 0 && <div className="empty-state">{t("noJobs")}</div>}
        {jobs.map((job) => {
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
