import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

// An explicitly synthetic terrain response tests rendering without public service access.
test("terrain reference renders before a job and preserves offline style selection", async ({ page }, testInfo) => {
  const image = await readFile(new URL("./fixtures/synthetic-terrarium.png", import.meta.url));
  const errors: string[] = [], terrainRequests: string[] = [];
  let failure = false;
  page.on("pageerror", error => errors.push(String(error)));
  await page.route("https://**/*", route => route.abort());
  await page.route("**/api/v1/reference/tiles/**", route => route.abort());
  await page.route("**/api/v1/reference/terrain/**", route => {
    terrainRequests.push(route.request().url());
    return failure ? route.fulfill({ status: 409, json: { detail: "fixture cache miss" } }) :
      route.fulfill({ status: 200, contentType: "image/png", body: image });
  });
  await page.addInitScript(() => {
    localStorage.setItem("topoforge-language", "zh-CN");
    if (!localStorage.getItem("topoforge-basemap-mode")) localStorage.setItem("topoforge-basemap-mode", "online");
    localStorage.setItem("topoforge-reference-camera", JSON.stringify({ center: [102.9, 31.1], zoom: 9 }));
  });
  await page.goto("/");
  await expect(page.getByRole("combobox", { name: "底图样式" })).toHaveValue("standard");
  expect(terrainRequests).toHaveLength(0);
  await page.getByRole("combobox", { name: "底图样式" }).selectOption("terrain");
  await expect(page.getByTestId("map-panel")).toHaveAttribute("data-basemap-style", "terrain");
  await expect.poll(() => terrainRequests.length).toBeGreaterThan(0);
  await expect(page.getByTestId("map-panel")).toHaveAttribute("data-terrain-loading", "false");
  await expect(page.getByTestId("map-panel")).toHaveAttribute("data-terrain-error", "false");
  await expect.poll(async () => page.locator(".maplibregl-canvas").evaluate((source: HTMLCanvasElement) => {
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 48;
    const ctx = canvas.getContext("2d")!; ctx.drawImage(source, 0, 0, 48, 48);
    const data = ctx.getImageData(0, 0, 48, 48).data;
    const colors = new Set<string>();
    for (let i = 0; i < data.length; i += 4) colors.add(`${data[i]},${data[i + 1]},${data[i + 2]}`);
    return colors.size;
  })).toBeGreaterThan(50);
  await expect(page.getByRole("button", { name: "本地 DEM", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: testInfo.outputPath("synthetic-terrain.png"), fullPage: true });
  await page.getByRole("checkbox", { name: "仅使用本地缓存", exact: true }).locator("..").click();
  const beforeReload = terrainRequests.length;
  await page.reload();
  await expect(page.getByRole("combobox", { name: "底图样式" })).toHaveValue("terrain");
  await expect.poll(() => terrainRequests.length).toBeGreaterThan(beforeReload);
  expect(terrainRequests.slice(beforeReload).every(url => url.endsWith("?cache_only=true"))).toBe(true);
  await page.getByRole("combobox", { name: "底图样式" }).selectOption("standard");
  failure = true;
  await page.getByRole("combobox", { name: "底图样式" }).selectOption("terrain");
  await expect(page.getByText("当前区域的地形尚未缓存。请先联网切换到地形地图浏览，再离线使用。")).toBeVisible();
  await page.getByRole("button", { name: "EN", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Basemap style" })).toHaveValue("terrain");
  await expect(page.getByText("Terrain is not cached here. View this area online in terrain mode before using it offline.")).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const notice = document.querySelector(".map-data-status")!.getBoundingClientRect();
    const credits = document.querySelector(".maplibregl-ctrl-attrib")!.getBoundingClientRect();
    return credits.top - notice.bottom;
  })).toBeGreaterThanOrEqual(8);
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1);
  expect(errors).toEqual([]);
});
