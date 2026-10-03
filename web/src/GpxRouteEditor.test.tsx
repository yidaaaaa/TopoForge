import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { GpxRouteEditor, type GpxRouteEditorProps } from "./GpxRouteEditor";
import { buildRouteOverlay, defaultRouteEditorDraft, type GpxPreviewResponse, type GpxRoutePreview, type RouteEditorDraft } from "./routeEditor";

const response: GpxPreviewResponse = {
  schema_version: "topoforge-gpx-preview-v1", path: "/data/walk.gpx", filename: "walk.gpx", sha256: "a".repeat(64), size_bytes: 320,
  source_crs: "EPSG:4326", bounds_wgs84: [100, 29, 100.01, 29.01], segment_count: 1, point_count: 2,
  geojson: { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[100, 29], [100.01, 29.01]] } }] },
};
function setup(overrides: Partial<GpxRouteEditorProps> = {}) {
  const loadPreview = vi.fn(async (_path: string, _signal: AbortSignal): Promise<GpxPreviewResponse> => response);
  const onPreviewChange = vi.fn();
  const onChange = vi.fn();
  const onBrowse = vi.fn();
  const onFocusRoute = vi.fn();
  const onRemove = vi.fn();
  let currentDraft = overrides.value ?? { ...defaultRouteEditorDraft(), path: "walk.gpx" };
  let currentPreview = overrides.preview ?? null;
  const props = { language: "en" as const, loadPreview, onBrowse, onFocusRoute, onRemove, ...overrides };
  function Harness() {
    const [value, setValue] = useState<RouteEditorDraft>(currentDraft);
    const [preview, setPreview] = useState<GpxRoutePreview | null>(currentPreview);
    return <GpxRouteEditor {...props} value={value} preview={preview} onChange={next => { currentDraft = next; onChange(next); setValue(next); }} onPreviewChange={next => { currentPreview = next; onPreviewChange(next); setPreview(next); }} />;
  }
  return { ...render(<Harness />), loadPreview, onPreviewChange, onChange, onBrowse, onFocusRoute, onRemove, draft: () => currentDraft, preview: () => currentPreview };
}

