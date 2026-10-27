from __future__ import annotations

import json

import pytest

from app.core.database import session_scope
from app.gis.spatial import DuckDBSpatialStore, reset_spatial_store
from app.models.catalog import AdminUnit
from app.services import locations
from _geo_fixtures import square_wkb as _square


def test_coordinate_query_is_recognised_without_a_geocoder(geo_store):
    found = locations.search_places("20.5937, 78.9629", allow_remote=False)
    assert found["interpretation"] == "coordinates"
    assert found["results"][0]["lat"] == pytest.approx(20.5937)
    assert found["results"][0]["lon"] == pytest.approx(78.9629)


def test_point_inside_a_district_resolves_cleanly(geo_store):
    ctx = locations.admin_context(78.2, 21.2)
    assert ctx.boundary_matched is True
    assert ctx.boundary_uncertain is False
    assert ctx.district == "Testpur"
    assert ctx.state == "Test Pradesh"
    assert "Testpur" in ctx.label


def test_point_outside_every_polygon_reports_the_nearest_with_a_distance(geo_store):
    """The Port Blair case: an island point a short way outside its own district."""
    ctx = locations.admin_context(92.85, 11.65)
    assert ctx.boundary_matched is False
    assert ctx.boundary_uncertain is True
    assert ctx.district == "Andaman Isle"
    assert ctx.boundary_distance_m and ctx.boundary_distance_m > 0
    assert any("outside every district polygon" in w for w in ctx.warnings)


def test_overlapping_districts_are_announced_not_hidden(geo_store):
    ctx = locations.admin_context(78.6, 21.3)
    assert ctx.boundary_matched is True
    assert any("contain this point" in w for w in ctx.warnings)


def test_unnamed_boundary_flags_uncertainty(tmp_path):
    engine = DuckDBSpatialStore(path=tmp_path / "unnamed.duckdb")
    engine.bootstrap()
    engine.replace_layer(
        "admin_districts",
        [_square(70.0, 25.0)],
        [{"district": "Unnamed area", "name_missing": True, "state_name": "Gujarat"}],
    )
    reset_spatial_store(engine)
    try:
        ctx = locations.admin_context(70.5, 25.5)
        assert ctx.boundary_uncertain is True
        assert any("no name upstream" in w for w in ctx.warnings)
    finally:
        reset_spatial_store(None)
        engine.close()


def test_missing_layers_do_not_crash_the_lookup(tmp_path):
    engine = DuckDBSpatialStore(path=tmp_path / "empty.duckdb")
    engine.bootstrap()
    reset_spatial_store(engine)
    try:
        ctx = locations.admin_context(78.0, 21.0)
        assert ctx.boundary_matched is False
        assert any("boundaries are loaded" in w for w in ctx.warnings)
    finally:
        reset_spatial_store(None)
        engine.close()


def test_polygon_selection_reports_area_coverage_and_districts(geo_store):
    polygon = {
        "type": "Polygon",
        "coordinates": [[[78.1, 21.1], [78.9, 21.1], [78.9, 21.9], [78.1, 21.9], [78.1, 21.1]]],
    }
    resolved = locations.resolve_area(polygon=polygon, allow_remote=False)
    assert resolved["selection"]["mode"] == "polygon"
    assert resolved["selection"]["area_km2"] == pytest.approx(8000, rel=0.25)
    assert {d["district"] for d in resolved["districts"]} == {"Testpur", "Overlap City"}
    assert resolved["districts"][0]["overlap_km2"] > 0
    assert any("fell outside every district boundary" in w for w in resolved["warnings"])


def test_polygon_feature_collection_and_string_input_are_accepted(geo_store):
    geometry = {
        "type": "Polygon",
        "coordinates": [[[78.05, 21.05], [78.35, 21.05], [78.35, 21.35], [78.05, 21.35], [78.05, 21.05]]],
    }
    as_string = json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": geometry, "properties": {}}]})
    resolved = locations.resolve_area(polygon=as_string, allow_remote=False)
    assert resolved["districts"][0]["district"] == "Testpur"
    assert resolved["selection"]["vertices"] == 5


def test_self_touching_polygon_is_repaired_not_rejected(geo_store):
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[78.0, 21.0], [78.4, 21.4], [78.4, 21.0], [78.0, 21.4], [78.0, 21.0]]],
    }
    resolved = locations.resolve_area(polygon=bowtie, allow_remote=False)
    assert resolved["selection"]["area_km2"] > 0


