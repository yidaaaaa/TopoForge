import type { ExpressionSpecification, LayerSpecification } from "maplibre-gl";
import type { Language } from "../types";

export const VECTOR_TILE_URL = "/api/v1/reference/tiles/{z}/{x}/{y}.mvt";

/** Natural/local context plus country and regional names; boundary geometry is curated separately. */
export function referenceLayers(language: Language): LayerSpecification[] {
  const name: ExpressionSpecification = [
    "coalesce", ["get", language === "zh-CN" ? "name_zh" : "name_en"],
    ["get", "name"], "",
  ];
  const regionMatch = (aliases: string[]): ExpressionSpecification => ["any",
    ...["name", "name_en", "name_zh"].map(key => ["in", ["get", key], ["literal", aliases]]),
  ] as ExpressionSpecification;
  const taiwan = regionMatch(["臺灣", "台灣", "台湾", "台湾省", "臺灣省", "台灣省", "Taiwan", "Taiwan (China)", "Taiwan, Province of China", "中華民國", "中华民国", "Republic of China"]);
  const hongKong = regionMatch(["香港", "香港特别行政区", "香港特別行政區", "Hong Kong", "Hong Kong SAR", "Hong Kong S.A.R."]);
  const macao = regionMatch(["澳門", "澳门", "澳门特别行政区", "澳門特別行政區", "Macao", "Macau", "Macao SAR"]);
  const isChinaRegion: ExpressionSpecification = ["any", taiwan, hongKong, macao];
  const regionName: ExpressionSpecification = ["case",
    taiwan, language === "zh-CN" ? "台湾省" : "Taiwan",
    hongKong, language === "zh-CN" ? "香港" : "Hong Kong",
    macao, language === "zh-CN" ? "澳门" : "Macao", name,
  ];
  const labelPaint = { "text-color": "#344b48", "text-halo-color": "#fafbf6", "text-halo-width": 1.5 };
  return [
    { id: "osm-land-background", type: "background", layout: { visibility: "none" }, paint: { "background-color": "#d4ddd1" } },
    { id: "osm-ocean", type: "fill", source: "osm", "source-layer": "ocean", paint: { "fill-color": "#c9dde1" } },
    { id: "osm-water", type: "fill", source: "osm", "source-layer": "water_polygons", paint: { "fill-color": "#b4d5df" } },
    { id: "osm-rivers", type: "line", source: "osm", "source-layer": "water_lines", paint: { "line-color": "#95c4d2", "line-width": 1.2 } },
    { id: "osm-buildings", type: "fill", source: "osm", "source-layer": "buildings", minzoom: 13, paint: { "fill-color": "#c1c6bc", "fill-outline-color": "#aeb6a9" } },
    { id: "osm-streets", type: "line", source: "osm", "source-layer": "streets", minzoom: 5, paint: { "line-color": "#fbfaf3", "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.7, 12, 2, 17, 5] } },
    { id: "osm-street-labels", type: "symbol", source: "osm", "source-layer": "street_labels", minzoom: 12,
      layout: { "symbol-placement": "line", "text-field": name, "text-font": ["sans-serif"], "text-size": 11 }, paint: labelPaint },
    { id: "osm-places", type: "symbol", source: "osm", "source-layer": "place_labels", minzoom: 3,
      filter: ["in", ["get", "kind"], ["literal", ["capital", "city", "town", "village", "hamlet", "suburb", "quarter", "neighbourhood", "isolated_dwelling"]]],
      layout: { "text-field": name, "text-font": ["sans-serif"], "text-size": ["interpolate", ["linear"], ["zoom"], 3, 11, 12, 15] }, paint: labelPaint },
    { id: "osm-country-names", type: "symbol", source: "osm", "source-layer": "boundary_labels", minzoom: 2, maxzoom: 8,
      filter: ["all", ["==", ["get", "admin_level"], 2], ["!", isChinaRegion]],
      layout: { "text-field": name, "text-font": ["sans-serif"], "text-size": ["interpolate", ["linear"], ["zoom"], 2, 13, 6, 18], "text-max-width": 8 },
      paint: { ...labelPaint, "text-color": "#69566f" } },
    { id: "osm-region-names", type: "symbol", source: "osm", "source-layer": "boundary_labels", minzoom: 3, maxzoom: 8,
      filter: ["all", ["==", ["get", "admin_level"], 2], isChinaRegion],
      layout: { "text-field": regionName, "text-font": ["sans-serif"], "text-size": 12, "text-max-width": 8 }, paint: labelPaint },
    { id: "osm-pois", type: "symbol", source: "osm", "source-layer": "pois", minzoom: 13,
      filter: ["in", ["get", "kind"], ["literal", ["peak", "viewpoint", "museum", "attraction", "park", "hotel", "restaurant", "cafe", "camp_site"]]],
      layout: { "text-field": name, "text-font": ["sans-serif"], "text-size": 11 }, paint: labelPaint },
  ];
}


/** Restore only a valid camera, so cache-only startup returns to the viewed area. */
export function savedReferenceCamera(): { center: [number, number]; zoom: number } | null {
  try {
    const value = JSON.parse(localStorage.getItem("topoforge-reference-camera") ?? "null");
    if (value && Array.isArray(value.center) && value.center.length === 2 &&
        value.center.every((n: unknown) => typeof n === "number" && Number.isFinite(n)) &&
        Math.abs(value.center[0]) <= 180 && Math.abs(value.center[1]) <= 85.051129 &&
        typeof value.zoom === "number" && Number.isFinite(value.zoom) && value.zoom >= 0 && value.zoom <= 22) {
      return { center: [value.center[0], value.center[1]], zoom: value.zoom };
    }
  } catch { /* Unavailable or invalid saved preferences do not prevent map startup. */ }
  return null;
}


/** Display the boundary catalog after source-viewpoint classification. */
export const boundaryVisibilityFilter: ExpressionSpecification = ["==", ["get", "display"], true];

/** Maritime strokes are already separated in the geometry; do not dash them again. */
export function referenceBoundaryLayers(): LayerSpecification[] {
  return [
    { id: "reference-boundaries", type: "line", source: "reference-boundaries",
      filter: ["all", boundaryVisibilityFilter, ["any", ["!=", ["get", "disputed"], true], ["==", ["get", "maritime_indicator"], true]]],
      paint: { "line-color": "#927c98", "line-opacity": 0.75,
        "line-width": ["case", ["==", ["get", "internal"], true], 0.65, 1.2] } },
    { id: "reference-disputed-boundaries", type: "line", source: "reference-boundaries",
      filter: ["all", boundaryVisibilityFilter, ["==", ["get", "disputed"], true], ["!=", ["get", "maritime_indicator"], true]],
      paint: { "line-color": "#927c98", "line-width": 1.1, "line-dasharray": [3, 3], "line-opacity": 0.75 } },
  ];
}
