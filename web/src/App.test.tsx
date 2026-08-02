import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./components/MapPanel", () => ({
  MapPanel: () => <div data-testid="map-panel">map</div>,
}));
vi.mock("./components/TerrainPreview", () => ({
  TerrainPreview: () => <div data-testid="terrain-preview">preview</div>,
}));

import App from "./App";

const health = {
  status: "ok",
  version: "0.8.0",
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
    await waitFor(() => expect(screen.getByText("v0.8.0")).toBeInTheDocument());
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
});
