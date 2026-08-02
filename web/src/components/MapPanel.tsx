import type { Feature, FeatureCollection, Geometry, LineString } from "geojson";
import { Crosshair, Layers3, MapPinned, SquareDashedMousePointer } from "lucide-react";
import maplibregl, {
  type GeoJSONSource,
  type MapMouseEvent,
  type StyleSpecification,
} from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import { feature } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import countriesTopologyJson from "world-atlas/countries-110m.json";

import { translate } from "../i18n";
import type {
  JobMapManifest,
  Language,
  MapTileStyle,
  NormalizedAoi,
  SourceMode,
} from "../types";

interface MapPanelProps {
  language: Language;
  sourceMode: SourceMode;
  normalizedAoi: NormalizedAoi | null;
  basemapEnabled: boolean;
  drawMode: "bbox" | "center" | null;
  manifest: JobMapManifest | null;
  selectedTileId: string | null;
  visualizationLoading: boolean;
  visualizationError: string | null;
  onSelectedTileChange: (tileId: string) => void;
  onBboxChange: (bbox: [number, number, number, number]) => void;
  onCenterChange: (center: [number, number]) => void;
}

const countriesTopology = countriesTopologyJson as unknown as Topology<{
  countries: GeometryCollection;
}>;

export const offlineCountries = feature(
  countriesTopology,
  countriesTopology.objects.countries,
) as FeatureCollection;

function buildGraticule(): FeatureCollection<LineString> {
  const features: Array<Feature<LineString>> = [];
  for (let longitude = -180; longitude <= 180; longitude += 30) {
    const coordinates: Array<[number, number]> = [];
    for (let latitude = -80; latitude <= 80; latitude += 4) {
      coordinates.push([longitude, latitude]);
    }
    features.push({
      type: "Feature",
      properties: { kind: longitude === 0 ? "prime-meridian" : "longitude" },
      geometry: { type: "LineString", coordinates },
    });
  }
  for (let latitude = -60; latitude <= 60; latitude += 30) {
    const coordinates: Array<[number, number]> = [];
    for (let longitude = -180; longitude <= 180; longitude += 4) {
      coordinates.push([longitude, latitude]);
    }
    features.push({
      type: "Feature",
      properties: { kind: latitude === 0 ? "equator" : "latitude" },
      geometry: { type: "LineString", coordinates },
    });
  }
  return { type: "FeatureCollection", features };
}

export const offlineGraticule = buildGraticule();

export function rasterSourceBounds(
  bounds: [number, number, number, number],
): [number, number, number, number] {
  const [west, south, east, north] = bounds;
  return west > east ? [west, south, east + 360, north] : bounds;
}

