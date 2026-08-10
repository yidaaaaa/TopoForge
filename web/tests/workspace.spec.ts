import { expect, test } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

const playwrightRuntimeRoot =
  process.env.TOPOFORGE_PLAYWRIGHT_ROOT ??
  join(tmpdir(), "topoforge-playwright-v0.11");
const playwrightWorkspaceRoot = join(playwrightRuntimeRoot, "workspaces");
const playwrightInput = join(
  playwrightRuntimeRoot, "input", "topoforge-playwright-input.tif",
);

async function nonBlankCanvas(page: import("@playwright/test").Page, selector: string) {
  return page.locator(selector).evaluate((canvas: HTMLCanvasElement) => {
    const context =
      canvas.getContext("webgl2", { preserveDrawingBuffer: true }) ??
      canvas.getContext("webgl", { preserveDrawingBuffer: true });
    if (!context) {
      return { nonZero: 0, width: canvas.width, height: canvas.height };
    }
    const pixels = new Uint8Array(4);
    context.readPixels(
      Math.floor(canvas.width / 2),
      Math.floor(canvas.height / 2),
      1,
      1,
      context.RGBA,
      context.UNSIGNED_BYTE,
      pixels,
    );
    return {
      nonZero: Array.from(pixels).filter((value) => value !== 0).length,
      width: canvas.width,
      height: canvas.height,
    };
  });
}

async function expectCanvasFitsContainer(
  page: import("@playwright/test").Page,
  selector: string,
) {
  const sizing = await page.locator(selector).evaluate((canvas: HTMLCanvasElement) => {
    const container = canvas.parentElement;
    return {
      canvasWidth: canvas.clientWidth,
      canvasHeight: canvas.clientHeight,
      containerWidth: container?.clientWidth ?? 0,
      containerHeight: container?.clientHeight ?? 0,
      bufferWidth: canvas.width,
      bufferHeight: canvas.height,
      devicePixelRatio: window.devicePixelRatio,
    };
  });
  expect(Math.abs(sizing.canvasWidth - sizing.containerWidth)).toBeLessThanOrEqual(
    1,
  );
  expect(
    Math.abs(sizing.canvasHeight - sizing.containerHeight),
  ).toBeLessThanOrEqual(1);
  expect(sizing.bufferWidth / sizing.canvasWidth).toBeCloseTo(
    sizing.devicePixelRatio,
    1,
  );
  expect(sizing.bufferHeight / sizing.canvasHeight).toBeCloseTo(
    sizing.devicePixelRatio,
    1,
  );
}

interface SampledTerrainFrame {
  colorful: number;
  centerX: number;
  centerY: number;
}

async function sampledTerrainFrame(
  page: import("@playwright/test").Page,
  selector: string,
): Promise<SampledTerrainFrame> {
  return page.locator(selector).evaluate((canvas: HTMLCanvasElement) => {
    const context =
      canvas.getContext("webgl2", { preserveDrawingBuffer: true }) ??
      canvas.getContext("webgl", { preserveDrawingBuffer: true });
    if (!context) {
      return { colorful: 0, centerX: 0, centerY: 0 };
    }
    let colorful = 0;
    let minX = 1;
    let minY = 1;
    let maxX = 0;
    let maxY = 0;
    const pixel = new Uint8Array(4);
    for (let y = 1; y < 24; y += 1) {
      for (let x = 1; x < 24; x += 1) {
        context.readPixels(
          Math.floor((canvas.width * x) / 24),
          Math.floor((canvas.height * y) / 24),
          1,
          1,
          context.RGBA,
          context.UNSIGNED_BYTE,
          pixel,
        );
        const spread =
          Math.max(pixel[0]!, pixel[1]!, pixel[2]!) -
          Math.min(pixel[0]!, pixel[1]!, pixel[2]!);
        if (spread > 18) {
          colorful += 1;
          minX = Math.min(minX, x / 24);
          minY = Math.min(minY, y / 24);
          maxX = Math.max(maxX, x / 24);
          maxY = Math.max(maxY, y / 24);
        }
      }
    }
    return {
      colorful,
      centerX: (minX + maxX) / 2,
      centerY: (minY + maxY) / 2,
    };
  });
}

