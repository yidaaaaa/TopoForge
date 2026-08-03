import {
  Box,
  Languages,
  LayoutGrid,
  Map as MapIcon,
  Mountain,
  RefreshCw,
} from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  ApiError,
  applyJobBatchDeletion,
  cancelJob,
  cleanupJob,
  createJob,
  createJobBackup,
  fetchJobAssembly,
  fetchJobMaintenance,
  fetchJobMap,
  fetchHealth,
  listJobs,
  listJobTrash,
  loadLocalConfig,
  normalizeAoi,
  planJobBatchDeletion,
  purgeJobTrash,
  restoreBackup,
  restoreJobTrash,
  validateJob,
} from "./api";
import { BuildPanel } from "./components/BuildPanel";
import { FileBrowser } from "./components/FileBrowser";
import { MapPanel } from "./components/MapPanel";
import { ResultsPanel } from "./components/ResultsPanel";
import {
  aoiInput,
  buildJobRequest,
  defaultFormState,
} from "./config";
import { translate, type TranslationKey } from "./i18n";
import type {
  FormState,
  Health,
  JobAssemblyOverview,
  JobBatchDeleteMode,
  JobBatchDeletePlan,
  JobMaintenanceOverview,
  JobMapManifest,
  JobRecord,
  JobTrashRecord,
  JsonObject,
  Language,
  NormalizedAoi,
  WorkspaceTab,
} from "./types";

const TerrainPreview = lazy(() =>
  import("./components/TerrainPreview").then((module) => ({
    default: module.TerrainPreview,
  })),
);

const AssemblyPanel = lazy(() =>
  import("./components/AssemblyPanel").then((module) => ({
    default: module.AssemblyPanel,
  })),
);

