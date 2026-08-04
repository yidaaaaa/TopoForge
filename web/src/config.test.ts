import { describe, expect, it } from "vitest";

import {
  aoiInput,
  buildJobRequest,
  defaultFormState,
  sanitizeWorkspaceName,
} from "./config";
import { translate } from "./i18n";
import type { FormState, Health } from "./types";

const health: Health = {
  status: "ok",
  version: "0.8.0",
  loopback_only: true,
  languages: ["zh-CN", "en"],
  workspace_root: "/tmp/workspaces",
  state_dir: "/tmp/state",
};

describe("bilingual configuration contracts", () => {
  it("provides distinct complete Chinese and English primary labels", () => {
    expect(translate("zh-CN", "startBuild")).toBe("开始构建");
    expect(translate("en", "startBuild")).toBe("Start build");
    expect(translate("zh-CN", "eastAxis")).toContain("+X");
    expect(translate("en", "northAxis")).toContain("+Y");
  });

  it("creates the local launch contract used by the Python workflow", () => {
    const form: FormState = {
      ...defaultFormState,
      workspaceName: "local-demo",
      sourcePath: "/data/dem.tif",
      samplingMode: "source-preserving",
      resourceBudgetMode: "strict",
    };
    const request = buildJobRequest(form, health);
    const launch = request.launch;
    expect(launch.workspace_dir).toBe("/tmp/workspaces/local-demo");
    expect(launch.global_source).toBeNull();
    expect(launch.slicing_enabled).toBe(false);
    expect(launch.build).toMatchObject({
      dem_path: "/data/dem.tif",
      printer_profile: { connector_tolerance_mm: 0.2 },
      vertical_scale_mode: "auto-perceptual",
      vertical_exaggeration: 1,
      sampling_mode: "source-preserving",
      mesh_sampling_mm: null,
      resource_budget_mode: "strict",
    });
  });

  it("passes a custom vertical exaggeration through to the core build", () => {
    const request = buildJobRequest(
      {
        ...defaultFormState,
        workspaceName: "vertical-demo",
        sourcePath: "/data/dem.tif",
        verticalScaleMode: "custom",
        verticalExaggeration: 2.5,
      },
      health,
    );
    expect(request.launch.build).toMatchObject({
      vertical_scale_mode: "custom",
      vertical_exaggeration: 2.5,
      max_height_mm: 45,
    });
  });

  it("passes the selected production connector clearance to the core profile", () => {
    const request = buildJobRequest(
      {
        ...defaultFormState,
        workspaceName: "connector-clearance-demo",
        sourcePath: "/data/dem.tif",
        connectorToleranceMm: 0.4,
      },
      health,
    );
    expect(request.launch.build).toMatchObject({
      printer_profile: { connector_tolerance_mm: 0.4 },
    });
  });

  it("creates bbox and center-radius provider requests without local placeholders leaking", () => {
    const bboxForm: FormState = {
      ...defaultFormState,
      sourceMode: "bbox",
      workspaceName: "bbox-demo",
      bbox: [179.8, -1, -179.7, 1],
      samplingMode: "custom",
      meshSamplingMm: 0.65,
    };
    const bbox = buildJobRequest(bboxForm, health);
    expect(aoiInput(bboxForm)).toEqual({
      bbox_wgs84: [179.8, -1, -179.7, 1],
    });
    expect(bbox.launch.global_source).toMatchObject({
      requested_provider_id: "auto",
      cache_dir: "/tmp/state/provider-cache",
    });
    expect(bbox.launch.build).toMatchObject({
      mesh_sampling_mm: 0.65,
      aoi: { bbox_wgs84: [179.8, -1, -179.7, 1] },
    });

    const radiusForm: FormState = {
      ...defaultFormState,
      sourceMode: "center-radius",
      center: [85, 28],
      radiusM: 5000,
    };
    expect(aoiInput(radiusForm)).toEqual({
      center_wgs84: [85, 28],
      radius_m: 5000,
    });
  });

  it("rejects unsafe workspace names and missing local sources", () => {
    expect(() => sanitizeWorkspaceName("../escape")).toThrow("invalid-workspace");
    expect(() =>
      buildJobRequest(
        { ...defaultFormState, workspaceName: "valid", sourcePath: "" },
        health,
      ),
    ).toThrow("source-required");
  });
});
