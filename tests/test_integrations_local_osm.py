from __future__ import annotations

"""Local OSM extract reads and the per-layer fallback to Overpass.

The extracts here are hand-written GeoJSON files, not real OSM downloads: the point is to
pin the contract - a listed file that covers the query answers locally, an unlisted or
non-covering file does not, and a layer the extract cannot serve is the only thing sent to
Overpass.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import get_settings
from app.integrations.local_osm import LocalOSMProvider
from app.services.lifelines import lifelines_near

EXTRACT_BBOX = [77.5, 12.85, 77.7, 13.05]
POINT = (77.6, 12.95)


def _write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )


def _point_feature(fid: str, lon: float, lat: float, **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": fid,
        "properties": {"id": fid, **properties},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


@pytest.fixture()
def local_osm(tmp_path, monkeypatch):
    """A manifest with one hospitals file and one bridges file covering the test point."""
    root = tmp_path / "osm_extracts"
    root.mkdir()
    _write_geojson(
        root / "hospitals.geojson",
        [
            _point_feature("h1", 77.601, 12.951, amenity="hospital", name="Victoria Hospital"),
            # A clinic and a pharmacy are near a hospital but are not one; the layer must
            # filter them out rather than counting every amenity point.
            _point_feature("c1", 77.602, 12.952, amenity="clinic", name="Corner Clinic"),
            _point_feature("p1", 77.603, 12.953, amenity="pharmacy", name="City Pharmacy"),
        ],
    )
    _write_geojson(
        root / "bridges.geojson",
        [
            {
                "type": "Feature",
                "properties": {"id": "b1", "highway": "primary", "bridge": "yes", "name": "Market Overbridge"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[77.598, 12.948], [77.606, 12.956]],
                },
            },
            # A road with no bridge tag is not a bridge even though it shares the file.
            {
                "type": "Feature",
                "properties": {"id": "r1", "highway": "primary"},
                "geometry": {"type": "LineString", "coordinates": [[77.59, 12.94], [77.61, 12.96]]},
            },
        ],
    )
    manifest = {
        "extracts": [
            {
                "name": "bengaluru-hospitals",
                "path": "hospitals.geojson",
                "bbox": EXTRACT_BBOX,
                "vintage": "2026-10-01",
                "source": "synthetic test extract",
                "license": "ODbL 1.0",
                "layers": {"hospitals": "hospitals"},
            },
            {
                "name": "bengaluru-bridges",
                "path": "bridges.geojson",
                "bbox": EXTRACT_BBOX,
                "vintage": "2026-10-01",
                "source": "synthetic test extract",
                "license": "ODbL 1.0",
                "layers": {"bridges": "bridges"},
            },
        ]
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(get_settings(), "osm_extract_dir", root)
    monkeypatch.setattr(get_settings(), "osm_local_enabled", True)
    return root


def test_a_covering_extract_answers_without_any_network(local_osm):
    provider = LocalOSMProvider()
    assert provider.extract_count == 2

    out = provider.infrastructure_near(*POINT, 2_000, layers=("hospitals",))

    assert out["local_status"] == "used"
    layer = out["layers"]["hospitals"]
    assert layer["status"] == "ok"
    assert layer["origin"] == "local"
    assert layer["count"] == 1
    hospital = layer["features"][0]
    assert hospital["name"] == "Victoria Hospital"
    assert hospital["kind"] == "node"
    # A plain `id` is not an OSM id and must not be given a node/way prefix.
    assert hospital["osm_id"] == "h1"
    assert hospital["distance_m"] > 0
    assert hospital["extract"] == "bengaluru-hospitals"


def test_every_local_answer_carries_file_provenance_with_its_vintage(local_osm):
    provider = LocalOSMProvider()
    out = provider.infrastructure_near(*POINT, 2_000, layers=("hospitals",))

    assert out["provenance"], "an empty result is still an observation and must be recorded"
    entry = out["provenance"][0]
    assert entry["provider"] == "osm_local"
    assert entry["endpoint"].startswith("file://")
    assert entry["from_cache"] is True
    assert entry["notes"]["origin"] == "local_extract"
    assert entry["notes"]["vintage"] == "2026-10-01"
    assert entry["notes"]["source"] == "synthetic test extract"
    assert entry["notes"]["features"] == 1


def test_bridge_lines_are_normalised_and_non_bridge_roads_dropped(local_osm):
    provider = LocalOSMProvider()
    out = provider.infrastructure_near(*POINT, 2_000, layers=("bridges",))

    layer = out["layers"]["bridges"]
    assert layer["status"] == "ok"
    assert layer["count"] == 1
    bridge = layer["features"][0]
    assert bridge["kind"] == "way"
    assert bridge["name"] == "Market Overbridge"
    assert bridge["geometry"]["type"] == "LineString"
    assert "lat" not in bridge, "a line is stored as a line, not collapsed to a point"


def test_a_point_outside_every_extract_is_a_coverage_miss_not_an_empty_layer(local_osm):
    provider = LocalOSMProvider()
    out = provider.infrastructure_near(77.0, 20.0, 2_000, layers=("hospitals",))

    assert out["local_status"] == "outside_coverage"
    layer = out["layers"]["hospitals"]
    assert layer["status"] == "outside_coverage"
    assert layer["features"] == []
    assert any("No local extract fully contains" in warning for warning in out["warnings"])


def test_a_manifest_entry_whose_file_is_missing_is_not_used(tmp_path, monkeypatch):
    root = tmp_path / "osm_extracts"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps({"extracts": [{"name": "gone", "path": "gone.geojson", "bbox": EXTRACT_BBOX}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "osm_extract_dir", root)

    provider = LocalOSMProvider()
    assert provider.extract_count == 0
    assert provider.healthcheck().state == "unconfigured"
    out = provider.infrastructure_near(*POINT, 2_000, layers=("hospitals",))
    assert out["local_status"] == "unconfigured"


def test_a_disabled_local_layer_falls_back_explicitly(local_osm, monkeypatch):
    monkeypatch.setattr(get_settings(), "osm_local_enabled", False)
    provider = LocalOSMProvider()
    out = provider.infrastructure_near(*POINT, 2_000, layers=("hospitals",))

    assert out["local_status"] == "disabled"
    assert out["layers"]["hospitals"]["status"] == "unavailable"
    assert any("OSM_LOCAL_ENABLED" in warning for warning in out["warnings"])


class _Overpass:
    attribution = "Map data (c) OpenStreetMap contributors, ODbL"
    license = "ODbL 1.0"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def infrastructure_near(self, lon, lat, radius_m, *, layers=(), deadline_seconds=None):
        self.calls.append({"layers": tuple(layers), "deadline_seconds": deadline_seconds})
        if self.fail:
            raise TimeoutError("mirror gave up")
        return {
            "layers": {
                name: {
                    "features": [],
                    "count": 0,
                    "status": "ok",
                    "truncated": False,
                    "role": None,
                }
                for name in layers
            },
            "provenance": [],
            "warnings": [],
            "deadline_seconds": deadline_seconds,
        }


def test_a_covered_lookup_never_touches_overpass(local_osm):
    local = LocalOSMProvider()
    remote = _Overpass()

    out = lifelines_near(*POINT, 2_000, layers=("hospitals", "bridges"), local=local, overpass=remote)

    assert remote.calls == [], "a fully covered lookup must not make an Overpass request"
    assert out["remote_used"] is False
    assert out["origins"] == {"local": 2}
    assert out["origin_states"] == {"local": "available"}
    assert set(out["layers"]) == {"hospitals", "bridges"}


def test_only_the_layers_the_extract_cannot_serve_go_to_overpass(local_osm):
    local = LocalOSMProvider()
    remote = _Overpass()

    out = lifelines_near(
        *POINT,
        2_000,
        layers=("hospitals", "emergency_services"),
        local=local,
        overpass=remote,
    )

    assert len(remote.calls) == 1
    assert remote.calls[0]["layers"] == ("emergency_services",)
    assert remote.calls[0]["deadline_seconds"] is not None
    assert out["origins"] == {"local": 1, "overpass": 1}
    assert out["origin_states"]["local"] == "available"
    assert out["origin_states"]["overpass"] == "available"
    assert out["layers"]["hospitals"]["origin"] == "local"
    assert out["layers"]["emergency_services"]["origin"] == "overpass"


def test_a_failed_fallback_keeps_the_local_reason_and_does_not_invent_features(local_osm):
    local = LocalOSMProvider()
    remote = _Overpass(fail=True)

    out = lifelines_near(
        *POINT,
        2_000,
        layers=("hospitals", "emergency_services"),
        local=local,
        overpass=remote,
    )

    assert out["layers"]["hospitals"]["status"] == "ok"
    failed = out["layers"]["emergency_services"]
    assert failed["status"] == "unavailable"
    assert failed["features"] == []
    assert "local:" in failed["reason"] and "overpass:" in failed["reason"]
    assert any("gaps, not zeros" in warning for warning in out["warnings"])


# -- GDAL OSM (.pbf) reads ----------------------------------------------------
def _pbf_manifest(tmp_path, monkeypatch) -> Path:
    """A manifest entry for a `.pbf`; the file is never parsed because reads are stubbed."""
    root = tmp_path / "osm_pbf"
    root.mkdir()
    (root / "region.osm.pbf").write_bytes(b"")
    manifest = {
        "extracts": [
            {
                "name": "region",
                "path": "region.osm.pbf",
                "bbox": EXTRACT_BBOX,
                "vintage": "2026-10-25",
                "source": "Geofabrik",
            }
        ]
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(get_settings(), "osm_extract_dir", root)
    monkeypatch.setattr(get_settings(), "osm_local_enabled", True)
    return root


def test_other_tags_column_is_parsed_like_real_gdal_output(tmp_path, monkeypatch):
    """GDAL promotes only a fixed set of tags to columns; `amenity` and `bridge` are not
    among them. An extract read without `other_tags` answers "ok" with zero features."""
    root = tmp_path / "osm_extracts"
    root.mkdir()
    _write_geojson(
        root / "hospitals.geojson",
        [
            {
                "type": "Feature",
                "properties": {
                    "id": "h9",
                    "name": 'The "Old" Hospital',
                    "other_tags": '"amenity"=>"hospital","healthcare"=>"hospital"',
                },
                "geometry": {"type": "Point", "coordinates": [77.601, 12.951]},
            },
            {
                "type": "Feature",
                "properties": {"id": "c9", "other_tags": '"amenity"=>"clinic"'},
                "geometry": {"type": "Point", "coordinates": [77.602, 12.952]},
            },
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps({"extracts": [{"name": "h", "path": "hospitals.geojson", "bbox": EXTRACT_BBOX, "layers": {"hospitals": "hospitals"}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "osm_extract_dir", root)
    monkeypatch.setattr(get_settings(), "osm_local_enabled", True)

    out = LocalOSMProvider().infrastructure_near(*POINT, 2_000, layers=("hospitals",))

    layer = out["layers"]["hospitals"]
    assert layer["status"] == "ok"
    assert layer["count"] == 1
    assert layer["features"][0]["tags"]["amenity"] == "hospital"
    assert layer["features"][0]["tags"]["healthcare"] == "hospital"


def test_other_tags_escaped_quotes_round_trip(tmp_path, monkeypatch):
    root = tmp_path / "osm_extracts"
    root.mkdir()
    _write_geojson(
        root / "schools.geojson",
        [
            {
                "type": "Feature",
                "properties": {"id": "s9", "other_tags": '"amenity"=>"school","operator"=>"St \\"Mary\\" Trust"'},
                "geometry": {"type": "Point", "coordinates": [77.601, 12.951]},
            }
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps({"extracts": [{"name": "s", "path": "schools.geojson", "bbox": EXTRACT_BBOX, "layers": {"schools": "schools"}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "osm_extract_dir", root)
    monkeypatch.setattr(get_settings(), "osm_local_enabled", True)

    out = LocalOSMProvider().infrastructure_near(*POINT, 2_000, layers=("schools",))

    assert out["layers"]["schools"]["features"][0]["tags"]["operator"] == 'St "Mary" Trust'


def test_pbf_layer_choice_follows_the_tags_not_the_name(tmp_path, monkeypatch):
    import pyogrio

    _pbf_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pyogrio,
        "list_layers",
        lambda path: [("points", "Point"), ("lines", "LineString"), ("multipolygons", "MultiPolygon")],
    )
    provider = LocalOSMProvider()
    spec = provider.extracts()[0]

    assert provider._candidate_layers(spec, "hospitals") == ["points", "multipolygons"]
    assert provider._candidate_layers(spec, "schools") == ["points", "multipolygons"]
    assert provider._candidate_layers(spec, "bridges") == ["lines"]
    assert provider._candidate_layers(spec, "water_structures") == ["points", "lines", "multipolygons"]


def test_points_and_areas_are_merged_with_typed_ids(tmp_path, monkeypatch):
    """A hospital mapped as a closed way is not a second-class citizen: both layers are read
    and the id prefix states which OSM namespace the feature belongs to."""
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point, Polygon

    _pbf_manifest(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pyogrio,
        "list_layers",
        lambda path: [("points", "Point"), ("lines", "LineString"), ("multipolygons", "MultiPolygon")],
    )
    frames = {
        "points": gpd.GeoDataFrame(
            {"osm_id": ["101"], "name": ["Point Hospital"], "other_tags": ['"amenity"=>"hospital"']},
            geometry=[Point(77.602, 12.951)],
            crs="EPSG:4326",
        ),
        "multipolygons": gpd.GeoDataFrame(
            {"osm_id": [None], "osm_way_id": ["202"], "name": ["Area Hospital"], "other_tags": ['"amenity"=>"hospital"']},
            geometry=[
                Polygon([(77.598, 12.948), (77.606, 12.948), (77.606, 12.956), (77.598, 12.956)])
            ],
            crs="EPSG:4326",
        ),
    }
    provider = LocalOSMProvider()
    monkeypatch.setattr(provider, "_read_layer", lambda spec, layer, box: frames.get(layer, gpd.GeoDataFrame()))

    out = provider.infrastructure_near(*POINT, 2_000, layers=("hospitals",))

    layer = out["layers"]["hospitals"]
    assert layer["status"] == "ok"
    assert layer["count"] == 2
    ids = {feature["osm_id"] for feature in layer["features"]}
    assert ids == {"node/101", "way/202"}


def test_emergency_services_matches_the_legacy_amenity_tags(tmp_path, monkeypatch):
    """`amenity=fire_station` / `amenity=police` are deprecated but still widespread; the
    local matcher must use the same set as the Overpass selectors."""
    root = tmp_path / "osm_extracts"
    root.mkdir()
    _write_geojson(
        root / "emergency.geojson",
        [
            {"type": "Feature", "properties": {"id": "f1", "other_tags": '"amenity"=>"fire_station"'}, "geometry": {"type": "Point", "coordinates": [77.601, 12.951]}},
            {"type": "Feature", "properties": {"id": "p1", "other_tags": '"emergency"=>"police"'}, "geometry": {"type": "Point", "coordinates": [77.603, 12.953]}},
            {"type": "Feature", "properties": {"id": "a1", "other_tags": '"emergency"=>"ambulance_station"'}, "geometry": {"type": "Point", "coordinates": [77.604, 12.954]}},
            {"type": "Feature", "properties": {"id": "x1", "other_tags": '"amenity"=>"hospital"'}, "geometry": {"type": "Point", "coordinates": [77.605, 12.955]}},
        ],
    )
    (root / "manifest.json").write_text(
        json.dumps({"extracts": [{"name": "e", "path": "emergency.geojson", "bbox": EXTRACT_BBOX, "layers": {"emergency_services": "emergency"}}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "osm_extract_dir", root)
    monkeypatch.setattr(get_settings(), "osm_local_enabled", True)

    out = LocalOSMProvider().infrastructure_near(*POINT, 2_000, layers=("emergency_services",))

    assert out["layers"]["emergency_services"]["count"] == 3
