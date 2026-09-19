import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PlaceSearch } from "./PlaceSearch";
import { coordinatePlace, readSavedPlaces, rememberPlace, SAVED_PLACES_KEY, SEARCH_SERVICE_KEY } from "./placeSearchState";
import type { PlaceCandidate, SavedPlace } from "../types";

const endpoint = "https://nominatim.openstreetmap.org";
const place: PlaceCandidate = { candidate_id: "1", display_name: "西湖, 杭州市", longitude: 120.13, latitude: 30.245, bounding_box_wgs84: [120.1, 30.2, 120.16, 30.29] };
const second = { ...place, candidate_id: "2", display_name: "西湖, 惠州市", longitude: 114.3, latitude: 23.1 };
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function result(candidates = [place, second]) {
  return { query: "西湖", candidates, endpoint, cache_status: "hit", attribution: "Geocoding © OpenStreetMap contributors" };
}
const config = { endpoint, is_public: true, policy_url: "https://operations.osmfoundation.org/policies/nominatim/", maximum_candidates: 10 };
let search: ReturnType<typeof vi.fn>;
function mount(cacheOnly = false) {
  const onLocate = vi.fn(), onUseCenter = vi.fn();
  const view = render(<PlaceSearch language="zh-CN" cacheOnly={cacheOnly} onLocate={onLocate} onUseCenter={onUseCenter} />);
  fireEvent.focus(screen.getByRole("textbox", { name: "查找地点" }));
  return { ...view, onLocate, onUseCenter };
}
async function ready() {
  await screen.findByText("搜索服务: nominatim.openstreetmap.org");
}
function submit(query = "西湖") {
  fireEvent.change(screen.getByRole("textbox", { name: "查找地点" }), { target: { value: query } });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
}

beforeEach(() => {
  localStorage.clear();
  search = vi.fn(async () => response(result()));
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    String(input).endsWith("/config") ? Promise.resolve(response(config)) : search(input, init)));
});

describe("explicit local place search", () => {
  it("does not autocomplete or use the public service before opt-in; locating is separate from print extent", async () => {
    const { onLocate, onUseCenter } = mount();
    await ready();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "西湖" } });
    expect(search).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "联网搜索" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await screen.findByRole("button", { name: "定位 西湖, 惠州市" });
    expect(JSON.parse(search.mock.calls[0][1].body)).toMatchObject({ cache_only: true, allow_public_service: false });
    expect(onLocate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "定位 西湖, 杭州市" }));
    expect(onLocate).toHaveBeenCalledWith(place);
    expect(onUseCenter).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "以此为打印中心" }));
    expect(onUseCenter).toHaveBeenCalledWith(place);
    expect(readSavedPlaces()[0].place).toEqual(place);
  });

  it("forces cache-only requests even with remembered online consent; coordinates and saved places make no search request", async () => {
    localStorage.setItem(SEARCH_SERVICE_KEY, endpoint);
    localStorage.setItem(SAVED_PLACES_KEY, JSON.stringify([{ place, endpoint, favorite: true }]));
    const { onLocate } = mount(true);
    await ready();
    expect(screen.getByRole("checkbox", { name: "联网搜索" })).toBeDisabled();
    submit();
    await screen.findByText("候选地点");
    expect(JSON.parse(search.mock.calls[0][1].body).cache_only).toBe(true);
    submit("101.9, 31.1");
    expect(onLocate).toHaveBeenLastCalledWith(expect.objectContaining({ longitude: 101.9, latitude: 31.1 }));
    expect(search).toHaveBeenCalledTimes(1);
    fireEvent.focus(screen.getByRole("textbox"));
    // Both the result and saved section contain this place.
    fireEvent.click(screen.getAllByRole("button", { name: "定位 西湖, 杭州市" }).at(-1)!);
    expect(onLocate).toHaveBeenLastCalledWith(place);
    expect(search).toHaveBeenCalledTimes(1);
  });

  it("saves favorites across remount and forgets consent when the configured service changes", async () => {
    localStorage.setItem(SEARCH_SERVICE_KEY, "https://different.example");
    const view = mount(); await ready();
    expect(screen.getByRole("checkbox", { name: "联网搜索" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "联网搜索" }));
    submit();
    await screen.findByRole("button", { name: "收藏 西湖, 杭州市" });
    expect(JSON.parse(search.mock.calls[0][1].body)).toMatchObject({ cache_only: false, allow_public_service: true });
    fireEvent.click(screen.getByRole("button", { name: "收藏 西湖, 杭州市" }));
    view.unmount(); mount(); await ready();
    expect(screen.getByRole("button", { name: "取消收藏 西湖, 杭州市" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "移除 西湖, 杭州市" }));
    expect(readSavedPlaces()).toEqual([]);
  });

  it("discards a delayed response after editing the query or switching to offline", async () => {
    const deferred: ((response: Response) => void)[] = [];
    search.mockImplementation(() => new Promise<Response>(resolve => deferred.push(resolve)));
    const view = mount(); await ready(); submit("杭州");
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
    const firstSignal = search.mock.calls[0][1].signal as AbortSignal;
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "成都" } });
    expect(firstSignal.aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(search).toHaveBeenCalledTimes(2));
    await act(async () => deferred[0](response(result())));
    expect(screen.queryByRole("button", { name: "定位 西湖, 杭州市" })).not.toBeInTheDocument();
    view.rerender(<PlaceSearch language="zh-CN" cacheOnly onLocate={view.onLocate} onUseCenter={view.onUseCenter} />);
    expect((search.mock.calls[1][1].signal as AbortSignal).aborted).toBe(true);
    await act(async () => deferred[1](response(result())));
    expect(screen.queryByText("候选地点")).not.toBeInTheDocument();
    expect(view.onLocate).not.toHaveBeenCalled();
  });

  it("reports cache misses and invalid coordinates without substituting a location", async () => {
    search.mockResolvedValue(response({ detail: { code: "search-cache-miss" } }, 409));
    const { onLocate } = mount(true); await ready(); submit("未缓存地点");
    await screen.findByText(/这个搜索尚未缓存/);
    submit("181, 20");
    expect(screen.getByText(/请输入有效的 WGS84/)).toBeInTheDocument();
    expect(search).toHaveBeenCalledTimes(1);
    expect(onLocate).not.toHaveBeenCalled();
  });
});

it("validates saved data and caps recent places without evicting favorites", () => {
  localStorage.setItem(SAVED_PLACES_KEY, JSON.stringify([{ place: { ...place, latitude: 999 }, endpoint, favorite: true }]));
  expect(readSavedPlaces()).toEqual([]);
  let saved: SavedPlace[] = [{ place, endpoint, favorite: true }];
  for (let n = 2; n < 40; n++) saved = rememberPlace(saved, { place: { ...place, candidate_id: String(n) }, endpoint, favorite: false });
  expect(saved).toHaveLength(21);
  expect(saved.find(item => item.place.candidate_id === "1")?.favorite).toBe(true);
  expect(coordinatePlace("-73.98，40.75")?.longitude).toBe(-73.98);
  expect(coordinatePlace("西湖")).toBeNull();
  expect(() => coordinatePlace("30, 120")).toThrow();
});
