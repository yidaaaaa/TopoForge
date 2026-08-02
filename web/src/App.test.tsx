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
  version: "0.10.0",
  loopback_only: true,
  languages: ["zh-CN", "en"],
  workspace_root: "/tmp/workspaces",
  state_dir: "/tmp/state",
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
    await waitFor(() => expect(screen.getByText("v0.10.0")).toBeInTheDocument());
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

  it("removes terminal job records and optionally deletes project files", async () => {
    let jobs = [cancelledJob, failedJob];
    const deletionBodies: unknown[] = [];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path.endsWith("/api/v1/health")) {
          return response(health);
        }
        if (init?.method === "DELETE") {
          const jobId = path.split("/").at(-1)!;
          const body = JSON.parse(String(init.body)) as {
            confirm_job_id: string;
            delete_workspace: boolean;
          };
          deletionBodies.push(body);
          expect(body.confirm_job_id).toBe(jobId);
          jobs = jobs.filter((job) => job.job_id !== jobId);
          return response({
            schema_version: "topoforge-web-job-delete-v1",
            job_id: jobId,
            previous_state: jobId === cancelledJob.job_id ? "cancelled" : "failed",
            workspace:
              jobId === cancelledJob.job_id
                ? cancelledJob.workspace_dir
                : failedJob.workspace_dir,
            workspace_existed: true,
            workspace_removed: body.delete_workspace,
            workspace_retained: !body.delete_workspace,
            deleted_job_record_bytes: 2048,
            deleted_workspace_bytes: body.delete_workspace ? 8192 : 0,
            reclaimed_bytes: body.delete_workspace ? 10240 : 2048,
            backups_preserved: true,
            required_checks_passed: true,
          });
        }
        if (path.endsWith("/api/v1/jobs")) {
          return response(jobs);
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
    await waitFor(() =>
      expect(screen.getByText("任务记录已移除，项目文件已保留")).toBeInTheDocument(),
    );
    expect(confirm).toHaveBeenCalledWith(
      "仅从任务列表移除“cancelled-project”？工作区文件和备份会保留。",
    );
    expect(
      await screen.findByRole("heading", { name: "failed-project" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除项目文件" }));
    await waitFor(() =>
      expect(screen.getByText("项目文件和任务记录已删除，备份已保留")).toBeInTheDocument(),
    );
    expect(confirm).toHaveBeenLastCalledWith(
      "永久删除“failed-project”的任务记录和工作区文件？备份会保留。此操作不可撤销。",
    );
    expect(deletionBodies).toEqual([
      { confirm_job_id: cancelledJob.job_id, delete_workspace: false },
      { confirm_job_id: failedJob.job_id, delete_workspace: true },
    ]);
    expect(screen.getByText("暂无任务")).toBeInTheDocument();
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
