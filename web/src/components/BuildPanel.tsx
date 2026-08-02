import {
  Box,
  Crosshair,
  FolderOpen,
  Layers3,
  MapPinned,
  Play,
  Ruler,
  Settings2,
  SquareDashedMousePointer,
} from "lucide-react";
import { useCallback } from "react";

import { translate, type TranslationKey } from "../i18n";
import type {
  FormState,
  Language,
  NormalizedAoi,
  SourceMode,
} from "../types";

interface BuildPanelProps {
  language: Language;
  form: FormState;
  normalizedAoi: NormalizedAoi | null;
  drawMode: "bbox" | "center" | null;
  busy: boolean;
  validating: boolean;
  onFormChange: (form: FormState) => void;
  onBrowseDem: () => void;
  onBrowseOverlay: () => void;
  onDrawMode: (mode: "bbox" | "center" | null) => void;
  onValidateAoi: () => void;
  onSubmit: () => void;
}

interface NumberFieldProps {
  label: string;
  value: number | null;
  min?: number;
  step?: number;
  placeholder?: string;
  onChange: (value: number | null) => void;
}

function NumberField({
  label,
  value,
  min,
  step = 0.1,
  placeholder,
  onChange,
}: NumberFieldProps) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        type="number"
        value={value ?? ""}
        min={min}
        step={step}
        placeholder={placeholder}
        onChange={(event) =>
          onChange(event.target.value === "" ? null : Number(event.target.value))
        }
      />
    </label>
  );
}

