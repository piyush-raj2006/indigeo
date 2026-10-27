from __future__ import annotations

import json

import shapely
from shapely.geometry import Point, Polygon
import pytest

from app.core.errors import NotFoundError, ValidationError


def square(lon0: float, lat0: float, side: float = 0.5) -> Polygon:
    return Polygon(
        [
            (lon0, lat0),
            (lon0 + side, lat0),
            (lon0 + side, lat0 + side),
            (lon0, lat0 + side),
            (lon0, lat0),
        ]
    )


def load_districts(store):
    geoms = [square(77.0, 20.0), square(77.6, 20.0), square(80.0, 25.0)]
    attrs = [
        {"district": "A", "state": "Test Pradesh", "population": 1000},
        {"district": "B", "state": "Test Pradesh", "population": 2000},
        {"district": "C", "state": "Other", "population": 3000},
    ]
    wkbs = [shapely.wkb.dumps(g) for g in geoms]
    return store.replace_layer("admin_districts", wkbs, attrs)


def test_replace_layer_returns_stats(store):
    stats = load_districts(store)
    assert stats["features"] == 3
    assert stats["extent"] == [77.0, 20.0, 80.5, 25.5]
    assert "admin_districts" in store.list_layers()
    assert store.count("admin_districts") == 3


def test_features_containing_point(store):
    load_districts(store)
    hits = store.features_containing("admin_districts", 77.2, 20.2)
    assert [h["attributes"]["district"] for h in hits] == ["A"]
    assert hits[0]["distance_m"] == 0.0
    assert hits[0]["geometry"]["type"] == "Polygon"


def test_features_containing_outside_all_polygons(store):
    load_districts(store)
    assert store.features_containing("admin_districts", 72.0, 30.0) == []


def test_missing_layer_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.features_containing("nope_layer", 78.0, 22.0)


def test_unsafe_layer_name_rejected(store):
    with pytest.raises(ValidationError):
        safe = "admin; DROP TABLE users"
        store.replace_layer(safe, [], [])
    with pytest.raises(ValidationError):
        store.count('quotes"bad')


def test_within_radius_uses_geodesic_distance(store):
    store.replace_layer(
        "points_of_interest",
        [shapely.wkb.dumps(Point(77.000, 21.0)), shapely.wkb.dumps(Point(77.450, 21.0))],
        [{"name": "near"}, {"name": "far"}],
    )
    near = store.features_within_radius("points_of_interest", 77.0, 21.0, 5_000)
    assert [n["attributes"]["name"] for n in near] == ["near"]
    assert near[0]["distance_m"] < 100

    both = store.features_within_radius("points_of_interest", 77.0, 21.0, 60_000)
    assert {b["attributes"]["name"] for b in both} == {"near", "far"}
    # 0.45 deg of longitude at 21N is about 46.6 km.
    assert 40_000 < both[1]["distance_m"] < 52_000


def test_nearest_features_sorted_and_capped(store):
    store.replace_layer(
        "hospitals",
        [shapely.wkb.dumps(Point(78.05, 22.0)), shapely.wkb.dumps(Point(78.01, 22.0)), shapely.wkb.dumps(Point(85.0, 28.0))],
        [{"id": 1}, {"id": 2}, {"id": 3}],
    )
    nearest = store.nearest_features("hospitals", 78.0, 22.0, limit=2, max_distance_m=20_000)
    assert [n["attributes"]["id"] for n in nearest] == [2, 1]
    assert nearest[0]["distance_m"] < nearest[1]["distance_m"]


def test_bbox_query_clips_large_polygons(store):
    load_districts(store)
    window = (77.0, 20.0, 77.7, 20.5)
    big = store.features_in_bbox("admin_districts", window, limit=10, max_area_deg2=0.1)
    clipped = [f for f in big if f["attributes"]["district"] == "C"]
    assert clipped == []
    assert {f["attributes"]["district"] for f in big} == {"A", "B"}


def test_replace_layer_is_atomic(store):
    load_districts(store)
    store.replace_layer("admin_districts", [shapely.wkb.dumps(square(70.0, 10.0))], [{"district": "Only"}])
    assert store.count("admin_districts") == 1
    assert store.extent("admin_districts") == [70.0, 10.0, 70.5, 10.5]


def test_geometry_roundtrip_preserves_type(store):
    line = shapely.wkb.dumps(shapely.geometry.LineString([(78.0, 22.0), (78.1, 22.1)]))
    store.replace_layer("rivers", [line], [{"name": "Ganga tributary"}])
    rows = store.features_in_bbox("rivers", (77.9, 21.9, 78.2, 22.2))
    assert rows[0]["geometry"]["type"] == "LineString"
    assert json.dumps(rows[0]["geometry"])
