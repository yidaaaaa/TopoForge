import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { StyleSpecification } from "maplibre-gl";
import type { JobMapManifest, NormalizedAoi } from "../types";
import type { GpxRoutePreview } from "../routeEditor";
import { MapPanel } from "./MapPanel";

type Handler = (event?: unknown) => void;
const mock = vi.hoisted(() => ({ maps: [] as Array<{
  emit: (name: string, event?: unknown) => void;
  data: ReturnType<typeof vi.fn>;
  routeData: ReturnType<typeof vi.fn>;
  fitBounds: ReturnType<typeof vi.fn>;
  diffStyle: boolean;
  sourceReady: boolean;
}> }));
vi.mock("../api", () => ({ fetchStandardMap: () => Promise.resolve(null) }));
vi.mock("maplibre-gl", () => {
  class Map {
    handlers = new globalThis.Map<string, Set<Handler>>();
    data = vi.fn();
    routeData = vi.fn();
    sourceReady = true;
    diffStyle = false;
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
    getSource(name: string) { return this.sourceReady ? { setData: name === "gpx-route" ? this.routeData : this.data } : undefined; }
    getLayer() { return undefined; }
    getCanvas() { return this.canvas; }
    getCenter() { return { wrap: () => ({ lng: 120, lat: 30 }) }; }
    getZoom() { return 10; }
    isStyleLoaded() { return this.sourceReady; }
    setStyle(style: StyleSpecification) {
      if (this.diffStyle) {
        // A successful MapLibre style diff applies GeoJSON source data without
        // emitting style.load. Model that path instead of forcing full reloads.
        const aoi = style.sources.aoi, route = style.sources["gpx-route"];
        if (aoi.type === "geojson") this.data(aoi.data);
        if (route.type === "geojson") this.routeData(route.data);
      } else {
        this.sourceReady = false;
      }
    }
    fitBounds = vi.fn();
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

function focusedRoute(): GpxRoutePreview {
  return {
    schema_version: "topoforge-gpx-preview-v1", path: "/route.gpx", requestedPath: "/route.gpx", filename: "route.gpx",
    sha256: "a".repeat(64), size_bytes: 200, source_crs: "EPSG:4326",
    bounds_wgs84: [105.000414, 29.828842, 105.002691, 29.829565], point_count: 2, segment_count: 1,
    geojson: { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[105.000414, 29.828842], [105.002691, 29.829565]] } }] },
  };
}
function terrainManifest(cacheKey = "copied-source"): JobMapManifest {
  return {
    schema_version: "topoforge-web-map-v1", tilejson: "3.0.0", job_id: "original-project", source_sha256: "b".repeat(64), cache_key: cacheKey,
    bounds_wgs84: [105, 29.8284, 105.0033, 29.8306], center_wgs84: [105.00165, 29.8295], minzoom: 8, maxzoom: 13, tile_size: 256,
    tile_url_template: "/api/v1/jobs/original-project/map/tiles/{style}/{z}/{x}/{y}.png", styles: ["terrain", "elevation", "hillshade"], default_style: "terrain",
    elevation_min_m: 120, elevation_max_m: 360, layout_id: "layout", tile_grid_shape: [1, 1], tile_count: 1,
    tile_footprints_geojson: { type: "FeatureCollection", features: [] }, attribution: "TopoForge processed DEM",
    crosses_antimeridian: false, web_mercator_latitude_clipped: false, generator: "topoforge-map-tiles-v2", required_checks_passed: true,
  };
}

