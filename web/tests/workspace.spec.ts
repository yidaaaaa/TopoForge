import { expect, test } from "@playwright/test";

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
  await page.addInitScript(() => {
    window.localStorage.setItem("topoforge-language", "zh-CN");
  });
  await page.goto("/");
  await expect(page.getByText("本地地形制造工作台")).toBeVisible();
  await expect(page.getByRole("button", { name: "开始构建" })).toBeVisible();
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

  await page.getByRole("tab", { name: "三维模型" }).click();
  const previewCanvas = page.locator(".preview-canvas canvas");
  await expect(previewCanvas).toBeVisible();
  const previewPixels = await nonBlankCanvas(page, ".preview-canvas canvas");
  expect(previewPixels.width).toBeGreaterThan(300);
  expect(previewPixels.height).toBeGreaterThan(300);
  expect(previewPixels.nonZero).toBeGreaterThan(0);
  expect((await sampledPalette(page, ".preview-canvas canvas")).length).toBeGreaterThan(8);

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
