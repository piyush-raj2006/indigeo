from __future__ import annotations

"""The analysis orchestration layer, tested with no network access.

Evidence gathering is replaced by a stub so these tests exercise what the layer owns:
coordinate validation, persistence of the analysis and its recommendations, readback, and
the rule that a run with nothing observed is stored as an honest gap rather than a score.
The boundary fixture supplies a district called Testpur around (78.2, 21.4).
"""

from typing import Any

import pytest
from sqlalchemy import select

from app.core.database import session_scope
from app.core.errors import InvalidCoordinates, NotFoundError, ValidationError
from app.models.hazard import HazardAnalysis, Recommendation
from app.services import analysis
from _hazard_fixtures import empty_context, full_context

INSIDE_TESTPUR = (21.4, 78.2)
OUTSIDE_EVERY_POLYGON = (11.65, 92.85)


@pytest.fixture()
def stub_evidence(monkeypatch, geo_store):
    """Point the pipeline at a hand-written EvidenceContext instead of live providers."""
    calls: list[dict[str, Any]] = []

    def _install(ctx: Any) -> Any:
        def fake_gather(
            lon, lat, *, radius_m=None, admin=None, allow_remote=True, include_infrastructure=False, on_progress=None
        ):
            calls.append(
                {
                    "lon": lon,
                    "lat": lat,
                    "radius_m": radius_m,
                    "admin": admin,
                    "allow_remote": allow_remote,
                    "include_infrastructure": include_infrastructure,
                }
            )
            ctx.admin = admin or {}
            return ctx

        monkeypatch.setattr(analysis, "gather_evidence", fake_gather)
        return ctx

    _install.calls = calls
    return _install


def test_a_completed_analysis_is_persisted_and_reads_back(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, requested_by="test-runner")

    assert payload["status"] == "completed"
    assert payload["is_synthetic"] is False
    assert len(payload["hazards"]) == 8
    assert payload["overall"]["band"] in {"low", "moderate", "high", "very_high", "extreme"}
    assert payload["location"]["admin"]["district"] == "Testpur"
    assert payload["duration_ms"] is not None

    stored = analysis.get_analysis(payload["analysis_uuid"])
    assert stored["analysis_uuid"] == payload["analysis_uuid"]
    assert stored["requested_by"] == "test-runner"
    # Read-back must be the same shape as the run response, not a variant of it.
    assert len(stored["hazards"]) == 8
    assert stored["hazards"][0]["hazard"] == payload["hazards"][0]["hazard"]
    assert len(stored["requested_hazards"]) == 8
    assert stored["request"]["include_infrastructure"] is False
    assert len(stored["recommendations"]) == len(payload["recommendations"])
    assert stored["is_synthetic"] is False


def test_scoring_block_states_heuristics_and_which_proxies_were_used(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, persist=False)

    scoring = payload["scoring"]
    # No assessor calls a model, so this must say so rather than leave the reader guessing.
    assert scoring["mode"] == "spatial_heuristics"
    assert scoring["ml_models_used"] == []
    assert payload["model_versions"]["ml_models_used"] == []
    assert scoring["engine"] == analysis.ENGINE_NAME
    assert "never imputed" in scoring["note"]

    proxies = scoring["heuristic_proxies"]
    assert set(proxies) == {hazard["hazard"] for hazard in payload["hazards"]}
    # Each proxy carries the source it was read from and whether it answered, so a missing
    # driver is visible as used=False instead of disappearing from the record.
    for hazard, rows in proxies.items():
        assert rows, hazard
        for row in rows:
            assert set(row) == {"proxy", "source", "used"}
            assert isinstance(row["used"], bool)
    assert any(row["used"] is False for rows in proxies.values() for row in rows)


def test_stored_analysis_returns_the_scoring_block(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, requested_by="test-runner")
    stored = analysis.get_analysis(payload["analysis_uuid"])
    assert stored["scoring"] == payload["scoring"]


def test_evidence_gathering_is_called_with_resolved_place(stub_evidence):
    ctx = stub_evidence(full_context())
    analysis.run_point_analysis(*INSIDE_TESTPUR, radius_m=12_000, persist=False)
    call = stub_evidence.calls[-1]
    assert call["lon"] == pytest.approx(78.2)
    assert call["lat"] == pytest.approx(21.4)
    assert call["radius_m"] == 12_000.0
    assert call["admin"]["district"] == "Testpur"
    assert ctx.admin["district"] == "Testpur"


