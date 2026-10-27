from __future__ import annotations

import math

import pytest

from app.core.errors import InvalidCoordinates
from app.gis.coords import (
    INDIA_BBOX,
    LatLng,
    haversine_m,
    is_indian_bbox,
    meters_to_degrees,
    parse_coordinates,
    parse_radius_m,
    validate_latlon,
)


@pytest.mark.parametrize(
    "raw, lat, lon",
    [
        ("20.5937, 78.9629", 20.5937, 78.9629),
        ("20.5937,78.9629", 20.5937, 78.9629),
        ("20.5937 78.9629", 20.5937, 78.9629),
        ("20.5937; 78.9629", 20.5937, 78.9629),
        ("20.5937N 78.9629E", 20.5937, 78.9629),
        ("-6.9271, 79.8612", -6.9271, 79.8612),
        ("20.5937, 78.9629.", 20.5937, 78.9629),
        ("  20.5937 ,  78.9629  ", 20.5937, 78.9629),
    ],
)
def test_decimal_pairs(raw, lat, lon):
    point = parse_coordinates(raw)
    assert point.lat == pytest.approx(lat)
    assert point.lon == pytest.approx(lon)


def test_dms_with_symbols():
    point = parse_coordinates("20°35'37\"N 78°57'46\"E")
    assert point.lat == pytest.approx(20.5936, abs=1e-3)
    assert point.lon == pytest.approx(78.9628, abs=1e-3)


def test_dms_spaced_without_symbols():
    point = parse_coordinates("18 31 20 N 75 45 12 E")
    assert point.lat == pytest.approx(18.5222, abs=1e-3)
    assert point.lon == pytest.approx(75.7533, abs=1e-3)


def test_southern_and_western_hemispheres_are_negative():
    point = parse_coordinates("6.9271S 79.8612W")
    assert point.lat == pytest.approx(-6.9271)
    assert point.lon == pytest.approx(-79.8612)


def test_longitude_first_input_is_rejected_with_a_suggestion():
    # 120 could never be a latitude, so the only readable ordering is longitude first.
    with pytest.raises(InvalidCoordinates) as excinfo:
        parse_coordinates("120.5937, 78.9629")
    assert "longitude first" in excinfo.value.message
    assert excinfo.value.details["suggested"] == "78.962900, 120.593700"


def test_an_ambiguous_pair_is_accepted_as_latitude_first():
    # 78.96 and 120.59 are both legal, so no reordering hint is invented.
    point = parse_coordinates("78.9629, 120.5937")
    assert point.lat == pytest.approx(78.9629)
    assert point.lon == pytest.approx(120.5937)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Nagpur",
        "20.5937",
        "20.5937, 78.9629, 100",
        "1000, 2000",
        "20.5937, 190.5",
        None,
    ],
)
def test_unusable_inputs_raise_a_clear_error(raw):
    with pytest.raises(InvalidCoordinates):
        parse_coordinates(raw)


def test_validate_latlon_rejects_nan_and_infinity():
    for lat, lon in ((float("nan"), 78.0), (20.0, float("inf")), (91.0, 78.0), (20.0, -181.0)):
        with pytest.raises(InvalidCoordinates):
            validate_latlon(lat, lon)


def test_latlng_string_form_is_machine_readable():
    point = parse_coordinates("20.5937, 78.9629")
    assert str(point) == "20.593700, 78.962900"
    assert point.as_tuple() == (78.9629, 20.5937)


def test_within_india_uses_the_national_envelope():
    assert LatLng(20.5937, 78.9629).within_india() is True
    assert LatLng(-6.9, 79.86).within_india() is False  # Diego Garcia
    assert LatLng(28.6, 77.2).within_india() is True


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 5000.0),
        (2500, 2500.0),
        ("2500", 2500.0),
        ("3 km", 3000.0),
        ("1.5km", 1500.0),
        ("2mi", 3218.688),
        ("5000 m", 5000.0),
    ],
)
def test_radius_accepts_units(raw, expected):
    assert parse_radius_m(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["0.5 m", "500 km", "100 mi", "furlong", "abc", "12,000"])
def test_out_of_range_or_unknown_radius_units_are_refused(raw):
    with pytest.raises(InvalidCoordinates):
        parse_radius_m(raw)


def test_meters_to_degrees_widens_near_the_equator_and_shrinks_at_height():
    dlon_eq, dlat_eq = meters_to_degrees(0.0, 10_000)
    dlon_high, dlat_high = meters_to_degrees(60.0, 10_000)
    assert dlat_eq == pytest.approx(dlat_high)
    assert dlon_high == pytest.approx(dlon_eq * 2.0, rel=0.01)


def test_haversine_matches_a_known_city_pair_distance():
    # Delhi to Mumbai is roughly 1150 km great-circle.
    distance = haversine_m(28.6139, 77.2090, 19.0760, 72.8777)
    assert 1_130_000 < distance < 1_180_000


def test_haversine_is_zero_for_the_same_point():
    assert haversine_m(20.5937, 78.9629, 20.5937, 78.9629) == pytest.approx(0.0)


def test_is_indian_bbox_only_needs_overlap():
    assert is_indian_bbox(77.0, 20.0, 78.0, 21.0) is True
    assert is_indian_bbox(70.0, 30.0, 71.0, 31.0) is True  # Gujarat/Rajasthan edge
    assert is_indian_bbox(100.0, 10.0, 101.0, 11.0) is False  # Myanmar/Thailand
    assert is_indian_bbox(60.0, 5.0, 65.0, 10.0) is False  # Arabian Sea, west of the envelope


def test_envelope_covers_the_country_extent():
    assert INDIA_BBOX[0] < 68.2 and INDIA_BBOX[1] < 7.0  # Gujarat west, Kerala south
    assert INDIA_BBOX[2] > 97.0 and INDIA_BBOX[3] > 36.0  # Arunachal east, Ladakh north


def test_extreme_latitude_does_not_divide_by_zero():
    dlon, dlat = meters_to_degrees(89.9999, 1000)
    assert math.isfinite(dlon) and math.isfinite(dlat)
