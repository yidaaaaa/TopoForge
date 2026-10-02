import { CONNECTOR_TOLERANCE_OPTIONS_MM, defaultFormState } from "./config";
import type { FormState } from "./types";

export const DRAFT_KEY = "topoforge-form-draft-v1";
export const PRESETS_KEY = "topoforge-model-presets-v1";
export const MAX_PRESETS = 12;
const MAX_DOCUMENT_LENGTH = 65_536;
type BrowserStorage = Pick<Storage, "getItem" | "setItem">;
export type StorageResult<T> =
  | { status: "ready"; value: T }
  | { status: "empty" | "invalid" | "unavailable"; value: null };

const enumValues: Partial<Record<keyof FormState, readonly unknown[]>> = {
  sourceMode: ["local", "bbox", "center-radius"],
  verticalScaleMode: ["natural", "fit-height", "auto-perceptual", "custom"],
  samplingMode: ["print-aware", "source-preserving", "custom"],
  resourceBudgetMode: ["adapt", "strict"],
  slicerName: ["bambu-studio", "orca", "prusa", "auto"],
  terrainMode: ["best-available", "dtm", "dsm", "bathymetry"],
  connectorToleranceMm: CONNECTOR_TOLERANCE_OPTIONS_MM,
};

export const PRESET_FIELDS = [
  "modelWidthMm", "modelDepthMm", "baseThicknessMm", "maxHeightMm",
  "verticalScaleMode", "verticalExaggeration", "samplingMode", "meshSamplingMm",
  "maxGridCells", "maxEstimatedTriangles", "maxEstimatedMemoryMb", "resourceBudgetMode",
  "maximumTileWidthMm", "maximumTileDepthMm", "connectorToleranceMm", "overlapCells",
] as const satisfies readonly (keyof FormState)[];
export type ModelSettings = Pick<FormState, (typeof PRESET_FIELDS)[number]>;
export interface ModelPreset {
  id: string;
  name: string;
  settings: ModelSettings;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Validate the browser document's shape, not manufacturing policy. The engine
// still validates ranges and printable geometry when the user submits a job.
function fieldValid(key: keyof FormState, value: unknown): boolean {
  if (enumValues[key]) return enumValues[key]!.includes(value);
  if (key === "modelDepthMm") return value === null || (typeof value === "number" && Number.isFinite(value));
  const reference = defaultFormState[key];
  if (Array.isArray(reference)) {
    return Array.isArray(value) && value.length === reference.length && value.every(item => typeof item === "number" && Number.isFinite(item));
  }
  if (typeof reference === "number") return typeof value === "number" && Number.isFinite(value);
  if (typeof reference === "boolean") return typeof value === "boolean";
  return typeof value === "string" && value.length <= 4096;
}

export function freshForm(): FormState {
  return { ...defaultFormState, bbox: [...defaultFormState.bbox], center: [...defaultFormState.center] };
}

export function modelSettings(form: ModelSettings): ModelSettings {
  return Object.fromEntries(PRESET_FIELDS.map(key => [key, form[key]])) as ModelSettings;
}

/** Apply only model settings; the location, files, project name and slicer stay put. */
export function applyModelPreset(form: FormState, preset: ModelPreset): FormState {
  return { ...form, ...modelSettings(preset.settings) };
}

function readDocument(key: string, storage?: BrowserStorage): StorageResult<unknown> {
  try {
    const raw = (storage ?? window.localStorage).getItem(key);
    if (raw === null) return { status: "empty", value: null };
    if (raw.length > MAX_DOCUMENT_LENGTH) return { status: "invalid", value: null };
    let document: unknown;
    try { document = JSON.parse(raw); }
    catch { return { status: "invalid", value: null }; }
    if (!object(document) || document.version !== 1 || !("value" in document)) return { status: "invalid", value: null };
    return { status: "ready", value: document.value };
  } catch {
    return { status: "unavailable", value: null };
  }
}

export function readDraft(storage?: BrowserStorage): StorageResult<FormState> {
  const result = readDocument(DRAFT_KEY, storage);
  if (result.status !== "ready") return result;
  const value = result.value;
  const keys = Object.keys(defaultFormState) as (keyof FormState)[];
  if (!object(value) || !keys.every(key => fieldValid(key, value[key]))) return { status: "invalid", value: null };
  // Reconstruct known keys so old/new browsers never carry unknown launch options.
  return { status: "ready", value: Object.fromEntries(keys.map(key => [key, value[key]])) as unknown as FormState };
}

function writeDocument(key: string, value: unknown, storage?: BrowserStorage): void {
  const raw = JSON.stringify({ version: 1, value });
  if (raw.length > MAX_DOCUMENT_LENGTH) throw new Error("storage-limit");
  (storage ?? window.localStorage).setItem(key, raw);
}

export function saveDraft(form: FormState, storage?: BrowserStorage): void {
  if (!(Object.keys(defaultFormState) as (keyof FormState)[]).every(key => fieldValid(key, form[key]))) throw new Error("invalid-draft");
  writeDocument(DRAFT_KEY, form, storage);
}

export function readPresets(storage?: BrowserStorage): StorageResult<ModelPreset[]> {
  const result = readDocument(PRESETS_KEY, storage);
  if (result.status !== "ready") return result;
  return parsePresets(result.value);
}

function parsePresets(value: unknown): StorageResult<ModelPreset[]> {
  if (!Array.isArray(value) || value.length > MAX_PRESETS) return { status: "invalid", value: null };
  const ids = new Set<string>();
  const names = new Set<string>();
  const presets: ModelPreset[] = [];
  for (const item of value) {
    if (!object(item) || typeof item.id !== "string" || !/^[a-zA-Z0-9-]{1,80}$/.test(item.id) ||
        typeof item.name !== "string" || !item.name.trim() || item.name.length > 60 ||
        !object(item.settings) || !PRESET_FIELDS.every(key => fieldValid(key, (item.settings as Record<string, unknown>)[key])) ||
        ids.has(item.id) || names.has(item.name.trim())) return { status: "invalid", value: null };
    ids.add(item.id);
    names.add(item.name.trim());
    presets.push({ id: item.id, name: item.name.trim(), settings: modelSettings(item.settings as unknown as ModelSettings) });
  }
  return { status: "ready", value: presets };
}

/** Re-read before editing, so another tab's saved presets are preserved. */
export function changePresets(update: (current: ModelPreset[]) => ModelPreset[], storage?: BrowserStorage): ModelPreset[] {
  const current = readPresets(storage);
  if (current.status === "invalid" || current.status === "unavailable") throw new Error(current.status);
  const next = update(current.value ?? []);
  if (next.length > MAX_PRESETS) throw new Error("preset-limit");
  if (parsePresets(next).status !== "ready") throw new Error("invalid-presets");
  writeDocument(PRESETS_KEY, next, storage);
  return next;
}
