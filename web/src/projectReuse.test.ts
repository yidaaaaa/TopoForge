import { describe, expect, it } from "vitest";

import { buildJobRequest, defaultFormState } from "./config";
import { buildReusedRequest, formFromProject, inheritedRoute, jsonObject, mergeRouteOverlay, projectAoi, projectOverlay, type ProjectReuseResponse } from "./projectReuse";
import type { Health, JsonObject } from "./types";

const health: Health = { status: "ok", version: "0.10.3", loopback_only: true, languages: ["zh-CN", "en"], workspace_root: "/workspaces", state_dir: "/state" };
const clip = { bbox_wgs84: [100, 29, 100.01, 29.01] };
function localProject(): ProjectReuseResponse {
  const request = buildJobRequest({ ...defaultFormState, workspaceName: "original", sourcePath: "/rasters/survey.tif" }, health);
  request.launch = {
    ...request.launch, slice_timeout_seconds: 765, project_timeout_seconds: 987,
    slicer_settings: ["/profiles/machine.json", "/profiles/print.json"], slicer_filaments: ["/profiles/material.json"],
    build: {
      ...jsonObject(request.launch.build), aoi: clip, output_formats: ["3mf", "glb"],
      baseline_mode: "custom", baseline_elevation_m: 2400, min_vertical_exaggeration: 0.2, max_vertical_exaggeration: 18, robust_low_percentile: 1, robust_high_percentile: 99, nodata_max_fraction: 0.02, source_provider: "local-survey", dataset_type: "dtm", dataset_name: "Measured surface", dataset_version: "2025-10",
      acquisition_period: "2025-10-02", source_urls: ["https://example.invalid/survey"], source_checksums: { source: "a".repeat(64) },
      source_download_time: "2025-10-03", source_acquisition_manifest: "/data/survey-acquisition.json", vertical_crs: "EPSG:5703", vertical_datum: "Known survey datum",
      data_license: "Owner permission", attribution: "Survey team", reference_peak_elevation_m: 5000, reference_peak_elevation_note: "Measured summit",
      printer_profile: { profile_id: "custom-p2s", connector_tolerance_mm: 0.25, nozzle_diameter_mm: 0.4, minimum_feature_mm: 0.5 },
    },
  };
  return { schema_version: "topoforge-project-reuse-v1", source_job_id: "a".repeat(32), original_workspace_dir: "/workspaces/original", suggested_workspace_name: "original-copy", request, issues: [] };
}
function globalProject(): ProjectReuseResponse {
  const project = localProject();
  project.request.launch.global_source = {
    aoi: clip, requested_provider_id: "copernicus-glo30", terrain_mode: "dsm", allow_semantic_fallback: true,
    preferred_provider_ids: ["copernicus-glo30"], cache_dir: "/cache/preserved", timeout_seconds: 73,
    max_attempts: 2, min_request_interval_seconds: 1.5,
  };
  jsonObject(project.request.launch.build).dem_path = "/workspaces/original/global-source-managed.tif";
  return project;
}
const routeSource: JsonObject = {
  source_id: "walk-2025", kind: "gpx", format: "gpx", path: "/tracks/original.gpx", source_crs: "EPSG:4326",
  dataset_name: "Original walk", dataset_version: "v2", license: "Track owner permission", attribution: "Original recorder",
  source_urls: ["https://example.invalid/track"], acquisition_period: "2025-08-01",
  style: { color: "#773344", line_width_mm: 1.2, raised_height_mm: 0.6, embed_depth_mm: 0.12, simplify_tolerance_mm: 0.04, label_font_height_mm: 5 },
};
const otherSource: JsonObject = { source_id: "river", kind: "river", format: "geojson", path: "/tracks/river.geojson", source_crs: "EPSG:4326", dataset_name: "River", license: "Permission", attribution: "Mapper", style: { color: "#1122aa" } };
function projectWithRoute(): ProjectReuseResponse {
  const project = localProject();
  project.request.launch.overlay = {
    sources: [structuredClone(routeSource), structuredClone(otherSource)],
    clip_to_model: true, allow_original_nodata: false, max_features: 1234, max_triangles: 654321, preview_width_px: 640,
  };
  return project;
}
function editedRoute(path = "/tracks/original.gpx"): JsonObject {
  return { sources: [{
    source_id: "gpx-route", kind: "gpx", format: "gpx", path, source_crs: "EPSG:4326", dataset_name: "Edited walk",
    license: "Recorder permission", attribution: "Named recorder",
    style: { color: "#33aa55", line_width_mm: 1.6, raised_height_mm: 0.8, embed_depth_mm: 0.12 },
  }] };
}

