from __future__ import annotations

"""Offline tests for the eight hazard assessors.

Each assessor is fed a literal EvidenceContext, so no network or spatial store is
touched. The assertions target the two guarantees this build owes the user: scores must
follow the measured evidence, and an absent dataset must show up as a declared gap that
lowers confidence rather than as an imputed value.
"""

import pytest

from app.services.hazard import ASSESSORS, HAZARDS, assess_context
from app.services.risk import band_for
from _hazard_fixtures import empty_context, factor, full_context, terrain_summary


def test_every_hazard_has_an_assessor():
    assert len(HAZARDS) == 8
    assert set(ASSESSORS) == set(HAZARDS)
    signals = assess_context(full_context())
    assert [s.hazard for s in signals] == list(HAZARDS)


def test_all_eight_assessors_run_on_a_complete_context():
    for signal in assess_context(full_context()):
        payload = signal.as_dict()
        assert 0.0 <= payload["score"] <= 100.0
        assert payload["band"] == band_for(payload["score"])
        assert signal.has_evidence is True
        assert payload["factors"], signal.hazard


def test_a_source_that_never_answered_is_declared_not_imputed():
    for signal in assess_context(empty_context()):
        payload = signal.as_dict()
        assert payload["band"] == "unassessed", signal.hazard
        assert payload["confidence"] == 0.0
        assert payload["data_completeness"]["factors_available"] == 0
        # Nothing may publish a measured severity it did not measure.
        assert all(f.available is False for f in signal.factors)
        assert all("severity" not in f.as_dict() for f in signal.factors)


def test_missing_layers_lower_confidence_without_changing_the_observed_score():
    """The BIS zoning gap is always present; one more gap must only cut confidence."""
    with_usgs = factor(assess_context(full_context(), ["earthquake"])[0], "regional_seismicity_density")
    blind = empty_context(source_status={**full_context().source_status, "usgs_earthquakes": "unavailable"})
    signal = assess_context(blind, ["earthquake"])[0]
    assert with_usgs.available is True
    blind_factor = factor(signal, "regional_seismicity_density")
    assert blind_factor is not None and blind_factor.available is False
    assert signal.confidence < 0.5
    assert "usgs_earthquakes" in signal.unavailable_sources


def test_terrain_summary_of_none_does_not_crash():
    """Regression: terrain_at() carries summary=None when no tile could be retrieved."""
    ctx = empty_context(source_status={**full_context().source_status, "dem": "unavailable"})
    for signal in assess_context(ctx):
        assert factor(signal, "slope") is None or factor(signal, "slope").available is False


def test_earthquake_uses_historical_seismicity_and_flags_the_zoning_gap():
    signal = assess_context(full_context(), ["earthquake"])[0]
    assert signal.evidence_kind == "historical"
    assert factor(signal, "maximum_historical_magnitude").observed == {"max_magnitude": 5.4}
    assert "bis_seismic_zoning" in signal.unavailable_sources
    assert any("not a forecast" in w for w in signal.warnings)
    assert signal.provenance == [{"provider": "usgs_earthquakes", "endpoint": "https://earthquake.usgs.gov/fdsnws/event/1/query"}]


def test_quiet_region_scores_low_but_still_reports_the_zoning_gap():
    ctx = full_context(seismicity={"total_events": 0, "events_within_150km": 0, "nearest_event_km": None, "max_magnitude": None, "max_magnitude_within_150km": None, "window_years": 50})
    signal = assess_context(ctx, ["earthquake"])[0]
    assert signal.score == 0.0
    assert signal.confidence == pytest.approx(3.4 / 4.9, abs=1e-3)
    assert signal.as_dict()["band"] == "low"


def test_flood_rises_with_rainfall_and_depression():
    dry = full_context(
        weather_recent={"series": [{"date": "2026-09-01", "precipitation_mm": 0.0}] * 20},
        weather_forecast={"daily": {"precipitation_sum": [0.0] * 7}},
        terrain={"available": True, "summary": terrain_summary(elevation_at_point_m=21.0)},
    )
    wet = full_context(
        weather_recent={"series": [{"date": f"2026-09-{day:02d}", "precipitation_mm": 20.0} for day in range(1, 21)]},
        weather_forecast={"daily": {"precipitation_sum": [30.0] * 7}},
        terrain={"available": True, "summary": terrain_summary(elevation_at_point_m=2.0, local_low_point=True)},
    )
    dry_signal = assess_context(dry, ["flood"])[0]
    wet_signal = assess_context(wet, ["flood"])[0]
    assert wet_signal.score > dry_signal.score
    assert factor(wet_signal, "terrain_depression").severity == 1.0
    assert factor(dry_signal, "terrain_depression").severity == 0.0
    assert "river_floodplain" in wet_signal.unavailable_sources


