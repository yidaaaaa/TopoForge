import type { FeatureCollection, LineString } from "geojson";

import type { JsonObject } from "./types";

/** Editable route settings; a loaded preview is deliberately kept out of drafts. */
export interface RouteEditorDraft {
  path: string;
  datasetName: string;
  license: string;
  attribution: string;
  color: string;
  lineWidthMm: number;
  raisedHeightMm: number;
  embedDepthMm: number;
}

export interface GpxPreviewResponse {
  schema_version: "topoforge-gpx-preview-v1";
  path: string;
  filename: string;
  sha256: string;
  size_bytes: number;
  source_crs: "EPSG:4326";
  bounds_wgs84: [number, number, number, number];
  segment_count: number;
  point_count: number;
  geojson: FeatureCollection<LineString>;
}

/** The user-entered path may differ from the server's resolved local path. */
export interface GpxRoutePreview extends GpxPreviewResponse {
  requestedPath: string;
}

export function defaultRouteEditorDraft(): RouteEditorDraft {
  return {
    path: "", datasetName: "", license: "", attribution: "",
    color: "#d1495b", lineWidthMm: 0.8, raisedHeightMm: 0.4, embedDepthMm: 0.2,
  };
}

/** Validate draft shape without treating unfinished fields as corrupt storage. */
export function isRouteEditorDraft(value: unknown): value is RouteEditorDraft {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const draft = value as Record<string, unknown>;
  const strings = ["path", "datasetName", "license", "attribution", "color"];
  const numbers = ["lineWidthMm", "raisedHeightMm", "embedDepthMm"];
  return strings.every(key => typeof draft[key] === "string") &&
    numbers.every(key => typeof draft[key] === "number" && Number.isFinite(draft[key]));
}

export function routePreviewMatches(draft: RouteEditorDraft, preview: GpxRoutePreview | null): preview is GpxRoutePreview {
  return Boolean(preview && draft.path.trim() && preview.requestedPath === draft.path.trim());
}

export type RouteEditorIssue = "path" | "preview" | "provenance" | "style";

/** Return the first actionable reason a draft cannot yet be used for a model. */
export function routeEditorIssue(draft: RouteEditorDraft, preview: GpxRoutePreview | null): RouteEditorIssue | null {
  if (!draft.path.trim()) return "path";
  if (!routePreviewMatches(draft, preview)) return "preview";
  if (![draft.datasetName, draft.license, draft.attribution].every(value => value.trim())) return "provenance";
  if (!/^#[0-9a-f]{6}$/i.test(draft.color) ||
    !Number.isFinite(draft.lineWidthMm) || draft.lineWidthMm <= 0 ||
    !Number.isFinite(draft.raisedHeightMm) || draft.raisedHeightMm <= 0 ||
    !Number.isFinite(draft.embedDepthMm) || draft.embedDepthMm < 0) return "style";
  return null;
}

/** Adapt a reviewed local route to the existing engine's overlay configuration. */
export function buildRouteOverlay(draft: RouteEditorDraft, preview: GpxRoutePreview | null): JsonObject | null {
  if (routeEditorIssue(draft, preview) || !preview) return null;
  return {
    sources: [{
      source_id: "gpx-route",
      kind: "gpx",
      format: "gpx",
      path: preview.path,
      source_crs: "EPSG:4326",
      dataset_name: draft.datasetName.trim(),
      license: draft.license.trim(),
      attribution: draft.attribution.trim(),
      style: {
        color: draft.color,
        line_width_mm: draft.lineWidthMm,
        raised_height_mm: draft.raisedHeightMm,
        embed_depth_mm: draft.embedDepthMm,
      },
    }],
  };
}
