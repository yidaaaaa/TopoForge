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

test("desktop bilingual map and 3D workspace is visible and nonblank", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop-only visual contract");
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

  await page.getByRole("tab", { name: "三维模型" }).click();
  const previewCanvas = page.locator(".preview-canvas canvas");
  await expect(previewCanvas).toBeVisible();
  const previewPixels = await nonBlankCanvas(page, ".preview-canvas canvas");
  expect(previewPixels.width).toBeGreaterThan(300);
  expect(previewPixels.height).toBeGreaterThan(300);
  expect(previewPixels.nonZero).toBeGreaterThan(0);

  await page.getByRole("button", { name: "EN" }).click();
  await expect(page.getByText("Local terrain manufacturing workspace")).toBeVisible();
  await expect(page.getByRole("button", { name: "Start build" })).toBeVisible();
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
