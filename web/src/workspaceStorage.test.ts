import { beforeEach, describe, expect, it } from "vitest";

import { defaultFormState } from "./config";
import { applyModelPreset, changePresets, DRAFT_KEY, MAX_PRESETS, modelSettings, PRESETS_KEY, readDraft, readPresets, saveDraft } from "./workspaceStorage";

const preset = { id: "desk-model", name: "桌面模型", settings: modelSettings({ ...defaultFormState, modelWidthMm: 120 }) };

describe("browser-local draft and model presets", () => {
  beforeEach(() => localStorage.clear());
  it("round-trips an unfinished draft, including its location and nullable aspect ratio", () => {
    const form = { ...defaultFormState, workspaceName: "", modelDepthMm: null, sourcePath: "/data/高程.tif", center: [121, 31] as [number, number], radiusM: 2750 };
    saveDraft(form);
    expect(readDraft()).toEqual({ status: "ready", value: form });
  });
  it.each([
    "{broken", JSON.stringify({ version: 2, value: defaultFormState }),
    JSON.stringify({ version: 1, value: { ...defaultFormState, sourceMode: "remote-shell" } }),
    JSON.stringify({ version: 1, value: { ...defaultFormState, center: [1] } }),
    JSON.stringify({ version: 1, value: { ...defaultFormState, modelWidthMm: "180" } }),
    "x".repeat(65_537),
  ])("rejects malformed, incompatible or oversized drafts without changing stored bytes", raw => {
    localStorage.setItem(DRAFT_KEY, raw);
    expect(readDraft().status).toBe("invalid");
    expect(localStorage.getItem(DRAFT_KEY)).toBe(raw);
  });
  it("handles denied storage and preserves the old draft if writing exceeds quota", () => {
    const denied = { getItem: () => { throw new DOMException("denied", "SecurityError"); }, setItem: () => { throw new DOMException("full", "QuotaExceededError"); } };
    expect(readDraft(denied).status).toBe("unavailable");
    expect(readPresets(denied).status).toBe("unavailable");
    expect(() => saveDraft(defaultFormState, denied)).toThrow();
    saveDraft(defaultFormState);
    const old = localStorage.getItem(DRAFT_KEY);
    expect(() => saveDraft({ ...defaultFormState, modelWidthMm: Infinity })).toThrow();
    expect(localStorage.getItem(DRAFT_KEY)).toBe(old);
  });
  it("applies model settings while preserving source, location, names, files and slicer choices", () => {
    const current = { ...defaultFormState, sourceMode: "bbox" as const, sourcePath: "/other.tif", workspaceName: "retain-me", bbox: [110, 20, 111, 21] as [number, number, number, number], overlayConfigPath: "/route.yml", slicingEnabled: true, projectEvidenceEnabled: true };
    expect(applyModelPreset(current, preset)).toEqual({ ...current, ...preset.settings });
    const stored = changePresets(() => [preset]);
    expect(stored[0].settings).not.toHaveProperty("sourcePath");
    expect(stored[0].settings).not.toHaveProperty("slicingEnabled");
  });
  it("re-reads presets before changes so independent additions are retained", () => {
    changePresets(current => [...current, preset]);
    const other = { ...preset, id: "other-tab", name: "另一页面" };
    changePresets(current => [...current, other]);
    changePresets(current => current.filter(item => item.id !== preset.id));
    expect(readPresets()).toEqual({ status: "ready", value: [other] });
  });
  it("rejects duplicate, corrupt, non-finite and over-limit presets without replacing a valid collection", () => {
    changePresets(() => [preset]);
    const old = localStorage.getItem(PRESETS_KEY);
    expect(() => changePresets(current => [...current, preset])).toThrow();
    expect(() => changePresets(() => [{ ...preset, settings: { ...preset.settings, modelWidthMm: NaN } }])).toThrow();
    expect(() => changePresets(() => Array.from({ length: MAX_PRESETS + 1 }, (_, index) => ({ ...preset, id: String(index), name: String(index) })))).toThrow();
    expect(localStorage.getItem(PRESETS_KEY)).toBe(old);
    localStorage.setItem(PRESETS_KEY, "malformed");
    expect(() => changePresets(() => [preset])).toThrow();
    expect(localStorage.getItem(PRESETS_KEY)).toBe("malformed");
  });
  it("retains new route/copy draft fields without storing preview geometry or hidden templates", () => {
    const form = { ...defaultFormState, reuseProjectId: "a".repeat(32), gpxRoute: { path: "/route.gpx", datasetName: "", license: "", attribution: "", color: "#d1495b", lineWidthMm: 0.8, raisedHeightMm: 0.4, embedDepthMm: 0.2 } };
    saveDraft(form);
    expect(readDraft()).toEqual({ status: "ready", value: form });
    localStorage.setItem(DRAFT_KEY, JSON.stringify({ version: 1, value: { ...form, reuseProjectId: "../invalid" } }));
    expect(readDraft().status).toBe("invalid");
  });
  it("does not touch any existing application preferences or runtime records", () => {
    localStorage.setItem("topoforge-basemap-mode", "cached");
    localStorage.setItem("topoforge-language", "en");
    saveDraft(defaultFormState);
    changePresets(() => [preset]);
    expect(localStorage.getItem("topoforge-basemap-mode")).toBe("cached");
    expect(localStorage.getItem("topoforge-language")).toBe("en");
  });
});
