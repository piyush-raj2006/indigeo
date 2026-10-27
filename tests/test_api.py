from __future__ import annotations

"""HTTP layer tests.

The hazard pipeline itself is covered offline elsewhere; these assert the contract on top of
it: status codes, the error envelope, which parameters are accepted and that nothing here
reaches the network unless a test asks for it.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import router as router_module
from app.main import app
from app.services import analysis

from _hazard_fixtures import empty_context, full_context

client = TestClient(app)


@pytest.fixture()
def stub_evidence(monkeypatch, geo_store):
    """Keep the request path off the network by replacing the only layer that talks."""
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
                    "include_infrastructure": include_infrastructure,
                }
            )
            ctx.admin = admin or {}
            return ctx

        monkeypatch.setattr(analysis, "gather_evidence", fake_gather)
        return calls

    return _install


def test_health_reports_loaded_layers_without_calling_out(geo_store):
    body = client.get("/api/v1/health").json()
    assert body["spatial"]["layers"]["admin_districts"] == 3
    assert body["spatial"]["indexed"]["admin_districts"] is True
    assert body["spatial"]["backend"] == "duckdb"
    assert body["database"]["ok"] is True
    assert len(body["hazards_assessed"]) == 8


def test_root_health_alias_answers_for_container_probes(geo_store):
    prefixed = client.get("/api/v1/health")
    root = client.get("/health")
    assert root.status_code == 200
    assert root.json()["app"] == prefixed.json()["app"]
    assert root.json()["database"] == prefixed.json()["database"]


def test_hazards_metadata_names_every_data_gap(geo_store):
    body = client.get("/api/v1/hazards").json()
    assert "BIS seismic zoning map" in " ".join(body["data_gaps"])
    assert "extreme" in body["band_labels"]


def test_a_point_analysis_round_trips_through_the_stored_record(stub_evidence):
    calls = stub_evidence(full_context())
    response = client.post(
        "/api/v1/analysis/point",
        json={"lat": 21.4, "lon": 78.2, "radius_m": "3 km", "requested_by": "api-test"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["request"]["include_infrastructure"] is False
    assert len(payload["hazards"]) == 8
    assert calls[-1]["radius_m"] == 3000.0
    assert calls[-1]["admin"]["district"] == "Testpur"
    assert calls[-1]["include_infrastructure"] is False

    read_back = client.get(f"/api/v1/analysis/{payload['analysis_uuid']}")
    assert read_back.status_code == 200
    stored = read_back.json()
    assert stored["requested_by"] == "api-test"
    assert len(stored["recommendations"]) == len(payload["recommendations"])


def test_an_unassessed_point_is_returned_as_a_gap_not_a_zero(stub_evidence):
    stub_evidence(empty_context())
    payload = client.post("/api/v1/analysis/point", json={"lat": 21.4, "lon": 78.2}).json()
    assert payload["overall"]["band"] == "unknown"
    assert payload["recommendations"] == []
    assert all(h["band"] == "unassessed" for h in payload["hazards"])


def test_an_unknown_hazard_is_rejected_with_the_known_set():
    response = client.post("/api/v1/analysis/point", json={"lat": 21.4, "lon": 78.2, "hazards": ["tsunami"]})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["unknown_hazards"] == ["tsunami"]
    assert "flood" in error["details"]["known_hazards"]


@pytest.mark.parametrize(
    "query",
    [{"lat": 95, "lon": 78.2}, {"lat": 21.4, "lon": 190}, {"lat": "abc", "lon": 78.2}],
)
def test_out_of_range_coordinates_never_reach_the_engine(query):
    assert client.get("/api/v1/locations/point", params=query).status_code == 422


def test_a_missing_record_is_a_404_with_its_id():
    response = client.get("/api/v1/analysis/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_provider_status_is_labelled_with_when_it_was_measured(monkeypatch, tmp_path):
    monkeypatch.setattr(router_module, "_SNAPSHOT", tmp_path / "provider_status.json")
    body = client.get("/api/v1/providers").json()
    assert body["mode"] == "none recorded"
    assert body["providers"] == []

    (tmp_path / "provider_status.json").write_text(
        '[{"provider": "firms", "state": "unconfigured", "detail": "FIRMS_MAP_KEY is not set"}]', encoding="utf-8"
    )
    body = client.get("/api/v1/providers").json()
    assert body["mode"] == "snapshot"
    assert body["observed_at"]
    assert body["providers"][0]["state"] == "unconfigured"


def test_an_invalid_radius_is_a_validation_error():
    response = client.post("/api/v1/analysis/point", json={"lat": 21.4, "lon": 78.2, "radius_m": "yesterday"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"validation_error", "invalid_coordinates"}


def test_layers_endpoint_lists_the_spatial_store(geo_store):
    body = client.get("/api/v1/layers").json()
    assert {row["table"] for row in body["layers"]} == {"admin_districts", "admin_states", "india_outline"}


def _coords(geometry: dict) -> list[tuple[float, float]]:
    """Every (lon, lat) pair in a GeoJSON Polygon/MultiPolygon, however deeply nested."""
    out: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if len(node) == 2 and all(isinstance(v, (int, float)) for v in node):
                out.append((node[0], node[1]))
            else:
                for child in node:
                    walk(child)

    walk(geometry["coordinates"])
    return out


def test_the_map_payload_serves_stored_boundaries(geo_store):
    body = client.get(
        "/api/v1/layers/admin_districts/geojson", params={"bbox": "77.9,20.9,79.6,22.3"}
    ).json()
    assert body["type"] == "FeatureCollection"
    assert body["crs"] == "EPSG:4326"
    assert {feature["properties"]["district"] for feature in body["features"]} == {"Testpur", "Overlap City"}
    assert body["returned"] == 2
    assert body["truncated"] is False
    assert body["clipping"]["applied"] is False


def test_a_window_fuller_than_the_limit_reports_that_it_was_cut_short(geo_store):
    body = client.get(
        "/api/v1/layers/admin_districts/geojson", params={"bbox": "77.9,20.9,79.6,22.3", "limit": 1}
    ).json()
    assert body["returned"] == 1
    assert body["truncated"] is True
    assert body["limit"] == 1


def test_a_large_boundary_is_clipped_to_the_window_when_asked_and_says_so(geo_store):
    window = (78.0, 21.0, 79.0, 22.0)
    params = {"bbox": ",".join(str(value) for value in window)}
    whole = client.get("/api/v1/layers/india_outline/geojson", params=params).json()
    clipped = client.get(
        "/api/v1/layers/india_outline/geojson", params={**params, "clip_above_area_deg2": 1.0}
    ).json()

    # The fixture outline spans 68..97.4E; unclipped it must arrive as loaded, clipped to the window.
    assert max(lon for lon, _ in _coords(whole["features"][0]["geometry"])) > 90.0
    assert clipped["clipping"]["applied"] is True
    assert clipped["clipping"]["requested"] == 1.0
    drawn = _coords(clipped["features"][0]["geometry"])
    assert all(window[0] - 1e-9 <= lon <= window[2] + 1e-9 for lon, _ in drawn)
    assert all(window[1] - 1e-9 <= lat <= window[3] + 1e-9 for _, lat in drawn)


def test_an_unloaded_layer_is_a_404_not_an_empty_collection(geo_store):
    response = client.get("/api/v1/layers/admin_villages/geojson", params={"bbox": "78,21,79,22"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "bbox",
    ["78,21,79", "78,21,79,22,25", "78,21,79,banana", "79,22,78,21", "181,21,79,22", "78,91,79,22"],
)
def test_a_malformed_window_is_rejected_before_it_reaches_the_store(geo_store, bbox):
    response = client.get("/api/v1/layers/admin_districts/geojson", params={"bbox": bbox})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert bbox in str(response.json()["error"]["details"])


# -- the unified analysis endpoint and drawn selections ----------------------
DRAWN_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[78.10, 21.10], [78.40, 21.10], [78.40, 21.40], [78.10, 21.40], [78.10, 21.10]]],
}


def test_the_unified_endpoint_accepts_a_point_and_radius(stub_evidence):
    calls = stub_evidence(full_context())
    response = client.post("/api/v1/analysis", json={"lat": 21.4, "lon": 78.2, "radius_m": "3 km"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["request"]["mode"] == "point"
    assert payload["location"]["geometry_kind"] == "point"
    assert calls[-1]["radius_m"] == 3000.0


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(lambda geometry: geometry, id="geometry"),
        pytest.param(
            lambda geometry: {"type": "Feature", "properties": {}, "geometry": geometry},
            id="feature",
        ),
        pytest.param(
            lambda geometry: {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "properties": {}, "geometry": geometry}],
            },
            id="feature-collection",
        ),
    ],
)
def test_the_unified_endpoint_accepts_geojson_boundaries(stub_evidence, wrap):
    stub_evidence(full_context())
    response = client.post("/api/v1/analysis", json={"geometry": wrap(DRAWN_SQUARE), "requested_by": "api-area"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selection"]["mode"] == "polygon"
    assert payload["selection"]["geometry"]["type"] == "Polygon"
    assert payload["location"]["geometry_kind"] == "polygon"
    assert payload["area"]["samples_total"] >= 1
    assert payload["map_overlays"]["scope"] == "area"
    assert payload["live_feeds"]["feeds"] == {}

    read_back = client.get(f"/api/v1/analysis/{payload['analysis_uuid']}").json()
    assert read_back["requested_by"] == "api-area"
    assert read_back["selection"] == payload["selection"]
    assert read_back["area"]["area_km2"] == payload["area"]["area_km2"]
    assert read_back["location"]["wkt"].startswith("POLYGON")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no-selection"),
        pytest.param({"lat": 21.4}, id="lat-only"),
        pytest.param({"lat": 21.4, "lon": 78.2, "geometry": DRAWN_SQUARE}, id="point-and-geometry"),
        pytest.param({"geometry": DRAWN_SQUARE, "radius_m": "3 km"}, id="radius-with-geometry"),
    ],
)
def test_a_contradictory_selection_is_rejected_in_the_standard_envelope(body):
    response = client.post("/api/v1/analysis", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["retryable"] is False
    assert error["details"]["errors"]
    assert all(set(item) == {"loc", "type", "message"} for item in error["details"]["errors"])


def test_a_non_polygon_geometry_is_rejected_with_its_type():
    response = client.post(
        "/api/v1/analysis", json={"geometry": {"type": "Point", "coordinates": [78.2, 21.4]}}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["type"] == "Point"