def test_the_fast_path_does_not_wait_on_overpass(stub_evidence):
    """Measured live: the four OSM layers cost minutes and feed no hazard factor."""
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, persist=False)
    assert stub_evidence.calls[-1]["include_infrastructure"] is False
    assert payload["request"]["include_infrastructure"] is False

    analysis.run_point_analysis(*INSIDE_TESTPUR, persist=False, include_infrastructure=True)
    assert stub_evidence.calls[-1]["include_infrastructure"] is True


def test_recommendation_rows_carry_their_own_evidence_and_limits(stub_evidence):
    hill = {
        "available": True,
        "summary": {
            "elevation_mean_m": 900.0,
            "elevation_at_point_m": 900.0,
            "elevation_relief_m": 650.0,
            "slope_deg": 31.0,
            "local_low_point": False,
            "bathymetry_only": False,
            "data_gap": False,
        },
    }
    ctx = full_context(
        terrain=hill,
        coast_distance_m=None,
        weather_recent={"series": [{"date": f"2026-09-{day:02d}", "precipitation_mm": 35.0, "tmax_c": 33.0, "soil_moisture_m3m3": 0.3} for day in range(1, 30)]},
        weather_forecast={"daily": {"precipitation_sum": [45.0] * 7, "temperature_2m_max": [32.0] * 7, "wind_speed_10m_max": [30.0] * 7, "wind_gusts_10m_max": [40.0] * 7}},
    )
    stub_evidence(ctx)
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR)
    rows = payload["recommendations"]
    assert rows
    for row in rows:
        assert row["evidence"]["band"]
        assert row["authority"]
        assert row["method"] == "rule_engine"
        if row["requires_professional_assessment"]:
            assert "licensed professional" in row["limitations"]

    with session_scope() as session:
        stored = session.scalars(select(Recommendation).where(Recommendation.analysis_uuid == payload["analysis_uuid"])).all()
    assert len(stored) == len(rows)
    assert any(r.requires_professional_assessment for r in stored)
    assert all(r.guidance_url for r in stored)


def test_no_observed_evidence_is_stored_as_a_gap_not_a_zero_risk(stub_evidence):
    stub_evidence(empty_context())
    payload = analysis.run_point_analysis(*OUTSIDE_EVERY_POLYGON)

    assert payload["recommendations"] == []
    assert payload["overall"]["band"] == "unknown"
    assert payload["overall"]["hazards_assessed"] == []
    assert sorted(payload["overall"]["hazards_unassessed"]) == sorted(h["hazard"] for h in payload["hazards"])
    assert all(h["band"] == "unassessed" for h in payload["hazards"])
    assert payload["data_completeness"]["sources_available"] == []
    assert payload["data_completeness"]["unavailable_layers"]

    stored = analysis.get_analysis(payload["analysis_uuid"])
    assert stored["overall"]["band"] == "unknown"
    assert stored["recommendations"] == []
    assert stored["is_synthetic"] is False
    assert stored["limitations"], "the gaps must be recorded, not silently dropped"


def test_a_failed_run_is_recorded_and_the_error_still_surfaces(stub_evidence, monkeypatch):
    stub_evidence(full_context())

    def boom(lon, lat, **kwargs):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(analysis, "gather_evidence", boom)
    with pytest.raises(RuntimeError):
        analysis.run_point_analysis(*INSIDE_TESTPUR)

    with session_scope() as session:
        rows = session.scalars(select(HazardAnalysis).where(HazardAnalysis.status == "failed")).all()
    assert rows, "a failed analysis must still be written to the audit trail"
    assert "provider unreachable" in rows[-1].error
    assert rows[-1].results == {}


def test_persist_false_leaves_no_trace(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, persist=False)
    with session_scope() as session:
        assert session.scalar(select(HazardAnalysis).where(HazardAnalysis.analysis_uuid == payload["analysis_uuid"])) is None


def test_hazard_selection_is_validated_before_any_work(stub_evidence):
    stub_evidence(full_context())
    with pytest.raises(ValidationError) as exc:
        analysis.run_point_analysis(*INSIDE_TESTPUR, hazards=["flood", "tsunami"])
    assert exc.value.details["unknown_hazards"] == ["tsunami"]
    assert "flood" in exc.value.details["known_hazards"]
    assert stub_evidence.calls == [], "validation must happen before any provider is touched"


