import {
  Box,
  Languages,
  LayoutGrid,
  Map as MapIcon,
  Mountain,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
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
  fetchProjectReuse,
  previewGpx,
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
import { PlaceSearch } from "./components/PlaceSearch";
import type { PlaceCandidate } from "./types";
import { MapPanel } from "./components/MapPanel";
import { ResultsPanel } from "./components/ResultsPanel";
import {
  aoiInput,
  buildJobRequest,
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
  ReferenceMapStyle,
  WorkspaceTab,
} from "./types";

import { GpxRouteEditor } from "./GpxRouteEditor";
import { buildRouteOverlay, defaultRouteEditorDraft, routePreviewMatches, type GpxRoutePreview } from "./routeEditor";
import { buildReusedRequest, formFromProject, projectAoi, projectOverlay, mergeRouteOverlay, type ProjectReuseResponse } from "./projectReuse";
import { freshForm } from "./workspaceStorage";
import { useWorkspaceDraft } from "./useWorkspaceDraft";

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
  let saved: string | null = null;
  try { saved = window.localStorage.getItem("topoforge-language"); } catch { /* Browser preferences are optional. */ }
  if (saved === "zh-CN" || saved === "en") {
    return saved;
  }
  return navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

function errorMessage(reason: unknown, language: Language): string {
  if (reason instanceof Error) {
    if (reason.message === "reuse-unsupported-aoi") return translate(language, "reuseUnsupported");
    if (reason.message === "reuse-not-ready") return translate(language, "reuseNotReady");
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

type BasemapMode = "off" | "online" | "cached";

function initialBasemapMode(): BasemapMode {
  let saved: string | null = null;
  try { saved = window.localStorage.getItem("topoforge-basemap-mode"); } catch { /* Stay offline if storage is unavailable. */ }
  return saved === "online" || saved === "cached" ? saved : "off";
}

function initialBasemapStyle(): ReferenceMapStyle {
  try { return localStorage.getItem("topoforge-basemap-style") === "terrain" ? "terrain" : "standard"; }
  catch { return "standard"; }
}

export default function App() {
  const [language, setLanguage] = useState<Language>(initialLanguage);
  const [health, setHealth] = useState<Health | null>(null);
  const { form, setForm, draftStatus } = useWorkspaceDraft();
  const [reuseProject, setReuseProject] = useState<ProjectReuseResponse | null>(null);
  const [reuseMap, setReuseMap] = useState<JobMapManifest | null>(null);
  const [reuseBusy, setReuseBusy] = useState(false);
  const [reuseError, setReuseError] = useState<string | null>(null);
  const [reuseReload, setReuseReload] = useState(0);
  const reuseGeneration = useRef(0);
  const [routePreview, setRoutePreview] = useState<GpxRoutePreview | null>(null);
  const [routeFocus, setRouteFocus] = useState<GpxRoutePreview | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(true);
  const [resultsOpen, setResultsOpen] = useState(true);
  const [normalizedAoi, setNormalizedAoi] = useState<NormalizedAoi | null>(null);
  const [locatedPlace, setLocatedPlace] = useState<PlaceCandidate | null>(null);
  const [drawMode, setDrawMode] = useState<"bbox" | "center" | null>(null);
  const [basemapMode, setBasemapMode] = useState<BasemapMode>(initialBasemapMode);
  const [basemapStyle, setBasemapStyle] = useState<ReferenceMapStyle>(initialBasemapStyle);
  useEffect(() => {
    try { localStorage.setItem("topoforge-basemap-style", basemapStyle); } catch { /* optional preference */ }
  }, [basemapStyle]);
  const basemapEnabled = basemapMode !== "off";
  const basemapCacheOnly = basemapMode === "cached";
  useEffect(() => {
    try { window.localStorage.setItem("topoforge-basemap-mode", basemapMode); } catch { /* Optional preference. */ }
  }, [basemapMode]);
  const [tab, setTab] = useState<WorkspaceTab>("map");
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const initialDraftAoi = useRef(draftStatus === "restored" ? aoiInput(form) : null);
  const currentDraftContext = useRef({ form, language });
  currentDraftContext.current = { form, language };
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
  const [browserPurpose, setBrowserPurpose] = useState<"dem" | "overlay" | "gpx" | null>(
    null,
  );
  const [notice, setNotice] = useState<{ tone: "error" | "success"; text: string } | null>(
    null,
  );
  const jobsLoadGeneration = useRef(0);
  const jobsLoadInFlight = useRef<{
    generation: number;
    promise: Promise<void>;
  } | null>(null);
  const lifecycleMutationInProgress = useRef(false);
  const selectionClearedByUser = useRef(false);
  const selectedJobIdRef = useRef<string | null>(null);
  const t = useCallback(
    (key: TranslationKey) => translate(language, key),
    [language],
  );

  useEffect(() => {
    const input = initialDraftAoi.current;
    if (!input) return;
    let active = true;
    // Reuse the engine's local AOI normalization; a restored draft never launches
    // a job or fetches elevation data. Edits made while this is pending win.
    normalizeAoi(input).then(area => {
      if (active && JSON.stringify(aoiInput(currentDraftContext.current.form)) === JSON.stringify(input)) setNormalizedAoi(area);
    }).catch(reason => {
      if (active && JSON.stringify(aoiInput(currentDraftContext.current.form)) === JSON.stringify(input)) setNotice({ tone: "error", text: errorMessage(reason, currentDraftContext.current.language) });
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const id = form.reuseProjectId;
    if (!id) { setReuseProject(null); setReuseError(null); setReuseBusy(false); return; }
    if (reuseProject?.source_job_id === id) return;
    const controller = new AbortController();
    setReuseBusy(true);
    setReuseError(null);
    fetchProjectReuse(id, controller.signal).then(project => {
      if (controller.signal.aborted) return;
      formFromProject(project); // Refuse a source format the controls cannot represent.
      setReuseProject(project);
    }).catch(reason => {
      if (!controller.signal.aborted) setReuseError(errorMessage(reason, currentDraftContext.current.language));
    }).finally(() => { if (!controller.signal.aborted) setReuseBusy(false); });
    return () => controller.abort();
  }, [form.reuseProjectId, reuseReload]);

  useEffect(() => {
    const input = projectAoi(form, reuseProject);
    if (!input || !reuseProject) return;
    let active = true;
    normalizeAoi(input).then(area => { if (active) setNormalizedAoi(area); })
      .catch(reason => { if (active) setNotice({ tone: "error", text: errorMessage(reason, currentDraftContext.current.language) }); });
    return () => { active = false; };
  }, [reuseProject, form.sourceMode, form.sourcePath, JSON.stringify(aoiInput(form))]);

  const loadJobs = useCallback((force = false): Promise<void> => {
    if (lifecycleMutationInProgress.current && !force) {
      return Promise.resolve();
    }
    const pending = jobsLoadInFlight.current;
    if (!force && pending?.generation === jobsLoadGeneration.current) {
      // Slow responses must survive subsequent polling ticks. Starting another
      // generation every second would discard every completed slow response.
      return pending.promise;
    }
    const generation = ++jobsLoadGeneration.current;
    setJobsLoading(true);
    const promise = (async () => {
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
          if (selectionClearedByUser.current) {
            return null;
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
        if (jobsLoadInFlight.current?.generation === generation) {
          jobsLoadInFlight.current = null;
        }
      }
    })();
    jobsLoadInFlight.current = { generation, promise };
    return promise;
  }, [language]);

  useEffect(() => {
    void fetchHealth()
      .then(setHealth)
      .catch((reason) =>
        setNotice({ tone: "error", text: errorMessage(reason, language) }),
      );
    void loadJobs(true);
  }, [language, loadJobs]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadJobs();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [loadJobs]);

  useEffect(() => {
    try { window.localStorage.setItem("topoforge-language", language); } catch { /* Optional preference. */ }
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
  selectedJobIdRef.current = selectedJobId;
  const visibleMaintenance =
    maintenance?.job_id === selectedJob?.job_id ? maintenance : null;
  const visibleJobMap = jobMap?.job_id === selectedJob?.job_id ? jobMap : null;
  const visibleJobAssembly =
    jobAssembly?.job_id === selectedJob?.job_id ? jobAssembly : null;

  useEffect(() => { setRouteFocus(null); }, [selectedJobId]);

  const handleJobSelect = useCallback((jobId: string | null) => {
    selectionClearedByUser.current = jobId === null;
    setSelectedJobId(jobId);
  }, []);

  const loadMaintenance = useCallback(
    async (jobId: string) => {
      setMaintenanceLoading(true);
      try {
        const overview = await fetchJobMaintenance(jobId);
        if (selectedJobIdRef.current === jobId) {
          setMaintenance(overview);
        }
      } catch (reason) {
        if (selectedJobIdRef.current === jobId) {
          setMaintenance(null);
          setNotice({ tone: "error", text: errorMessage(reason, language) });
        }
      } finally {
        if (selectedJobIdRef.current === jobId) {
          setMaintenanceLoading(false);
        }
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
    setMaintenance(null);
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
    setJobMap(null);
    setJobAssembly(null);
    setSelectedTileId(null);
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

  useEffect(() => {
    setReuseMap(null);
    if (!reuseProject || !jobs.some(job => job.job_id === reuseProject.source_job_id && job.state === "completed")) return;
    const controller = new AbortController();
    fetchJobMap(reuseProject.source_job_id, controller.signal).then(manifest => {
      if (!controller.signal.aborted) setReuseMap({ ...manifest, tile_footprints_geojson: { type: "FeatureCollection", features: [] } });
    }).catch(() => { /* The source settings remain editable if no processed map is available. */ });
    return () => controller.abort();
  }, [reuseProject?.source_job_id, jobs.some(job => job.job_id === reuseProject?.source_job_id && job.state === "completed")]);

  const copiedSourceMap = useMemo(() => {
    if (!reuseProject || !reuseMap || reuseMap.job_id !== reuseProject.source_job_id || selectedJobId) return null;
    const baseline = formFromProject(reuseProject);
    const sameSource = form.sourceMode === baseline.sourceMode && (form.sourceMode !== "local" || form.sourcePath.trim() === baseline.sourcePath.trim());
    return sameSource && JSON.stringify(aoiInput(form)) === JSON.stringify(aoiInput(baseline)) ? reuseMap : null;
  }, [reuseMap, reuseProject, selectedJobId, form.sourceMode, form.sourcePath, JSON.stringify(aoiInput(form))]);

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
    reuseGeneration.current += 1;
    if (next.gpxRoute?.path.trim() !== form.gpxRoute?.path.trim()) { setRoutePreview(null); setRouteFocus(null); }
    if (
      next.sourceMode !== form.sourceMode ||
      (next.sourceMode === "local" && next.sourcePath !== form.sourcePath) ||
      JSON.stringify(aoiInput(next)) !== JSON.stringify(aoiInput(form))
    ) {
      setNormalizedAoi(null);
      setRouteFocus(null);
    }
    setForm(next);
  };

  const validateCurrentAoi = async () => {
    const input = projectAoi(form, reuseProject);
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
      if (projectAoi(form, reuseProject)) {
        const normalized = await validateCurrentAoi();
        if (!normalized) {
          return;
        }
      }
      if (form.reuseProjectId && reuseProject?.source_job_id !== form.reuseProjectId) throw new Error("reuse-not-ready");
      let route: JsonObject | null = null;
      if (form.gpxRoute) {
        route = buildRouteOverlay(form.gpxRoute, routePreview);
        if (!route || !routePreview) throw new Error(t("routeNeedsReview"));
        const checked = await previewGpx(form.gpxRoute.path, undefined, form.reuseProjectId);
        if (checked.sha256 !== routePreview.sha256 || checked.path !== routePreview.path) {
          setRoutePreview(null);
          throw new Error(t("routeChanged"));
        }
      }
      let overlay = reuseProject ? projectOverlay(reuseProject, route) : route;
      if (form.overlayConfigPath.trim()) {
        const configured = await loadLocalConfig("overlay", form.overlayConfigPath.trim());
        overlay = route ? mergeRouteOverlay(configured, route) : configured;
      }
      const payload = reuseProject ? buildReusedRequest(form, health, reuseProject, overlay) : buildJobRequest(form, health, overlay);
      await validateJob(payload);
      const record = await createJob(payload);
      setResultsOpen(true);
      selectionClearedByUser.current = false;
      setSelectedJobId(record.job_id);
      setNotice({ tone: "success", text: t("jobQueued") });
      await loadJobs(true);
    } catch (reason) {
      setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally {
      setSubmitting(false);
    }
  };

  const handleReuse = async (jobId: string) => {
    const generation = ++reuseGeneration.current;
    setReuseBusy(true);
    try {
      const project = await fetchProjectReuse(jobId);
      if (generation !== reuseGeneration.current || selectedJobIdRef.current !== jobId) return;
      const next = formFromProject(project);
      setReuseProject(project);
      setReuseError(null);
      setForm(next);
      setRoutePreview(null);
      setRouteFocus(null);
      setNormalizedAoi(null);
      setLocatedPlace(null);
      setDrawMode(null);
      selectionClearedByUser.current = true;
      setSelectedJobId(null);
      setSettingsOpen(true);
      setTab("map");
      setNotice({ tone: "success", text: t("copyReady") });
    } catch (reason) {
      if (generation === reuseGeneration.current) setNotice({ tone: "error", text: errorMessage(reason, language) });
    } finally { setReuseBusy(false); }
  };

  const handleCancel = async (jobId: string) => {
    try {
      await cancelJob(jobId);
      await loadJobs(true);
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

  const handleCleanup = async (jobId: string, workflowId: string, planId: string) => {
    const confirmation = t("confirmCleanup").replace("{workflowId}", workflowId);
    if (!window.confirm(confirmation)) {
      return;
    }
    setMaintenanceBusy("cleanup");
    try {
      await cleanupJob(jobId, workflowId, planId);
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
      await loadJobs(true);
      selectionClearedByUser.current = false;
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
      await loadJobs(true);
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
      await loadJobs(true);
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
          <nav className="panel-toggles" aria-label={t("workspaceControls")}>
            <button type="button" aria-label={t(settingsOpen ? "hideSettings" : "showSettings")} title={t(settingsOpen ? "hideSettings" : "showSettings")} aria-controls="build-settings-panel" aria-expanded={settingsOpen} onClick={() => setSettingsOpen(value => !value)}>
              {settingsOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}<span>{t("settingsShort")}</span>
            </button>
            <button type="button" aria-label={t(resultsOpen ? "hideResults" : "showResults")} title={t(resultsOpen ? "hideResults" : "showResults")} aria-controls="job-results-panel" aria-expanded={resultsOpen} onClick={() => setResultsOpen(value => !value)}>
              {resultsOpen ? <PanelRightClose size={17} /> : <PanelRightOpen size={17} />}<span>{t("resultsShort")}</span>
            </button>
          </nav>
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

      <div className={`workspace-layout${settingsOpen ? "" : " settings-collapsed"}${resultsOpen ? "" : " results-collapsed"}`}>
        <BuildPanel
          collapsed={!settingsOpen}
          draftStatus={draftStatus}
          language={language}
          form={form}
          normalizedAoi={normalizedAoi}
          drawMode={drawMode}
          busy={submitting || !health || reuseBusy || Boolean(form.reuseProjectId && !reuseProject)}
          validating={validating}
          onFormChange={updateForm}
          onBrowseDem={() => setBrowserPurpose("dem")}
          onBrowseOverlay={() => setBrowserPurpose("overlay")}
          onDrawMode={setDrawMode}
          onValidateAoi={() => void validateCurrentAoi()}
          projectControls={form.reuseProjectId && <section className="control-section reuse-context">
            <strong>{t("copyContext")}</strong><p>{t("copyHelp")}</p>
            {copiedSourceMap && <p>{t("copyMapReference")}</p>}
            {reuseProject?.issues.map((issue, index) => <p className="inline-warning" key={index}>{language === "zh-CN" ? issue.code === "missing-input" ? "请恢复此源文件，或选择替代文件后生成。" : issue.code === "workspace-dependency" ? "此副本仍使用原工作区内的源文件，请保留原工作区。" : "此文件不在当前文件浏览范围内；更换文件时请从允许的目录选择。" : issue.message}<br /><code>{issue.path}</code></p>)}
            {reuseError && <><p className="inline-error">{reuseError}</p><button type="button" onClick={() => setReuseReload(value => value + 1)}>{t("retryReuse")}</button></>}
            <button type="button" className="text-button" disabled={submitting} onClick={() => { updateForm(freshForm()); setReuseProject(null); setNormalizedAoi(null); }}>{t("newBlankProject")}</button>
          </section>}
          routeEditor={form.gpxRoute ? <GpxRouteEditor language={language} value={form.gpxRoute}
            onChange={gpxRoute => updateForm({ ...form, gpxRoute })} onBrowse={() => setBrowserPurpose("gpx")}
            loadPreview={(path, signal) => previewGpx(path, signal, form.reuseProjectId)} preview={routePreview} onPreviewChange={setRoutePreview}
            onFocusRoute={preview => { setRouteFocus({ ...preview }); setTab("map"); }}
            onRemove={() => updateForm({ ...form, gpxRoute: null })} disabled={submitting}
          /> : <section className="control-section"><button type="button" className="secondary" disabled={submitting} onClick={() => updateForm({ ...form, gpxRoute: defaultRouteEditorDraft() })}>{t("routeAdd")}</button></section>}
          onSubmit={() => void submit()}
        />

        <main className="visual-workspace">
          <div className="workspace-steps" aria-label={t("workflowGuide")}>
            <span><b>1</b>{t("selectionStep")}</span><span className="step-rule" />
            <span><b>2</b>{t("settingsStep")}</span><span className="step-rule" />
            <span><b>3</b>{t("resultStep")}</span>
          </div>
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
              <div className="basemap-tools">
                <select className="basemap-style-picker" aria-label={t("referenceMapStyle")}
                  value={basemapStyle} disabled={!basemapEnabled}
                  onChange={event => setBasemapStyle(event.target.value as ReferenceMapStyle)}>
                  <option value="standard">{t("referenceStandard")}</option>
                  <option value="terrain">{t("referenceTerrain")}</option>
                </select>
                <div className="basemap-options">
                  <label className="toolbar-toggle">
                    <input
                      type="checkbox"
                      checked={basemapEnabled}
                      onChange={(event) => setBasemapMode(event.target.checked ? "online" : "off")}
                    />
                    <span className="toggle" aria-hidden="true" />
                    <span>{basemapCacheOnly ? t("cachedBasemap") : basemapEnabled ? t("basemap") : t("offlineMap")}</span>
                  </label>
                  {basemapEnabled && (
                    <label className="cache-only-toggle" title={t("basemapCacheHelp")}>
                      <input
                        type="checkbox"
                        checked={basemapCacheOnly}
                        onChange={(event) => setBasemapMode(event.target.checked ? "cached" : "online")}
                      />
                      <span>{t("basemapCacheOnly")}</span>
                    </label>
                  )}
                </div>
              </div>
            )}
            {tab === "preview" && selectedJob?.state === "running" && (
              <span className="toolbar-progress">
                <RefreshCw size={15} className="spin" />
                {Math.round(selectedJob.progress_fraction * 100)}%
              </span>
            )}
          </div>
          <div className="visual-stage">
            <div hidden={tab !== "map"} className="stage-view map-stage">
              <PlaceSearch language={language} cacheOnly={basemapCacheOnly}
                onEnableBasemap={basemapMode === "off" ? () => setBasemapMode("online") : undefined}
                onLocate={place => { setRouteFocus(null); setLocatedPlace(place); setDrawMode(null); }}
                onUseCenter={place => {
                  updateForm({ ...form, center: [place.longitude, place.latitude], sourceMode: "center-radius" });
                  setDrawMode(null);
                }} />
              <MapPanel
                route={form.gpxRoute && routePreviewMatches(form.gpxRoute, routePreview) ? { preview: routePreview, color: form.gpxRoute.color, widthMm: form.gpxRoute.lineWidthMm } : null}
                routeFocus={routeFocus}
                locatedPlace={locatedPlace}
                language={language}
                sourceMode={form.sourceMode}
                normalizedAoi={normalizedAoi}
                basemapStyle={basemapStyle}
                basemapEnabled={basemapEnabled}
                basemapCacheOnly={basemapCacheOnly}
                drawMode={drawMode}
                manifest={visibleJobMap ?? copiedSourceMap}
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
                    assembly={visibleJobAssembly}
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
          onReuse={jobId => void handleReuse(jobId)}
          reuseBusy={reuseBusy || submitting}
          collapsed={!resultsOpen}
          onPreview={() => setTab("preview")}
          language={language}
          jobs={jobs}
          selectedJob={selectedJob}
          maintenance={visibleMaintenance}
          loading={jobsLoading}
          maintenanceLoading={maintenanceLoading}
          maintenanceBusy={maintenanceBusy}
          batchPlan={batchPlan}
          jobTrash={jobTrash}
          batchBusy={batchBusy}
          onRefresh={() => void loadJobs()}
          onSelect={handleJobSelect}
          onCancel={handleCancel}
          onBackup={(jobId) => void handleBackup(jobId)}
          onCleanup={(jobId, workflowId, planId) =>
            void handleCleanup(jobId, workflowId, planId)
          }
          onRestore={(backupId) => void handleRestore(backupId)}
          onPlanBatch={(jobIds, mode) => void handlePlanBatch(jobIds, mode)}
          onApplyBatch={() => void handleApplyBatch()}
          onRestoreTrash={(batchId) => void handleRestoreTrash(batchId)}
          onPurgeTrash={(batchId) => void handlePurgeTrash(batchId)}
        />
      </div>

      <FileBrowser
        open={browserPurpose !== null}
        suffix={browserPurpose === "gpx" ? ".gpx" : undefined}
        language={language}
        onClose={() => setBrowserPurpose(null)}
        onSelect={(path) => {
          if (browserPurpose === "dem") {
            updateForm({ ...form, sourcePath: path });
          } else if (browserPurpose === "gpx" && form.gpxRoute) {
            updateForm({ ...form, gpxRoute: { ...form.gpxRoute, path } });
          } else if (browserPurpose === "overlay") {
            updateForm({ ...form, overlayConfigPath: path });
          }
        }}
      />
    </div>
  );
}
