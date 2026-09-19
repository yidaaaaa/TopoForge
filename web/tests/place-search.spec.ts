import { expect, test } from "@playwright/test";

// Deterministic UI tests never contact the public geocoder or tile servers.
test("place selection, print center and saved/offline locations", async ({ page }, testInfo) => {
  const errors: string[] = [];
  const searches: { query: string; cache_only: boolean; allow_public_service: boolean }[] = [];
  const place = { candidate_id: "fixture-west-lake", display_name: "西湖, 杭州市（测试地点）", longitude: 120.13, latitude: 30.245, bounding_box_wgs84: [120.1, 30.2, 120.16, 30.29] };
  page.on("pageerror", error => errors.push(String(error)));
  await page.route("https://**/*", route => route.abort());
  await page.route("**/api/v1/reference/tiles/**", route => route.abort());
  await page.route("**/api/v1/places/search", async route => {
    searches.push(route.request().postDataJSON());
    await route.fulfill({ json: { query: "西湖", candidates: [place, { ...place, candidate_id: "fixture-huizhou", display_name: "西湖, 惠州市（测试地点）" }], cache_status: "hit", attribution: "Geocoding © OpenStreetMap contributors", endpoint: "https://nominatim.openstreetmap.org" } });
  });
  await page.addInitScript(() => {
    localStorage.setItem("topoforge-language", "zh-CN");
    localStorage.setItem("topoforge-basemap-mode", "off");
  });
  await page.goto("/");
  const input = page.getByRole("textbox", { name: "查找地点" });
  await input.fill("西湖");
  await expect(page.getByText("搜索服务: nominatim.openstreetmap.org")).toBeVisible();
  expect(searches).toHaveLength(0);
  await expect(page.getByRole("checkbox", { name: "联网搜索" })).not.toBeChecked();
  await page.getByRole("checkbox", { name: "联网搜索" }).check();
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.getByRole("button", { name: "定位 西湖, 惠州市（测试地点）", exact: true })).toBeVisible();
  expect(searches[0]).toMatchObject({ query: "西湖", cache_only: false, allow_public_service: true });
  await page.getByRole("button", { name: "收藏 西湖, 杭州市（测试地点）", exact: true }).click();
  await page.getByRole("button", { name: "定位 西湖, 杭州市（测试地点）", exact: true }).first().click();
  await expect(page.getByTestId("map-panel")).toHaveAttribute("data-place-center", "120.13,30.245");
  await expect(page.getByRole("button", { name: "本地 DEM", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".place-map-pin")).toBeVisible();
  await expect.poll(async () => page.locator(".maplibregl-canvas").evaluate((canvas: HTMLCanvasElement) => {
    const box = canvas.closest(".maplibregl-map")!.getBoundingClientRect();
    return Math.abs(canvas.clientHeight - box.height);
  })).toBeLessThanOrEqual(1);
  await page.getByRole("button", { name: "以此为打印中心" }).click();
  await expect(page.getByRole("spinbutton", { name: "经度", exact: true })).toHaveValue("120.13");
  await expect(page.getByRole("spinbutton", { name: "纬度", exact: true })).toHaveValue("30.245");
  await expect(page.getByRole("spinbutton", { name: "半径（米）" })).toHaveValue("10000");
  await page.reload();
  await input.click();
  await expect(page.getByRole("button", { name: "取消收藏 西湖, 杭州市（测试地点）", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "定位 西湖, 杭州市（测试地点）", exact: true }).click();
  expect(searches).toHaveLength(1);
  await page.getByRole("button", { name: "显示道路与地名（联网）", exact: true }).click();
  await page.getByRole("checkbox", { name: "仅使用本地缓存", exact: true }).locator("..").click();
  await input.fill("本地缓存测试");
  await expect(page.getByRole("checkbox", { name: "联网搜索" })).toBeDisabled();
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect.poll(() => searches.length).toBe(2);
  expect(searches[1].cache_only).toBe(true);
  await input.fill("101.9, 31.1");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.getByTestId("map-panel")).toHaveAttribute("data-place-center", "101.9,31.1");
  expect(searches).toHaveLength(2);
  await page.getByRole("button", { name: "EN", exact: true }).click();
  await page.getByRole("textbox", { name: "Find a place" }).click();
  await expect(page.getByRole("checkbox", { name: "Search online" })).toBeDisabled();
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1);
  await page.screenshot({ path: testInfo.outputPath("place-search.png"), fullPage: true });
  expect(errors).toEqual([]);
});
