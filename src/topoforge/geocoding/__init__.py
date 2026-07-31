"""Place geocoding with explicit ambiguity handling."""

from topoforge.geocoding.nominatim import (
    NominatimConfig,
    NominatimGeocoder,
    PlaceCandidate,
    PlaceCandidateSelectionError,
    PlaceSearchResult,
    place_candidate_aoi_input,
    select_place_candidate,
)

__all__ = [
    "NominatimConfig",
    "NominatimGeocoder",
    "PlaceCandidate",
    "PlaceCandidateSelectionError",
    "PlaceSearchResult",
    "place_candidate_aoi_input",
    "select_place_candidate",
]