def test_terrain_depression_is_measured_from_the_window_not_a_constant():
    """A fixed severity per branch would report an invented number as if the DEM said it."""
    mid = assess_context(full_context(), ["flood"])[0]
    f = factor(mid, "terrain_depression")
    assert f.severity == pytest.approx(1.0 - (6.5 - 2.0) / (21.0 - 2.0), abs=1e-4)
    assert f.observed["window_min_m"] == 2.0 and f.observed["window_max_m"] == 21.0
    assert f.observed["local_low_point"] is False


def test_a_flat_dem_window_is_scored_neutrally_and_says_so():
    ctx = full_context(terrain={"available": True, "summary": terrain_summary(elevation_min_m=9.0, elevation_max_m=9.0, elevation_relief_m=0.0)})
    f = factor(assess_context(ctx, ["flood"])[0], "terrain_depression")
    assert f.severity == 0.5
    assert f.note and "scored neutrally" in f.note


def test_a_terrain_without_point_elevation_is_a_gap_not_a_number():
    ctx = full_context(terrain={"available": True, "summary": terrain_summary(elevation_at_point_m=None)})
    signal = assess_context(ctx, ["flood"])[0]
    f = factor(signal, "terrain_depression")
    assert f.available is False
    assert f.severity == 0.0
    assert "dem" in signal.unavailable_sources


def test_coastal_flood_is_a_proxy_and_says_so():
    signal = assess_context(full_context(), ["coastal_flood"])[0]
    assert factor(signal, "coastal_proximity").observed == {"distance_to_india_coastline_km": 1.7}
    assert "storm_surge_model" in signal.unavailable_sources
    assert any("no surge height is modelled" in w for w in signal.warnings)


def test_bathymetry_only_windows_are_not_used_as_land_elevation():
    ctx = full_context(terrain={"available": True, "summary": terrain_summary(elevation_at_point_m=-2_086.0, bathymetry_only=True, elevation_max_m=-1_900.0)})
    signal = assess_context(ctx, ["coastal_flood"])[0]
    gap = factor(signal, "low_elevation")
    assert gap.available is False
    assert "dem" in signal.unavailable_sources


def test_shoreline_points_disclose_that_their_own_pixel_is_not_land():
    """Measured live: at 92.5715 11.6234 the 2 km window is wholly sea and the 5 km window
    mixes water with land, so a 0 m point reading has to be labelled for what it is."""
    ctx = full_context(
        terrain={"available": True, "summary": terrain_summary(elevation_at_point_m=0.0, elevation_min_m=-1.7, elevation_max_m=61.8)}
    )
    signal = assess_context(ctx, ["coastal_flood"])[0]
    low = factor(signal, "low_elevation")
    assert low.available is True
    assert low.severity == 1.0
    assert low.note and "intertidal water" in low.note
    assert any("intertidal water" in w for w in signal.warnings)
    heat = assess_context(ctx, ["heatwave"])[0]
    assert "not a measured land height" in factor(heat, "altitude_mitigation").note
    # Inland flood reads the same pixel, so it carries the same disclosure.
    wet = assess_context(ctx, ["flood"])[0]
    assert "intertidal water" in factor(wet, "terrain_depression").note
    assert any("intertidal water" in w for w in wet.warnings)
    # A genuinely elevated point gets no such caveat.
    assert factor(assess_context(full_context(), ["coastal_flood"])[0], "low_elevation").note is None


def test_landslide_follows_slope_and_relief():
    flat = full_context(terrain={"available": True, "summary": terrain_summary(slope_deg=0.4, elevation_relief_m=5.0)})
    hill = full_context(terrain={"available": True, "summary": terrain_summary(slope_deg=28.0, elevation_relief_m=420.0, elevation_mean_m=900.0, elevation_at_point_m=900.0)})
    flat_signal = assess_context(flat, ["landslide"])[0]
    hill_signal = assess_context(hill, ["landslide"])[0]
    assert hill_signal.score > flat_signal.score
    assert hill_signal.details["applicable"] is True
    assert flat_signal.details["applicable"] is False
    assert "geology_landcover" in hill_signal.unavailable_sources