describe("project reuse preserves the original request", () => {
  it("copies into a new workspace without defaulting hidden engine, printer, slicer or provenance settings", () => {
    const project = localProject();
    const before = structuredClone(project);
    const form = formFromProject(project);
    expect(form).toMatchObject({ workspaceName: "original-copy", reuseProjectId: project.source_job_id, sourceMode: "local", sourcePath: "/rasters/survey.tif", connectorToleranceMm: 0.25 });
    const request = buildReusedRequest(form, health, project, null);
    expect(request).toEqual({
      reuse_source_job_id: project.source_job_id,
      launch: { ...before.request.launch, workspace_dir: "/workspaces/original-copy", build: { ...jsonObject(before.request.launch.build), output_dir: "/workspaces/original-copy" } },
    });
    expect(projectAoi(form, project)).toEqual(clip);
    expect(project).toEqual(before);
  });

  it("retains provenance and clipping when the input changes only by ignored surrounding whitespace", () => {
    const project = localProject();
    const form = { ...formFromProject(project), sourcePath: "  /rasters/survey.tif  " };
    const request = buildReusedRequest(form, health, project, null);
    expect(jsonObject(request.launch.build)).toEqual({ ...jsonObject(project.request.launch.build), output_dir: "/workspaces/original-copy" });
    expect(projectAoi(form, project)).toEqual(clip);
  });

  it("relocates a copied project's private provider cache again when its destination name is edited", () => {
    const project = globalProject();
    project.request.launch.workspace_dir = "/workspaces/suggested-copy";
    project.suggested_workspace_name = "suggested-copy";
    Object.assign(jsonObject(project.request.launch.build), { output_dir: "/workspaces/suggested-copy", dem_path: "/workspaces/suggested-copy/global-source-managed.tif" });
    jsonObject(project.request.launch.global_source).cache_dir = "/workspaces/suggested-copy/providers";
    const before = structuredClone(project);
    const form = { ...formFromProject(project), workspaceName: "renamed-copy" };
    const request = buildReusedRequest(form, health, project, null);
    expect(jsonObject(request.launch.global_source).cache_dir).toBe("/workspaces/renamed-copy/providers");
    expect(jsonObject(request.launch.build).dem_path).toBe("/workspaces/renamed-copy/global-source-managed.tif");
    expect(project).toEqual(before);
  });

  it("patches only edited controls while retaining unrelated custom fields", () => {
    const project = localProject();
    const before = structuredClone(project);
    const form = { ...formFromProject(project), modelWidthMm: 240, maximumTileDepthMm: 150, connectorToleranceMm: 0.3 };
    const result = buildReusedRequest(form, health, project, null).launch;
    expect(result).toEqual({
      ...before.request.launch, workspace_dir: "/workspaces/original-copy", maximum_tile_depth_mm: 150,
      build: { ...jsonObject(before.request.launch.build), output_dir: "/workspaces/original-copy", model_width_mm: 240, printer_profile: { ...jsonObject(jsonObject(before.request.launch.build).printer_profile), connector_tolerance_mm: 0.3 } },
    });
    expect(project).toEqual(before);
  });

  it("clears source-bound provenance and clipping when replacing the local raster", () => {
    const project = localProject();
    const form = { ...formFromProject(project), sourcePath: "/rasters/new-source.tif" };
    const result = buildReusedRequest(form, health, project, null).launch;
    expect(jsonObject(result.build)).toMatchObject({ dem_path: "/rasters/new-source.tif", aoi: null });
    for (const field of ["dataset_type", "dataset_name", "dataset_version", "acquisition_period", "source_urls", "vertical_crs", "vertical_datum", "data_license", "attribution", "source_provider", "source_download_time", "source_checksums", "source_acquisition_manifest", "reference_peak_elevation_m", "reference_peak_elevation_note"]) {
      expect(jsonObject(result.build)).not.toHaveProperty(field);
    }
    expect(projectAoi(form, project)).toBeNull();
    expect(jsonObject(project.request.launch.build).dem_path).toBe("/rasters/survey.tif");
  });

  it("retains provider policies when changing a global region, and uses a new managed raster path", () => {
    const project = globalProject();
    const form = { ...formFromProject(project), bbox: [101, 30, 101.1, 30.1] as [number, number, number, number] };
    const result = buildReusedRequest(form, health, project, null).launch;
    const aoi = { bbox_wgs84: form.bbox };
    expect(result.global_source).toEqual({ ...jsonObject(project.request.launch.global_source), aoi });
    expect(jsonObject(result.build)).toMatchObject({ dem_path: "/workspaces/original-copy/global-source-managed.tif", aoi });
    expect(projectAoi(form, project)).toEqual(aoi);
  });

  it("treats bbox-to-radius as an AOI edit and preserves the user's global provider and fallback choices", () => {
    const project = globalProject();
    const form = { ...formFromProject(project), sourceMode: "center-radius" as const, center: [101, 30] as [number, number], radiusM: 12000 };
    const result = buildReusedRequest(form, health, project, null).launch;
    const aoi = { center_wgs84: [101, 30], radius_m: 12000 };
    expect(result.global_source).toEqual({ ...jsonObject(project.request.launch.global_source), aoi });
    expect(jsonObject(result.build).aoi).toEqual(aoi);
  });

  it("clears the prior custom sampling when switching mode, and refuses a mismatched reuse origin", () => {
    const project = localProject();
    Object.assign(jsonObject(project.request.launch.build), { sampling_mode: "custom", mesh_sampling_mm: 0.37 });
    const form = { ...formFromProject(project), samplingMode: "source-preserving" as const };
    expect(jsonObject(buildReusedRequest(form, health, project, null).launch.build)).toMatchObject({ sampling_mode: "source-preserving", mesh_sampling_mm: null });
    expect(() => buildReusedRequest({ ...form, reuseProjectId: "b".repeat(32) }, health, project, null)).toThrow("reuse-not-ready");
  });

  it("rejects an unrepresentable global area instead of showing a different default region", () => {
    const project = globalProject();
    jsonObject(project.request.launch.global_source).aoi = { place_query: "Summit", place_candidate_id: "record-42", place_display_name: "Summit, Region", resolved_place_bbox_wgs84: [100, 29, 100.1, 29.1] };
    expect(() => formFromProject(project)).toThrow("reuse-unsupported-aoi");
  });
});

