from __future__ import annotations

"""The evidence layer is the only part of the engine that talks to providers, so the
availability flags it writes are what the whole build's honesty rests on. These tests run
it against stub providers and assert which calls were actually made.
"""

from typing import Any

import pytest

from app.services import evidence


class _Result:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def provenance(self) -> dict[str, Any]:
        return {"provider": "stub", "endpoint": "https://example.invalid/"}


class _StubProviders:
    """One object answering every provider role, so call counting is unambiguous."""

    def __init__(self) -> None:
        self.infrastructure_calls: list[tuple[float, float, float]] = []

    # open_meteo
    def recent_observation_window(self, lon: float, lat: float, *, days: int) -> dict[str, Any]:
        return {"series": [], "provenance": {"provider": "open_meteo", "endpoint": "https://example.invalid/archive"}}

    def forecast(self, lon: float, lat: float, *, days: int, past_days: int) -> _Result:
        return _Result({"daily": {"precipitation_sum": [0.0] * days}})

    # usgs
    def query_india(self, **kwargs: Any) -> _Result:
        return _Result({"features": []})

    # overpass
    def infrastructure_near(
        self, lon: float, lat: float, radius_m: float, *, layers=(), limit: int = 50, deadline_seconds: float | None = None
    ) -> dict[str, Any]:
        self.infrastructure_calls.append((lon, lat, radius_m))
        return {
            "layers": {key: {"status": "ok", "features": [], "count": 0} for key in layers},
            "provenance": [],
            "deadline_seconds": 90.0,
        }

    # firms
    def auth_problem(self) -> str | None:
        return "FIRMS_MAP_KEY is not set"


class _StubStore:
    def has_layer(self, name: str) -> bool:
        return name == "india_outline"

    def distance_to_boundary(self, table: str, lon: float, lat: float, *, max_distance_m: float) -> float | None:
        return 1_700.0


@pytest.fixture()
def stubbed(monkeypatch):
    providers = _StubProviders()
    monkeypatch.setattr(evidence, "get_provider", lambda key: providers)
    monkeypatch.setattr(
        "app.services.locations.terrain_at",
        lambda lon, lat, radius_m: {"available": True, "summary": {"elevation_at_point_m": 10.0, "elevation_min_m": 2.0, "elevation_max_m": 20.0, "elevation_relief_m": 18.0, "slope_deg": 3.0}, "provenance": []},
    )
    monkeypatch.setattr("app.gis.spatial.get_spatial_store", lambda: _StubStore())
    return providers


def test_infrastructure_lookup_is_a_single_decision(stubbed):
    """The gatherer still asks by default; it is the analysis path that declines (see
    test_services_analysis), and nothing else should be able to pay for it by accident."""
    ctx = evidence.gather_evidence(80.2785, 13.0878, include_infrastructure=False)
    assert stubbed.infrastructure_calls == []
    assert ctx.source_status.get("osm_overpass") is None
    assert ctx.infrastructure == {}
    assert any("deliberate omission" in w for w in ctx.warnings)

    ctx = evidence.gather_evidence(80.2785, 13.0878)
    assert len(stubbed.infrastructure_calls) == 1
    assert ctx.source_status["osm_overpass"] == "available"
    assert set(ctx.infrastructure["layers"]) == {"hospitals", "emergency_services", "bridges", "water_structures"}


@pytest.mark.parametrize(
    ("layer_states", "expected", "expected_warning"),
    [
        ({"hospitals": "ok", "bridges": "unavailable"}, "partial", "1 of 2 layers answered"),
        ({"hospitals": "unavailable", "bridges": "unavailable"}, "unavailable", "no layer answered"),
    ],
)
def test_a_partly_answered_lifeline_lookup_is_not_called_available(
    stubbed, monkeypatch, layer_states, expected, expected_warning
):
    monkeypatch.setattr(
        stubbed,
        "infrastructure_near",
        lambda lon, lat, radius_m, *, layers=(), limit=50, deadline_seconds=None: {
            "layers": {name: {"status": state, "features": []} for name, state in layer_states.items()},
            "provenance": [],
            "deadline_seconds": 90.0,
        },
    )
    ctx = evidence.gather_evidence(80.2785, 13.0878)

    assert ctx.source_status["osm_overpass"] == expected
    assert any(expected_warning in warning for warning in ctx.warnings)


def test_a_source_that_raises_is_flagged_not_defaulted(monkeypatch, stubbed):
    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("mirror gave up")

    monkeypatch.setattr(stubbed, "query_india", boom)
    ctx = evidence.gather_evidence(80.2785, 13.0878)
    assert ctx.source_status["usgs_earthquakes"] == "unavailable"
    assert ctx.seismicity == {}
    assert any("Seismicity data unavailable" in w for w in ctx.warnings)
    # A failed source must not leave a stale "available" flag behind for its dependants.
    assert "usgs_earthquakes" in ctx.source_status


def test_an_absent_coastline_layer_is_reported_as_a_gap(monkeypatch, stubbed):
    class _NoOutline(_StubStore):
        def has_layer(self, name: str) -> bool:
            return False

    monkeypatch.setattr("app.gis.spatial.get_spatial_store", lambda: _NoOutline())
    ctx = evidence.gather_evidence(80.2785, 13.0878)
    assert ctx.source_status["coastline"] == "unavailable"
    assert ctx.coast_measured is False
    assert any("cannot be measured" in w for w in ctx.warnings)


def test_a_measured_coast_distance_is_labelled_as_the_national_outline(monkeypatch, stubbed):
    ctx = evidence.gather_evidence(80.2785, 13.0878)
    assert ctx.coast_distance_m == 1_700.0
    assert ctx.coast_measured is True
    assert any("India's own coastline outline" in w for w in ctx.warnings)
