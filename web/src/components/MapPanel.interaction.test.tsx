import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { NormalizedAoi } from "../types";
import { MapPanel } from "./MapPanel";

type Handler = (event?: unknown) => void;
const mock = vi.hoisted(() => ({ maps: [] as Array<{
  emit: (name: string, event?: unknown) => void;
  data: ReturnType<typeof vi.fn>;
  sourceReady: boolean;
}> }));
vi.mock("../api", () => ({ fetchStandardMap: () => Promise.resolve(null) }));
vi.mock("maplibre-gl", () => {
  class Map {
    handlers = new globalThis.Map<string, Set<Handler>>();
    data = vi.fn();
    sourceReady = true;
    canvas = document.createElement("canvas");
    dragPan = { enable: vi.fn(), disable: vi.fn() };
    constructor() { mock.maps.push(this); }
    on(name: string, handler: Handler) {
      if (!this.handlers.has(name)) this.handlers.set(name, new Set());
      this.handlers.get(name)!.add(handler);
    }
    off(name: string, handler: Handler) { this.handlers.get(name)?.delete(handler); }
    once(name: string, handler: Handler) {
      const wrapped: Handler = event => { this.off(name, wrapped); handler(event); };
      this.on(name, wrapped);
    }
    emit(name: string, event?: unknown) { for (const handler of [...(this.handlers.get(name) ?? [])]) handler(event); }
    addControl() {}
    getSource() { return this.sourceReady ? { setData: this.data } : undefined; }
    getLayer() { return undefined; }
    getCanvas() { return this.canvas; }
    getCenter() { return { wrap: () => ({ lng: 120, lat: 30 }) }; }
    getZoom() { return 10; }
    isStyleLoaded() { return this.sourceReady; }
    setStyle() { this.sourceReady = false; }
    fitBounds() {}
    remove() { this.handlers.clear(); }
  }
  return { default: { Map, NavigationControl: class {}, AttributionControl: class {} } };
});

function props(): ComponentProps<typeof MapPanel> {
  return { language: "zh-CN", sourceMode: "bbox", normalizedAoi: null, basemapEnabled: false,
    drawMode: null, manifest: null, selectedTileId: null, visualizationLoading: false,
    visualizationError: null, onSelectedTileChange: vi.fn(), onBboxChange: vi.fn(), onCenterChange: vi.fn() };
}
function aoi(west: number): NormalizedAoi {
  return { bounds_wgs84: [west, 29, west + 1, 30],
    normalized_geometry_geojson: { type: "Polygon", coordinates: [[[west,29],[west+1,29],[west+1,30],[west,30],[west,29]]] },
  } as NormalizedAoi;
}

describe("map interaction lifecycle", () => {
  beforeEach(() => { mock.maps.length = 0; localStorage.clear(); vi.restoreAllMocks(); });
  it("persists the camera once after repeated parent updates", () => {
    const input = props(); const view = render(<MapPanel {...input} />);
    for (let i = 0; i < 6; i++) view.rerender(<MapPanel {...input} onBboxChange={vi.fn()} onCenterChange={vi.fn()} />);
    const write = vi.spyOn(Storage.prototype, "setItem");
    act(() => mock.maps[0].emit("moveend"));
    expect(write).toHaveBeenCalledTimes(1);
  });
  it("normalizes coordinates selected in a repeated world and across the date line", () => {
    const input = props(); const view = render(<MapPanel {...input} drawMode="center" />);
    act(() => mock.maps[0].emit("click", { lngLat: { lng: 190, lat: 30 } }));
    expect(input.onCenterChange).toHaveBeenCalledWith([-170, 30]);
    view.rerender(<MapPanel {...input} drawMode="bbox" />);
    act(() => mock.maps[0].emit("mousedown", { lngLat: { lng: 179, lat: 29 } }));
    act(() => mock.maps[0].emit("mouseup", { lngLat: { lng: 181, lat: 30 } }));
    expect(input.onBboxChange).toHaveBeenCalledWith([179, 29, -179, 30]);
  });
  it("finishes a drag across parent polling updates using the latest callback", () => {
    const input = props(); const view = render(<MapPanel {...input} drawMode="bbox" />);
    act(() => mock.maps[0].emit("mousedown", { lngLat: { lng: 110, lat: 29 } }));
    const latest = vi.fn();
    view.rerender(<MapPanel {...input} drawMode="bbox" onBboxChange={latest} />);
    act(() => mock.maps[0].emit("mouseup", { lngLat: { lng: 111, lat: 30 } }));
    expect(latest).toHaveBeenCalledWith([110, 29, 111, 30]);
    expect(input.onBboxChange).not.toHaveBeenCalled();
  });
  it("keeps the newest print outline when inputs change during style loading", () => {
    const input = props(); const view = render(<MapPanel {...input} normalizedAoi={aoi(110)} />);
    view.rerender(<MapPanel {...input} basemapEnabled normalizedAoi={aoi(110)} />);
    view.rerender(<MapPanel {...input} basemapEnabled normalizedAoi={aoi(120)} />);
    const map = mock.maps[0]; map.sourceReady = true;
    act(() => map.emit("style.load"));
    expect(map.data).toHaveBeenLastCalledWith(expect.objectContaining({
      features: [expect.objectContaining({ geometry: aoi(120).normalized_geometry_geojson })],
    }));
  });
});