describe("route editing inside copied projects", () => {
  it("retains the original equivalent WGS84 CRS identifier for the same GPX", () => {
    const project = projectWithRoute();
    const base = jsonObject(project.request.launch.overlay);
    base.sources = [{ ...routeSource, source_crs: "OGC:CRS84" }, otherSource];
    const result = projectOverlay(project, editedRoute());
    expect(jsonObject((result?.sources as unknown[])[0]).source_crs).toBe("OGC:CRS84");
  });

  it("restores the actual GPX style and provenance, preserving the existing embed depth", () => {
    const project = projectWithRoute();
    const route = inheritedRoute(project);
    expect(route?.draft).toEqual({ path: "/tracks/original.gpx", datasetName: "Original walk", license: "Track owner permission", attribution: "Original recorder", color: "#773344", lineWidthMm: 1.2, raisedHeightMm: 0.6, embedDepthMm: 0.12 });
    expect(formFromProject(project).gpxRoute).toEqual(route?.draft);
  });

  it("preserves extra source metadata, geometry style, remaining layers and limits while editing the same GPX", () => {
    const project = projectWithRoute();
    const before = structuredClone(project);
    const route = editedRoute();
    const result = projectOverlay(project, route);
    const replacement = jsonObject((route.sources as unknown[])[0]);
    expect(result).toEqual({
      ...jsonObject(project.request.launch.overlay), sources: [
        { ...routeSource, ...replacement, source_id: "walk-2025", style: { ...jsonObject(routeSource.style), ...jsonObject(replacement.style) } },
        otherSource,
      ],
    });
    expect(project).toEqual(before);
  });

  it("does not attach the previous track's hidden metadata to a replacement file", () => {
    const project = projectWithRoute();
    const route = editedRoute("/tracks/new-recording.gpx");
    const result = projectOverlay(project, route);
    const source = jsonObject((result?.sources as unknown[])[0]);
    expect(source).toMatchObject({ path: "/tracks/new-recording.gpx", source_id: "walk-2025", dataset_name: "Edited walk", attribution: "Named recorder" });
    for (const key of ["dataset_version", "source_urls", "acquisition_period"]) expect(source).not.toHaveProperty(key);
    expect(source.style).toMatchObject({ simplify_tolerance_mm: 0.04, color: "#33aa55", line_width_mm: 1.6 });
    expect(jsonObject((result?.sources as unknown[])[1])).toEqual(otherSource);
  });

  it("removes only the edited route, and returns null if it was the last layer", () => {
    const project = projectWithRoute();
    expect(projectOverlay(project, null)).toEqual({ ...jsonObject(project.request.launch.overlay), sources: [otherSource] });
    jsonObject(project.request.launch.overlay).sources = [routeSource];
    expect(projectOverlay(project, null)).toBeNull();
  });

  it("keeps all other GPX layers when editing the first route", () => {
    const project = projectWithRoute();
    const secondRoute = { ...routeSource, source_id: "another-track", path: "/tracks/another.gpx" };
    (jsonObject(project.request.launch.overlay).sources as unknown[]).push(secondRoute);
    const result = projectOverlay(project, editedRoute());
    expect(result?.sources).toHaveLength(3);
    expect((result?.sources as unknown[])[2]).toEqual(secondRoute);
  });

  it("allocates a unique route source id without overwriting existing layers or overlay limits", () => {
    const base = { max_features: 50, sources: [{ ...otherSource, source_id: "gpx-route" }, { ...otherSource, source_id: "gpx-route-2" }] };
    const before = structuredClone(base);
    const result = mergeRouteOverlay(base, editedRoute());
    expect(result.max_features).toBe(50);
    expect((result.sources as JsonObject[]).map(source => source.source_id)).toEqual(["gpx-route", "gpx-route-2", "gpx-route-3"]);
    expect(base).toEqual(before);
    expect(() => mergeRouteOverlay({ sources: {} }, editedRoute())).toThrow("invalid-overlay");
  });
});
