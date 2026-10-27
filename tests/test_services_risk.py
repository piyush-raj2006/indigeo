from __future__ import annotations

"""Offline tests for the pure scoring primitives.

The rules under test are the honesty guarantees: a missing source lowers confidence
instead of imputing a value, and a hazard with nothing observed is never reported as
"low risk".
"""

import pytest

from app.services.risk import Factor, HazardSignal, band_for, overall_band, rank_severity, saturate


def test_band_thresholds_are_ordinal_and_exhaustive():
    assert [band_for(v) for v in (0, 24.9, 25, 49.9, 50, 69.9, 70, 84.9, 85, 100)] == [
        "low",
        "low",
        "moderate",
        "moderate",
        "high",
        "high",
        "very_high",
        "very_high",
        "extreme",
        "extreme",
    ]


def test_saturate_clips_both_ends_and_handles_missing_values():
    assert saturate(None, 0, 10) == 0.0
    assert saturate(-5, 0, 10) == 0.0
    assert saturate(999, 0, 10) == 1.0
    assert saturate(5, 0, 10) == 0.5
    assert saturate(5, 7, 7) == 0.0


def test_rank_severity_puts_worst_rank_at_one():
    assert rank_severity(1, 5) == 1.0
    assert rank_severity(5, 5) == 0.0
    assert rank_severity(3, 5) == 0.5
    assert rank_severity(1, 1) == 0.0


def test_unavailable_factor_does_not_depress_the_score():
    """Two identical observed drivers score the same whether or not a gap is declared."""
    with_gap = HazardSignal(
        hazard="flood",
        factors=[
            Factor("a", "open_meteo", severity=0.8, weight=1.0),
            Factor("b", "dem", severity=0.4, weight=1.0),
            Factor("c", "not_loaded", weight=1.5, available=False),
        ],
    )
    without_gap = HazardSignal(
        hazard="flood",
        factors=[Factor("a", "open_meteo", severity=0.8, weight=1.0), Factor("b", "dem", severity=0.4, weight=1.0)],
    )
    assert with_gap.score == without_gap.score == 60.0
    assert without_gap.confidence == 1.0
    assert with_gap.confidence == pytest.approx(2 / 3.5, abs=1e-3)


def test_signal_with_nothing_observed_is_unassessed_not_low():
    signal = HazardSignal(hazard="wildfire", factors=[Factor("detections", "firms", weight=1.6, available=False)])
    payload = signal.as_dict()
    assert payload["band"] == "unassessed"
    assert payload["confidence"] == 0.0
    assert signal.has_evidence is False
    # The unavailable factor must not publish a severity it never measured.
    assert "severity" not in payload["factors"][0]
    assert payload["factors"][0]["available"] is False


def test_weight_only_active_factors_count_toward_the_score():
    signal = HazardSignal(
        hazard="landslide",
        factors=[
            Factor("slope", "dem", severity=1.0, weight=3.0),
            Factor("rain", "open_meteo", severity=0.0, weight=1.0),
        ],
    )
    assert signal.score == 75.0
    assert signal.confidence == 1.0


def test_manual_score_override_is_respected():
    signal = HazardSignal(hazard="flood", factors=[], _score=91.0)
    assert signal.score == 91.0
    assert signal.confidence == 0.0
    assert signal.has_evidence is False


def test_overall_band_ignores_signals_without_evidence():
    quiet = HazardSignal(hazard="drought", factors=[Factor("x", "not_loaded", available=False)])
    loud = HazardSignal(hazard="earthquake", factors=[Factor("mag", "usgs", severity=0.9, weight=1.0)])
    assert overall_band([quiet, loud]) == band_for(loud.score)
    assert overall_band([quiet]) == "unknown"


def test_as_dict_reports_data_completeness():
    signal = HazardSignal(
        hazard="flood",
        factors=[Factor("a", "open_meteo", severity=0.5), Factor("b", "not_loaded", available=False)],
        unavailable_sources=["river_floodplain"],
        warnings=["gap"],
        provenance=[{"provider": "open_meteo"}],
    )
    payload = signal.as_dict()
    completeness = payload["data_completeness"]
    assert completeness["factors_total"] == 2
    assert completeness["factors_available"] == 1
    assert completeness["unavailable_sources"] == ["river_floodplain"]
    assert completeness["confidence"] == payload["confidence"]
    assert payload["warnings"] == ["gap"]
    assert payload["provenance"] == [{"provider": "open_meteo"}]
