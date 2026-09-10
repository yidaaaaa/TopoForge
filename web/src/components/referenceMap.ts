import type { ExpressionSpecification, LayerSpecification } from "maplibre-gl";
import type { Language } from "../types";

export const VECTOR_TILE_URL = "/api/v1/reference/tiles/{z}/{x}/{y}.mvt";

/** Only natural features and local navigation context; no political layers. */
export function referenceLayers(language: Language): LayerSpecification[] {
  const name: ExpressionSpecification = [
    "coalesce", ["get", language === "zh-CN" ? "name_zh" : "name_en"],
    ["get", "name"], "",
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