export function mapStyle(
  basemapEnabled: boolean,
  manifest: JobMapManifest | null = null,
  terrainStyle: MapTileStyle = "terrain",
): StyleSpecification {
  const sources: StyleSpecification["sources"] = {
    countries: { type: "geojson", data: offlineCountries },
    graticule: { type: "geojson", data: offlineGraticule },
    aoi: { type: "geojson", data: emptyCollection() },
  };
  if (basemapEnabled) {
    sources.osm = {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
      maxzoom: 19,
    };
  }
  if (manifest) {
    sources["job-terrain"] = {
      type: "raster",
      tiles: [manifest.tile_url_template.replace("{style}", terrainStyle)],
      tileSize: manifest.tile_size,
      minzoom: manifest.minzoom,
      maxzoom: manifest.maxzoom,
      bounds: rasterSourceBounds(manifest.bounds_wgs84),
      attribution: manifest.attribution,
    };
    sources["manufacturing-tiles"] = {
      type: "geojson",
      data: manifest.tile_footprints_geojson,
    };
  }
  const referenceLayers: StyleSpecification["layers"] = basemapEnabled
    ? [{ id: "osm", type: "raster", source: "osm" }]
    : [
        {
          id: "land",
          type: "fill",
          source: "countries",
          paint: { "fill-color": "#d4ddd1", "fill-opacity": 1 },
        },
        {
          id: "country-borders",
          type: "line",
          source: "countries",
          paint: { "line-color": "#8d9d97", "line-width": 0.65 },
        },
        {
          id: "graticule",
          type: "line",
          source: "graticule",
          paint: {
            "line-color": "#a9bbb8",
            "line-width": 0.55,
            "line-opacity": 0.72,
          },
        },
      ];
  const terrainLayers: StyleSpecification["layers"] = manifest
    ? [
        {
          id: "job-terrain",
          type: "raster",
          source: "job-terrain",
          paint: { "raster-opacity": 0.96 },
        },
        {
          id: "manufacturing-tile-fill",
          type: "fill",
          source: "manufacturing-tiles",
          paint: { "fill-color": "#0f766e", "fill-opacity": 0.08 },
        },
        {
          id: "manufacturing-tile-line",
          type: "line",
          source: "manufacturing-tiles",
          paint: { "line-color": "#174f4a", "line-width": 1.5 },
        },
        {
          id: "manufacturing-tile-selected",
          type: "fill",
          source: "manufacturing-tiles",
          filter: ["==", ["get", "tile_id"], ""],
          paint: {
            "fill-color": "#b85d2d",
            "fill-opacity": 0.36,
            "fill-outline-color": "#8e431f",
          },
        },
      ]
    : [];
  return {
    version: 8,
    sources,
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#c9dde1" },
      },
      ...referenceLayers,
      ...terrainLayers,
      {
        id: "aoi-fill",
        type: "fill",
        source: "aoi",
        paint: { "fill-color": "#0f766e", "fill-opacity": 0.2 },
      },
      {
        id: "aoi-line",
        type: "line",
        source: "aoi",
        paint: { "line-color": "#0f5f59", "line-width": 2.5 },
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
  manifest,
  selectedTileId,
  visualizationLoading,
  visualizationError,
  onSelectedTileChange,
  onBboxChange,
  onCenterChange,
}: MapPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const styleIdentityRef = useRef("");
  const [cursor, setCursor] = useState<[number, number] | null>(null);
  const [draft, setDraft] = useState<Geometry | null>(null);
  const [terrainStyle, setTerrainStyle] = useState<MapTileStyle>(
    manifest?.default_style ?? "terrain",
  );
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
    styleIdentityRef.current =
      `${basemapEnabled}:${manifest?.cache_key ?? "none"}:${terrainStyle}`;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: mapStyle(basemapEnabled, manifest, terrainStyle),
      center: manifest?.center_wgs84 ?? [20, 25],
      zoom: manifest ? manifest.minzoom : 1.4,
      attributionControl: false,
      renderWorldCopies: true,
      canvasContextAttributes: { preserveDrawingBuffer: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");
    map.addControl(
      new maplibregl.AttributionControl({ compact: true }),
      "bottom-right",
    );
    map.on("load", () =>
      (map.getSource("aoi") as GeoJSONSource | undefined)?.setData(collection),
    );
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
    setTerrainStyle(manifest?.default_style ?? "terrain");
  }, [manifest?.job_id, manifest?.default_style]);

  useEffect(() => {
    const map = mapRef.current;
    const identity = `${basemapEnabled}:${manifest?.cache_key ?? "none"}:${terrainStyle}`;
    if (!map || styleIdentityRef.current === identity) {
      return;
    }
    styleIdentityRef.current = identity;
    map.setStyle(mapStyle(basemapEnabled, manifest, terrainStyle));
    map.once("style.load", () =>
      (map.getSource("aoi") as GeoJSONSource | undefined)?.setData(collection),
    );
  }, [basemapEnabled, manifest, terrainStyle]);

  useEffect(() => {
    const source = mapRef.current?.getSource("aoi") as GeoJSONSource | undefined;
    source?.setData(collection);
  }, [collection]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const updateSelection = () => {
      if (map.getLayer("manufacturing-tile-selected")) {
        map.setFilter("manufacturing-tile-selected", [
          "==",
          ["get", "tile_id"],
          selectedTileId ?? "",
        ]);
      }
    };
    if (map.isStyleLoaded()) {
      updateSelection();
      return;
    }
    map.once("style.load", updateSelection);
    return () => {
      map.off("style.load", updateSelection);
    };
  }, [selectedTileId, manifest?.cache_key, terrainStyle, basemapEnabled]);

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
    if (!map || !manifest) {
      return;
    }
    const [west, south, east, north] = manifest.bounds_wgs84;
    const adjustedEast = west > east ? east + 360 : east;
    map.fitBounds(
      [
        [west, south],
        [adjustedEast, north],
      ],
      {
        padding: 54,
        maxZoom: Math.min(manifest.maxzoom + 5, 20),
        duration: 500,
      },
    );
  }, [manifest?.cache_key]);

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

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const selectTile = (event: MapMouseEvent) => {
      if (drawMode || !map.getLayer("manufacturing-tile-fill")) {
        return;
      }
      const hit = map.queryRenderedFeatures(event.point, {
        layers: ["manufacturing-tile-fill"],
      })[0];
      const tileId = hit?.properties?.tile_id;
      if (typeof tileId === "string") {
        onSelectedTileChange(tileId);
      }
    };
    map.on("click", selectTile);
    return () => {
      map.off("click", selectTile);
    };
  }, [drawMode, onSelectedTileChange]);

  return (
    <div
      className="map-shell"
      data-testid="map-panel"
      data-offline-reference="natural-earth-countries-and-graticule"
      data-has-terrain={manifest ? "true" : "false"}
      data-tile-style={terrainStyle}
    >
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
      {manifest && (
        <div className="map-layer-control" aria-label={translate(language, "mapLayers")}>
          <span>
            <Layers3 size={14} />
            {translate(language, "mapLayers")}
          </span>
          <div className="segmented three">
            {manifest.styles.map((style) => (
              <button
                type="button"
                key={style}
                className={terrainStyle === style ? "active" : ""}
                onClick={() => setTerrainStyle(style)}
              >
                {translate(
                  language,
                  style === "terrain"
                    ? "mapTerrain"
                    : style === "elevation"
                      ? "mapElevation"
                      : "mapHillshade",
                )}
              </button>
            ))}
          </div>
          <small>
            {manifest.elevation_min_m.toFixed(1)}–{manifest.elevation_max_m.toFixed(1)} m
          </small>
        </div>
      )}
      {(visualizationLoading || visualizationError) && (
        <div className={`map-data-status${visualizationError ? " error" : ""}`}>
          {visualizationError ?? translate(language, "visualizationLoading")}
        </div>
      )}
      {manifest && selectedTileId && (
        <code className="map-selected-tile">
          {translate(language, "selectedTile")}: {selectedTileId}
        </code>
      )}
      {cursor && (
        <code className="map-coordinate">
          {cursor[0].toFixed(5)}, {cursor[1].toFixed(5)}
        </code>
      )}
    </div>
  );
}
