import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./components/MapPanel", () => ({
  MapPanel: ({
    selectedTileId,
    onSelectedTileChange,
  }: {
    selectedTileId: string | null;
    onSelectedTileChange: (tileId: string) => void;
  }) => (
    <div data-testid="map-panel" data-selected-tile={selectedTileId ?? ""}>
      map
      <button
        type="button"
        aria-label="select east map tile"
        onClick={() => onSelectedTileChange("tile-r0000-c0001")}
      />
    </div>
  ),
}));
vi.mock("./components/TerrainPreview", () => ({
  TerrainPreview: () => <div data-testid="terrain-preview">preview</div>,
}));

import App from "./App";

const health = {
  status: "ok",
  version: "0.10.2",
  loopback_only: true,
  languages: ["zh-CN", "en"],
  workspace_root: "/tmp/workspaces",
  state_dir: "/tmp/state",
};

const trashBatch = {
  schema_version: "topoforge-web-job-trash-v1",
  batch_id: "9".repeat(32),
  plan_id: "8".repeat(64),
  mode: "quarantine-workspace",
  created_at: "2026-08-03T00:00:00Z",
  purge_after: "2026-08-10T00:00:00Z",
  job_ids: ["b".repeat(32)],
  job_record_bytes: 2048,
  workspaces: [],
  backup_ids: [],
  total_quarantined_bytes: 10240,
  backups_preserved: true,
  required_checks_passed: true,
};

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const completedJob = {
  job_id: "job-phase10",
  created_at: "2026-08-02T00:00:00Z",
  updated_at: "2026-08-02T00:01:00Z",
  state: "completed",
  workspace_dir: "/tmp/workspaces/phase10",
  expected_stages: ["source", "build", "layout", "extract", "mesh", "connect"],
  progress_fraction: 1,
  current_stage: "connect",
  ready_stages: ["source", "build", "layout", "extract", "mesh", "connect"],
  pid: null,
  exit_code: 0,
  cancellation_requested: false,
  error: null,
  summary: {
    workflow_id: "workflow-phase10",
    source_mode: "local",
    final_stage: "connect",
    ready_stages: ["source", "build", "layout", "extract", "mesh", "connect"],
    metrics: {},
    artifacts: {},
    required_checks_passed: true,
  },
  artifacts: [],
};

const completedJobWithPrintArtifacts = {
  ...completedJob,
  artifacts: [
    {
      artifact_id: "bambu_project_3mf",
      relative_path: "stages/70-project/tile/model.bambu-p2s.3mf",
      filename: "model.bambu-p2s.3mf",
      kind: "file",
      media_type: "application/vnd.ms-3mfdocument",
      size_bytes: 4096,
      sha256: "1".repeat(64),
      download_url: "/api/v1/jobs/job-phase10/artifacts/bambu_project_3mf",
    },
    {
      artifact_id: "bambu_project_validation",
      relative_path: "stages/70-project/tile/project_validation.json",
      filename: "project_validation.json",
      kind: "file",
      media_type: "application/json",
      size_bytes: 1024,
      sha256: "2".repeat(64),
      download_url:
        "/api/v1/jobs/job-phase10/artifacts/bambu_project_validation",
    },
    {
      artifact_id: "model_3mf",
      relative_path: "stages/10-build/model.3mf",
      filename: "model.3mf",
      kind: "file",
      media_type: "application/vnd.ms-3mfdocument",
      size_bytes: 3072,
      sha256: "3".repeat(64),
      download_url: "/api/v1/jobs/job-phase10/artifacts/model_3mf",
    },
  ],
};

const cancelledJob = {
  ...completedJob,
  job_id: "a".repeat(32),
  state: "cancelled",
  workspace_dir: "/tmp/workspaces/cancelled-project",
  progress_fraction: 0.4,
  current_stage: "build",
  ready_stages: ["source"],
  exit_code: -15,
  cancellation_requested: true,
  summary: null,
};

