import type { FeatureCollection, Geometry } from "geojson";
import { Crosshair, MapPinned, SquareDashedMousePointer } from "lucide-react";
import maplibregl, {
  type GeoJSONSource,
  type MapMouseEvent,
  type StyleSpecification,
} from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";

import { translate } from "../i18n";
import type { Language, NormalizedAoi, SourceMode } from "../types";

interface MapPanelProps {
  language: Language;
  sourceMode: SourceMode;
  normalizedAoi: NormalizedAoi | null;
  basemapEnabled: boolean;
  drawMode: "bbox" | "center" | null;
  onBboxChange: (bbox: [number, number, number, number]) => void;
  onCenterChange: (center: [number, number]) => void;
}

function mapStyle(basemapEnabled: boolean): StyleSpecification {
  if (basemapEnabled) {
    return {
      version: 8,
      sources: {
        osm: {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          attribution: "© OpenStreetMap contributors",
          maxzoom: 19,
        },
      },
      layers: [
        {
          id: "background",
          type: "background",
          paint: { "background-color": "#dce5e2" },
        },
        { id: "osm", type: "raster", source: "osm" },
      ],
    };
  }
  return {
    version: 8,
    sources: {},
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#dce5e2" },
      },
    ],
  };
}

function emptyCollection(): FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

export function MapPanel({
  language,
  sourceMode,
  normalizedAoi,
  basemapEnabled,
  drawMode,
  onBboxChange,
  onCenterChange,
}: MapPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [cursor, setCursor] = useState<[number, number] | null>(null);
  const [draft, setDraft] = useState<Geometry | null>(null);
  const activeGeometry = draft ?? normalizedAoi?.normalized_geometry_geojson ?? null;

  const collection = useMemo<FeatureCollection>(
    () =>
      activeGeometry
        ? {
            type: "FeatureCollection",
            features: [
              {
                type: "Feature",
                properties: {},
                geometry: activeGeometry,
              },
            ],
          }
        : emptyCollection(),
    [activeGeometry],
  );

  useEffect(() => {
    if (!containerRef.current || mapRef.current) {
      return;
    }
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: mapStyle(basemapEnabled),
      center: [20, 25],
      zoom: 1.4,
      attributionControl: false,
      renderWorldCopies: true,
      canvasContextAttributes: { preserveDrawingBuffer: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");
    map.addControl(
      new maplibregl.AttributionControl({ compact: true }),
      "bottom-right",
    );
    map.on("load", () => {
      map.addSource("aoi", { type: "geojson", data: emptyCollection() });
      map.addLayer({
        id: "aoi-fill",
        type: "fill",
        source: "aoi",
        paint: {
          "fill-color": "#0f766e",
          "fill-opacity": 0.2,
        },
      });
      map.addLayer({
        id: "aoi-line",
        type: "line",
        source: "aoi",
        paint: {
          "line-color": "#0f5f59",
          "line-width": 2.5,
        },
      });
    });
    map.on("mousemove", (event) => {
      setCursor([
        Number(event.lngLat.lng.toFixed(5)),
        Number(event.lngLat.lat.toFixed(5)),
      ]);
    });
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    map.setStyle(mapStyle(basemapEnabled));
    map.once("styledata", () => {
      if (!map.getSource("aoi")) {
        map.addSource("aoi", { type: "geojson", data: collection });
        map.addLayer({
          id: "aoi-fill",
          type: "fill",
          source: "aoi",
          paint: { "fill-color": "#0f766e", "fill-opacity": 0.2 },
        });
        map.addLayer({
          id: "aoi-line",
          type: "line",
          source: "aoi",
          paint: { "line-color": "#0f5f59", "line-width": 2.5 },
        });
      }
    });
  }, [basemapEnabled]);

  useEffect(() => {
    const source = mapRef.current?.getSource("aoi") as GeoJSONSource | undefined;
    source?.setData(collection);
  }, [collection]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !normalizedAoi) {
      return;
    }
    const [west, south, east, north] = normalizedAoi.bounds_wgs84;
    const adjustedEast = west > east ? east + 360 : east;
    map.fitBounds(
      [
        [west, south],
        [adjustedEast, north],
      ],
      { padding: 64, maxZoom: 11, duration: 500 },
    );
  }, [normalizedAoi]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const canvas = map.getCanvas();
    canvas.style.cursor = drawMode ? "crosshair" : "";
    let start: [number, number] | null = null;

    const down = (event: MapMouseEvent) => {
      if (drawMode !== "bbox") {
        return;
      }
      start = [event.lngLat.lng, event.lngLat.lat];
      map.dragPan.disable();
    };
    const move = (event: MapMouseEvent) => {
      if (!start || drawMode !== "bbox") {
        return;
      }
      const west = Math.min(start[0], event.lngLat.lng);
      const east = Math.max(start[0], event.lngLat.lng);
      const south = Math.min(start[1], event.lngLat.lat);
      const north = Math.max(start[1], event.lngLat.lat);
      setDraft({
        type: "Polygon",
        coordinates: [
          [
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
          ],
        ],
      });
    };
    const up = (event: MapMouseEvent) => {
      if (!start || drawMode !== "bbox") {
        return;
      }
      const west = Math.min(start[0], event.lngLat.lng);
      const east = Math.max(start[0], event.lngLat.lng);
      const south = Math.min(start[1], event.lngLat.lat);
      const north = Math.max(start[1], event.lngLat.lat);
      start = null;
      map.dragPan.enable();
      setDraft(null);
      if (west !== east && south !== north) {
        onBboxChange([west, south, east, north]);
      }
    };
    const click = (event: MapMouseEvent) => {
      if (drawMode === "center") {
        onCenterChange([event.lngLat.lng, event.lngLat.lat]);
      }
    };
    map.on("mousedown", down);
    map.on("mousemove", move);
    map.on("mouseup", up);
    map.on("click", click);
    return () => {
      map.off("mousedown", down);
      map.off("mousemove", move);
      map.off("mouseup", up);
      map.off("click", click);
      map.dragPan.enable();
      canvas.style.cursor = "";
    };
  }, [drawMode, onBboxChange, onCenterChange]);

  return (
    <div className="map-shell" data-testid="map-panel">
      <div ref={containerRef} className="map-canvas" />
      <div className="map-mode">
        {drawMode === "bbox" && (
          <>
            <SquareDashedMousePointer size={16} />
            {translate(language, "drawBbox")}
          </>
        )}
        {drawMode === "center" && (
          <>
            <Crosshair size={16} />
            {translate(language, "pickCenter")}
          </>
        )}
        {!drawMode && (
          <>
            <MapPinned size={16} />
            {translate(language, sourceMode === "local" ? "offlineMap" : "aoiStatus")}
          </>
        )}
      </div>
      {cursor && (
        <code className="map-coordinate">
          {cursor[0].toFixed(5)}, {cursor[1].toFixed(5)}
        </code>
      )}
    </div>
  );
}
