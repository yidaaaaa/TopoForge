import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { JobCreateRequest } from "./types";
import { buildJobRequest, defaultFormState } from "./config";
import { defaultRouteEditorDraft } from "./routeEditor";
import { saveDraft } from "./workspaceStorage";

vi.mock("./components/MapPanel", () => ({ MapPanel: ({ route, manifest }: { manifest?: { job_id: string } | null; route?: { preview: { point_count: number }; color: string } }) => <div data-testid="route-map" data-job-id={manifest?.job_id ?? ""} data-points={route?.preview.point_count ?? 0} data-color={route?.color ?? ""} /> }));
vi.mock("./components/TerrainPreview", () => ({ TerrainPreview: () => <div /> }));
import App from "./App";
const health = { status: "ok", version: "0.10.3", loopback_only: true, languages: [] as [], workspace_root: "/data/workspaces", state_dir: "/data/state" };
const id = "a".repeat(32);
const original = { job_id: id, created_at: "2026-10-04T00:00:00Z", updated_at: "2026-10-04T00:00:00Z", state: "cancelled", workspace_dir: "/data/workspaces/original", expected_stages: [], ready_stages: [], progress_fraction: 0, current_stage: null, cancellation_requested: false, error: null, artifacts: [], summary: null };
const request = buildJobRequest({ ...defaultFormState, workspaceName: "original-copy", sourcePath: "/data/dem.tif" }, health);
Object.assign(request.launch.build as object, { vertical_datum: "unknown", nodata_max_fraction: 0.012, source_checksums: { "dem.tif": "b".repeat(64) } });
const project = { schema_version: "topoforge-project-reuse-v1", source_job_id: id, original_workspace_dir: original.workspace_dir, suggested_workspace_name: "original-copy", request, issues: [] };
const preview = { schema_version: "topoforge-gpx-preview-v1", path: "/data/route.gpx", filename: "route.gpx", source_crs: "EPSG:4326", size_bytes: 128, sha256: "c".repeat(64), point_count: 2, segment_count: 1, bounds_wgs84: [100, 30, 100.1, 30.1], geojson: { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[100, 30], [100.1, 30.1]] } }] } };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
let posts: JobCreateRequest[];
let pendingCopy: Promise<Response> | null;
let previewCalls: number;
let changeRoute: boolean;
let completedOriginal: boolean;
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("topoforge-language", "en");
  posts = []; pendingCopy = null; previewCalls = 0; changeRoute = false; completedOriginal = false;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/health")) return response(health);
    if (path.endsWith("/reuse")) return pendingCopy ?? response(project);
    if (path.endsWith("/jobs/validate")) return response({ valid: true });
    if (path.endsWith("/jobs") && init?.method === "POST") { posts.push(JSON.parse(String(init.body))); return response({ ...original, job_id: "d".repeat(32), state: "queued" }); }
    if (path.endsWith("/jobs")) return response([{ ...original, state: completedOriginal ? "completed" : "cancelled" }]);
    if (path.endsWith("/map/manifest")) return response({ job_id: id, cache_key: "original-map", bounds_wgs84: [100, 30, 101, 31] });
    if (path.endsWith("/assembly")) return response({ job_id: id, tiles: [] });
    if (path.endsWith("/maintenance")) return response({ detail: "No maintenance in this fixture" }, 404);
    if (path.endsWith("/gpx/preview")) { previewCalls++; return response({ ...preview, sha256: changeRoute && previewCalls > 1 ? "e".repeat(64) : preview.sha256 }); }
    if (path.endsWith("/trash")) return response([]);
    return response({});
  }));
});

it("copies only on request, restores editing after reload and retains hidden launch settings", async () => {
  const first = render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Copy and edit" }));
  await screen.findByText("Editing a project copy");
  expect(posts).toHaveLength(0);
  expect(screen.getByLabelText("Workspace name")).toHaveValue("original-copy");
  fireEvent.change(screen.getByLabelText("Width (mm)"), { target: { value: "140" } });
  first.unmount();
  render(<App />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Start build" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Start build" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].reuse_source_job_id).toBe(id);
  expect(posts[0].launch.workspace_dir).toBe("/data/workspaces/original-copy");
  expect(posts[0].launch.build).toMatchObject({ model_width_mm: 140, nodata_max_fraction: 0.012, source_checksums: { "dem.tif": "b".repeat(64) } });
}, 15_000);

it("does not let delayed copy overwrite edits made while loading", async () => {
  let release!: (value: Response) => void;
  pendingCopy = new Promise(resolve => { release = resolve; });
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Copy and edit" }));
  fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "keep-my-edit" } });
  await act(async () => release(response(project)));
  expect(screen.getByLabelText("Workspace name")).toHaveValue("keep-my-edit");
  expect(screen.queryByText("Editing a project copy")).not.toBeInTheDocument();
  expect(posts).toHaveLength(0);
});

it.each([false, true])("reviews a restored GPX and refuses changed source bytes (%s)", async changed => {
  changeRoute = changed;
  saveDraft({ ...defaultFormState, sourcePath: "/data/dem.tif", gpxRoute: { ...defaultRouteEditorDraft(), path: "/data/route.gpx", datasetName: "My route", license: "CC0", attribution: "Test fixture" } });
  render(<App />);
  expect(previewCalls).toBe(0);
  fireEvent.click(await screen.findByRole("button", { name: "Load route" }));
  await screen.findByText("route.gpx");
  fireEvent.change(screen.getByLabelText("Route color"), { target: { value: "#124578" } });
  fireEvent.change(screen.getByLabelText(/Line width/), { target: { value: "1.2" } });
  expect(screen.getByTestId("route-map")).toHaveAttribute("data-points", "2");
  fireEvent.click(screen.getByRole("button", { name: "Start build" }));
  if (changed) {
    await screen.findByText("The GPX file changed. Reload the route and review the preview.");
    expect(posts).toHaveLength(0);
    expect(screen.getByTestId("route-map")).toHaveAttribute("data-points", "0");
  } else {
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].launch.overlay).toMatchObject({ sources: [expect.objectContaining({ path: "/data/route.gpx", style: expect.objectContaining({ color: "#124578", line_width_mm: 1.2 }) })] });
  }
  expect(previewCalls).toBe(2);
}, 15_000);

it("keeps the original local terrain as a copy reference and clears it when the source changes", async () => {
  completedOriginal = true;
  render(<App />);
  await waitFor(() => expect(screen.getByTestId("route-map")).toHaveAttribute("data-job-id", id));
  fireEvent.click(screen.getByRole("button", { name: "Copy and edit" }));
  await screen.findByText("Editing a project copy");
  await waitFor(() => expect(screen.getByTestId("route-map")).toHaveAttribute("data-job-id", id));
  fireEvent.change(screen.getByRole("textbox", { name: /DEM file/ }), { target: { value: "/data/replacement.tif" } });
  await waitFor(() => expect(screen.getByTestId("route-map")).toHaveAttribute("data-job-id", ""));
  expect(posts).toHaveLength(0);
}, 15_000);
