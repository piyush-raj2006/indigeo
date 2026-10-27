from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.errors import ProviderAuthError, ProviderRateLimitError, ProviderUnavailableError
from app.integrations.base import ProviderStatus
from app.integrations.earthdata import EarthdataProvider
from app.integrations.firms import FirmsProvider
from app.integrations.nominatim import NominatimProvider
from app.integrations.open_meteo import ARCHIVE, ARCHIVE_DAILY, OpenMeteoProvider
from app.integrations.osm_infra import OverpassProvider
from app.integrations.usgs import UsgsEarthquakeProvider


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Providers write to a per-test cache, so a hit is always something the test asked for."""
    from app.core.config import get_settings

    directory = tmp_path / "provider_cache"
    directory.mkdir()
    monkeypatch.setattr(get_settings(), "provider_cache_dir", str(directory))
    return directory


def _fresh(cls):
    return cls()


def test_usgs_uses_fdsn_latlon_parameters_not_bbox():
    """Verified live: ComCat answers HTTP 400 for a `bbox` parameter."""
    provider = _fresh(UsgsEarthquakeProvider)
    with respx.mock:
        route = respx.get(provider.base_url).respond(200, json={"type": "FeatureCollection", "features": []})
        provider.query_bbox(77.0, 22.0, 79.0, 24.0, days=30)
    params = route.calls.last.request.url.params
    assert {"minlongitude", "minlatitude", "maxlongitude", "maxlatitude"} <= set(params.keys())
    assert "bbox" not in params


def test_retry_then_success_and_provenance_recorded():
    provider = _fresh(NominatimProvider)
    provider.min_interval_seconds = 0.0
    url = f"{provider.base_url}/search"
    with respx.mock:
        route = respx.get(url)
        route.side_effect = [
            httpx.Response(502, text="at capacity"),
            httpx.Response(200, json=[{"place_id": 1, "display_name": "Nagpur, Maharashtra"}]),
        ]
        result = provider.search("Nagpur")
    assert result.http_status == 200
    assert result.from_cache is False
    prov = result.provenance()
    assert prov["provider"] == "nominatim"
    assert prov["attribution"]
    assert route.call_count == 2


def test_rate_limit_raises_retryable_error():
    provider = _fresh(NominatimProvider)
    provider.min_interval_seconds = 0.0
    with respx.mock:
        respx.get(f"{provider.base_url}/search").respond(429, text="slow down")
        with pytest.raises(ProviderRateLimitError) as excinfo:
            provider.search("Pune")
    assert excinfo.value.retryable is True


def test_auth_rejection_reports_missing_env():
    provider = _fresh(EarthdataProvider)
    provider.map_key = None
    with respx.mock:
        respx.get(provider.base_url).respond(401, json={"error": "unauthorized"})
        with pytest.raises(ProviderAuthError) as excinfo:
            provider.get_json(provider.base_url)
    assert excinfo.value.details["missing_env"] == "EARTHDATA_TOKEN"


def test_validator_rejects_surprising_payload():
    provider = _fresh(UsgsEarthquakeProvider)
    with respx.mock:
        respx.get(provider.base_url).respond(200, json={"unexpected": True})
        with pytest.raises(ProviderUnavailableError, match="failed validation"):
            provider.query_bbox(77.0, 22.0, 79.0, 24.0)


def test_second_identical_call_is_served_from_cache():
    provider = _fresh(UsgsEarthquakeProvider)
    with respx.mock:
        route = respx.get(provider.base_url).respond(200, json={"type": "FeatureCollection", "features": []})
        first = provider.query_bbox(77.0, 22.0, 79.0, 24.0)
        second = provider.query_bbox(77.0, 22.0, 79.0, 24.0)
    assert first.from_cache is False and second.from_cache is True
    assert route.call_count == 1
    assert second.provenance()["notes"]["cache_age_seconds"] >= 0


def test_firms_without_key_is_unconfigured_not_silently_skipped():
    provider = _fresh(FirmsProvider)
    provider.map_key = None
    status = provider.healthcheck()
    assert status.state == "unconfigured"
    assert status.requires_auth is True
    with pytest.raises(ProviderAuthError) as excinfo:
        provider.hotspots_bbox(74.0, 8.0, 78.0, 12.0)
    assert excinfo.value.details["missing_env"] == "FIRMS_MAP_KEY"


def test_firms_invalid_map_key_is_reported_as_key_problem():
    """The retired anonymous key returns HTTP 400 'Invalid MAP_KEY.' - caught live."""
    provider = _fresh(FirmsProvider)
    provider.min_interval_seconds = 0.0
    provider.map_key = "FIRMS_PUBLIC"
    with respx.mock:
        respx.route(method="GET", host="firms.modaps.eosdis.nasa.gov").respond(400, text="Invalid MAP_KEY.")
        status = provider.healthcheck()
    assert status.state == "unconfigured"
    assert "MAP_KEY" in status.detail


def test_firms_never_leaks_the_map_key_into_provenance():
    provider = _fresh(FirmsProvider)
    provider.min_interval_seconds = 0.0
    provider.map_key = "SECRET123"
    body = "latitude,longitude,confidence\n10.5,76.5,90\n"
    with respx.mock:
        respx.route(method="GET", host="firms.modaps.eosdis.nasa.gov").respond(200, text=body)
        rows, result = provider.hotspots_bbox(74.0, 8.0, 78.0, 12.0)
    assert len(rows) == 1
    assert rows[0]["confidence"] == 90.0
    assert "SECRET123" not in json.dumps(result.provenance())
    assert "<MAP_KEY>" in result.endpoint


def test_overpass_fails_over_to_the_next_mirror():
    provider = _fresh(OverpassProvider)
    provider.min_interval_seconds = 0.0
    provider.cacheable = True
    provider.mirrors = [
        "https://down.example/api/interpreter",
        "https://good.example/api/interpreter",
    ]
    payload = {"version": 0.6, "osm3s": {"timestamp_osm_base": "2026-10-25T00:00:00Z"}, "elements": [
        {"type": "node", "id": 1, "lat": 12.95, "lon": 77.6, "tags": {"amenity": "hospital", "name": "AIIMS"}},
    ]}
    with respx.mock:
        respx.post(provider.mirrors[0]).respond(502, text="at capacity")
        respx.post(provider.mirrors[1]).respond(200, json=payload)
        result = provider.run_query("[out:json];node(1);out;", cache=False)
    assert result.notes["mirror"] == "good.example"
    assert len(result.notes["mirror_failures"]) == 1
    assert result.notes["mirror_failures"][0].startswith("down.example:")
    assert "502" in result.notes["mirror_failures"][0]
    assert result.notes["osm_base"] == "2026-10-25T00:00:00Z"


def test_overpass_total_failure_says_nothing_was_substituted():
    provider = _fresh(OverpassProvider)
    provider.min_interval_seconds = 0.0
    provider.mirrors = ["https://a.example/api/interpreter", "https://b.example/api/interpreter"]
    with respx.mock:
        respx.post(provider.mirrors[0]).respond(502, text="busy")
        respx.post(provider.mirrors[1]).respond(403, text="whitelisted only")
        with pytest.raises(ProviderUnavailableError) as excinfo:
            provider.run_query("[out:json];node(1);out;", cache=False)
    assert "Nothing was substituted" in excinfo.value.message


def test_infrastructure_near_groups_layers_and_keeps_attribution():
    provider = _fresh(OverpassProvider)
    provider.min_interval_seconds = 0.0
    provider.mirrors = ["https://good.example/api/interpreter"]
    hospital = {"type": "node", "id": 7, "lat": 12.95, "lon": 77.61, "tags": {"amenity": "hospital", "name": "Victoria"}}
    bridge = {
        "type": "way",
        "id": 9,
        "center": {"lat": 12.96, "lon": 77.62},
        "tags": {"highway": "primary", "bridge": "yes", "name": "Overbridge"},
    }
    with respx.mock:
        respx.post(provider.mirrors[0]).respond(
            200, json={"elements": [hospital], "osm3s": {"timestamp_osm_base": "2026-10-25T00:00:00Z"}}
        )
        first = provider.infrastructure_near(77.6, 12.95, 2000, layers=("hospitals",))
    with respx.mock:
        respx.post(provider.mirrors[0]).respond(200, json={"elements": [bridge]})
        second = provider.infrastructure_near(77.6, 12.95, 2000, layers=("bridges",))
    assert first["layers"]["hospitals"]["count"] == 1
    assert first["layers"]["hospitals"]["features"][0]["name"] == "Victoria"
    assert second["layers"]["bridges"]["features"][0]["lat"] == 12.96  # way centre used
    assert "ODbL" in second["attribution"]
    assert second["provenance"][0]["notes"]["mirror"] == "good.example"


def test_overpass_records_unavailable_layer_instead_of_empty_ok():
    provider = _fresh(OverpassProvider)
    provider.min_interval_seconds = 0.0
    provider.mirrors = ["https://a.example/api/interpreter"]
    with respx.mock:
        respx.post(provider.mirrors[0]).respond(502, text="busy")
        out = provider.infrastructure_near(77.6, 12.95, 2000, layers=("bridges",))
    assert out["layers"]["bridges"]["status"] == "unavailable"
    assert out["warnings"]


def test_mixed_layer_query_is_a_valid_overpass_union():
    """A concatenated 'node[...]way[...]' is rejected with HTTP 400 by every mirror.

    Caught live: the water-structures layer used to emit exactly that, so the query never
    returned data and the failure looked like an outage.
    """
    provider = _fresh(OverpassProvider)
    provider.min_interval_seconds = 0.0
    provider.mirrors = ["https://good.example/api/interpreter"]
    with respx.mock:
        route = respx.post(provider.mirrors[0]).respond(200, json={"elements": []})
        provider.infrastructure_near(80.27, 13.08, 2000, layers=("water_structures",))
    sent = httpx.QueryParams(route.calls.last.request.content.decode())["data"]
    assert 'nwr["man_made"~' in sent and 'nwr["waterway"="dam"]' in sent
    assert ']nwr[' not in sent, "element types must be separate union members, not concatenated"
    assert sent.count("(13.0") == 2, "each selector needs its own bbox statement"


def test_overpass_stops_failing_over_on_a_rejected_query():
    """Invalid QL is invalid on every mirror, so a 400 must not cost four timeouts."""
    provider = _fresh(OverpassProvider)
    provider.min_interval_seconds = 0.0
    provider.mirrors = [
        "https://a.example/api/interpreter",
        "https://b.example/api/interpreter",
    ]
    with respx.mock:
        first = respx.post(provider.mirrors[0]).respond(400, text="parse error: outer token")
        second = respx.post(provider.mirrors[1]).respond(200, json={"elements": []})
        with pytest.raises(ProviderUnavailableError) as excinfo:
            provider.run_query("[out:json];node(1);out;", cache=False)
    assert first.called
    assert not second.called, "a rejected query was failed over to another mirror"
    assert "invalid Overpass QL" in excinfo.value.message
    assert excinfo.value.details["provider_status"] == 400
    assert provider.last_status.state == "degraded"


def test_open_meteo_daily_variables_exclude_rejected_fields():
    """`cape` and soil moisture are refused as archive *daily* variables (caught live)."""
    for refused in ("cape", "soil_moisture_0_to_1cm_mean", "water_evaporation_sum"):
        assert refused not in ARCHIVE_DAILY
    provider = _fresh(OpenMeteoProvider)
    with respx.mock:
        route = respx.get(ARCHIVE).respond(
            200, json={"daily": {"time": ["2026-10-01"], "precipitation_sum": [12.0]}}
        )
        import datetime as _dt

        provider.history(77.59, 12.97, _dt.date(2026, 10, 1), _dt.date(2026, 10, 1))
    assert route.calls.last.request.url.params["daily"] == ",".join(ARCHIVE_DAILY)


def test_open_meteo_error_body_raises_not_silent_empty():
    provider = _fresh(OpenMeteoProvider)
    with respx.mock:
        respx.get(ARCHIVE).respond(200, json={"error": True, "reason": "Invalid value"})
        with pytest.raises(ProviderUnavailableError, match="Open-Meteo error"):
            import datetime as _dt

            provider.history(77.59, 12.97, _dt.date(2026, 10, 1), _dt.date(2026, 10, 2))


def test_earthdata_token_without_probe_url_is_unverified():
    """Public metadata returning 200 must never be reported as verified authentication."""
    provider = _fresh(EarthdataProvider)
    provider.map_key = None
    monkey_settings = provider._settings
    original = monkey_settings.earthdata_probe_url
    monkey_settings.earthdata_probe_url = None
    monkey_settings.earthdata_token = "TOKEN"
    try:
        status = provider.verify_token()
        assert status.state == "unverified"
        assert "unverified" in status.detail or "PROBE_URL" in status.detail
        monkey_settings.earthdata_probe_url = "https://protected.example/data.h5"
        with respx.mock:
            respx.get("https://protected.example/data.h5").respond(403, text="forbidden")
            rejected = provider.verify_token()
        assert rejected.state == "unverified"
        assert "403" in rejected.detail or "rejected" in rejected.detail
    finally:
        monkey_settings.earthdata_token = None
        monkey_settings.earthdata_probe_url = original


def test_earthdata_metadata_search_passes_filters_as_params():
    provider = _fresh(EarthdataProvider)
    with respx.mock:
        route = respx.get(provider.base_url).respond(200, json={"hits": 2, "took": 5, "items": []})
        provider.search_collections(keyword="IMERG", page_size=2)
    params = route.calls.last.request.url.params
    assert params["keyword"] == "IMERG"
    assert params["page_size"] == "2"


def test_provider_status_states_are_from_the_allowed_vocabulary():
    allowed = {"healthy", "degraded", "unavailable", "unconfigured", "unverified"}
    for cls in (NominatimProvider, UsgsEarthquakeProvider, FirmsProvider, OpenMeteoProvider, EarthdataProvider, OverpassProvider):
        provider = _fresh(cls)
        assert provider.last_status.state in allowed
        assert isinstance(provider.last_status, ProviderStatus)
