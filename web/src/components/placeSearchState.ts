import type { PlaceCandidate, SavedPlace } from "../types";

export const SAVED_PLACES_KEY = "topoforge-saved-places-v1";
export const SEARCH_SERVICE_KEY = "topoforge-online-place-service-v1";

export function validPlace(value: unknown): value is PlaceCandidate {
  if (!value || typeof value !== "object") return false;
  const place = value as PlaceCandidate;
  const bounds = place.bounding_box_wgs84;
  return typeof place.candidate_id === "string" && place.candidate_id.length <= 200 &&
    typeof place.display_name === "string" && place.display_name.length > 0 && place.display_name.length <= 1000 &&
    Number.isFinite(place.longitude) && Math.abs(place.longitude) <= 180 &&
    Number.isFinite(place.latitude) && Math.abs(place.latitude) <= 90 &&
    Array.isArray(bounds) && bounds.length === 4 && bounds.every(Number.isFinite) &&
    Math.abs(bounds[0]) <= 180 && Math.abs(bounds[2]) <= 180 && bounds[0] !== bounds[2] &&
    bounds[1] >= -90 && bounds[3] <= 90 && bounds[1] < bounds[3];
}

export function readSavedPlaces(): SavedPlace[] {
  try {
    const raw = localStorage.getItem(SAVED_PLACES_KEY);
    if (!raw || raw.length > 150_000) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(0, 70).filter((item): item is SavedPlace =>
      item && typeof item === "object" && typeof item.endpoint === "string" &&
      item.endpoint.length <= 1000 && typeof item.favorite === "boolean" && validPlace(item.place));
  } catch { return []; }
}

export function savedPlaceKey(item: SavedPlace): string {
  return `${item.endpoint}:${item.place.candidate_id}`;
}

export function rememberPlace(previous: SavedPlace[], next: SavedPlace): SavedPlace[] {
  const key = savedPlaceKey(next);
  const existing = previous.find(item => savedPlaceKey(item) === key);
  const all = [{ ...next, favorite: existing?.favorite ?? next.favorite },
    ...previous.filter(item => savedPlaceKey(item) !== key)];
  let recent = 0, favorites = 0;
  return all.filter(item => item.favorite ? ++favorites <= 50 : ++recent <= 20);
}

/** Explicit longitude, latitude input; invalid numeric pairs must not become online queries. */
export function coordinatePlace(query: string): PlaceCandidate | null {
  const number = "[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)";
  const match = query.trim().match(new RegExp(`^(${number})(?:\\s*[,，]\\s*|\\s+)(${number})$`));
  if (!match) return null;
  const longitude = Number(match[1]), latitude = Number(match[2]);
  if (Math.abs(longitude) > 180 || Math.abs(latitude) > 85.051129) throw new Error("coordinates");
  return {
    candidate_id: `coordinates:${longitude},${latitude}`,
    display_name: `${longitude}, ${latitude}`,
    longitude, latitude,
    bounding_box_wgs84: [Math.max(-180, longitude - .01), latitude - .01,
      Math.min(180, longitude + .01), latitude + .01],
  };
}