def test_heatwave_separates_observed_forecast_from_station_data():
    signal = assess_context(full_context(), ["heatwave"])[0]
    assert factor(signal, "forecast_heat").observed == {"forecast_tmax_c": 36.2}
    assert factor(signal, "recent_heat_basis").observed == {"recent_tmax_c": 34.0}
    assert any("not a weather-station observation" in w for w in signal.warnings)


def test_drought_rises_when_rain_and_soil_moisture_fall():
    wet = assess_context(full_context(), ["drought"])[0]
    dry_ctx = full_context(
        weather_recent={"series": [{"date": f"2026-09-{day:02d}", "precipitation_mm": 0.0, "tmax_c": 41.0, "soil_moisture_m3m3": 0.04} for day in range(1, 41)]},
        weather_forecast={"daily": {"precipitation_sum": [0.0] * 7, "temperature_2m_max": [44.0] * 7}},
    )
    dry = assess_context(dry_ctx, ["drought"])[0]
    assert dry.score > wet.score
    assert factor(dry, "precipitation_deficit").severity == 1.0
    assert factor(dry, "root_zone_soil_moisture").severity == 1.0
    assert set(["ndvi_vegetation", "groundwater"]) <= set(dry.unavailable_sources)


def test_wildfire_without_a_firms_key_reports_no_detections():
    ctx = full_context(source_status={**full_context().source_status, "firms": "unconfigured"}, fires={})
    signal = assess_context(ctx, ["wildfire"])[0]
    gap = factor(signal, "active_fire_detection")
    assert gap.available is False
    assert "not configured" in gap.note
    assert "FIRMS_MAP_KEY" in gap.note
    assert "firms" in signal.unavailable_sources
    assert signal.evidence_kind == "model"
    assert all(f.name != "active_fire_detection" or f.available is False for f in signal.factors)


def test_wildfire_with_detections_is_labelled_observed():
    signal = assess_context(full_context(fires={"count": 40}), ["wildfire"])[0]
    assert signal.evidence_kind == "observed"
    assert factor(signal, "active_fire_detection").observed == {"hotspots_7d": 40}
    assert factor(signal, "active_fire_detection").severity == 1.0


def test_no_factor_names_an_invented_source():
    """Every driver must trace to a provider this build actually has, or a declared gap."""
    known = {
        "open_meteo",
        "usgs_earthquakes",
        "dem",
        "coastline",
        "firms",
        "not_loaded",
        "imd_alerts",
        "cwc_gauges",
        "bhuvan_hazards",
    }
    for ctx in (full_context(), empty_context()):
        for signal in assess_context(ctx):
            for factor in signal.factors:
                assert factor.source in known, f"{signal.hazard}/{factor.name} cites {factor.source}"


def test_inland_site_loses_coastal_exposure_factor():
    ctx = full_context(coast_distance_m=None, coast_measured=False, source_status={**full_context().source_status, "coastline": "unavailable"})
    cyclone = assess_context(ctx, ["cyclone"])[0]
    assert factor(cyclone, "coastal_exposure").available is False
    assert "coastline" in cyclone.unavailable_sources


def test_a_point_far_from_the_sea_is_measured_not_missing():
    """No coastline inside the search radius is an observation, so confidence must stay."""
    ctx = full_context(coast_distance_m=None, coast_measured=True)
    cyclone = assess_context(ctx, ["cyclone"])[0]
    exposure = factor(cyclone, "coastal_exposure")
    assert exposure.available is True
    assert exposure.severity == 0.0
    assert "coastline" not in cyclone.unavailable_sources
    assert exposure.observed == {"distance_to_india_coastline_km": None, "at_least_km": 1000.0}


def test_hazard_selection_skips_unknown_names():
    signals = assess_context(full_context(), ["flood", "tsunami", "flood"])
    assert [s.hazard for s in signals] == ["flood", "flood"]


def test_assessors_stay_deterministic_for_identical_evidence():
    first = [s.as_dict() for s in assess_context(full_context())]
    second = [s.as_dict() for s in assess_context(full_context())]
    assert first == second