it("only reads the selected local route after an explicit action; previewing does not assert a license", async () => {
  const view = setup();
  expect(view.loadPreview).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Choose GPX file" }));
  expect(view.onBrowse).toHaveBeenCalledTimes(1);
  expect(view.loadPreview).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Load route" }));
  await screen.findByText("walk.gpx");
  expect(view.loadPreview).toHaveBeenCalledWith("walk.gpx", expect.any(AbortSignal));
  expect(screen.getByText("1 segments · 2 track points")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Route name" })).toHaveValue("walk");
  expect(screen.getByRole("textbox", { name: "Source license" })).toHaveValue("");
  expect(view.draft().attribution).toBe("");
  expect(buildRouteOverlay(view.draft(), view.preview())).toBeNull();
  expect(view.onFocusRoute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Show on map" }));
  expect(view.onFocusRoute).toHaveBeenCalledWith(expect.objectContaining({ ...response, requestedPath: "walk.gpx" }));
});

it("edits physical style and provenance without rereading the GPX or losing the stored embed depth", async () => {
  const view = setup({ value: { ...defaultRouteEditorDraft(), path: "walk.gpx", embedDepthMm: 0.15 } });
  fireEvent.click(screen.getByRole("button", { name: "Load route" }));
  await screen.findByText("walk.gpx");
  fireEvent.change(screen.getByLabelText("Route color"), { target: { value: "#2255cc" } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Line width mm" }), { target: { value: "1.2" } });
  fireEvent.change(screen.getByRole("spinbutton", { name: "Raised height mm" }), { target: { value: "0.6" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Source license" }), { target: { value: "Owner permission" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Author / attribution" }), { target: { value: "I recorded this" } });
  expect(screen.getByText("Route ready. It will be generated with the new model.")).toBeInTheDocument();
  expect(view.loadPreview).toHaveBeenCalledTimes(1);
  expect(view.draft()).toMatchObject({ color: "#2255cc", lineWidthMm: 1.2, raisedHeightMm: 0.6, embedDepthMm: 0.15, license: "Owner permission", attribution: "I recorded this" });
  expect(buildRouteOverlay(view.draft(), view.preview())).not.toBeNull();
  fireEvent.change(screen.getByRole("spinbutton", { name: "Line width mm" }), { target: { value: "" } });
  expect(screen.getByRole("spinbutton", { name: "Line width mm" })).toHaveValue(null);
  expect(buildRouteOverlay(view.draft(), view.preview())).toBeNull();
});

it("drops a delayed response after changing paths even when the loader ignores cancellation", async () => {
  const resolves: Array<(value: GpxPreviewResponse) => void> = [];
  const loadPreview = vi.fn((_path: string, _signal: AbortSignal) => new Promise<GpxPreviewResponse>(resolve => resolves.push(resolve)));
  const view = setup({ loadPreview });
  fireEvent.click(screen.getByRole("button", { name: "Load route" }));
  fireEvent.change(screen.getByRole("textbox", { name: /GPX file/ }), { target: { value: "second.gpx" } });
  expect(loadPreview.mock.calls[0][1].aborted).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Load route" }));
  await act(async () => resolves[1]({ ...response, filename: "second.gpx", path: "/data/second.gpx" }));
  await screen.findByText("second.gpx");
  await act(async () => resolves[0](response));
  expect(screen.queryByText("walk.gpx")).not.toBeInTheDocument();
  expect(view.preview()?.requestedPath).toBe("second.gpx");
  expect(view.draft().datasetName).toBe("second");
});

it("clears the accepted preview on path edits and on failed reload, while retaining the user's settings", async () => {
  const loadPreview = vi.fn(async () => response);
  const view = setup({ loadPreview, value: { ...defaultRouteEditorDraft(), path: "walk.gpx", datasetName: "Named route", license: "Permission", attribution: "Recorder", color: "#aa5522" } });
  fireEvent.click(screen.getByRole("button", { name: "Load route" }));
  await screen.findByText("walk.gpx");
  expect(screen.getByRole("textbox", { name: "Route name" })).toHaveValue("Named route");
  loadPreview.mockRejectedValueOnce(new Error("GPX file is unavailable. Choose another local file."));
  fireEvent.click(screen.getByRole("button", { name: "Reload route" }));
  await screen.findByRole("alert");
  expect(view.preview()).toBeNull();
  expect(view.draft().color).toBe("#aa5522");
  expect(buildRouteOverlay(view.draft(), view.preview())).toBeNull();
  fireEvent.change(screen.getByRole("textbox", { name: /GPX file/ }), { target: { value: "new.gpx" } });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(view.loadPreview).not.toHaveBeenCalled();
});

it("cancels work on removal/unmount and never emits a late preview", async () => {
  let resolve!: (result: GpxPreviewResponse) => void;
  const loadPreview = vi.fn((_path: string, _signal: AbortSignal) => new Promise<GpxPreviewResponse>(done => { resolve = done; }));
  const view = setup({ loadPreview });
  fireEvent.click(screen.getByRole("button", { name: "Load route" }));
  const callsBeforeUnmount = view.onPreviewChange.mock.calls.length;
  view.unmount();
  expect(loadPreview.mock.calls[0][1].aborted).toBe(true);
  await act(async () => resolve(response));
  expect(view.onPreviewChange).toHaveBeenCalledTimes(callsBeforeUnmount);
});

describe("Chinese copy and busy state", () => {
  it("provides localized actions, and disables every edit while a submission is in progress", async () => {
    const view = setup({ language: "zh-CN", disabled: true });
    expect(screen.getByRole("region", { name: "路线" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "选择 GPX 文件" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: /GPX 文件/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "载入路线" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "移除路线" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "载入路线" }));
    await waitFor(() => expect(view.loadPreview).not.toHaveBeenCalled());
  });
});