def test_subselecting_hazards_limits_the_analysis(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, hazards=["flood", "drought"], persist=False)
    assert [h["hazard"] for h in payload["hazards"]] == ["flood", "drought"]


def test_coordinates_and_radius_are_validated(stub_evidence):
    stub_evidence(full_context())
    with pytest.raises(InvalidCoordinates):
        analysis.run_point_analysis(95.0, 12.0, persist=False)
    with pytest.raises(InvalidCoordinates):
        analysis.run_point_analysis(*INSIDE_TESTPUR, radius_m=5_000_000, persist=False)
    with pytest.raises(InvalidCoordinates):
        analysis.run_point_analysis(*INSIDE_TESTPUR, radius_m="10 stone", persist=False)


def test_radius_accepts_units(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, radius_m="3 km", persist=False)
    assert stub_evidence.calls[-1]["radius_m"] == 3000.0
    assert payload["location"]["radius_m"] == 3000.0


def test_missing_analysis_raises_not_found(stub_evidence):
    stub_evidence(full_context())
    with pytest.raises(NotFoundError):
        analysis.get_analysis("00000000-0000-4000-8000-000000000000")


def test_an_approximate_boundary_label_is_a_limitation_of_the_advice(stub_evidence):
    """The advice is addressed to a ward or district, so a nearest-polygon match has to be
    disclosed with the result, not only buried in the admin block."""
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*OUTSIDE_EVERY_POLYGON, persist=False)
    assert any("district" in w.lower() for w in payload["location"]["admin"]["warnings"])
    assert any("district" in w.lower() for w in payload["limitations"])
    assert payload["limitations"][0] in payload["location"]["admin"]["warnings"]


def test_recent_analyses_lists_the_runs_without_their_blobs(stub_evidence):
    stub_evidence(full_context())
    first = analysis.run_point_analysis(*INSIDE_TESTPUR)
    second = analysis.run_point_analysis(*OUTSIDE_EVERY_POLYGON, hazards=["cyclone"])
    rows = analysis.recent_analyses(limit=5)
    uuids = [r["analysis_uuid"] for r in rows]
    assert set(uuids) >= {first["analysis_uuid"], second["analysis_uuid"]}
    assert [r["created_at"] for r in rows] == sorted([r["created_at"] for r in rows], reverse=True)
    assert all("hazard_results" not in r and "evidence" not in r for r in rows)
    newest = next(r for r in rows if r["analysis_uuid"] == second["analysis_uuid"])
    assert newest["hazards"] == ["cyclone"]
    assert newest["status"] == "completed"


# -- drawn areas, overlays and live feeds ------------------------------------
DRAWN_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[78.10, 21.10], [78.40, 21.10], [78.40, 21.40], [78.10, 21.40], [78.10, 21.10]]],
}


def _live_feed_bundle() -> dict[str, Any]:
    return {
        "configured": True,
        "unconfigured": [],
        "unavailable": [],
        "warnings": [],
        "feeds": {
            "imd": {
                "label": "IMD severe-weather warnings",
                "provider": "imd_alerts",
                "env_var": "IMD_ALERTS_URL",
                "status": "available",
                "count": 1,
                "matched_count": 1,
                "matched": [
                    {
                        "id": "imd-1",
                        "title": "Heavy Rainfall warning",
                        "description": "Heavy rain likely over the district.",
                        "severity": "Severe",
                        "severity_score": 0.75,
                        "onset": "2026-09-01T00:00:00Z",
                        "expires": "2026-09-02T00:00:00Z",
                        "areas": ["Testpur"],
                        "tags": ["rain"],
                        "matched_by": "geometry",
                        "url": "https://example.invalid/imd/1",
                        "format": "cap",
                    }
                ],
            },
            "cwc": {
                "label": "CWC river gauges",
                "provider": "cwc_gauges",
                "env_var": "CWC_GAUGES_URL",
                "status": "available",
                "count": 1,
                "search_radius_m": 5000.0,
                "max_severity": 1.0,
                "unrated": 0,
                "gauges": [
                    {
                        "station": "Halasur",
                        "river": "Pennar",
                        "lat": 21.4,
                        "lon": 78.2,
                        "level_m": 6.2,
                        "warning_level_m": 5.0,
                        "danger_level_m": 6.0,
                        "distance_m": 1200.0,
                        "trend": "rising",
                        "observed_at": "2026-09-01T06:00:00Z",
                        "severity": "danger",
                    }
                ],
            },
            "bhuvan": {
                "label": "Bhuvan hazard susceptibility",
                "provider": "bhuvan_hazards",
                "env_var": "BHUVAN_HAZARDS_URL",
                "status": "available",
                "features": 4,
                "matched": 1,
                "unclassified": 0,
                "hazards": {"flood": {"max_severity": 0.8, "features": 2, "classes": ["High"]}},
            },
        },
    }