def test_non_polygon_selection_is_rejected(geo_store):
    with pytest.raises(locations.ValidationError) as excinfo:
        locations.resolve_area(polygon={"type": "Point", "coordinates": [78.2, 21.2]}, allow_remote=False)
    assert excinfo.value.details["type"] == "Point"


def test_empty_feature_collection_is_rejected(geo_store):
    with pytest.raises(locations.ValidationError):
        locations.resolve_area(polygon={"type": "FeatureCollection", "features": []}, allow_remote=False)


def test_selection_outside_india_is_rejected(geo_store):
    polygon = {
        "type": "Polygon",
        "coordinates": [[[10.0, 50.0], [10.5, 50.0], [10.5, 50.5], [10.0, 50.5], [10.0, 50.0]]],
    }
    with pytest.raises(locations.ValidationError, match="outside India"):
        locations.resolve_area(polygon=polygon, allow_remote=False)


def test_vertex_limit_protects_the_request_budget(geo_store):
    ring = [[92.5 + (i % 2) * 0.001, 11.5 + (i // 2) * 0.001] for i in range(locations.MAX_POLYGON_VERTICES + 10)]
    ring.append(ring[0])
    with pytest.raises(locations.ValidationError) as excinfo:
        locations.resolve_area(polygon={"type": "Polygon", "coordinates": [ring]}, allow_remote=False)
    assert excinfo.value.details["vertices"] > locations.MAX_POLYGON_VERTICES


def test_radius_selection_defaults_and_parses_text(geo_store):
    resolved = locations.resolve_area(lat=21.2, lon=78.2, radius="3 km", allow_remote=False)
    assert resolved["selection"] == {"mode": "radius", "radius_m": 3000.0, "lat": 21.2, "lon": 78.2}
    assert resolved["terrain"]["available"] in (True, False)
    with pytest.raises(locations.ValidationError):
        locations.resolve_area(lat=21.2, lon=78.2, radius="9000 km", allow_remote=False)


def test_selection_without_any_input_is_rejected(geo_store):
    with pytest.raises(locations.ValidationError):
        locations.resolve_area(allow_remote=False)


def test_blank_query_is_rejected():
    with pytest.raises(locations.ValidationError):
        locations.search_places("   ")


def test_pin_code_without_a_gazetteer_says_so(geo_store):
    found = locations.search_places("380015", allow_remote=False)
    assert found["interpretation"] == "pin_code"
    assert any("no offline PIN gazetteer" in w for w in found["warnings"])


def test_local_register_matches_rank_exact_over_prefix(geo_store):
    with session_scope() as session:
        session.add_all(
            [
                AdminUnit(kind="district", code="0101", name="Testpur", name_norm="testpur", state_code="01", lon=78.2, lat=21.2, source="test"),
                AdminUnit(kind="district", code="0102", name="Testpur Nagar", name_norm="testpur nagar", state_code="01", lon=78.3, lat=21.3, source="test"),
                AdminUnit(kind="state", code="01", name="Test Pradesh", name_norm="test pradesh", state_code="01", lon=78.5, lat=21.5, source="test"),
            ]
        )
    found = locations.search_places("Testpur", limit=5, allow_remote=False)
    labels = [r["label"] for r in found["results"]]
    assert labels[0] == "Testpur"
    assert "Testpur Nagar" in labels
    assert found["results"][0]["source"] == "administrative_register"

    states = locations.search_places("Test Pradesh", limit=5, allow_remote=False)
    assert states["results"][0]["kind"] == "state"


def test_reverse_without_network_still_returns_boundaries(geo_store):
    resolved = locations.reverse_location(78.2, 21.2, allow_remote=False)
    assert resolved["place"]["district"] == "Testpur"
    assert resolved["osm_reverse"] is None


def test_viewport_search_is_truncated_and_reported(geo_store):
    payload = locations.search_by_bounds(78.0, 21.0, 79.0, 22.0, limit=1)
    assert payload["type"] == "FeatureCollection"
    assert payload["truncated"] is True
    assert payload["number_returned"] == 1


def test_viewport_without_layers_raises(tmp_path):
    from app.core.errors import NotFoundError

    engine = DuckDBSpatialStore(path=tmp_path / "empty2.duckdb")
    engine.bootstrap()
    reset_spatial_store(engine)
    try:
        with pytest.raises(NotFoundError):
            locations.search_by_bounds(0, 0, 1, 1)
    finally:
        reset_spatial_store(None)
        engine.close()
