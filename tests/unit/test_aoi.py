import pytest
from pydantic import ValidationError
from pyproj import CRS

from topoforge.models import AreaOfInterestInput
from topoforge.raster import normalize_area_of_interest


def test_bbox_aoi_normalizes_to_wgs84_geometry_and_utm() -> None:
    result = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(101.5, 29.4, 101.9, 29.8)))

    assert result.kind == "bbox"
    assert result.bounds_wgs84 == (101.5, 29.4, 101.9, 29.8)
    assert result.crosses_antimeridian is False
    assert result.area_m2 > 0
    assert CRS.from_user_input(result.target_local_crs).to_epsg() == 32647


def test_center_radius_aoi_records_input_and_geodesic_bounds() -> None:
    result = normalize_area_of_interest(
        AreaOfInterestInput(center_wgs84=(101.8, 29.6), radius_m=10_000.0)
    )

    assert result.kind == "center-radius"
    assert result.user_input == {"center_wgs84": [101.8, 29.6], "radius_m": 10_000.0}
    west, south, east, north = result.bounds_wgs84
    assert west < 101.8 < east
    assert south < 29.6 < north
    assert "geodesic" in result.normalization_method


def test_cross_zone_high_latitude_and_antimeridian_use_local_aeqd() -> None:
    cross_zone = normalize_area_of_interest(AreaOfInterestInput(bbox_wgs84=(5.9, 45.0, 6.1, 45.2)))
    high_latitude = normalize_area_of_interest(
        AreaOfInterestInput(bbox_wgs84=(20.0, 84.5, 21.0, 85.0))
    )
    antimeridian = normalize_area_of_interest(
        AreaOfInterestInput(bbox_wgs84=(170.0, -10.0, -170.0, 10.0))
    )

    assert CRS.from_user_input(cross_zone.target_local_crs).to_epsg() is None
    assert CRS.from_user_input(high_latitude.target_local_crs).to_epsg() is None
    assert antimeridian.crosses_antimeridian is True
    assert antimeridian.normalized_geometry_geojson["type"] == "MultiPolygon"
    assert CRS.from_user_input(antimeridian.target_local_crs).to_epsg() is None


@pytest.mark.parametrize(
    "aoi_request",
    [
        {"bbox_wgs84": (-181.0, 0.0, 1.0, 2.0)},
        {"bbox_wgs84": (0.0, -91.0, 1.0, 2.0)},
        {"bbox_wgs84": (0.0, 2.0, 1.0, 1.0)},
        {"center_wgs84": (0.0, 0.0), "radius_m": -1.0},
    ],
)
def test_invalid_aoi_inputs_are_rejected(aoi_request: dict[str, object]) -> None:
    with pytest.raises((ValueError, ValidationError)):
        normalize_area_of_interest(AreaOfInterestInput.model_validate(aoi_request))
