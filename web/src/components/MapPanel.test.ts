import { describe, expect, it } from "vitest";

import type { JobMapManifest } from "../types";
import { mapStyle, rasterSourceBounds } from "./MapPanel";

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
    expect(style.sources.osm).toMatchObject({ type: "raster" });
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