describe("map interaction lifecycle", () => {
  it("keeps the latest GPX style and removal across a basemap style load", () => {
    const input = props();
    const preview = { schema_version: "topoforge-gpx-preview-v1" as const, path: "/route.gpx", requestedPath: "/route.gpx", filename: "route.gpx", sha256: "a".repeat(64), size_bytes: 200, source_crs: "EPSG:4326" as const, bounds_wgs84: [100, 30, 101, 31] as [number, number, number, number], point_count: 2, segment_count: 1, geojson: { type: "FeatureCollection" as const, features: [{ type: "Feature" as const, properties: {}, geometry: { type: "LineString" as const, coordinates: [[100, 30], [101, 31]] } }] } };
    const route = { preview, color: "#123456", widthMm: 1 };
    const view = render(<MapPanel {...input} route={route} />);
    const dataCalls = mock.maps[0].routeData.mock.calls.length;
    view.rerender(<MapPanel {...input} route={{ ...route }} />);
    expect(mock.maps[0].routeData).toHaveBeenCalledTimes(dataCalls);
    view.rerender(<MapPanel {...input} route={route} basemapEnabled />);
    view.rerender(<MapPanel {...input} route={{ ...route, color: "#abcdef" }} basemapEnabled />);
    const map = mock.maps[0]; map.sourceReady = true;
    act(() => map.emit("style.load"));
    expect(map.routeData).toHaveBeenLastCalledWith(expect.objectContaining({ features: [expect.objectContaining({ properties: expect.objectContaining({ route_color: "#abcdef" }) })] }));
    view.rerender(<MapPanel {...input} route={null} />);
    map.sourceReady = true; act(() => map.emit("style.load"));
    expect(map.routeData).toHaveBeenLastCalledWith({ type: "FeatureCollection", features: [] });
  });

  it("retains route colors and the print area when terrain is added through a style diff with no load event", () => {
    const input = props(), preview = focusedRoute(), area = aoi(105);
    const route = { preview, color: "#cc22aa", widthMm: 1.2 };
    const view = render(<MapPanel {...input} route={route} normalizedAoi={area} />);
    const map = mock.maps[0];
    map.diffStyle = true;
    view.rerender(<MapPanel {...input} route={route} normalizedAoi={area} manifest={terrainManifest()} />);
    expect(map.routeData).toHaveBeenLastCalledWith(expect.objectContaining({ features: [expect.objectContaining({
      geometry: preview.geojson.features[0].geometry, properties: expect.objectContaining({ route_color: "#cc22aa", route_width: 4.4 }),
    })] }));
    expect(map.data).toHaveBeenLastCalledWith(expect.objectContaining({ features: [expect.objectContaining({ geometry: area.normalized_geometry_geojson })] }));
    view.rerender(<MapPanel {...input} route={null} normalizedAoi={null} manifest={terrainManifest()} language="en" />);
    expect(map.routeData).toHaveBeenLastCalledWith({ type: "FeatureCollection", features: [] });
    expect(map.data).toHaveBeenLastCalledWith({ type: "FeatureCollection", features: [] });
  });

  it("restores the latest route after a full reload even if parent polling replaces an identical manifest", () => {
    const input = props(), preview = focusedRoute(), manifest = terrainManifest();
    const view = render(<MapPanel {...input} route={{ preview, color: "#123456", widthMm: 1 }} />);
    const map = mock.maps[0];
    view.rerender(<MapPanel {...input} manifest={manifest} route={{ preview, color: "#123456", widthMm: 1 }} />);
    expect(map.sourceReady).toBe(false);
    view.rerender(<MapPanel {...input} manifest={{ ...manifest }} route={{ preview, color: "#cc22aa", widthMm: 1.2 }} />);
    map.sourceReady = true;
    act(() => map.emit("style.load"));
    expect(map.routeData).toHaveBeenLastCalledWith(expect.objectContaining({ features: [expect.objectContaining({ properties: expect.objectContaining({ route_color: "#cc22aa", route_width: 4.4 }) })] }));
  });

  it("fits a short local route closely without capping the camera at city-level zoom", () => {
    const route = focusedRoute();
    render(<MapPanel {...props()} routeFocus={route} />);
    expect(mock.maps[0].fitBounds).toHaveBeenLastCalledWith(
      [[route.bounds_wgs84[0], route.bounds_wgs84[1]], [route.bounds_wgs84[2], route.bounds_wgs84[3]]],
      { padding: 64, maxZoom: 20, duration: 500 },
    );
  });

  it("loads a late copied terrain source without moving away from the located route", () => {
    const input = props(), route = focusedRoute();
    const view = render(<MapPanel {...input} routeFocus={route} route={{ preview: route, color: "#cc22aa", widthMm: 1.2 }} />);
    const map = mock.maps[0], cameraCalls = map.fitBounds.mock.calls.length;
    view.rerender(<MapPanel {...input} routeFocus={route} route={{ preview: route, color: "#cc22aa", widthMm: 1.2 }} manifest={terrainManifest()} />);
    expect(map.fitBounds).toHaveBeenCalledTimes(cameraCalls);
    map.sourceReady = true;
    act(() => map.emit("style.load"));
    expect(map.routeData).toHaveBeenLastCalledWith(expect.objectContaining({ features: [expect.objectContaining({ properties: expect.objectContaining({ route_color: "#cc22aa" }) })] }));
  });

  it("gives an explicit route focus priority when the terrain and route arrive together", () => {
    const input = props(), route = focusedRoute();
    const view = render(<MapPanel {...input} />);
    view.rerender(<MapPanel {...input} routeFocus={route} manifest={terrainManifest()} normalizedAoi={aoi(105)} />);
    expect(mock.maps[0].fitBounds).toHaveBeenCalledTimes(1);
    expect(mock.maps[0].fitBounds).toHaveBeenLastCalledWith(
      [[route.bounds_wgs84[0], route.bounds_wgs84[1]], [route.bounds_wgs84[2], route.bounds_wgs84[3]]],
      expect.objectContaining({ maxZoom: 20 }),
    );
  });

  it("does not let delayed AOI normalization override route focus, and accepts a later explicit area change", () => {
    const input = props(), route = focusedRoute();
    const view = render(<MapPanel {...input} routeFocus={route} />);
    const map = mock.maps[0], cameraCalls = map.fitBounds.mock.calls.length;
    view.rerender(<MapPanel {...input} routeFocus={route} normalizedAoi={aoi(105)} />);
    expect(map.fitBounds).toHaveBeenCalledTimes(cameraCalls);
    // Changing the form's source/AOI clears routeFocus in App.
    view.rerender(<MapPanel {...input} routeFocus={null} normalizedAoi={aoi(120)} />);
    expect(map.fitBounds).toHaveBeenLastCalledWith([[120, 29], [121, 30]], expect.objectContaining({ maxZoom: 11 }));
  });

  it("frames a newly selected terrain normally after the previous route focus is cleared", () => {
    const input = props(), route = focusedRoute();
    const view = render(<MapPanel {...input} routeFocus={route} manifest={terrainManifest()} />);
    const next = { ...terrainManifest("next-source"), bounds_wgs84: [110, 20, 110.1, 20.1] as [number, number, number, number] };
    view.rerender(<MapPanel {...input} routeFocus={null} manifest={next} />);
    expect(mock.maps[0].fitBounds).toHaveBeenLastCalledWith([[110, 20], [110.1, 20.1]], expect.objectContaining({ maxZoom: 18 }));
  });

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
