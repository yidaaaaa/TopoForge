import { useEffect, useRef, useState } from "react";
import { ApiError, fetchPlaceSearchConfig, searchPlaces } from "../api";
import type { Language, PlaceCandidate, PlaceSearchConfig, SavedPlace } from "../types";
import { coordinatePlace, readSavedPlaces, rememberPlace, savedPlaceKey, SAVED_PLACES_KEY, SEARCH_SERVICE_KEY } from "./placeSearchState";

const words = {
  "zh-CN": {
    label: "查找地点", placeholder: "地名、地址或经度, 纬度", search: "搜索", busy: "搜索中…",
    online: "联网搜索", offline: "仅查询本地搜索缓存", cacheLocked: "仅使用本地缓存：搜索也不会联网。",
    publicHelp: "使用 OSM 公共搜索服务。查询会发送给 OSM，请勿提交私人信息。仅手动搜索，应用合计每秒最多一次。",
    customHelp: "查询会发送到已配置的搜索服务。", policy: "服务使用规则", source: "搜索服务",
    hint: "地名可写成“西湖, 杭州”，点搜索后核对完整地址。WGS84 经度, 纬度可离线定位。",
    streets: "显示道路与地名（联网）",
    candidates: "候选地点", saved: "最近与收藏", empty: "没有找到地点，请补充城市或换个名称。",
    cacheMiss: "这个搜索尚未缓存。可启用联网搜索，或使用最近地点、收藏和经纬度。",
    failed: "地点搜索暂不可用，请稍后重试；也可使用经纬度或已保存地点。",
    serviceFailed: "搜索服务配置未能读取，请刷新；坐标和已保存地点仍可使用。",
    limited: "已有搜索正在处理，请稍后再试。", invalid: "请输入有效的 WGS84 经度, 纬度；地图支持纬度 ±85.051129°。",
    favorite: "收藏", unfavorite: "取消收藏", remove: "移除", locate: "定位", close: "收起地点搜索",
    center: "以此为打印中心", located: "已定位", resultHint: "选中地点只移动地图；随后可框选，或设为打印中心并调整半径。",
    cached: "来自本地缓存", storage: "浏览器无法保存地点，本次定位仍可使用。", favoritesFull: "最多收藏 50 个地点，请先取消一项收藏。",
  },
  en: {
    label: "Find a place", placeholder: "Place, address or longitude, latitude", search: "Search", busy: "Searching…",
    online: "Search online", offline: "Search local query cache only", cacheLocked: "Cache-only mode: place search also stays offline.",
    publicHelp: "Uses the public OSM search service. Queries are sent to OSM; do not submit private information. Manual searches only, at most one request per second across the application.",
    customHelp: "Queries are sent to the configured search service.", policy: "Service usage policy", source: "Search service",
    hint: "Try a place followed by its city (West Lake, Hangzhou), then check the full address. WGS84 longitude, latitude works offline.",
    streets: "Show streets and labels (online)",
    candidates: "Matching places", saved: "Recent and saved", empty: "No matching places. Add a city or try another name.",
    cacheMiss: "This search is not cached. Enable online search, or use recent places, favorites or coordinates.",
    failed: "Place search is unavailable. Retry later, or use coordinates and saved places.",
    serviceFailed: "Search settings could not be loaded. Refresh; coordinates and saved places still work.",
    limited: "A search is already running. Please try again shortly.", invalid: "Enter valid WGS84 longitude, latitude; the map supports latitudes within ±85.051129°.",
    favorite: "Save", unfavorite: "Unsave", remove: "Remove", locate: "Locate", close: "Close place search",
    center: "Use as print center", located: "Located", resultHint: "Choosing a place only moves the map. Draw an area, or use it as the print center and adjust the radius.",
    cached: "From local cache", storage: "Browser storage is unavailable; you can still locate this place.", favoritesFull: "Up to 50 favorites can be saved. Unsave one first.",
  },
};

interface Props {
  language: Language;
  cacheOnly: boolean;
  onLocate: (place: PlaceCandidate) => void;
  onUseCenter: (place: PlaceCandidate) => void;
  onEnableBasemap?: () => void;
}