def test_a_drawn_polygon_is_assessed_at_interior_samples_and_reads_back(stub_evidence):
    from shapely.geometry import Point, shape

    stub_evidence(full_context())
    payload = analysis.run_area_analysis(DRAWN_SQUARE, requested_by="area-runner")

    assert payload["status"] == "completed"
    assert payload["is_synthetic"] is False
    assert payload["request"]["mode"] == "polygon"
    assert payload["request"]["allow_remote"] is False
    assert payload["location"]["geometry_kind"] == "polygon"
    assert payload["selection"]["mode"] == "polygon"
    assert payload["selection"]["area_km2"] > 0

    area = payload["area"]
    assert area["aggregation"] == "worst_sampled_point"
    assert 1 <= area["samples_total"] <= analysis.AREA_SAMPLE_LIMIT
    assert area["samples_total"] == len(area["samples"])

    drawn = shape(payload["selection"]["geometry"])
    for sample in area["samples"]:
        assert drawn.covers(Point(sample["lon"], sample["lat"])), "a sample must lie inside the selection"

    # Regional evidence is gathered once; local surface readings are per sample.
    assert len(stub_evidence.calls) == 1
    assert stub_evidence.calls[0]["radius_m"] == area["sample_radius_m"]

    assert payload["districts"] and payload["districts"][0]["district"] == "Testpur"
    assert payload["districts"][0]["name_missing"] is False
    assert any("interior point samples" in item for item in payload["limitations"])

    for row in payload["hazards"]:
        stats = row["details"]["area"]
        assert stats["samples_total"] == area["samples_total"]
        if stats["max_score"] is not None:
            assert row["score"] == stats["max_score"]
            assert row["band"] == stats["band_at_max"]
    multi_sample = [row for row in payload["hazards"] if row["details"]["area"]["samples_assessed"] > 1]
    assert multi_sample
    assert all(any("highest of" in warning for warning in row["warnings"]) for row in multi_sample)

    stored = analysis.get_analysis(payload["analysis_uuid"])
    assert stored["requested_by"] == "area-runner"
    assert stored["selection"] == payload["selection"]
    assert stored["area"] == area
    assert stored["location"]["wkt"].startswith("POLYGON")
    assert stored["live_feeds"] == payload["live_feeds"]
    assert stored["map_overlays"]["layers"] == payload["map_overlays"]["layers"]

    listed = next(
        row for row in analysis.recent_analyses(limit=50) if row["analysis_uuid"] == payload["analysis_uuid"]
    )
    assert listed["geometry_kind"] == "polygon"
    assert listed["area_km2"] == area["area_km2"]


def test_a_drawn_selection_outside_india_is_rejected_before_any_gather(stub_evidence):
    stub_evidence(full_context())
    outside = {"type": "Polygon", "coordinates": [[[10.0, 10.0], [10.2, 10.0], [10.2, 10.2], [10.0, 10.2], [10.0, 10.0]]]}
    with pytest.raises(ValidationError) as exc:
        analysis.run_area_analysis(outside)
    assert "outside India" in exc.value.message
    assert stub_evidence.calls == [], "a rejected selection must not touch a provider"


def test_a_failed_area_run_is_recorded_with_its_drawn_geometry(stub_evidence, monkeypatch):
    stub_evidence(full_context())

    def boom(lon, lat, **kwargs):
        raise RuntimeError("weather mirror gave up")

    monkeypatch.setattr(analysis, "gather_evidence", boom)
    with pytest.raises(RuntimeError):
        analysis.run_area_analysis(DRAWN_SQUARE)

    with session_scope() as session:
        row = session.scalars(
            select(HazardAnalysis)
            .where(HazardAnalysis.status == "failed", HazardAnalysis.geometry_kind == "polygon")
            .order_by(HazardAnalysis.id.desc())
        ).first()
    assert row is not None
    assert row.wkt and row.wkt.startswith("POLYGON")
    assert "weather mirror gave up" in row.error
    assert row.results == {}


