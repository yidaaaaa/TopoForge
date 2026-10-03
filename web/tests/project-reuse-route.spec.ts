import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

const runtimeRoot = process.env.TOPOFORGE_PLAYWRIGHT_ROOT ?? join(tmpdir(), "topoforge-playwright-v0.11");
const demPath = join(runtimeRoot, "input", "topoforge-playwright-input.tif");
const routePath = join(runtimeRoot, "input", "topoforge-playwright-route.gpx");

interface Artifact {
  artifact_id: string;
  sha256: string | null;
  download_url: string | null;
}
interface Job {
  job_id: string;
  workspace_dir: string;
  state: string;
  error: unknown;
  artifacts: Artifact[];
}
interface RoutePreview {
  path: string;
  sha256: string;
  point_count: number;
  segment_count: number;
  bounds_wgs84: [number, number, number, number];
}

function hash(payload: Buffer): string {
  return createHash("sha256").update(payload).digest("hex");
}

function artifact(job: Job, role: string): Artifact & { download_url: string } {
  const result = job.artifacts.find(item => item.artifact_id === role);
  if (!result?.download_url) throw new Error(`Missing downloadable ${role} in ${job.job_id}`);
  return result as Artifact & { download_url: string };
}

async function downloadArtifact(page: Page, job: Job, role: string): Promise<Buffer> {
  const item = artifact(job, role);
  const response = await page.request.get(item.download_url);
  expect(response.ok()).toBe(true);
  const payload = await response.body();
  expect(hash(payload)).toBe(item.sha256);
  return payload;
}