const failedJob = {
  ...completedJob,
  job_id: "b".repeat(32),
  state: "failed",
  workspace_dir: "/tmp/workspaces/failed-project",
  progress_fraction: 0.2,
  current_stage: "source",
  ready_stages: [],
  exit_code: 2,
  error: {
    code: "workflow-execution-failed",
    message: "fixture failure",
    corrective_action: "review fixture",
    exception_type: "ConfigurationError",
  },
  summary: null,
};

const backupRecord = {
  backup_id: "e".repeat(64),
  workflow_id: "workflow-phase10",
  original_workspace: "/tmp/workspaces/phase10",
  archive_size_bytes: 4096,
  archive_sha256: "f".repeat(64),
  file_count: 24,
  download_url: "/api/v1/backups/" + "e".repeat(64),
  required_checks_passed: true,
};

const maintenanceOverview = {
  job_id: "job-phase10",
  storage: {
    workspace: "/tmp/workspaces/phase10",
    estimate_basis: "completed_workflow_measurements",
    current_workspace_bytes: 8192,
    estimated_peak_workspace_bytes: 8192,
    estimated_additional_bytes: 0,
    available_bytes: 1024 * 1024,
    estimated_headroom_bytes: 1024 * 1024,
    sufficient_for_estimate: true,
    cleanup_reclaimable_bytes: 2048,
    backup_input_bytes: 8192,
  },
  cleanup: {
    workflow_id: "workflow-phase10",
    workspace: "/tmp/workspaces/phase10",
    current_workspace_bytes: 8192,
    reclaimable_bytes: 2048,
    candidates: [
      {
        path: "stages/old",
        kind: "directory",
        size_bytes: 2048,
        reason: "unreferenced",
      },
    ],
    required_checks_passed: true,
  },
  backups: [],
  required_checks_passed: true,
};

const mapManifest = {
  schema_version: "topoforge-web-map-v1",
  tilejson: "3.0.0",
  job_id: "job-phase10",
  source_sha256: "a".repeat(64),
  cache_key: "b".repeat(64),
  bounds_wgs84: [105, 29.8, 105.01, 29.81],
  center_wgs84: [105.005, 29.805],
  minzoom: 8,
  maxzoom: 13,
  tile_size: 256,
  tile_url_template:
    "/api/v1/jobs/job-phase10/map/tiles/{style}/{z}/{x}/{y}.png",
  styles: ["terrain", "elevation", "hillshade"],
  default_style: "terrain",
  elevation_min_m: 1200,
  elevation_max_m: 3200,
  layout_id: "layout-phase10",
  tile_grid_shape: [1, 2],
  tile_count: 2,
  tile_footprints_geojson: { type: "FeatureCollection", features: [] },
  attribution: "TopoForge processed DEM",
  crosses_antimeridian: false,
  web_mercator_latitude_clipped: false,
  generator: "topoforge-map-tiles-v2",
  required_checks_passed: true,
};

const assembly = {
  schema_version: "topoforge-web-assembly-v1",
  job_id: "job-phase10",
  layout_id: "layout-phase10",
  model_size_mm: [64, 24],
  tile_grid_shape: [1, 2],
  tile_count: 2,
  seam_count: 1,
  connector_count: 0,
  row_origin: "north",
  column_origin: "west",
  east_axis: "+X",
  north_axis: "+Y",
  up_axis: "+Z",
  aggregate_glb_url: "/assembly.glb",
  connector_map_url: "/connector-map.png",
  tiles: [
    {
      tile_id: "tile-r0000-c0000",
      row: 0,
      column: 0,
      physical_bounds_mm: [0, 0, 32, 24],
      global_bounds_mm: [0, 0, 0, 32, 24, 20],
      triangle_count: 100,
      volume_mm3: 1000,
      male_connector_ids: [],
      female_connector_ids: [],
      glb_url: "/west.glb",
      glb_sha256: "c".repeat(64),
    },
    {
      tile_id: "tile-r0000-c0001",
      row: 0,
      column: 1,
      physical_bounds_mm: [32, 0, 64, 24],
      global_bounds_mm: [32, 0, 0, 64, 24, 18],
      triangle_count: 100,
      volume_mm3: 1000,
      male_connector_ids: [],
      female_connector_ids: [],
      glb_url: "/east.glb",
      glb_sha256: "d".repeat(64),
    },
  ],
  connectors: [],
  required_checks_passed: true,
};

