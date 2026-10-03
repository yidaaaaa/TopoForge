import { FolderOpen, LocateFixed, Route, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { Language } from "./types";
import { routeEditorIssue, routePreviewMatches, type GpxPreviewResponse, type GpxRoutePreview, type RouteEditorDraft } from "./routeEditor";
import "./routeEditor.css";

export interface GpxRouteEditorProps {
  language: Language;
  value: RouteEditorDraft;
  preview: GpxRoutePreview | null;
  onChange: (value: RouteEditorDraft) => void;
  onPreviewChange: (preview: GpxRoutePreview | null) => void;
  loadPreview: (path: string, signal: AbortSignal) => Promise<GpxPreviewResponse>;
  onBrowse: () => void;
  onFocusRoute?: (preview: GpxRoutePreview) => void;
  onRemove?: () => void;
  disabled?: boolean;
}

const copy = {
  "zh-CN": {
    title: "路线", intro: "载入 GPX，在地图上查看路线并设置模型上的样式。",
    path: "GPX 文件", placeholder: "选择本地 .gpx 文件", browse: "选择 GPX 文件", load: "载入路线", reload: "重新载入", loading: "正在读取路线…",
    remove: "移除路线", focus: "在地图上查看", segments: "段", points: "个轨迹点",
    color: "路线颜色", width: "线宽", height: "凸起高度", modelUnits: "线宽和高度是打印模型上的毫米数。地图线条仅用于定位。",
    name: "路线名称", namePlaceholder: "为这条路线命名", license: "来源许可", licensePlaceholder: "按路线来源填写使用许可或条款",
    attribution: "作者 / 署名", attributionPlaceholder: "记录者或原作者", provenance: "这些来源信息会随模型保存；自录轨迹也请填写自己的许可与署名。",
    ready: "路线已就绪，将随新模型一起生成。", pending: "请先载入路线，再填写来源信息。",
    pathNeeded: "选择 GPX 文件后载入路线。", previewNeeded: "请载入当前文件，再生成带路线的模型。",
    provenanceNeeded: "补充路线名称、来源许可与署名后，即可生成模型。", styleNeeded: "请选择有效颜色；线宽和凸起高度必须大于 0，嵌入深度不能为负数。",
    extra: "这条路线会加入新模型；高级设置中的其他叠加内容也会保留。",
  },
  en: {
    title: "Route", intro: "Load a GPX track, see it on the map and choose its model appearance.",
    path: "GPX file", placeholder: "Choose a local .gpx file", browse: "Choose GPX file", load: "Load route", reload: "Reload route", loading: "Reading route…",
    remove: "Remove route", focus: "Show on map", segments: "segments", points: "track points",
    color: "Route color", width: "Line width", height: "Raised height", modelUnits: "Width and height are millimetres on the printed model. The map line is for location only.",
    name: "Route name", namePlaceholder: "Name this route", license: "Source license", licensePlaceholder: "Enter the source's permission or license terms",
    attribution: "Author / attribution", attributionPlaceholder: "Recorder or original author", provenance: "These source details stay with the model. For your own track, enter your own permission and attribution.",
    ready: "Route ready. It will be generated with the new model.", pending: "Load a route, then complete its source details.",
    pathNeeded: "Choose a GPX file and load the route.", previewNeeded: "Load the current file before creating a model with this route.",
    provenanceNeeded: "Enter a route name, source license and attribution to create the model.", styleNeeded: "Choose a valid color. Line width and raised height must be positive; embed depth cannot be negative.",
    extra: "This route is added to the new model. Other overlays in advanced settings are preserved.",
  },
};

export function GpxRouteEditor(props: GpxRouteEditorProps) {
  const { language, value, preview, onChange, onPreviewChange, loadPreview, onBrowse, onFocusRoute, onRemove, disabled = false } = props;
  const t = copy[language];
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const sequence = useRef(0);
  const request = useRef<AbortController | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const previousPath = useRef(value.path.trim());
  const path = value.path.trim();
  const loaded = routePreviewMatches(value, preview) ? preview : null;
  const issue = routeEditorIssue(value, preview);

  useEffect(() => {
    if (previousPath.current === path) return;
    previousPath.current = path;
    sequence.current += 1;
    request.current?.abort();
    setLoading(false);
    setError("");
    latest.current.onPreviewChange(null);
  }, [path]);

  useEffect(() => () => {
    sequence.current += 1;
    request.current?.abort();
  }, []);

  const update = <K extends keyof RouteEditorDraft>(key: K, next: RouteEditorDraft[K]) => {
    onChange({ ...value, [key]: next });
  };

  const load = async () => {
    if (!path || loading || disabled) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const ticket = ++sequence.current;
    setLoading(true);
    setError("");
    onPreviewChange(null);
    try {
      const result = await loadPreview(path, controller.signal);
      if (ticket !== sequence.current || controller.signal.aborted || latest.current.value.path.trim() !== path) return;
      const current = latest.current;
      if (!current.value.datasetName.trim()) {
        current.onChange({ ...current.value, datasetName: result.filename.replace(/\.gpx$/i, "") });
      }
      current.onPreviewChange({ ...result, requestedPath: path });
    } catch (reason) {
      if (ticket !== sequence.current || controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (ticket === sequence.current) setLoading(false);
    }
  };

  const messages = { path: t.pathNeeded, preview: t.previewNeeded, provenance: t.provenanceNeeded, style: t.styleNeeded };
  return (
    <section className="control-section route-editor" aria-label={t.title}>
      <div className="section-heading route-editor-heading">
        <Route size={17} aria-hidden="true" /><h2>{t.title}</h2><span className="count-badge">GPX</span>
        {onRemove && <button type="button" className="icon-button route-remove" aria-label={t.remove} title={t.remove} disabled={disabled} onClick={onRemove}><Trash2 size={15} /></button>}
      </div>
      <p className="field-help">{t.intro}</p>
      <fieldset className="route-editor-fields" disabled={disabled}>
        <label className="field"><span>{t.path}</span>
          <span className="route-path-field">
            <input aria-label={t.path} value={value.path} placeholder={t.placeholder} spellCheck={false} onChange={event => update("path", event.target.value)} />
            <button type="button" className="icon-button" onClick={onBrowse} aria-label={t.browse} title={t.browse}><FolderOpen size={17} /></button>
          </span>
        </label>
        <div className="route-actions">
          <button type="button" className="secondary" disabled={!path || loading} onClick={() => void load()}>{loading ? t.loading : loaded ? t.reload : t.load}</button>
          {loaded && onFocusRoute && <button type="button" className="secondary" onClick={() => onFocusRoute(loaded)}><LocateFixed size={14} />{t.focus}</button>}
        </div>
        {loaded && <div className="route-loaded" role="status">
          <strong>{loaded.filename}</strong>
          <span>{loaded.segment_count.toLocaleString(language)} {t.segments} · {loaded.point_count.toLocaleString(language)} {t.points}</span>
        </div>}
        {loaded && <>
          <div className="route-style-fields">
            <label className="field route-color-field"><span>{t.color}</span><input type="color" value={/^#[0-9a-f]{6}$/i.test(value.color) ? value.color : "#d1495b"} onChange={event => update("color", event.target.value)} /></label>
            <label className="field"><span>{t.width} <small>mm</small></span><input type="number" min="0" step="any" value={value.lineWidthMm || ""} onChange={event => update("lineWidthMm", event.target.value === "" ? 0 : Number(event.target.value))} /></label>
            <label className="field"><span>{t.height} <small>mm</small></span><input type="number" min="0" step="any" value={value.raisedHeightMm || ""} onChange={event => update("raisedHeightMm", event.target.value === "" ? 0 : Number(event.target.value))} /></label>
          </div>
          <p className="field-help">{t.modelUnits}</p>
          <div className="route-provenance">
            <label className="field"><span>{t.name}</span><input value={value.datasetName} placeholder={t.namePlaceholder} onChange={event => update("datasetName", event.target.value)} /></label>
            <label className="field"><span>{t.license}</span><input value={value.license} placeholder={t.licensePlaceholder} onChange={event => update("license", event.target.value)} /></label>
            <label className="field"><span>{t.attribution}</span><input value={value.attribution} placeholder={t.attributionPlaceholder} onChange={event => update("attribution", event.target.value)} /></label>
            <p className="field-help">{t.provenance}</p>
          </div>
        </>}
      </fieldset>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <p className={`field-help route-readiness${issue ? "" : " ready"}`} role="status">{issue ? messages[issue] : t.ready}</p>
      <p className="field-help route-extra">{t.extra}</p>
    </section>
  );
}