async function completed(page: Page, jobId: string): Promise<Job> {
  let current: Job | null = null;
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/jobs/${jobId}`);
    expect(response.ok()).toBe(true);
    current = await response.json() as Job;
    return current.state;
  }, { timeout: 120_000, intervals: [250, 500, 1_000] }).toMatch(/^(completed|failed|cancelled)$/);
  const result = current!;
  expect(result.state, JSON.stringify(result.error)).toBe("completed");
  return result;
}

/** Independently measure the binary STL's vertex coordinates, not a form value. */
function stlWidthMm(payload: Buffer): number {
  const triangles = payload.readUInt32LE(80);
  expect(triangles).toBeGreaterThan(0);
  expect(payload.length).toBe(84 + triangles * 50);
  let minX = Infinity;
  let maxX = -Infinity;
  for (let triangle = 0; triangle < triangles; triangle += 1) {
    for (let vertex = 0; vertex < 3; vertex += 1) {
      const x = payload.readFloatLE(84 + triangle * 50 + 12 + vertex * 12);
      expect(Number.isFinite(x)).toBe(true);
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
    }
  }
  return maxX - minX;
}

async function magentaRoutePixels(page: Page, selector = ".maplibregl-canvas"): Promise<number> {
  return page.locator(selector).evaluate((canvas: HTMLCanvasElement) => {
    const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (!context) return 0;
    const pixels = new Uint8Array(canvas.width * canvas.height * 4);
    context.readPixels(0, 0, canvas.width, canvas.height, context.RGBA, context.UNSIGNED_BYTE, pixels);
    let routePixels = 0;
    // The chosen route color (#cc22aa) is absent from the offline basemap.
    for (let index = 0; index < pixels.length; index += 16) {
      if (pixels[index]! > 140 && pixels[index + 2]! > 110 && pixels[index + 1]! < pixels[index]! * 0.6 && pixels[index + 1]! < pixels[index + 2]! * 0.65) routePixels += 1;
    }
    return routePixels;
  });
}

test("copy a real project, edit its size and GPX route, and retain the original", async ({ page }, testInfo) => {
  test.slow();
  // Two complete manufacturing jobs plus route rendering/downloads: 180 s by default.
  // Preserve proportional command-line timeout overrides; existing tests keep their budgets.
  testInfo.setTimeout(testInfo.timeout * 2);
  const browserErrors: string[] = [];
  const submitted: unknown[] = [];
  page.on("pageerror", error => browserErrors.push(error.message));
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/api/v1/jobs")) {
      submitted.push(request.postDataJSON());
    }
  });
  const workspace = join(runtimeRoot, "workspaces", `reuse-route-${testInfo.project.name}-${Date.now()}`);
  const createdResponse = await page.request.post("/api/v1/jobs", { data: {
    launch: {
      workspace_dir: workspace,
      build: {
        dem_path: demPath, output_dir: workspace, model_width_mm: 40,
        base_thickness_mm: 3, max_height_mm: 20,
        sampling_mode: "source-preserving", max_grid_cells: 10_000,
        max_estimated_triangles: 50_000, resource_budget_mode: "strict",
        dataset_name: "Browser reuse synthetic DEM", data_license: "Apache-2.0 synthetic fixture",
        attribution: "TopoForge browser acceptance", output_formats: ["stl", "3mf", "glb"],
      },
      maximum_tile_width_mm: 180, maximum_tile_depth_mm: 180,
      slicing_enabled: false, slice_timeout_seconds: 1234,
    },
  } });
  expect(createdResponse.status()).toBe(201);
  const original = await completed(page, (await createdResponse.json() as Job).job_id);
  const originalModel = await downloadArtifact(page, original, "model_3mf");
  const originalRequest = await downloadArtifact(page, original, "workflow_request");
  expect(stlWidthMm(await downloadArtifact(page, original, "model_stl"))).toBeCloseTo(40, 4);

  await page.addInitScript(() => {
    localStorage.setItem("topoforge-language", "zh-CN");
    localStorage.setItem("topoforge-basemap-mode", "off");
  });
  await page.goto("/");
  const originalRow = page.locator(".job-row-main").filter({ hasText: basename(workspace) });
  await expect(originalRow).toBeVisible();
  if (await originalRow.getAttribute("aria-pressed") !== "true") await originalRow.click();
  await page.getByRole("button", { name: "复制并编辑", exact: true }).click();
  await expect(page.getByText("正在编辑项目副本", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /DEM 文件/ })).toHaveValue(demPath);
  const width = page.getByRole("spinbutton", { name: "宽度（毫米）", exact: true });
  await expect(width).toHaveValue("40");
  const copyName = await page.getByRole("textbox", { name: "工作区名称", exact: true }).inputValue();
  expect(copyName).not.toBe(basename(workspace));
  expect(copyName).toContain("-copy-");
  await width.fill("48");
  expect(submitted).toEqual([]);

  await page.getByRole("button", { name: "添加 GPX 路线", exact: true }).click();
  const editor = page.getByRole("region", { name: "路线", exact: true });
  await editor.getByRole("textbox", { name: /GPX 文件/ }).fill(routePath);
  const [previewResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith("/api/v1/overlays/gpx/preview") && response.request().method() === "POST"),
    editor.getByRole("button", { name: "载入路线", exact: true }).click(),
  ]);
  expect(previewResponse.ok()).toBe(true);
  const preview = await previewResponse.json() as RoutePreview;
  expect(preview.point_count).toBe(3);
  expect(preview.segment_count).toBe(1);
  expect(preview.sha256).toBe(hash(await readFile(routePath)));
  await expect(editor.getByText("1 段 · 3 个轨迹点", { exact: true })).toBeVisible();
  await editor.getByLabel("路线颜色", { exact: true }).fill("#cc22aa");
  await editor.getByRole("spinbutton", { name: "线宽 mm", exact: true }).fill("1.2");
  await editor.getByRole("spinbutton", { name: "凸起高度 mm", exact: true }).fill("0.6");
  await editor.getByRole("textbox", { name: "路线名称", exact: true }).fill("Synthetic browser route");
  await editor.getByRole("textbox", { name: "来源许可", exact: true }).fill("Apache-2.0 synthetic fixture");
  await editor.getByRole("textbox", { name: "作者 / 署名", exact: true }).fill("TopoForge browser acceptance");
  await expect(editor.getByText("路线已就绪，将随新模型一起生成。", { exact: true })).toBeVisible();
  await editor.getByRole("button", { name: "在地图上查看", exact: true }).click();
  const map = page.getByTestId("map-panel");
  await expect(map).toHaveAttribute("data-route-points", "3");
  await map.scrollIntoViewIfNeeded();
  const [west, south, east, north] = preview.bounds_wgs84;
  await expect.poll(() => page.evaluate(({ longitude, latitude }) => {
    const raw = localStorage.getItem("topoforge-reference-camera");
    if (!raw) return Infinity;
    const camera = JSON.parse(raw) as { center: [number, number]; zoom: number };
    return Math.max(Math.abs(camera.center[0] - longitude), Math.abs(camera.center[1] - latitude));
  }, { longitude: (west + east) / 2, latitude: (south + north) / 2 })).toBeLessThan(0.00001);
  await expect.poll(() => magentaRoutePixels(page), { timeout: 20_000 }).toBeGreaterThan(15);
  await expect(page.getByText("地图显示原项目的地形参考；修改后的模型在生成后查看。", { exact: true })).toBeVisible();
  // A real style diff used to clear GPX data without emitting style.load.
  // Wait for the changed terrain to load before asserting that the route survives it.
  const [elevationTile] = await Promise.all([
    page.waitForResponse(response => response.url().includes(`/api/v1/jobs/${original.job_id}/map/tiles/elevation/`) && response.status() === 200),
    page.getByRole("button", { name: "高程", exact: true }).click(),
  ]);
  expect(elevationTile.ok()).toBe(true);
  await expect.poll(() => magentaRoutePixels(page), { timeout: 20_000 }).toBeGreaterThan(15);
  await map.screenshot({ path: testInfo.outputPath("gpx-route-map.png") });
  expect(submitted).toEqual([]);

  const [newResponse] = await Promise.all([
    page.waitForResponse(response => response.url().endsWith("/api/v1/jobs") && response.request().method() === "POST"),
    page.getByRole("button", { name: "开始构建", exact: true }).click(),
  ]);
  expect(newResponse.status()).toBe(201);
  expect(submitted).toHaveLength(1);
  expect(submitted[0]).toMatchObject({
    reuse_source_job_id: original.job_id,
    launch: {
      build: {
        model_width_mm: 48, dem_path: demPath, sampling_mode: "source-preserving",
        dataset_name: "Browser reuse synthetic DEM", data_license: "Apache-2.0 synthetic fixture",
        attribution: "TopoForge browser acceptance",
      },
      slice_timeout_seconds: 1234,
      overlay: { sources: [{ path: preview.path, style: { color: "#cc22aa", line_width_mm: 1.2, raised_height_mm: 0.6 } }] },
    },
  });
  const copied = await completed(page, (await newResponse.json() as Job).job_id);
  expect(copied.job_id).not.toBe(original.job_id);
  expect(copied.workspace_dir).not.toBe(original.workspace_dir);
  expect(basename(copied.workspace_dir)).toBe(copyName);
  expect(stlWidthMm(await downloadArtifact(page, copied, "model_stl"))).toBeCloseTo(48, 4);
  const overlayValidation = JSON.parse((await downloadArtifact(page, copied, "overlay_validation")).toString("utf8"));
  expect(overlayValidation).toMatchObject({
    required_checks_passed: true, terrain_artifacts_unchanged: true,
    layer_geometry_checks_passed: true, format_reopen_checks_passed: true,
    combined_3mf_strict_warning_count: 0,
    source_records: [{ sha256: preview.sha256, license: "Apache-2.0 synthetic fixture", attribution: "TopoForge browser acceptance" }],
    layers: [{ color: "#cc22aa", watertight: true, winding_consistent: true, positive_volume: true }],
  });
  expect(overlayValidation.combined_3mf_object_count).toBeGreaterThanOrEqual(2);
  const overlayLink = page.getByRole("link", { name: /^含覆盖物的 3MF 模型/ });
  await expect(overlayLink).toHaveAttribute("href", artifact(copied, "overlay_model_3mf").download_url);
  const [download] = await Promise.all([page.waitForEvent("download"), overlayLink.click()]);
  expect(await download.failure()).toBeNull();
  expect(download.suggestedFilename()).toBe("model-with-overlays.3mf");
  const downloadedPath = await download.path();
  if (!downloadedPath) throw new Error("Browser did not retain the downloaded overlay 3MF");
  const downloaded = await readFile(downloadedPath);
  expect(hash(downloaded)).toBe(artifact(copied, "overlay_model_3mf").sha256);
  expect(downloaded.subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
  expect(downloaded.includes(Buffer.from("3D/3dmodel.model"))).toBe(true);
  expect(downloaded.includes(Buffer.from("[Content_Types].xml"))).toBe(true);

  await page.getByRole("button", { name: "查看三维模型", exact: true }).click();
  const model = page.getByTestId("terrain-preview");
  await expect(model).toHaveAttribute("data-model-loaded", "true");
  await model.scrollIntoViewIfNeeded();
  await expect.poll(() => magentaRoutePixels(page, '[data-testid="terrain-preview"] canvas'), { timeout: 20_000 }).toBeGreaterThan(15);
  await model.screenshot({ path: testInfo.outputPath("gpx-route-model.png") });

  const originalAfter = await completed(page, original.job_id);
  expect(originalAfter.workspace_dir).toBe(original.workspace_dir);
  expect(await downloadArtifact(page, originalAfter, "model_3mf")).toEqual(originalModel);
  expect(await downloadArtifact(page, originalAfter, "workflow_request")).toEqual(originalRequest);
  const dimensions = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
  expect(browserErrors).toEqual([]);
});