export function BuildPanel({
  language,
  form,
  normalizedAoi,
  drawMode,
  busy,
  validating,
  onFormChange,
  onBrowseDem,
  onBrowseOverlay,
  onDrawMode,
  onValidateAoi,
  onSubmit,
}: BuildPanelProps) {
  const t = useCallback(
    (key: TranslationKey) => translate(language, key),
    [language],
  );
  const update = <Key extends keyof FormState>(
    key: Key,
    value: FormState[Key],
  ) => onFormChange({ ...form, [key]: value });
  const setSourceMode = (sourceMode: SourceMode) => {
    update("sourceMode", sourceMode);
    onDrawMode(null);
  };
  const updateBbox = (index: number, value: number | null) => {
    const next = [...form.bbox] as [number, number, number, number];
    next[index] = value ?? 0;
    update("bbox", next);
  };
  const updateCenter = (index: number, value: number | null) => {
    const next = [...form.center] as [number, number];
    next[index] = value ?? 0;
    update("center", next);
  };

  return (
    <aside className="control-panel" aria-label={t("tabConfiguration")}>
      <section className="control-section">
        <div className="section-heading">
          <MapPinned size={17} />
          <h2>{t("source")}</h2>
        </div>
        <div className="segmented three" role="group" aria-label={t("source")}>
          {(
            [
              ["local", t("localDem")],
              ["bbox", t("bbox")],
              ["center-radius", t("centerRadius")],
            ] as const
          ).map(([value, label]) => (
            <button
              type="button"
              key={value}
              className={form.sourceMode === value ? "active" : ""}
              aria-pressed={form.sourceMode === value}
              onClick={() => setSourceMode(value)}
            >
              {label}
            </button>
          ))}
        </div>

        {form.sourceMode === "local" && (
          <label className="field">
            <span>{t("sourcePath")}</span>
            <div className="input-action">
              <input
                type="text"
                value={form.sourcePath}
                onChange={(event) => update("sourcePath", event.target.value)}
              />
              <button
                type="button"
                className="icon-button"
                onClick={onBrowseDem}
                title={t("browse")}
                aria-label={t("browse")}
              >
                <FolderOpen size={17} />
              </button>
            </div>
          </label>
        )}

        {form.sourceMode === "bbox" && (
          <>
            <div className="field-grid two">
              {[t("west"), t("south"), t("east"), t("north")].map(
                (label, index) => (
                  <NumberField
                    key={label}
                    label={label}
                    value={form.bbox[index]}
                    step={0.0001}
                    onChange={(value) => updateBbox(index, value)}
                  />
                ),
              )}
            </div>
            <div className="button-row">
              <button
                type="button"
                className={drawMode === "bbox" ? "secondary active" : "secondary"}
                onClick={() => onDrawMode(drawMode === "bbox" ? null : "bbox")}
              >
                <SquareDashedMousePointer size={16} />
                {t("drawBbox")}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={onValidateAoi}
                disabled={validating}
              >
                <Crosshair size={16} />
                {validating ? t("validating") : t("validateAoi")}
              </button>
            </div>
          </>
        )}

        {form.sourceMode === "center-radius" && (
          <>
            <div className="field-grid two">
              <NumberField
                label={t("longitude")}
                value={form.center[0]}
                step={0.0001}
                onChange={(value) => updateCenter(0, value)}
              />
              <NumberField
                label={t("latitude")}
                value={form.center[1]}
                step={0.0001}
                onChange={(value) => updateCenter(1, value)}
              />
              <NumberField
                label={t("radiusM")}
                value={form.radiusM}
                min={0.001}
                step={100}
                onChange={(value) => update("radiusM", value ?? 0)}
              />
            </div>
            <div className="button-row">
              <button
                type="button"
                className={drawMode === "center" ? "secondary active" : "secondary"}
                onClick={() => onDrawMode(drawMode === "center" ? null : "center")}
              >
                <Crosshair size={16} />
                {t("pickCenter")}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={onValidateAoi}
                disabled={validating}
              >
                <MapPinned size={16} />
                {validating ? t("validating") : t("validateAoi")}
              </button>
            </div>
          </>
        )}

        {form.sourceMode !== "local" && (
          <div className={normalizedAoi ? "aoi-readout valid" : "aoi-readout"}>
            <span>{normalizedAoi ? t("aoiValid") : t("aoiNotValidated")}</span>
            {normalizedAoi && (
              <>
                <code>{normalizedAoi.target_local_crs}</code>
                <small>
                  {(normalizedAoi.area_m2 / 1_000_000).toFixed(2)} km² ·{" "}
                  {t("crossesAntimeridian")}:{" "}
                  {normalizedAoi.crosses_antimeridian ? t("yes") : t("no")}
                </small>
              </>
            )}
          </div>
        )}
      </section>

      <section className="control-section">
        <div className="section-heading">
          <Box size={17} />
          <h2>{t("model")}</h2>
        </div>
        <label className="field">
          <span>{t("workspaceName")}</span>
          <input
            type="text"
            value={form.workspaceName}
            onChange={(event) => update("workspaceName", event.target.value)}
          />
        </label>
        <div className="field-grid two">
          <NumberField
            label={t("widthMm")}
            value={form.modelWidthMm}
            min={0.1}
            onChange={(value) => update("modelWidthMm", value ?? 0)}
          />
          <NumberField
            label={t("depthMm")}
            value={form.modelDepthMm}
            min={0.1}
            placeholder={t("autoAspect")}
            onChange={(value) => update("modelDepthMm", value)}
          />
          <NumberField
            label={t("maxHeightMm")}
            value={form.maxHeightMm}
            min={0.1}
            onChange={(value) => update("maxHeightMm", value ?? 0)}
          />
          <NumberField
            label={t("baseMm")}
            value={form.baseThicknessMm}
            min={0.01}
            onChange={(value) => update("baseThicknessMm", value ?? 0)}
          />
        </div>
      </section>

      <section className="control-section">
        <div className="section-heading">
          <Ruler size={17} />
          <h2>{t("sampling")}</h2>
        </div>
        <div className="segmented three" role="group" aria-label={t("sampling")}>
          {(
            [
              ["print-aware", t("samplingPrintAware")],
              ["source-preserving", t("samplingSource")],
              ["custom", t("samplingCustom")],
            ] as const
          ).map(([value, label]) => (
            <button
              type="button"
              key={value}
              className={form.samplingMode === value ? "active" : ""}
              aria-pressed={form.samplingMode === value}
              onClick={() => update("samplingMode", value)}
            >
              {label}
            </button>
          ))}
        </div>
        {form.samplingMode === "custom" && (
          <NumberField
            label={t("meshSpacingMm")}
            value={form.meshSamplingMm}
            min={0.001}
            step={0.05}
            onChange={(value) => update("meshSamplingMm", value ?? 0)}
          />
        )}
        <div className="segmented two" role="group" aria-label={t("resourceBudget")}>
          <button
            type="button"
            className={form.resourceBudgetMode === "adapt" ? "active" : ""}
            onClick={() => update("resourceBudgetMode", "adapt")}
          >
            {t("budgetAdapt")}
          </button>
          <button
            type="button"
            className={form.resourceBudgetMode === "strict" ? "active" : ""}
            onClick={() => update("resourceBudgetMode", "strict")}
          >
            {t("budgetStrict")}
          </button>
        </div>
        <details className="advanced-settings">
          <summary>
            <Settings2 size={16} />
            {t("advanced")}
          </summary>
          <div className="field-grid two">
            <NumberField
              label={t("maxCells")}
              value={form.maxGridCells}
              min={16}
              step={1000}
              onChange={(value) => update("maxGridCells", value ?? 16)}
            />
            <NumberField
              label={t("maxTriangles")}
              value={form.maxEstimatedTriangles}
              min={12}
              step={1000}
              onChange={(value) => update("maxEstimatedTriangles", value ?? 12)}
            />
            <NumberField
              label={t("maxMemory")}
              value={form.maxEstimatedMemoryMb}
              min={1}
              step={64}
              onChange={(value) => update("maxEstimatedMemoryMb", value ?? 1)}
            />
          </div>
        </details>
      </section>

      <section className="control-section">
        <div className="section-heading">
          <Layers3 size={17} />
          <h2>{t("tiling")}</h2>
        </div>
        <div className="field-grid two">
          <NumberField
            label={t("tileWidth")}
            value={form.maximumTileWidthMm}
            min={1}
            onChange={(value) => update("maximumTileWidthMm", value ?? 1)}
          />
          <NumberField
            label={t("tileDepth")}
            value={form.maximumTileDepthMm}
            min={1}
            onChange={(value) => update("maximumTileDepthMm", value ?? 1)}
          />
          <NumberField
            label={t("overlapCells")}
            value={form.overlapCells}
            min={0}
            step={1}
            onChange={(value) => update("overlapCells", value ?? 0)}
          />
        </div>
        <label className="field">
          <span>
            {t("overlayConfig")} <small>{t("optional")}</small>
          </span>
          <div className="input-action">
            <input
              type="text"
              value={form.overlayConfigPath}
              onChange={(event) => update("overlayConfigPath", event.target.value)}
            />
            <button
              type="button"
              className="icon-button"
              onClick={onBrowseOverlay}
              title={t("browse")}
              aria-label={t("browse")}
            >
              <FolderOpen size={17} />
            </button>
          </div>
        </label>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={form.slicingEnabled}
            onChange={(event) => update("slicingEnabled", event.target.checked)}
          />
          <span className="toggle" aria-hidden="true" />
          <span>{t("enableSlicing")}</span>
        </label>
        {form.slicingEnabled && (
          <>
            <label className="field">
              <span>{t("slicer")}</span>
              <select
                value={form.slicerName}
                onChange={(event) =>
                  update(
                    "slicerName",
                    event.target.value as FormState["slicerName"],
                  )
                }
              >
                <option value="bambu-studio">Bambu Studio</option>
                <option value="orca">OrcaSlicer</option>
                <option value="prusa">PrusaSlicer</option>
                <option value="auto">Auto</option>
              </select>
            </label>
            {form.slicerName === "bambu-studio" && (
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={form.projectEvidenceEnabled}
                  onChange={(event) =>
                    update("projectEvidenceEnabled", event.target.checked)
                  }
                />
                <span className="toggle" aria-hidden="true" />
                <span>{t("projectEvidence")}</span>
              </label>
            )}
          </>
        )}
      </section>

      <div className="primary-action">
        <button type="button" className="primary" onClick={onSubmit} disabled={busy}>
          <Play size={18} fill="currentColor" />
          {busy ? t("submitting") : t("startBuild")}
        </button>
      </div>
    </aside>
  );
}
