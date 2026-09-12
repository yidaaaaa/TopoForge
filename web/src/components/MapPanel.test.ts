import { describe, expect, it } from "vitest";

import type { JobMapManifest } from "../types";
import { mapStyle, rasterSourceBounds } from "./MapPanel";
import { savedReferenceCamera } from "./referenceMap";

const manifest: JobMapManifest = {
  schema_version: "topoforge-web-map-v1",
  tilejson: "3.0.0",
  job_id: "job-phase10",
  source_sha256: "a".repeat(64),
  cache_key: "b".repeat(64),
  bounds_wgs84: [105, 29.8, 105.01, 29.81],
  center_wgs84: [105.005, 29.805],
  minzoom: 8,
  maxzoom: 13,
  tile_size: 256,
  tile_url_template:
    "/api/v1/jobs/job-phase10/map/tiles/{style}/{z}/{x}/{y}.png",
  styles: ["terrain", "elevation", "hillshade"],
  default_style: "terrain",
  elevation_min_m: 1200,
  elevation_max_m: 3200,
  layout_id: "layout-phase10",
  tile_grid_shape: [1, 2],
  tile_count: 2,
  tile_footprints_geojson: {
    type: "FeatureCollection",
    features: [],
  },
  attribution: "TopoForge processed DEM",
  crosses_antimeridian: false,
  web_mercator_latitude_clipped: false,
  generator: "topoforge-map-tiles-v2",
  required_checks_passed: true,
};

describe("MapLibre local terrain style", () => {
  it("binds the selected deterministic XYZ style and manufacturing footprints", () => {
    const style = mapStyle(false, manifest, "hillshade");
    const terrain = style.sources["job-terrain"];
    expect(terrain).toMatchObject({
      type: "raster",
      tiles: [
        "/api/v1/jobs/job-phase10/map/tiles/hillshade/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      minzoom: 8,
      maxzoom: 13,
      bounds: manifest.bounds_wgs84,
    });
    expect(style.sources["manufacturing-tiles"]).toMatchObject({
      type: "geojson",
    });
    expect(style.layers.map((layer) => layer.id)).toEqual(
      expect.arrayContaining([
        "job-terrain",
        "manufacturing-tile-fill",
        "manufacturing-tile-line",
        "manufacturing-tile-selected",
      ]),
    );
  });

  it("keeps the optional OSM source separate from local terrain", () => {
    const style = mapStyle(true, manifest, "elevation");
    expect(style.sources.osm).toMatchObject({ type: "vector", tiles: [`${window.location.origin}/api/v1/reference/tiles/{z}/{x}/{y}.mvt`], maxzoom: 14 });
    expect(style.sources["job-terrain"]).toMatchObject({
      tiles: [
        "/api/v1/jobs/job-phase10/map/tiles/elevation/{z}/{x}/{y}.png",
      ],
    });
  });

  it("unwraps raster source bounds across the antimeridian", () => {
    expect(rasterSourceBounds([179.8, -16, -179.7, -15.5])).toEqual([
      179.8,
      -16,
      180.3,
      -15.5,
    ]);
  });
});


describe("reference map contents", () => {
  it.each([false, true])("never adds unfiltered political line tiles (online=%s)", (online) => {
    const style = mapStyle(online);
    const layers = style.layers.filter((layer) => "source-layer" in layer);
    expect(layers.every((layer) => !["boundaries"].includes(("source-layer" in layer ? layer["source-layer"] : "") ?? ""))).toBe(true);
    expect(style.sources).not.toHaveProperty("countries");
    expect(style.sources["reference-boundaries"]).toMatchObject({
      type: "geojson", data: expect.stringContaining(window.location.origin),
    });
    expect(style.layers.some((layer) => layer.id === "country-borders")).toBe(false);
    expect(style.glyphs).toBeUndefined();
    if (online) {
      expect(layers.map((layer) => "source-layer" in layer ? layer["source-layer"] : undefined)).toEqual(expect.arrayContaining(["streets", "street_labels", "place_labels"]));
    } else {
      expect(style.sources).not.toHaveProperty("osm");
    }
  });
  it("selects Chinese or English place names without changing the AOI layers", () => {
    const zh = mapStyle(true, null, "terrain", "zh-CN");
    const en = mapStyle(true, null, "terrain", "en");
    expect(JSON.stringify(zh.layers.find((layer) => layer.id === "osm-places"))).toContain("name_zh");
    expect(JSON.stringify(en.layers.find((layer) => layer.id === "osm-places"))).toContain("name_en");
    expect(zh.layers.filter((layer) => layer.id.startsWith("aoi-"))).toEqual(en.layers.filter((layer) => layer.id.startsWith("aoi-")));
  });
});


it("routes cache-only maps through a separate absolute URL without changing layers", () => {
  const online = mapStyle(true);
  const cached = mapStyle(true, null, "terrain", "zh-CN", true);
  expect(cached.sources.osm).toMatchObject({
    tiles: [`${window.location.origin}/api/v1/reference/tiles/{z}/{x}/{y}.mvt?cache_only=true`],
  });
  expect(cached.layers).toEqual(online.layers);
});


it("restores the last camera and rejects broken or out-of-range saved coordinates", () => {
  localStorage.setItem("topoforge-reference-camera", JSON.stringify({ center: [120.155, 30.25], zoom: 13 }));
  expect(savedReferenceCamera()).toEqual({ center: [120.155, 30.25], zoom: 13 });
  for (const value of ["broken", "null", JSON.stringify({ center: [120, 95], zoom: 13 }), JSON.stringify({ center: [120, 30], zoom: 99 })]) {
    localStorage.setItem("topoforge-reference-camera", value);
    expect(savedReferenceCamera()).toBeNull();
  }
  localStorage.removeItem("topoforge-reference-camera");
});