describe("TopoForge bilingual workspace", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.localStorage.setItem("topoforge-language", "zh-CN");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (path.endsWith("/api/v1/jobs")) {
          return response([]);
        }
        if (path.endsWith("/api/v1/lifecycle/trash")) {
          return response([]);
        }
        if (path.startsWith("/api/v1/files")) {
          return response({ path: null, parent: null, roots: [], entries: [] });
        }
        return response({});
      }),
    );
  });

  it("renders the actual Chinese work surface and switches every primary command to English", async () => {
    render(<App />);
    expect(screen.getByText("本地地形制造工作台")).toBeInTheDocument();
    expect(screen.getByTestId("map-panel")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("v0.10.2")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "开始构建" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(screen.getByText("Local terrain manufacturing workspace")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start build" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });

  it("keeps source, sampling, map, preview, jobs, and artifacts visible as one workspace", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "数据源" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "采样" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "地图" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "三维模型" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "任务" })).toBeInTheDocument();
  });

  it("reveals the custom vertical exaggeration control in both languages", () => {
    render(<App />);
    const verticalScale = screen.getByRole("combobox", { name: "垂直缩放" });
    expect(verticalScale).toHaveValue("auto-perceptual");
    expect(
      screen.queryByRole("spinbutton", { name: "垂直夸张系数" }),
    ).not.toBeInTheDocument();

    fireEvent.change(verticalScale, { target: { value: "custom" } });
    const exaggeration = screen.getByRole("spinbutton", {
      name: "垂直夸张系数",
    });
    expect(exaggeration).toHaveValue(1);
    fireEvent.change(exaggeration, { target: { value: "2.5" } });
    expect(exaggeration).toHaveValue(2.5);

    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(
      screen.getByRole("combobox", { name: "Vertical scaling" }),
    ).toHaveValue("custom");
    expect(
      screen.getByRole("spinbutton", { name: "Vertical exaggeration" }),
    ).toHaveValue(2.5);
  });

  it("synchronizes selected manufacturing tiles between map and assembly", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/map/manifest")) {
          return response(mapManifest);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/assembly")) {
          return response(assembly);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/maintenance")) {
          return response(maintenanceOverview);
        }
        if (path.endsWith("/api/v1/jobs")) {
          return response([completedJob]);
        }
        if (path.endsWith("/api/v1/lifecycle/trash")) {
          return response([]);
        }
        return response({});
      }),
    );

    render(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("map-panel")).toHaveAttribute(
        "data-selected-tile",
        "tile-r0000-c0000",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "select east map tile" }));
    expect(screen.getByTestId("map-panel")).toHaveAttribute(
      "data-selected-tile",
      "tile-r0000-c0001",
    );

    fireEvent.click(screen.getByRole("tab", { name: "拼装" }));
    await waitFor(() =>
      expect(screen.getByTestId("assembly-panel")).toHaveAttribute(
        "data-selected-tile",
        "tile-r0000-c0001",
      ),
    );
    fireEvent.click(screen.getByText("R1 C1"));

    fireEvent.click(screen.getByRole("tab", { name: "地图" }));
    expect(screen.getByTestId("map-panel")).toHaveAttribute(
      "data-selected-tile",
      "tile-r0000-c0000",
    );
  });

  it("keeps a cleared job selection empty and distinguishes printable 3MF roles", async () => {
    let jobListRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/maintenance")) {
          return response(maintenanceOverview);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/map/manifest")) {
          return response(mapManifest);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/assembly")) {
          return response(assembly);
        }
        if (path.endsWith("/api/v1/jobs")) {
          jobListRequests += 1;
          return response([completedJobWithPrintArtifacts]);
        }
        if (path.endsWith("/api/v1/lifecycle/trash")) {
          return response([]);
        }
        return response({});
      }),
    );

    render(<App />);
    expect(
      await screen.findByText("Bambu Studio 工程 3MF（推荐打印）"),
    ).toBeInTheDocument();
    expect(screen.getByText("通用 3MF（仅几何）")).toBeInTheDocument();
    expect(screen.getByText("Bambu 工程验证报告")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(
      screen.getByText("Bambu Studio project 3MF (recommended for printing)"),
    ).toBeInTheDocument();
    expect(screen.getByText("Generic 3MF (geometry only)")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close job details" }));
    expect(screen.getByText("Select a job")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("map-panel")).toHaveAttribute(
        "data-selected-tile",
        "",
      ),
    );

    const requestCountBeforeRefresh = jobListRequests;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(jobListRequests).toBeGreaterThan(requestCountBeforeRefresh),
    );
    expect(screen.getByText("Select a job")).toBeInTheDocument();
    expect(
      screen.queryByText("Bambu Studio project 3MF (recommended for printing)"),
    ).not.toBeInTheDocument();
  });

  it("searches and filters retained jobs with bilingual result counts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/maintenance")) {
          return response(maintenanceOverview);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/map/manifest")) {
          return response(mapManifest);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/assembly")) {
          return response(assembly);
        }
        if (path.endsWith("/api/v1/jobs")) {
          return response([completedJob, cancelledJob, failedJob]);
        }
        if (path.endsWith("/api/v1/lifecycle/trash")) {
          return response([]);
        }
        return response({});
      }),
    );

    render(<App />);
    expect(await screen.findByLabelText("可见任务数")).toHaveTextContent("3/3");

    const search = screen.getByRole("searchbox", { name: "搜索任务" });
    fireEvent.change(search, { target: { value: "failed-project" } });
    expect(screen.getByLabelText("可见任务数")).toHaveTextContent("1/3");
    expect(screen.getByText("failed-project")).toBeInTheDocument();
    expect(screen.queryByText("cancelled-project")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "no-such-job" } });
    expect(screen.getByText("没有匹配的任务")).toBeInTheDocument();
    expect(screen.getByLabelText("可见任务数")).toHaveTextContent("0/3");

    fireEvent.change(search, { target: { value: "" } });
    const filter = screen.getByRole("combobox", { name: "筛选任务状态" });
    fireEvent.change(filter, { target: { value: "cancelled" } });
    expect(screen.getByLabelText("可见任务数")).toHaveTextContent("1/3");
    expect(screen.getByText("cancelled-project")).toBeInTheDocument();
    expect(screen.queryByText("failed-project")).not.toBeInTheDocument();

    fireEvent.change(filter, { target: { value: "all" } });
    const sort = screen.getByRole("combobox", { name: "任务排序" });
    fireEvent.change(sort, { target: { value: "name" } });
    const names = screen
      .getAllByRole("button")
      .filter((button) =>
        ["phase10", "cancelled-project", "failed-project"].some((name) =>
          button.textContent?.includes(name),
        ),
      )
      .map((button) => button.textContent);
    expect(names[0]).toContain("cancelled-project");

    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(screen.getByRole("searchbox", { name: "Search jobs" })).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Filter job status" }),
    ).toHaveValue("all");
    expect(screen.getByRole("combobox", { name: "Sort jobs" })).toHaveValue("name");
    expect(screen.getByLabelText("Visible jobs")).toHaveTextContent("3/3");
  });

  it("selects visible terminal jobs and submits one measured batch plan", async () => {
    const planBodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (path.endsWith("/api/v1/lifecycle/trash")) {
          return response([]);
        }
        if (path.endsWith("/api/v1/lifecycle/deletions/plan")) {
          const body = JSON.parse(String(init?.body));
          planBodies.push(body);
          return response({
            schema_version: "topoforge-web-job-batch-delete-plan-v1",
            plan_id: "7".repeat(64),
            mode: body.mode,
            job_ids: body.job_ids,
            items: [],
            selected_job_count: 3,
            eligible_job_count: 3,
            unique_workspace_count: 3,
            job_record_bytes: 6144,
            workspace_bytes: 24576,
            total_target_bytes: 30720,
            backup_job_ids: [],
            blockers: [],
            required_checks_passed: true,
          });
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/maintenance")) {
          return response(maintenanceOverview);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/map/manifest")) {
          return response(mapManifest);
        }
        if (path.endsWith("/api/v1/jobs/job-phase10/assembly")) {
          return response(assembly);
        }
        if (path.endsWith("/api/v1/jobs")) {
          return response([completedJob, cancelledJob, failedJob]);
        }
        return response({});
      }),
    );

    render(<App />);
    await screen.findByLabelText("可见任务数");
    fireEvent.click(screen.getByRole("button", { name: "选择当前终态任务" }));
    expect(screen.getByText(/已选任务:/)).toHaveTextContent("3");
    fireEvent.change(screen.getByRole("combobox", { name: "批量操作" }), {
      target: { value: "quarantine-workspace" },
    });
    fireEvent.click(screen.getByRole("button", { name: "预检批量操作" }));
    expect(await screen.findByLabelText("批量操作预检")).toBeInTheDocument();
    expect(screen.getByText("30.0 KiB")).toBeInTheDocument();
    expect(planBodies).toEqual([
      {
        job_ids: [completedJob.job_id, cancelledJob.job_id, failedJob.job_id],
        mode: "quarantine-workspace",
      },
    ]);
  });

  it("reviews terminal job batches and moves selected projects into recoverable trash", async () => {
    let jobs = [cancelledJob, failedJob];
    let trash: unknown[] = [];
    const planBodies: unknown[] = [];
    const applyBodies: unknown[] = [];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (path.endsWith("/api/v1/lifecycle/deletions/plan")) {
          const body = JSON.parse(String(init?.body)) as {
            job_ids: string[];
            mode: string;
          };
          planBodies.push(body);
          const jobId = body.job_ids[0];
          return response({
            schema_version: "topoforge-web-job-batch-delete-plan-v1",
            plan_id: body.mode === "record-only" ? "1".repeat(64) : "2".repeat(64),
            mode: body.mode,
            job_ids: body.job_ids,
            items: [],
            selected_job_count: 1,
            eligible_job_count: 1,
            unique_workspace_count: 1,
            job_record_bytes: 2048,
            workspace_bytes: body.mode === "record-only" ? 0 : 8192,
            total_target_bytes: body.mode === "record-only" ? 2048 : 10240,
            backup_job_ids: [],
            blockers: [],
            required_checks_passed: true,
            job_id: jobId,
          });
        }
        if (path.endsWith("/api/v1/lifecycle/deletions/apply")) {
          const body = JSON.parse(String(init?.body)) as {
            job_ids: string[];
            mode: string;
            confirm_plan_id: string;
          };
          applyBodies.push(body);
          jobs = jobs.filter((job) => !body.job_ids.includes(job.job_id));
          trash = [{ ...trashBatch, mode: body.mode, job_ids: body.job_ids }];
          return response(trash[0], 201);
        }
        if (path.endsWith("/api/v1/jobs")) {
          return response(jobs);
        }
        if (path.endsWith("/api/v1/lifecycle/trash")) {
          return response(trash);
        }
        return response({});
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    const removeRecord = await screen.findByRole("button", {
      name: "移除任务记录",
    });
    fireEvent.click(removeRecord);
    expect(await screen.findByLabelText("批量操作预检")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "执行已核对操作" }));
    await waitFor(() =>
      expect(screen.getByText("所选任务已移入回收站")).toBeInTheDocument(),
    );
    expect(confirm).toHaveBeenCalledWith(
      "执行已核对的批次 111111111111？所选记录将进入可恢复回收站。",
    );
    expect(
      await screen.findByRole("heading", { name: "failed-project" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "将项目移入回收站" }));
    expect(await screen.findByLabelText("批量操作预检")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "执行已核对操作" }));
    await waitFor(() =>
      expect(screen.getByText("所选任务已移入回收站")).toBeInTheDocument(),
    );
    expect(confirm).toHaveBeenLastCalledWith(
      "执行已核对的批次 222222222222？所选记录将进入可恢复回收站。",
    );
    expect(planBodies).toEqual([
      { job_ids: [cancelledJob.job_id], mode: "record-only" },
      { job_ids: [failedJob.job_id], mode: "quarantine-workspace" },
    ]);
    expect(applyBodies).toEqual([
      {
        job_ids: [cancelledJob.job_id],
        mode: "record-only",
        confirm_plan_id: "1".repeat(64),
      },
      {
        job_ids: [failedJob.job_id],
        mode: "quarantine-workspace",
        confirm_plan_id: "2".repeat(64),
      },
    ]);
    expect(screen.getByText("暂无任务")).toBeInTheDocument();
    expect(screen.getByText(/批次 999999999999/)).toBeInTheDocument();
  });

  it("creates backups, confirms cleanup, and restores a completed project", async () => {
    let backedUp = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/api/v1/health")) {
        return response(health);
      }
      if (path.endsWith("/api/v1/jobs/job-phase10/maintenance")) {
        return response({
          ...maintenanceOverview,
          backups: backedUp ? [backupRecord] : [],
        });
      }
      if (path.endsWith("/api/v1/jobs/job-phase10/backup")) {
        backedUp = true;
        return response(backupRecord, 201);
      }
      if (path.endsWith("/api/v1/jobs/job-phase10/cleanup")) {
        expect(init?.body).toBe(
          JSON.stringify({ confirm_workflow_id: "workflow-phase10" }),
        );
        return response({
          workflow_id: "workflow-phase10",
          workspace: "/tmp/workspaces/phase10",
          removed_paths: ["stages/old"],
          reclaimed_bytes: 2048,
          remaining_workspace_bytes: 6144,
          required_checks_passed: true,
        });
      }
      if (
        path.endsWith(
          "/api/v1/backups/" + backupRecord.backup_id + "/restore",
        )
      ) {
        return response(
          {
            ...completedJob,
            job_id: "job-restored",
            workspace_dir: "/tmp/workspaces/phase10-restored",
          },
          201,
        );
      }
      if (path.endsWith("/api/v1/jobs")) {
        return response([completedJob]);
      }
      if (path.endsWith("/api/v1/lifecycle/trash")) {
        return response([]);
      }
      if (path.endsWith("/api/v1/jobs/job-phase10/map/manifest")) {
        return response(mapManifest);
      }
      if (path.endsWith("/api/v1/jobs/job-phase10/assembly")) {
        return response(assembly);
      }
      return response({});
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    const backupButton = await screen.findByRole("button", { name: "创建备份" });
    fireEvent.click(backupButton);
    await waitFor(() => expect(screen.getByText("备份已校验")).toBeInTheDocument());
    expect(await screen.findByText(backupRecord.backup_id.slice(0, 12))).toBeInTheDocument();

    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "清理旧阶段" }));
    await waitFor(() => expect(screen.getByText("旧阶段已清理")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "恢复副本" }));
    await waitFor(() =>
      expect(screen.getByText("备份已恢复为新任务")).toBeInTheDocument(),
    );
    expect(
      fetchMock.mock.calls.some(([input]) =>
        String(input).endsWith(
          "/api/v1/backups/" + backupRecord.backup_id + "/restore",
        ),
      ),
    ).toBe(true);
  });
});