def test_the_overlay_catalogue_never_advertises_a_layer_that_is_not_there(stub_evidence):
    ctx = full_context(
        seismicity={
            "total_events": 2,
            "events_within_150km": 1,
            "nearest_event_km": 42.0,
            "max_magnitude": 4.8,
            "max_magnitude_within_150km": 4.8,
            "window_years": 50,
            "events_returned": 1,
            "nearest_events": [
                {
                    "event_id": "us1",
                    "magnitude": 4.8,
                    "lat": 13.05,
                    "lon": 80.30,
                    "depth_km": 12.0,
                    "place": "Test Sea",
                    "time": "2026-01-02T03:04:05Z",
                    "url": "https://example.invalid/event/us1",
                }
            ],
        },
        fires={
            "count": 2,
            "available": True,
            "hotspots_total": 2,
            "hotspots_returned": 1,
            "hotspots": [
                {
                    "lat": 13.10,
                    "lon": 80.20,
                    "brightness": 331.2,
                    "confidence": "nominal",
                    "frp": 1.4,
                    "acq_date": "2026-09-01",
                    "acq_time": "1200",
                    "satellite": "N",
                    "instrument": "VIIRS",
                }
            ],
        },
    )
    stub_evidence(ctx)
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, persist=False)
    overlays = payload["map_overlays"]

    assert overlays["scope"] == "point"
    assert overlays["radius_m"] == payload["location"]["radius_m"]
    assert overlays["seismic_events"][0]["magnitude"] == 4.8
    assert overlays["fire_hotspots"][0]["lat"] == 13.10

    catalogue = {layer["id"]: layer for layer in overlays["layers"]}
    assert set(catalogue) == {
        "selection",
        "hazard_samples",
        "seismic_events",
        "firms_hotspots",
        "elevation_samples",
        "osm_infrastructure",
        "live_alerts",
        "seismic_faults",
    }
    assert catalogue["seismic_faults"]["available"] is False
    assert "No fault-line dataset" in catalogue["seismic_faults"]["reason"]
    assert catalogue["osm_infrastructure"]["available"] is False
    assert "not requested" in catalogue["osm_infrastructure"]["reason"]
    assert catalogue["elevation_samples"]["available"] is True
    assert any("interpolated" in note for note in overlays["notes"])
    assert any("no elevation gradient raster" in note.lower() for note in overlays["notes"])
    assert overlays["osm_infrastructure"]["queried"] is False


def test_area_overlays_and_live_feeds_carry_the_matched_records(stub_evidence):
    stub_evidence(full_context(alerts=_live_feed_bundle()))
    payload = analysis.run_area_analysis(DRAWN_SQUARE, persist=False)
    overlays = payload["map_overlays"]
    feeds = payload["live_feeds"]

    assert overlays["scope"] == "area"
    assert overlays["geometry"] == payload["selection"]["geometry"]
    assert len(overlays["samples"]) == payload["area"]["samples_total"]
    assert overlays["live_alerts"]["imd"][0]["title"] == "Heavy Rainfall warning"
    assert overlays["live_alerts"]["cwc"][0]["station"] == "Halasur"
    # Bhuvan scores intersecting susceptibility groups but retains no geometry.
    assert overlays["live_alerts"]["bhuvan"]["geometry_available"] is False
    assert overlays["live_alerts"]["bhuvan"]["matched_features"] == 1

    assert feeds["configured"] is True
    assert feeds["feeds"]["imd"]["alerts_total"] == 1
    assert feeds["feeds"]["imd"]["items"][0]["matched_by"] == "geometry"
    assert feeds["feeds"]["cwc"]["gauges"][0]["distance_km"] == 1.2
    assert feeds["feeds"]["bhuvan"]["hazards"]["flood"]["max_severity"] == 0.8
    assert set(feeds["blended_into"]["flood"]) == {"imd_alerts", "cwc_gauges", "bhuvan_hazards"}

    live = next(layer for layer in overlays["layers"] if layer["id"] == "live_alerts")
    assert live["available"] is True
    assert live["feeds"] == {"imd": "available", "cwc": "available", "bhuvan": "available"}


def test_a_context_without_live_feeds_says_so_instead_of_reporting_no_alerts(stub_evidence):
    stub_evidence(full_context())
    payload = analysis.run_point_analysis(*INSIDE_TESTPUR, persist=False)
    feeds = payload["live_feeds"]
    assert feeds["feeds"] == {}
    assert "not a finding of no alerts" in feeds["note"]