function initialLanguage(): Language {
  const saved = window.localStorage.getItem("topoforge-language");
  if (saved === "zh-CN" || saved === "en") {
    return saved;
  }
  return navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

function errorMessage(reason: unknown, language: Language): string {
  if (reason instanceof Error) {
    if (reason.message === "invalid-workspace") {
      return translate(language, "invalidWorkspace");
    }
    if (reason.message === "source-required") {
      return translate(language, "sourceRequired");
    }
    if (reason instanceof ApiError) {
      const detail = reason.detail;
      if (
        typeof detail === "object" &&
        detail !== null &&
        "detail" in detail &&
        typeof detail.detail === "object" &&
        detail.detail !== null &&
        "message" in detail.detail &&
        typeof detail.detail.message === "string"
      ) {
        return detail.detail.message;
      }
    }
    return reason.message;
  }
  return String(reason);
}

export default function App() {
  const [language, setLanguage] = useState<Language>(initialLanguage);
  const [health, setHealth] = useState<Health | null>(null);
  const [form, setForm] = useState<FormState>(defaultFormState);
  const [normalizedAoi, setNormalizedAoi] = useState<NormalizedAoi | null>(null);
  const [drawMode, setDrawMode] = useState<"bbox" | "center" | null>(null);
  const [basemapEnabled, setBasemapEnabled] = useState(false);
  const [tab, setTab] = useState<WorkspaceTab>("map");
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [maintenance, setMaintenance] = useState<JobMaintenanceOverview | null>(null);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceBusy, setMaintenanceBusy] = useState<
    "backup" | "cleanup" | "restore" | null
  >(null);
  const [batchPlan, setBatchPlan] = useState<JobBatchDeletePlan | null>(null);
  const [jobTrash, setJobTrash] = useState<JobTrashRecord[]>([]);
  const [batchBusy, setBatchBusy] = useState<
    "plan" | "apply" | "restore" | "purge" | null
  >(null);
  const [jobMap, setJobMap] = useState<JobMapManifest | null>(null);
  const [jobAssembly, setJobAssembly] = useState<JobAssemblyOverview | null>(null);
  const [selectedTileId, setSelectedTileId] = useState<string | null>(null);
  const [visualizationLoading, setVisualizationLoading] = useState(false);
  const [visualizationError, setVisualizationError] = useState<string | null>(null);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [validating, setValidating] = useState(false);
  const [browserPurpose, setBrowserPurpose] = useState<"dem" | "overlay" | null>(
    null,
  );
  const [notice, setNotice] = useState<{ tone: "error" | "success"; text: string } | null>(
    null,
  );
  const jobsLoadGeneration = useRef(0);
  const lifecycleMutationInProgress = useRef(false);
  const t = useCallback(
    (key: TranslationKey) => translate(language, key),
    [language],
  );

  const loadJobs = useCallback(async (force = false) => {
    if (lifecycleMutationInProgress.current && !force) {
      return;
    }
    const generation = ++jobsLoadGeneration.current;
    setJobsLoading(true);
    try {
      const [records, trashRecords] = await Promise.all([listJobs(), listJobTrash()]);
      if (generation !== jobsLoadGeneration.current) {
        return;
      }
      setJobs(records);
      setJobTrash(trashRecords);
      setSelectedJobId((current) => {
        if (current && records.some((job) => job.job_id === current)) {
          return current;
        }
        return records[0]?.job_id ?? null;
      });
    } catch (reason) {
      if (generation === jobsLoadGeneration.current) {
        setNotice({ tone: "error", text: errorMessage(reason, language) });
      }
    } finally {
      if (generation === jobsLoadGeneration.current) {
        setJobsLoading(false);
      }
    }
  }, [language]);

  useEffect(() => {
    void fetchHealth()
      .then(setHealth)
      .catch((reason) =>
        setNotice({ tone: "error", text: errorMessage(reason, language) }),
      );
    void loadJobs();
  }, [language, loadJobs]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadJobs();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [loadJobs]);

  useEffect(() => {
    window.localStorage.setItem("topoforge-language", language);
    document.documentElement.lang = language;
  }, [language]);

  useEffect(() => {
    if (!notice) {
      return;
    }
    const timer = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const selectedJob = useMemo(
    () => jobs.find((job) => job.job_id === selectedJobId) ?? null,
    [jobs, selectedJobId],
  );

  const loadMaintenance = useCallback(
    async (jobId: string) => {
      setMaintenanceLoading(true);
      try {
        setMaintenance(await fetchJobMaintenance(jobId));
      } catch (reason) {
        setMaintenance(null);
        setNotice({ tone: "error", text: errorMessage(reason, language) });
      } finally {
        setMaintenanceLoading(false);
      }
    },
    [language],
  );

  useEffect(() => {
    if (!selectedJob || selectedJob.state !== "completed") {
      setMaintenance(null);
      setMaintenanceLoading(false);
      return;
    }
    let active = true;
    const controller = new AbortController();
    setMaintenanceLoading(true);
    void fetchJobMaintenance(selectedJob.job_id, controller.signal)
      .then((overview) => {
        if (active) {
          setMaintenance(overview);
        }
      })
      .catch((reason) => {
        if (active) {
          setMaintenance(null);
          setNotice({ tone: "error", text: errorMessage(reason, language) });
        }
      })
      .finally(() => {
        if (active) {
          setMaintenanceLoading(false);
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [language, selectedJob?.job_id, selectedJob?.state]);

  useEffect(() => {
    const jobId = selectedJob?.job_id;
    if (!jobId || selectedJob.state !== "completed") {
      setJobMap(null);
      setJobAssembly(null);
      setSelectedTileId(null);
      setVisualizationError(null);
      setVisualizationLoading(false);
      return;
    }
    let active = true;
    const controller = new AbortController();
    setVisualizationLoading(true);
    setVisualizationError(null);
    void Promise.all([
      fetchJobMap(jobId, controller.signal),
      fetchJobAssembly(jobId, controller.signal),
    ])
      .then(([mapManifest, assembly]) => {
        if (!active) {
          return;
        }
        setJobMap(mapManifest);
        setJobAssembly(assembly);
        setSelectedTileId((current) =>
          current && assembly.tiles.some((tile) => tile.tile_id === current)
            ? current
            : (assembly.tiles[0]?.tile_id ?? null),
        );
      })
      .catch((reason) => {
        if (!active) {
          return;
        }
        setJobMap(null);
        setJobAssembly(null);
        setSelectedTileId(null);
        setVisualizationError(errorMessage(reason, language));
      })
      .finally(() => {
        if (active) {
          setVisualizationLoading(false);
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [language, selectedJob?.job_id, selectedJob?.state]);

  const modelUrl = useMemo(() => {
    if (!selectedJob) {
      return null;
    }
    const preferred = [
      "overlay_preview_glb",
      "preview_glb",
      "connector_assembly_glb",
    ];
    for (const role of preferred) {
      const artifact = selectedJob.artifacts.find(
        (candidate) => candidate.artifact_id === role,
      );
      if (artifact?.download_url) {
        return artifact.download_url;
      }
    }
    return null;
  }, [selectedJob]);

  const updateForm = (next: FormState) => {
    if (
      next.sourceMode !== form.sourceMode ||
      JSON.stringify(aoiInput(next)) !== JSON.stringify(aoiInput(form))
    ) {
      setNormalizedAoi(null);
    }
    setForm(next);
  };

  const validateCurrentAoi = async () => {
    const input = aoiInput(form);
    if (!input) {
      return null;
    }
    setValidating(true);
    try {
      const normalized = await normalizeAoi(input);
      setNormalizedAoi(normalized);
      return normalized;
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
      return null;
    } finally {
      setValidating(false);
    }
  };

  const submit = async () => {
    if (!health) {
      return;
    }
    setSubmitting(true);
    try {
      if (aoiInput(form)) {
        const normalized = await validateCurrentAoi();
        if (!normalized) {
          return;
        }
      }
      let overlay: JsonObject | null = null;
      if (form.overlayConfigPath.trim()) {
        overlay = await loadLocalConfig("overlay", form.overlayConfigPath.trim());
      }
      const payload = buildJobRequest(form, health, overlay);
      await validateJob(payload);
      const record = await createJob(payload);
      setSelectedJobId(record.job_id);
      setNotice({ tone: "success", text: t("jobQueued") });
      await loadJobs();
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async (jobId: string) => {
    try {
      await cancelJob(jobId);
      await loadJobs();
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    }
  };

  const handleBackup = async (jobId: string) => {
    setMaintenanceBusy("backup");
    try {
      await createJobBackup(jobId);
      await loadMaintenance(jobId);
      setNotice({ tone: "success", text: t("backupReady") });
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setMaintenanceBusy(null);
    }
  };

  const handleCleanup = async (jobId: string, workflowId: string) => {
    const confirmation = t("confirmCleanup").replace("{workflowId}", workflowId);
    if (!window.confirm(confirmation)) {
      return;
    }
    setMaintenanceBusy("cleanup");
    try {
      await cleanupJob(jobId, workflowId);
      await loadMaintenance(jobId);
      setNotice({ tone: "success", text: t("cleanupCompleted") });
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setMaintenanceBusy(null);
    }
  };

  const handleRestore = async (backupId: string) => {
    setMaintenanceBusy("restore");
    try {
      const restored = await restoreBackup(backupId);
      await loadJobs();
      setSelectedJobId(restored.job_id);
      setNotice({ tone: "success", text: t("restoreCompleted") });
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setMaintenanceBusy(null);
    }
  };

  const handlePlanBatch = async (jobIds: string[], mode: JobBatchDeleteMode) => {
    setBatchBusy("plan");
    try {
      setBatchPlan(await planJobBatchDeletion(jobIds, mode));
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setBatchBusy(null);
    }
  };

  const handleApplyBatch = async () => {
    if (!batchPlan) {
      return;
    }
    const confirmation = t("confirmBatchApply").replace(
      "{planId}",
      batchPlan.plan_id.slice(0, 12),
    );
    if (!window.confirm(confirmation)) {
      return;
    }
    lifecycleMutationInProgress.current = true;
    jobsLoadGeneration.current += 1;
    setJobsLoading(false);
    setBatchBusy("apply");
    if (selectedJobId && batchPlan.job_ids.includes(selectedJobId)) {
      setSelectedJobId(null);
      setMaintenance(null);
      setJobMap(null);
      setJobAssembly(null);
      setSelectedTileId(null);
    }
    try {
      await applyJobBatchDeletion(batchPlan);
      setBatchPlan(null);
      await loadJobs(true);
      setNotice({ tone: "success", text: t("batchMovedToTrash") });
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
      await loadJobs(true);
    } finally {
      lifecycleMutationInProgress.current = false;
      setBatchBusy(null);
    }
  };

  const handleRestoreTrash = async (batchId: string) => {
    const confirmation = t("confirmRestoreTrash").replace(
      "{batchId}",
      batchId.slice(0, 12),
    );
    if (!window.confirm(confirmation)) {
      return;
    }
    setBatchBusy("restore");
    try {
      await restoreJobTrash(batchId);
      await loadJobs();
      setNotice({ tone: "success", text: t("trashRestored") });
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setBatchBusy(null);
    }
  };

  const handlePurgeTrash = async (batchId: string) => {
    const confirmation = t("confirmPurgeTrash").replace(
      "{batchId}",
      batchId.slice(0, 12),
    );
    if (!window.confirm(confirmation)) {
      return;
    }
    setBatchBusy("purge");
    try {
      await purgeJobTrash(batchId);
      await loadJobs();
      setNotice({ tone: "success", text: t("trashPurged") });
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setBatchBusy(null);
    }
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <Mountain size={23} />
          </span>
          <div>
            <strong>{t("appName")}</strong>
            <span>{t("appSubtitle")}</span>
          </div>
        </div>
        <div className="header-status">
          <span className="local-badge">
            <span className="status-dot" />
            {t("localOnly")}
          </span>
          <span className="version">{health ? `v${health.version}` : "—"}</span>
          <div className="language-switch" aria-label={t("language")}>
            <Languages size={16} aria-hidden="true" />
            <button
              type="button"
              className={language === "zh-CN" ? "active" : ""}
              onClick={() => setLanguage("zh-CN")}
            >
              中
            </button>
            <button
              type="button"
              className={language === "en" ? "active" : ""}
              onClick={() => setLanguage("en")}
            >
              EN
            </button>
          </div>
        </div>
      </header>

      {notice && <div className={`notice ${notice.tone}`}>{notice.text}</div>}

      <div className="workspace-layout">
        <BuildPanel
          language={language}
          form={form}
          normalizedAoi={normalizedAoi}
          drawMode={drawMode}
          busy={submitting || !health}
          validating={validating}
          onFormChange={updateForm}
          onBrowseDem={() => setBrowserPurpose("dem")}
          onBrowseOverlay={() => setBrowserPurpose("overlay")}
          onDrawMode={setDrawMode}
          onValidateAoi={() => void validateCurrentAoi()}
          onSubmit={() => void submit()}
        />

        <main className="visual-workspace">
          <div className="workspace-toolbar">
            <div className="view-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "map"}
                className={tab === "map" ? "active" : ""}
                onClick={() => setTab("map")}
              >
                <MapIcon size={16} />
                {t("map")}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "preview"}
                className={tab === "preview" ? "active" : ""}
                onClick={() => setTab("preview")}
              >
                <Box size={16} />
                {t("preview3d")}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "assembly"}
                className={tab === "assembly" ? "active" : ""}
                onClick={() => setTab("assembly")}
              >
                <LayoutGrid size={16} />
                {t("assembly")}
              </button>
            </div>
            {tab === "map" && (
              <label className="toolbar-toggle">
                <input
                  type="checkbox"
                  checked={basemapEnabled}
                  onChange={(event) => setBasemapEnabled(event.target.checked)}
                />
                <span className="toggle" aria-hidden="true" />
                <span>{basemapEnabled ? t("basemap") : t("offlineMap")}</span>
              </label>
            )}
            {tab === "preview" && selectedJob?.state === "running" && (
              <span className="toolbar-progress">
                <RefreshCw size={15} className="spin" />
                {Math.round(selectedJob.progress_fraction * 100)}%
              </span>
            )}
          </div>
          <div className="visual-stage">
            <div hidden={tab !== "map"} className="stage-view">
              <MapPanel
                language={language}
                sourceMode={form.sourceMode}
                normalizedAoi={normalizedAoi}
                basemapEnabled={basemapEnabled}
                drawMode={drawMode}
                manifest={jobMap}
                selectedTileId={selectedTileId}
                visualizationLoading={visualizationLoading}
                visualizationError={visualizationError}
                onSelectedTileChange={setSelectedTileId}
                onBboxChange={(bbox) => {
                  updateForm({ ...form, bbox, sourceMode: "bbox" });
                  setDrawMode(null);
                }}
                onCenterChange={(center) => {
                  updateForm({ ...form, center, sourceMode: "center-radius" });
                  setDrawMode(null);
                }}
              />
            </div>
            <div hidden={tab !== "preview"} className="stage-view">
              {tab === "preview" && (
                <Suspense fallback={<div className="empty-state">{t("loading")}</div>}>
                  <TerrainPreview language={language} modelUrl={modelUrl} />
                </Suspense>
              )}
            </div>
            <div hidden={tab !== "assembly"} className="stage-view">
              {tab === "assembly" && (
                <Suspense fallback={<div className="empty-state">{t("loading")}</div>}>
                  <AssemblyPanel
                    language={language}
                    assembly={jobAssembly}
                    selectedTileId={selectedTileId}
                    loading={visualizationLoading}
                    error={visualizationError}
                    onSelectedTileChange={setSelectedTileId}
                  />
                </Suspense>
              )}
            </div>
          </div>
        </main>

        <ResultsPanel
          language={language}
          jobs={jobs}
          selectedJob={selectedJob}
          maintenance={maintenance}
          loading={jobsLoading}
          maintenanceLoading={maintenanceLoading}
          maintenanceBusy={maintenanceBusy}
          batchPlan={batchPlan}
          jobTrash={jobTrash}
          batchBusy={batchBusy}
          onRefresh={() => void loadJobs()}
          onSelect={setSelectedJobId}
          onCancel={(jobId) => void handleCancel(jobId)}
          onBackup={(jobId) => void handleBackup(jobId)}
          onCleanup={(jobId, workflowId) => void handleCleanup(jobId, workflowId)}
          onRestore={(backupId) => void handleRestore(backupId)}
          onPlanBatch={(jobIds, mode) => void handlePlanBatch(jobIds, mode)}
          onApplyBatch={() => void handleApplyBatch()}
          onRestoreTrash={(batchId) => void handleRestoreTrash(batchId)}
          onPurgeTrash={(batchId) => void handlePurgeTrash(batchId)}
        />
      </div>

      <FileBrowser
        open={browserPurpose !== null}
        language={language}
        onClose={() => setBrowserPurpose(null)}
        onSelect={(path) => {
          if (browserPurpose === "dem") {
            updateForm({ ...form, sourcePath: path });
          } else if (browserPurpose === "overlay") {
            updateForm({ ...form, overlayConfigPath: path });
          }
        }}
      />
    </div>
  );
}