export function PlaceSearch({ language, cacheOnly, onLocate, onUseCenter, onEnableBasemap }: Props) {
  const t = words[language];
  const [config, setConfig] = useState<PlaceSearchConfig | null>(null);
  const [configError, setConfigError] = useState(false);
  const [online, setOnline] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<PlaceCandidate[]>([]);
  const [saved, setSaved] = useState(readSavedPlaces);
  const [selected, setSelected] = useState<SavedPlace | null>(null);
  const [message, setMessage] = useState<keyof typeof t | null>(null);
  const [cacheHit, setCacheHit] = useState(false);
  const [attribution, setAttribution] = useState("");
  const generation = useRef(0);
  const active = useRef<AbortController | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchPlaceSearchConfig(controller.signal).then(value => {
      if (controller.signal.aborted) return;
      if (!value || typeof value.endpoint !== "string" || typeof value.is_public !== "boolean") throw Error("config");
      if (!["http:", "https:"].includes(new URL(value.endpoint).protocol)) throw Error("config");
      setConfig(value);
      try { setOnline(localStorage.getItem(SEARCH_SERVICE_KEY) === value.endpoint); } catch { /* optional storage */ }
    }).catch(() => { if (!controller.signal.aborted) setConfigError(true); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    generation.current++;
    active.current?.abort();
    setBusy(false);
    setResults([]);
    setMessage(null);
    setCacheHit(false);
    setAttribution("");
  }, [language, cacheOnly, online]);
  useEffect(() => () => { generation.current++; active.current?.abort(); }, []);
  useEffect(() => {
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !panel.current?.contains(event.target)) setExpanded(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, []);

  function persist(next: SavedPlace[]) {
    setSaved(next);
    try { localStorage.setItem(SAVED_PLACES_KEY, JSON.stringify(next)); }
    catch { setMessage("storage"); }
  }
  function choose(place: PlaceCandidate, endpoint: string) {
    active.current?.abort(); generation.current++; setBusy(false);
    setSelected({ place, endpoint, favorite: false });
    onLocate({ ...place });
    persist(rememberPlace(saved, { place, endpoint, favorite: false }));
    setExpanded(false);
  }
  function favorite(item: SavedPlace) {
    const key = savedPlaceKey(item);
    const exists = saved.find(value => savedPlaceKey(value) === key);
    const nextFavorite = !exists?.favorite;
    if (nextFavorite && saved.filter(value => value.favorite).length >= 50) { setMessage("favoritesFull"); return; }
    const next = { ...item, favorite: nextFavorite };
    persist(rememberPlace(saved.filter(value => savedPlaceKey(value) !== key), next));
  }
  function changeQuery(value: string) {
    generation.current++; active.current?.abort(); setBusy(false);
    setQuery(value); setResults([]); setMessage(null); setCacheHit(false); setAttribution(""); setExpanded(true);
  }
  async function submit() {
    if (!query.trim() || busy) return;
    setExpanded(true); setMessage(null); setResults([]); setCacheHit(false); setAttribution("");
    try {
      const coordinates = coordinatePlace(query);
      if (coordinates) { choose(coordinates, "coordinates"); return; }
    } catch { setMessage("invalid"); return; }
    if (!config) { setMessage("serviceFailed"); return; }
    const current = ++generation.current;
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    setBusy(true);
    try {
      const result = await searchPlaces(query, language, cacheOnly || !online, online && config.is_public, controller.signal);
      if (controller.signal.aborted || current !== generation.current) return;
      setResults(result.candidates); setCacheHit(result.cache_status === "hit"); setAttribution(result.attribution);
      if (!result.candidates.length) setMessage("empty");
    } catch (error) {
      if (controller.signal.aborted || current !== generation.current) return;
      setMessage(error instanceof ApiError && error.status === 409 ? "cacheMiss" :
        error instanceof ApiError && error.status === 429 ? "limited" : "failed");
    } finally { if (current === generation.current) setBusy(false); }
  }
  const ordered = [...saved].sort((a, b) => Number(b.favorite) - Number(a.favorite));
  const candidateRow = (item: SavedPlace, stored: boolean) => {
    const key = savedPlaceKey(item);
    const isFavorite = saved.some(value => savedPlaceKey(value) === key && value.favorite);
    return <li key={key}>
      <button type="button" className="place-choice" onClick={() => choose(item.place, item.endpoint)} aria-label={`${t.locate} ${item.place.display_name}`}>
        <span>{item.place.display_name}</span>
        <small>{item.place.longitude.toFixed(5)}, {item.place.latitude.toFixed(5)}</small>
      </button>
      <button type="button" className="place-save" aria-label={`${isFavorite ? t.unfavorite : t.favorite} ${item.place.display_name}`} aria-pressed={isFavorite} onClick={() => favorite(item)}>{isFavorite ? "★" : "☆"}</button>
      {stored && <button type="button" className="place-remove" aria-label={`${t.remove} ${item.place.display_name}`} onClick={() => persist(saved.filter(value => savedPlaceKey(value) !== key))}>×</button>}
    </li>;
  };

  return <div className="place-search" ref={panel} onKeyDown={event => {
    if (event.key === "Escape") { active.current?.abort(); generation.current++; setBusy(false); setExpanded(false); }
  }}>
    <form className="place-search-form" aria-label={t.label} onSubmit={event => { event.preventDefault(); void submit(); }}>
      <input aria-label={t.label} placeholder={t.placeholder} value={query} maxLength={200} onFocus={() => setExpanded(true)} onChange={event => changeQuery(event.target.value)} autoComplete="off" />
      <button type="submit" disabled={busy || !query.trim()}>{busy ? t.busy : t.search}</button>
    </form>
    {selected && <div className="place-selected">
      <span title={selected.place.display_name}>{t.located} · {selected.place.display_name}{selected.endpoint !== "coordinates" && <small className="place-selected-attribution"><a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a></small>}</span>
      <button type="button" onClick={() => onUseCenter(selected.place)}>{t.center}</button>
    </div>}
    {selected && onEnableBasemap && <button type="button" className="place-enable-basemap" onClick={onEnableBasemap}>{t.streets}</button>}
    {expanded && <div className="place-search-popover">
      <div className="place-service-row">
        <label><input type="checkbox" checked={online && !cacheOnly} disabled={!config || cacheOnly} onChange={event => {
          const enabled = event.target.checked; setOnline(enabled);
          try { if (enabled && config) localStorage.setItem(SEARCH_SERVICE_KEY, config.endpoint); else localStorage.removeItem(SEARCH_SERVICE_KEY); }
          catch { setMessage("storage"); }
        }} />{t.online}</label>
        <button type="button" className="place-close" aria-label={t.close} onClick={() => setExpanded(false)}>×</button>
      </div>
      {config && <p className="place-service-name">{t.source}: {new URL(config.endpoint).host}</p>}
      {config?.is_public ? <p className="place-help">{t.publicHelp} <a href="https://operations.osmfoundation.org/policies/nominatim/" target="_blank" rel="noopener noreferrer">{t.policy}</a></p> : config && <p className="place-help">{t.customHelp}</p>}
      <p className="place-help">{cacheOnly ? t.cacheLocked : online ? t.hint : `${t.offline} · ${t.hint}`}</p>
      {configError && <p role="status">{t.serviceFailed}</p>}
      {message && <p role="status" className="place-message">{t[message]}</p>}
      {busy && <p role="status">{t.busy}</p>}
      {cacheHit && <p className="place-help">{t.cached}</p>}
      {results.length > 0 && <><p className="place-list-title">{t.candidates}</p><p className="place-help">{t.resultHint}</p>
        <ul className="place-list">{results.map(place => candidateRow({ place, endpoint: config?.endpoint ?? "", favorite: false }, false))}</ul></>}
      {ordered.length > 0 && <><p className="place-list-title">{t.saved}</p><ul className="place-list">{ordered.map(item => candidateRow(item, true))}</ul></>}
      {(attribution || ordered.some(item => item.endpoint !== "coordinates")) && <p className="place-attribution">{attribution || "Geocoding © OpenStreetMap contributors"}</p>}
    </div>}
  </div>;
}
