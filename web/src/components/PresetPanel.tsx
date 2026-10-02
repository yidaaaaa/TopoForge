import { BookmarkPlus, Check, Trash2 } from "lucide-react";
import { useState } from "react";

import { translate } from "../i18n";
import type { FormState, Language } from "../types";
import { applyModelPreset, changePresets, MAX_PRESETS, modelSettings, readPresets } from "../workspaceStorage";

export function PresetPanel({ language, form, onApply }: {
  language: Language;
  form: FormState;
  onApply: (next: FormState) => void;
}) {
  const t = (key: Parameters<typeof translate>[1]) => translate(language, key);
  const [initial] = useState(readPresets);
  const [presets, setPresets] = useState(initial.value ?? []);
  const [name, setName] = useState("");
  const [selection, setSelection] = useState("");
  const [message, setMessage] = useState<"saved" | "applied" | "deleted" | "duplicate" | "limit" | "unavailable" | null>(
    initial.status === "invalid" || initial.status === "unavailable" ? "unavailable" : null,
  );
  const selected = presets.find(preset => preset.id === selection);
  const save = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    try {
      const id = crypto.randomUUID();
      const next = changePresets(current => {
        if (current.some(preset => preset.name === trimmed)) throw new Error("duplicate");
        if (current.length >= MAX_PRESETS) throw new Error("limit");
        return [...current, { id, name: trimmed, settings: modelSettings(form) }];
      });
      setPresets(next);
      setSelection(id);
      setName("");
      setMessage("saved");
    } catch (error) {
      setMessage(error instanceof Error && (error.message === "duplicate" || error.message === "limit") ? error.message : "unavailable");
    }
  };
  const remove = () => {
    if (!selected) return;
    try {
      setPresets(changePresets(current => current.filter(preset => preset.id !== selected.id)));
      setSelection("");
      setMessage("deleted");
    } catch { setMessage("unavailable"); }
  };
  const messages = {
    saved: "presetSaved", applied: "presetApplied", deleted: "presetDeleted",
    duplicate: "presetDuplicate", limit: "presetLimit", unavailable: "presetUnavailable",
  } as const;
  return (
    <details className="preset-manager">
      <summary><BookmarkPlus size={16} />{t("modelPresets")}<span className="count-badge">{presets.length}</span></summary>
      <div className="preset-content">
        <p className="field-help">{t("presetScope")}</p>
        {presets.length > 0 && <>
          <label className="field"><span>{t("choosePreset")}</span>
            <select value={selection} onChange={event => { setSelection(event.target.value); setMessage(null); }}>
              <option value="">{t("choosePresetPlaceholder")}</option>
              {presets.map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
            </select>
          </label>
          <div className="button-row">
            <button type="button" className="secondary" disabled={!selected} onClick={() => {
              if (selected) { onApply(applyModelPreset(form, selected)); setMessage("applied"); }
            }}><Check size={14} />{t("applyPreset")}</button>
            <button type="button" className="icon-button" disabled={!selected} aria-label={t("deletePreset")} title={t("deletePreset")} onClick={remove}><Trash2 size={15} /></button>
          </div>
        </>}
        <label className="field"><span>{t("presetName")}</span>
          <input value={name} maxLength={60} placeholder={t("presetNamePlaceholder")} onChange={event => setName(event.target.value)} />
        </label>
        <button type="button" className="secondary preset-save" disabled={!name.trim()} onClick={save}><BookmarkPlus size={15} />{t("savePreset")}</button>
        {message && <p className={`field-help preset-message ${["duplicate", "limit", "unavailable"].includes(message) ? "warning" : ""}`} role="status">{t(messages[message])}</p>}
      </div>
    </details>
  );
}