async function sampledPalette(page: import("@playwright/test").Page, selector: string) {
  return page.locator(selector).evaluate((canvas: HTMLCanvasElement) => {
    const context =
      canvas.getContext("webgl2", { preserveDrawingBuffer: true }) ??
      canvas.getContext("webgl", { preserveDrawingBuffer: true });
    if (!context) return [];
    const colors = new Set<string>();
    const pixel = new Uint8Array(4);
    for (let y = 1; y < 12; y += 1) {
      for (let x = 1; x < 12; x += 1) {
        context.readPixels(
          Math.floor((canvas.width * x) / 12),
          Math.floor((canvas.height * y) / 12),
          1,
          1,
          context.RGBA,
          context.UNSIGNED_BYTE,
          pixel,
        );
        colors.add(Array.from(pixel).join(","));
      }
    }
    return [...colors];
  });
}


interface CompletedJob {
  job_id: string;
  workspace_dir: string;
  state: string;
  summary: { workflow_id: string } | null;
}

async function createCompletedLifecycleJob(
  page: import("@playwright/test").Page,
): Promise<CompletedJob> {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  const workspace = join(playwrightWorkspaceRoot, `phase12-${suffix}`);
  const response = await page.request.post("/api/v1/jobs", {
    data: {
      launch: {
        workspace_dir: workspace,
        build: {
          dem_path: playwrightInput,
          output_dir: workspace,
          model_width_mm: 64,
          model_depth_mm: null,
          base_thickness_mm: 3,
          max_height_mm: 20,
          sampling_mode: "source-preserving",
          max_grid_cells: 10000,
          max_estimated_triangles: 50000,
          max_estimated_memory_mb: 1024,
          resource_budget_mode: "strict",
          output_formats: ["stl", "3mf", "glb"],
        },
        maximum_tile_width_mm: 32,
        maximum_tile_depth_mm: 32,
        overlap_cells: 1,
        slicing_enabled: false,
        slicer_name: "bambu-studio",
        slicer_settings: [],
        slicer_filaments: [],
        slice_timeout_seconds: 1200,
        project_evidence_enabled: false,
        project_timeout_seconds: 1800,
      },
    },
  });
  expect(response.status()).toBe(201);
  const created = (await response.json()) as CompletedJob;
  await expect
    .poll(
      async () => {
        const current = await page.request.get(`/api/v1/jobs/${created.job_id}`);
        expect(current.ok()).toBe(true);
        return ((await current.json()) as CompletedJob).state;
      },
      { timeout: 120_000 },
    )
    .toBe("completed");
  const completedResponse = await page.request.get(
    `/api/v1/jobs/${created.job_id}`,
  );
  return (await completedResponse.json()) as CompletedJob;
}

