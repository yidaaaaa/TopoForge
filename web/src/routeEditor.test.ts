import { describe, expect, it } from "vitest";

import { buildRouteOverlay, defaultRouteEditorDraft, isRouteEditorDraft, routeEditorIssue, routePreviewMatches, type GpxRoutePreview } from "./routeEditor";

const preview: GpxRoutePreview = {
  schema_version: "topoforge-gpx-preview-v1", requestedPath: "tracks/walk.gpx", path: "/data/tracks/walk.gpx", filename: "walk.gpx",
  sha256: "a".repeat(64), size_bytes: 320, source_crs: "EPSG:4326", bounds_wgs84: [100, 29, 100.01, 29.01], segment_count: 1, point_count: 2,
  geojson: { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[100, 29], [100.01, 29.01]] } }] },
};
const complete = () => ({ ...defaultRouteEditorDraft(), path: "tracks/walk.gpx", datasetName: "My walk", license: "Private use, by permission", attribution: "Track recorder" });

describe("route draft and overlay adaptation", () => {
  it("does not invent provenance or reuse a preview for another file", () => {
    const draft = defaultRouteEditorDraft();
    expect(draft.license).toBe("");
    expect(draft.attribution).toBe("");
    expect(routeEditorIssue(draft, null)).toBe("path");
    expect(buildRouteOverlay(draft, null)).toBeNull();
    expect(routeEditorIssue(complete(), null)).toBe("preview");
    expect(buildRouteOverlay({ ...complete(), path: "other.gpx" }, preview)).toBeNull();
    expect(routePreviewMatches(complete(), preview)).toBe(true);
  });

  it("passes resolved source path and measured-unit style to the existing engine, without changing its inputs", () => {
    const draft = { ...complete(), datasetName: "  Morning walk  ", color: "#00AA55", lineWidthMm: 1.2, raisedHeightMm: 0.7, embedDepthMm: 0.15 };
    const before = structuredClone(draft);
    expect(buildRouteOverlay(draft, preview)).toEqual({ sources: [{
      source_id: "gpx-route", kind: "gpx", format: "gpx", path: "/data/tracks/walk.gpx", source_crs: "EPSG:4326",
      dataset_name: "Morning walk", license: "Private use, by permission", attribution: "Track recorder",
      style: { color: "#00AA55", line_width_mm: 1.2, raised_height_mm: 0.7, embed_depth_mm: 0.15 },
    }] });
    expect(draft).toEqual(before);
    expect(preview.requestedPath).toBe("tracks/walk.gpx");
  });

  it("blocks incomplete attribution and invalid physical settings instead of silently substituting defaults", () => {
    for (const key of ["datasetName", "license", "attribution"] as const) {
      const draft = { ...complete(), [key]: "  " };
      expect(routeEditorIssue(draft, preview)).toBe("provenance");
      expect(buildRouteOverlay(draft, preview)).toBeNull();
    }
    for (const invalid of [{ color: "red" }, { lineWidthMm: 0 }, { raisedHeightMm: -1 }, { embedDepthMm: -0.1 }, { lineWidthMm: Number.POSITIVE_INFINITY }, { raisedHeightMm: Number.NaN }]) {
      expect(routeEditorIssue({ ...complete(), ...invalid }, preview)).toBe("style");
      expect(buildRouteOverlay({ ...complete(), ...invalid }, preview)).toBeNull();
    }
    expect(routeEditorIssue({ ...complete(), embedDepthMm: 0 }, preview)).toBeNull();
  });

  it("accepts an unfinished finite draft for saving, but rejects malformed persisted values", () => {
    expect(isRouteEditorDraft(defaultRouteEditorDraft())).toBe(true);
    expect(isRouteEditorDraft({ ...defaultRouteEditorDraft(), lineWidthMm: 0 })).toBe(true);
    for (const invalid of [null, [], {}, { ...defaultRouteEditorDraft(), color: 12 }, { ...defaultRouteEditorDraft(), lineWidthMm: "0.8" }, { ...defaultRouteEditorDraft(), embedDepthMm: Number.NaN }]) {
      expect(isRouteEditorDraft(invalid)).toBe(false);
    }
    const first = defaultRouteEditorDraft();
    first.path = "modified.gpx";
    expect(defaultRouteEditorDraft().path).toBe("");
  });
});
