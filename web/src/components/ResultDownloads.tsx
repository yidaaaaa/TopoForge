import { Box, CheckCircle2, Download } from "lucide-react";

import { artifactLabel, isPrimaryArtifact } from "../artifactLabels";
import { formatBytes } from "../config";
import { translate } from "../i18n";
import type { JobRecord, Language } from "../types";

export function ResultDownloads({ job, language, onPreview }: {
  job: JobRecord;
  language: Language;
  onPreview: () => void;
}) {
  const t = (key: Parameters<typeof translate>[1]) => translate(language, key);
  const rank = (role: string) => role.startsWith("bambu_project_3mf") ? 0 : role === "overlay_model_3mf" ? 1 : role === "model_3mf" ? 2 : 3;
  const files = job.artifacts.filter(isPrimaryArtifact).sort((a, b) => rank(a.artifact_id) - rank(b.artifact_id));
  const hasPreview = job.artifacts.some(artifact => ["preview_glb", "overlay_preview_glb", "connector_assembly_glb"].includes(artifact.artifact_id) && artifact.download_url);
  if (!files.length && !hasPreview) return null;
  const name = job.workspace_dir.replaceAll("\\", "/").replace(/\/+$/, "").split("/").at(-1);
  return (
    <section className="download-card" aria-label={t("primaryDownloads")}>
      <div className="download-card-heading"><CheckCircle2 size={19} /><span>{t("readyDownloads")}</span></div>
      <strong className="download-project-name">{name}</strong>
      <p>{t("downloadHint")}</p>
      {hasPreview && <button type="button" className="secondary model-preview-action" onClick={onPreview}><Box size={16} />{t("viewCompletedModel")}</button>}
      <div className="primary-download-list">
        {files.map(artifact => <a key={artifact.artifact_id} className="model-download" href={artifact.download_url!} download>
          <Download size={18} />
          <span><strong>{artifactLabel(artifact.artifact_id, t)}</strong><small>{t(artifact.artifact_id.startsWith("bambu_project_3mf") ? "bambuProjectHelp" : artifact.artifact_id === "model_stl" ? "modelStlHelp" : "model3mfHelp")}</small></span>
          <span className="download-size">{formatBytes(artifact.size_bytes)}</span>
        </a>)}
      </div>
    </section>
  );
}
