from __future__ import annotations

"""Overpass lifeline lookups under a wall-clock budget.

The provider is measured at 504-624 s through the serial mirror walk (four layers x four
mirrors x retries), which is longer than any gateway will hold a request open. These tests
pin the replacement behaviour: layers run concurrently inside one budget, and a layer that
does not answer is reported unavailable - never as "no features".
"""

import time
from datetime import datetime, timezone

import pytest

from app.core.errors import ProviderUnavailableError
from app.integrations.base import ProviderResult
from app.integrations.osm_infra import OverpassProvider


def _result() -> ProviderResult:
    return ProviderResult(
        provider="osm_overpass",
        endpoint="https://example.invalid/api/interpreter",
        fetched_at=datetime.now(timezone.utc),
        payload={"elements": [{"type": "node", "id": 1, "lat": 21.1, "lon": 79.1, "tags": {"name": "Clinic"}}]},
        http_status=200,
        elapsed_ms=1.0,
        license="ODbL 1.0 (OpenStreetMap contributors)",
        attribution="(c) OpenStreetMap contributors",
    )


def test_layers_within_the_budget_are_reported_ok(monkeypatch):
    provider = OverpassProvider()
    monkeypatch.setattr(provider, "run_query", lambda query, **kwargs: _result())

    out = provider.infrastructure_near(79.0931, 21.1458, 1000.0, layers=("hospitals", "bridges"), deadline_seconds=5)

    assert set(out["layers"]) == {"hospitals", "bridges"}
    assert all(layer["status"] == "ok" for layer in out["layers"].values())
    assert out["layers"]["hospitals"]["count"] == 1
    assert len(out["provenance"]) == 2
    assert out["deadline_seconds"] == 5
    assert out["warnings"] == []


def test_a_layer_that_overruns_the_budget_is_unavailable_not_empty(monkeypatch):
    provider = OverpassProvider()

    def slow_bridges(query, **kwargs):
        if "bridge" in query:
            time.sleep(1.5)
        return _result()

    monkeypatch.setattr(provider, "run_query", slow_bridges)

    started = time.monotonic()
    out = provider.infrastructure_near(79.0931, 21.1458, 1000.0, layers=("hospitals", "bridges"), deadline_seconds=0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 1.2, "the caller must return at the budget, not wait for the slow layer"
    assert out["layers"]["hospitals"]["status"] == "ok"
    bridges = out["layers"]["bridges"]
    assert bridges["status"] == "unavailable"
    assert bridges["features"] == []
    assert "budget" in bridges["reason"]
    assert any("budget" in warning for warning in out["warnings"])
    assert any("not as zero features" in warning for warning in out["warnings"])


def test_an_unknown_layer_is_named_and_skipped(monkeypatch):
    provider = OverpassProvider()
    monkeypatch.setattr(provider, "run_query", lambda query, **kwargs: _result())

    out = provider.infrastructure_near(79.0931, 21.1458, 1000.0, layers=("hospitals", "teleports"), deadline_seconds=5)

    assert out["layers"]["teleports"]["status"] == "unavailable"
    assert any("teleports" in warning for warning in out["warnings"])
    assert out["layers"]["hospitals"]["status"] == "ok"


def test_a_query_never_starts_a_mirror_it_cannot_finish(monkeypatch):
    provider = OverpassProvider()
    calls: list[str] = []

    def never(mirror, **kwargs):
        calls.append(mirror)
        raise AssertionError("no mirror should be contacted after the deadline")

    monkeypatch.setattr(provider, "get_json", never)

    with pytest.raises(ProviderUnavailableError) as caught:
        provider.run_query("out;", deadline=time.monotonic() - 1)

    assert calls == []
    assert "budget" in caught.value.message