test("desktop bilingual map and 3D workspace is visible and nonblank", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop-only visual contract");
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
  });
  let localTileRequests = 0;
  let elevationTileRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/map/tiles/")) localTileRequests += 1;
    if (request.url().includes("/map/tiles/elevation/")) {
      elevationTileRequests += 1;
    }
  });
  let osmTileRequests = 0;
  await page.route("https://tile.openstreetmap.org/**", async (route) => {
    osmTileRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        "base64",
      ),
    });
  });
  const completedJob = await createCompletedLifecycleJob(page);
  expect(completedJob.summary).not.toBeNull();
  const stale = join(
    completedJob.workspace_dir,
    "stages",
    "99-unused",
    "playwright-stale",
  );
  await mkdir(stale, { recursive: true });
  await writeFile(join(stale, "payload.bin"), "playwright-unreferenced-stage");
  await page.addInitScript(() => {
    window.localStorage.setItem("topoforge-language", "zh-CN");
  });
  await page.goto("/");
  await expect(page.getByText("本地地形制造工作台")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: basename(completedJob.workspace_dir) }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "开始构建" })).toBeVisible();
  const jobSearch = page.getByRole("searchbox", { name: "搜索任务" });
  const jobFilter = page.getByRole("combobox", { name: "筛选任务状态" });
  const visibleJobs = page.getByLabel("可见任务数");
  await expect(jobSearch).toBeVisible();
  await expect(jobFilter).toHaveValue("all");
  await jobSearch.fill(basename(completedJob.workspace_dir));
  await expect(visibleJobs).toHaveText(/1\/\d+/);
  await expect(
    page.getByRole("button", {
      name: new RegExp(basename(completedJob.workspace_dir)),
    }),
  ).toBeVisible();
  await jobSearch.fill("");
  await page.setViewportSize({ width: 1365, height: 758 });
  const controlPanel = page.locator(".control-panel");
  const verticalScale = page.getByRole("combobox", { name: "垂直缩放" });
  await expect(verticalScale).toHaveValue("auto-perceptual");
  await verticalScale.selectOption("custom");
  const verticalExaggeration = page.getByRole("spinbutton", {
    name: "垂直夸张系数",
  });
  await expect(verticalExaggeration).toBeVisible();
  await verticalExaggeration.fill("2.5");
  await expect(verticalExaggeration).toHaveValue("2.5");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await verticalScale.selectOption("auto-perceptual");
  await expect(verticalExaggeration).toBeHidden();
  const connectorTolerance = page.getByRole("combobox", {
    name: "连接器总间隙（毫米）",
  });
  await connectorTolerance.scrollIntoViewIfNeeded();
  await expect(connectorTolerance).toHaveValue("0.2");
  await expect(connectorTolerance.locator("option")).toHaveCount(6);
  await connectorTolerance.selectOption("0.4");
  const slicingToggle = page.getByText("执行软件切片", { exact: true });
  await slicingToggle.scrollIntoViewIfNeeded();
  const panelScrollBeforeSlicing = await controlPanel.evaluate(
    (element) => element.scrollTop,
  );
  await slicingToggle.click();
  await expect(page.getByRole("combobox", { name: "切片器" })).toHaveValue(
    "bambu-studio",
  );
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect
    .poll(() =>
      page.locator(".app-header").evaluate((element) => element.getBoundingClientRect().top),
    )
    .toBe(0);
  await expect
    .poll(() =>
      page.locator(".app-shell").evaluate((element) => element.getBoundingClientRect().top),
    )
    .toBe(0);
  expect(await controlPanel.evaluate((element) => element.scrollTop)).toBe(
    panelScrollBeforeSlicing,
  );
  await slicingToggle.click();
  const backupButton = page.getByRole("button", { name: "创建备份" });
  const cleanupButton = page.getByRole("button", { name: "清理旧阶段" });
  await expect(backupButton).toBeEnabled();
  await expect(cleanupButton).toBeEnabled();
  const [backupResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/jobs/${completedJob.job_id}/backup`) &&
        response.request().method() === "POST",
    ),
    backupButton.click(),
  ]);
  expect(backupResponse.ok()).toBe(true);
  const backup = (await backupResponse.json()) as {
    backup_id: string;
    workflow_id: string;
    archive_sha256: string;
    download_url: string;
  };
  expect(backup.workflow_id).toBe(completedJob.summary!.workflow_id);
  await expect(page.getByText("备份已校验")).toBeVisible();
  const backupsResponse = await page.request.get("/api/v1/backups");
  expect(backupsResponse.ok()).toBe(true);
  const backups = (await backupsResponse.json()) as Array<{
    backup_id: string;
    workflow_id: string;
    archive_sha256: string;
    download_url: string;
  }>;
  expect(backups.some((candidate) => candidate.backup_id === backup.backup_id)).toBe(
    true,
  );
  const backupRow = page.locator(".backup-row", {
    hasText: backup.backup_id.slice(0, 12),
  });
  await expect(backupRow).toBeVisible();
  const download = await page.request.get(backup.download_url);
  expect(download.ok()).toBe(true);
  expect(download.headers()["x-topoforge-backup-sha256"]).toBe(
    backup.archive_sha256,
  );
  page.once("dialog", (dialog) => dialog.accept());
  await cleanupButton.click();
  await expect(page.getByText("旧阶段已清理")).toBeVisible();
  await expect(cleanupButton).toBeDisabled();
  const [restoreResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/backups/${backup.backup_id}/restore`) &&
        response.request().method() === "POST",
    ),
    backupRow.getByRole("button", { name: "恢复副本" }).click(),
  ]);
  expect(restoreResponse.ok()).toBe(true);
  const restoredJob = (await restoreResponse.json()) as {
    job_id: string;
    workspace_dir: string;
  };
  await expect(page.getByText("备份已恢复为新任务")).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: `${basename(completedJob.workspace_dir)}-restored-${backup.backup_id.slice(0, 8)}`,
    }),
  ).toBeVisible();
  const mapCanvas = page.locator(".maplibregl-canvas");
  await expect(mapCanvas).toBeVisible();
  await expect.poll(async () => (await mapCanvas.boundingBox())?.width ?? 0).toBeGreaterThan(300);
  const mapPixels = await nonBlankCanvas(page, ".maplibregl-canvas");
  expect(mapPixels.width).toBeGreaterThan(300);
  expect(mapPixels.height).toBeGreaterThan(300);
  expect(mapPixels.nonZero).toBeGreaterThan(0);
  await expect(page.getByTestId("map-panel")).toHaveAttribute(
    "data-offline-reference",
    "natural-earth-countries-and-graticule",
  );
  await expect(page.getByTestId("map-panel")).toHaveAttribute(
    "data-has-terrain",
    "true",
  );
  await expect.poll(() => localTileRequests).toBeGreaterThan(0);
  await expect
    .poll(
      async () => (await sampledPalette(page, ".maplibregl-canvas")).length,
    )
    .toBeGreaterThan(5);

  await page.getByRole("button", { name: "高程", exact: true }).click();
  await expect(page.getByTestId("map-panel")).toHaveAttribute(
    "data-tile-style",
    "elevation",
  );
  await expect.poll(() => elevationTileRequests).toBeGreaterThan(0);

  await page.locator(".toolbar-toggle").click();
  await expect.poll(() => osmTileRequests).toBeGreaterThan(0);

  await page.setViewportSize({ width: 1365, height: 758 });
  await page.getByRole("tab", { name: "三维模型" }).click();
  const previewCanvas = page.locator(".preview-canvas canvas");
  await expect(previewCanvas).toBeVisible();
  await expect(page.getByTestId("terrain-preview")).toHaveAttribute(
    "data-model-loaded",
    "true",
  );
  await expectCanvasFitsContainer(page, ".preview-canvas canvas");
  const previewPixels = await nonBlankCanvas(page, ".preview-canvas canvas");
  expect(previewPixels.width).toBeGreaterThan(300);
  expect(previewPixels.height).toBeGreaterThan(300);
  expect(previewPixels.nonZero).toBeGreaterThan(0);
  expect((await sampledPalette(page, ".preview-canvas canvas")).length).toBeGreaterThan(8);
  await expect
    .poll(
      async () =>
        (await sampledTerrainFrame(page, ".preview-canvas canvas")).colorful,
    )
    .toBeGreaterThan(24);
  const terrainFrame = await sampledTerrainFrame(
    page,
    ".preview-canvas canvas",
  );
  expect(terrainFrame.centerX).toBeGreaterThan(0.35);
  expect(terrainFrame.centerX).toBeLessThan(0.65);
  expect(terrainFrame.centerY).toBeGreaterThan(0.35);
  expect(terrainFrame.centerY).toBeLessThan(0.65);
  const resetModelView = page.getByRole("button", { name: "重置三维视图" });
  await expect(resetModelView).toBeVisible();
  await resetModelView.click();

  await page.getByRole("tab", { name: "拼装" }).click();
  await expect(page.getByTestId("assembly-panel")).toBeVisible();
  await expect(page.getByTestId("assembly-diagram")).toBeVisible();
  await expect(page.getByText("4/4")).toBeVisible();

  await page.getByText("tile-r0001-c0001", { exact: true }).click();
  await expect(page.getByTestId("assembly-panel")).toHaveAttribute(
    "data-selected-tile",
    "tile-r0001-c0001",
  );
  const selectedTileRow = page.locator(".assembly-roster-row", {
    hasText: "tile-r0001-c0001",
  });
  await selectedTileRow.getByTitle("隐藏分块").click();
  await expect(selectedTileRow.getByRole("checkbox")).not.toBeChecked();
  await expect(page.getByText("3/4")).toBeVisible();

  await page.getByRole("button", { name: "三维拼装" }).click();
  const assemblyCanvas = page.locator(".assembly-3d-canvas canvas");
  await expect(assemblyCanvas).toBeVisible();
  await expectCanvasFitsContainer(page, ".assembly-3d-canvas canvas");
  await page.getByRole("slider", { name: "爆炸距离" }).fill("1");
  await expect(page.getByTestId("assembly-panel")).toHaveAttribute(
    "data-explosion",
    "1.00",
  );
  await page.setViewportSize({ width: 1180, height: 900 });
  await expect.poll(async () => (await assemblyCanvas.boundingBox())?.width ?? 0).toBeGreaterThan(250);
  const assemblyPixels = await nonBlankCanvas(
    page,
    ".assembly-3d-canvas canvas",
  );
  expect(assemblyPixels.nonZero).toBeGreaterThan(0);
  await expect
    .poll(
      async () =>
        (await sampledPalette(page, ".assembly-3d-canvas canvas")).length,
    )
    .toBeGreaterThan(8);

  const resetToken = await page.getByTestId("assembly-panel").getAttribute("data-reset-token");
  await page.getByRole("button", { name: "重置拼装视图" }).click();
  await expect(page.getByTestId("assembly-panel")).toHaveAttribute("data-explosion", "0.00");
  await expect(page.getByText("4/4")).toBeVisible();
  await expect
    .poll(async () => page.getByTestId("assembly-panel").getAttribute("data-reset-token"))
    .not.toBe(resetToken);
  expect((await sampledPalette(page, ".assembly-3d-canvas canvas")).length).toBeGreaterThan(8);

  await page.getByRole("button", { name: "EN" }).click();
  await expect(page.getByText("Local terrain manufacturing workspace")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start build" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Assembly" })).toBeVisible();

  const restoredWorkspaceName = basename(restoredJob.workspace_dir);
  const taskSearchEnglish = page.getByRole("searchbox", { name: "Search jobs" });
  await taskSearchEnglish.fill(restoredWorkspaceName);
  await expect(page.getByLabel("Visible jobs")).toHaveText(/1\/\d+/);
  await page
    .getByRole("checkbox", {
      name: new RegExp(`Select job for batch management: ${restoredWorkspaceName}`),
    })
    .check();
  await page
    .getByRole("combobox", { name: "Batch action" })
    .selectOption("quarantine-workspace");
  const [planResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/lifecycle/deletions/plan") &&
        response.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Review batch action" }).click(),
  ]);
  expect(planResponse.ok()).toBe(true);
  const plan = (await planResponse.json()) as {
    plan_id: string;
    selected_job_count: number;
    total_target_bytes: number;
    required_checks_passed: boolean;
  };
  expect(plan.selected_job_count).toBe(1);
  expect(plan.total_target_bytes).toBeGreaterThan(0);
  expect(plan.required_checks_passed).toBe(true);
  await expect(page.getByLabel("Batch action review")).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  const [applyResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/lifecycle/deletions/apply") &&
        response.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Apply reviewed action" }).click(),
  ]);
  expect(applyResponse.ok()).toBe(true);
  const trashed = (await applyResponse.json()) as {
    batch_id: string;
    job_ids: string[];
    backups_preserved: boolean;
    required_checks_passed: boolean;
  };
  expect(trashed.job_ids).toEqual([restoredJob.job_id]);
  expect(trashed.backups_preserved).toBe(true);
  expect(trashed.required_checks_passed).toBe(true);
  await expect(page.getByText("Selected jobs moved to trash")).toBeVisible();
  expect((await page.request.get(`/api/v1/jobs/${restoredJob.job_id}`)).status()).toBe(404);
  await taskSearchEnglish.fill("");
  await expect(page.getByText(new RegExp(`Batch ${trashed.batch_id.slice(0, 12)}`))).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  const [trashRestoreResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith(
          `/api/v1/lifecycle/trash/${trashed.batch_id}/restore`,
        ) && response.request().method() === "POST",
    ),
    page.getByRole("button", { name: "Restore batch" }).click(),
  ]);
  expect(trashRestoreResponse.ok()).toBe(true);
  await expect(page.getByText("Trash batch restored")).toBeVisible();
  expect((await page.request.get(`/api/v1/jobs/${restoredJob.job_id}`)).ok()).toBe(true);

  const repeatPlan = await page.request.post("/api/v1/lifecycle/deletions/plan", {
    data: {
      job_ids: [restoredJob.job_id],
      mode: "quarantine-workspace",
    },
  });
  expect(repeatPlan.ok()).toBe(true);
  const repeatPlanPayload = (await repeatPlan.json()) as { plan_id: string };
  const repeatApply = await page.request.post("/api/v1/lifecycle/deletions/apply", {
    data: {
      job_ids: [restoredJob.job_id],
      mode: "quarantine-workspace",
      confirm_plan_id: repeatPlanPayload.plan_id,
    },
  });
  expect(repeatApply.status()).toBe(201);
  const repeatTrash = (await repeatApply.json()) as { batch_id: string };
  const purge = await page.request.delete(
    `/api/v1/lifecycle/trash/${repeatTrash.batch_id}`,
    { data: { confirm_batch_id: repeatTrash.batch_id } },
  );
  expect(purge.ok()).toBe(true);
  expect((await purge.json()).action).toBe("purged");
  expect((await page.request.get(`/api/v1/jobs/${restoredJob.job_id}`)).status()).toBe(404);
  expect((await page.request.get(backup.download_url)).ok()).toBe(true);
  expect((await page.request.get("/api/v1/jobs")).ok()).toBe(true);
  expect(restoredJob.workspace_dir).toContain("-restored-");
  expect(browserErrors).toEqual([]);
});

test("mobile layout has no horizontal overflow or overlapping primary controls", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile-only layout contract");
  await page.addInitScript(() => {
    window.localStorage.setItem("topoforge-language", "en");
  });
  await page.goto("/");
  await expect(page.getByText("Local terrain manufacturing workspace")).toBeVisible();
  const dimensions = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
  expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.clientHeight);
  await expect(page.getByRole("tab", { name: "Map" })).toBeVisible();
  const startBuild = page.getByRole("button", { name: "Start build" });
  await expect(startBuild).toBeVisible();
  expect(
    await startBuild.locator("..").evaluate((element) => getComputedStyle(element).position),
  ).toBe("static");
  const startBox = await startBuild.boundingBox();
  const heightLabelBox = await page
    .getByText("Maximum height (mm)", { exact: true })
    .boundingBox();
  expect(startBox).not.toBeNull();
  expect(heightLabelBox).not.toBeNull();
  expect(
    startBox!.y >= heightLabelBox!.y + heightLabelBox!.height ||
      heightLabelBox!.y >= startBox!.y + startBox!.height,
  ).toBe(true);
});
