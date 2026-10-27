from __future__ import annotations

"""Offline tests for the recommendation rules.

The engine must stay subordinate to the evidence: it may only speak when a hazard was
actually assessed, it must quote the measured values it is acting on, and it must never
present a rule as an engineering judgement or invent an authority URL.
"""

from urllib.parse import urlparse

import pytest

from app.services.hazard import assess_context
from app.services.recommendations import AUTHORITIES, BAND_LABELS, recommend, recommendations_for
from app.services.risk import Factor, HazardSignal

from _hazard_fixtures import full_context, terrain_summary


def allowed(category: str) -> bool:
    return category in {"preparedness", "structural", "monitoring", "policy", "response"}


def test_unassessed_hazard_generates_no_advice():
    """No evidence must not produce confident instructions."""
    signal = HazardSignal(hazard="wildfire", factors=[Factor("detections", "firms", available=False, weight=1.6)])
    assert recommendations_for(signal) == []


def test_low_band_hazard_generates_no_advice():
    ctx = full_context(
        seismicity={"total_events": 0, "events_within_150km": 0, "nearest_event_km": None, "max_magnitude": None, "max_magnitude_within_150km": None},
        weather_recent={"series": [{"date": "2026-09-01", "precipitation_mm": 0.0, "tmax_c": 20.0, "soil_moisture_m3m3": 0.35}]},
        weather_forecast={"daily": {"precipitation_sum": [0.0] * 7, "temperature_2m_max": [21.0] * 7, "wind_speed_10m_max": [5.0] * 7, "wind_gusts_10m_max": [8.0] * 7}},
        coast_distance_m=None,
        terrain={"available": True, "summary": terrain_summary(slope_deg=0.5, elevation_at_point_m=400.0, elevation_relief_m=6.0)},
    )
    earthquake = assess_context(ctx, ["earthquake"])[0]
    assert earthquake.as_dict()["band"] == "low"
    assert recommendations_for(earthquake) == []


def test_high_risk_hazard_returns_graded_actions():
    ctx = full_context(
        weather_recent={"series": [{"date": f"2026-09-{day:02d}", "precipitation_mm": 25.0, "soil_moisture_m3m3": 0.3} for day in range(1, 30)]},
        weather_forecast={"daily": {"precipitation_sum": [40.0] * 7, "temperature_2m_max": [30.0] * 7}},
        terrain={"available": True, "summary": terrain_summary(local_low_point=True, elevation_at_point_m=2.0)},
    )
    signal = assess_context(ctx, ["flood"])[0]
    rows = recommendations_for(signal)
    assert rows, "a high flood signal must produce guidance"
    assert all(allowed(r["category"]) for r in rows)
    assert {r["horizon"] for r in rows} <= {"immediate", "short_term", "long_term"}
    assert any(r["category"] == "structural" and r["requires_professional_assessment"] for r in rows)
    assert all(r["evidence"]["band"] == signal.as_dict()["band"] for r in rows)


def test_rationale_quotes_measured_values_not_generic_text():
    ctx = full_context(weather_forecast={"daily": {"precipitation_sum": [0.0] * 6, "temperature_2m_max": [46.5] * 7}})
    signal = assess_context(ctx, ["heatwave"])[0]
    rows = recommendations_for(signal)
    assert rows
    joined = " ".join(r["rationale"] for r in rows)
    assert "forecast_tmax_c=46.5" in joined
    prefix = rows[0]["rationale"].split(" heatwave risk.")[0]
    assert prefix in BAND_LABELS.values(), "band label must be human-readable, not a snake_case key"


def test_structural_actions_always_flag_professional_assessment():
    ctx = full_context(
        terrain={"available": True, "summary": terrain_summary(slope_deg=32.0, elevation_relief_m=600.0, elevation_mean_m=1100.0, elevation_at_point_m=1100.0)},
        weather_recent={"series": [{"date": f"2026-09-{day:02d}", "precipitation_mm": 40.0} for day in range(1, 30)]},
        weather_forecast={"daily": {"precipitation_sum": [60.0] * 7}},
    )
    rows = recommendations_for(assess_context(ctx, ["landslide"])[0])
    assert rows
    for row in rows:
        if row["category"] == "structural":
            assert row["requires_professional_assessment"] is True
            assert "licensed professional" in row["limitations"]
        else:
            assert row["requires_professional_assessment"] is False


def test_authority_links_are_homepages_only():
    """Deep document paths are never guessed, so every cited URL must be a bare origin."""
    for key, (name, url) in AUTHORITIES.items():
        assert name, key
        if url is None:
            continue
        parsed = urlparse(url)
        assert parsed.scheme in {"http", "https"}, key
        assert parsed.netloc, key
        assert parsed.path in {"", "/"}, f"{key} cites a deep path: {url}"


def test_every_recommendation_cites_a_known_authority():
    ctx = full_context(
        weather_recent={"series": [{"date": f"2026-09-{day:02d}", "precipitation_mm": 30.0, "tmax_c": 44.0, "soil_moisture_m3m3": 0.06} for day in range(1, 40)]},
        weather_forecast={"daily": {"precipitation_sum": [35.0] * 7, "temperature_2m_max": [45.0] * 7, "wind_speed_10m_max": [60.0] * 7, "wind_gusts_10m_max": [95.0] * 7}},
        coast_distance_m=800.0,
        terrain={"available": True, "summary": terrain_summary(local_low_point=True, elevation_at_point_m=1.5)},
    )
    rows = recommend(assess_context(ctx))
    assert rows
    names = {name for name, _ in AUTHORITIES.values()}
    for row in rows:
        assert row["authority"] in names
        assert row["method"] == "rule_engine"
        assert 0.0 <= row["score"] <= 100.0


def test_recommendations_are_ordered_by_risk_severity():
    ctx = full_context(
        terrain={"available": True, "summary": terrain_summary(slope_deg=33.0, elevation_relief_m=700.0, elevation_mean_m=1200.0, elevation_at_point_m=1200.0)},
        coast_distance_m=None,
        source_status={**full_context().source_status, "coastline": "unavailable"},
    )
    rows = recommend(assess_context(ctx))
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
