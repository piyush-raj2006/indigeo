from __future__ import annotations

"""Parsing and blending of the live Indian hazard feeds.

The connectors are tested at two levels: the pure parsers against hand-written CAP, RSS,
gauge JSON and GeoJSON payloads, and the providers against respx-mocked HTTP responses so
the request shape (notably the WFS bbox) is verified. An unconfigured feed must stay a gap,
never a zero-severity observation.
"""

from typing import Any

import pytest
import respx

from app.core.errors import ProviderUnavailableError
from app.integrations.india_alerts import (
    BhuvanHazardsProvider,
    CwcGaugesProvider,
    ImdAlertsProvider,
    gauge_severity,
    parse_bhuvan_features,
    parse_cwc_gauges,
    parse_imd_alerts,
)
from app.services import live_alerts
from app.services.hazard import assess_context
from _hazard_fixtures import factor, full_context

POINT = (77.6, 12.95)


def _cap(identifier: str, *, event: str, severity: str, polygon: str | None, area: str) -> str:
    polygon_xml = f"<polygon>{polygon}</polygon>" if polygon else ""
    return f"""
    <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
      <identifier>{identifier}</identifier>
      <info>
        <category>Met</category>
        <event>{event}</event>
        <severity>{severity}</severity>
        <urgency>Expected</urgency>
        <certainty>Likely</certainty>
        <headline>{event} warning</headline>
        <description>{event} is likely over the district.</description>
        <area>
          <areaDesc>{area}</areaDesc>
          {polygon_xml}
        </area>
      </info>
    </alert>
    """


CAP_DOCUMENT = (
    "<alerts>"
    + _cap(
        "IMD-1",
        event="Heavy Rainfall",
        severity="Severe",
        polygon="12.90,77.55 13.00,77.55 13.00,77.65 12.90,77.55",
        area="Bengaluru Urban",
    )
    + _cap("IMD-2", event="Cyclone Warning", severity="Extreme", polygon=None, area="Bengaluru Urban")
    + "</alerts>"
)

RSS_DOCUMENT = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>IMD warnings</title>
    <item>
      <title>Cyclone warning for the east coast</title>
      <description>Deep depression likely to intensify; gale winds expected.</description>
      <category>Cyclone</category>
      <guid>IMD-RSS-1</guid>
      <pubDate>Mon, 26 Oct 2026 06:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

GAUGE_DOCUMENT = {
    "gauges": [
        {
            "station": "Halasur",
            "river": "Pennar",
            "lat": 12.95,
            "lon": 77.60,
            "current_level": 5.5,
            "warning_level": 5.0,
            "danger_level": 6.0,
            "observed_at": "2026-10-26T06:00:00+05:30",
        },
        {
            "station": "Unrated",
            "lat": 12.951,
            "lon": 77.601,
            "water_level": 3.1,
        },
        {"station": "No coordinates", "level": 2.0, "warning_level": 3.0},
    ]
}

BHUVAN_DOCUMENT = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"hazard": "flood", "susceptibility": "High", "district": "Bengaluru Urban"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[77.55, 12.90], [77.65, 12.90], [77.65, 13.00], [77.55, 12.90]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"theme": "landslide", "hazard_class": "Moderate"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[77.59, 12.94], [77.61, 12.94], [77.61, 12.96], [77.59, 12.94]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"layer": "flood inundation extent"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[77.595, 12.945], [77.605, 12.945], [77.605, 12.955], [77.595, 12.945]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"hazard": "flood", "susceptibility": "Low"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[70.0, 20.0], [70.1, 20.0], [70.1, 20.1], [70.0, 20.0]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"name": "Unattributed polygon"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[77.599, 12.949], [77.601, 12.949], [77.601, 12.951], [77.599, 12.949]]],
            },
        },
    ],
}


# -- IMD parsing --------------------------------------------------------------
def test_cap_is_parsed_with_a_swapped_geojson_polygon_and_tags():
    alerts = parse_imd_alerts(CAP_DOCUMENT)
    assert len(alerts) == 2

    rainfall = alerts[0]
    assert rainfall["format"] == "cap"
    assert rainfall["severity_score"] == 0.75
    assert "rain" in rainfall["tags"]
    assert rainfall["areas"] == ["Bengaluru Urban"]
    geometry = rainfall["geometry"]
    assert geometry["type"] == "Polygon"
    # CAP states lat,lon; GeoJSON requires lon,lat. A silent swap would move the warning.
    assert geometry["coordinates"][0][0] == [77.55, 12.9]
    assert alerts[1]["severity_score"] == 1.0
    assert "cyclone" in alerts[1]["tags"]


def test_rss_items_are_parsed_and_tagged():
    alerts = parse_imd_alerts(RSS_DOCUMENT)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["format"] == "rss"
    assert alert["id"] == "IMD-RSS-1"
    assert {"cyclone", "wind"} <= set(alert["tags"])
    # RSS carries no CAP severity, so no number may be invented for it.
    assert alert["severity_score"] is None


def test_json_alerts_are_read_from_a_wrapper():
    alerts = parse_imd_alerts(
        '{"alerts": [{"title": "Heat wave warning", "severity": "High", "areas": ["Delhi"]}]}'
    )
    assert len(alerts) == 1
    assert alerts[0]["severity_score"] == 0.75
    assert "heat" in alerts[0]["tags"]


@pytest.mark.parametrize("document", ["", "not xml at all", "<html><body>maintenance</body></html>"])
def test_unusable_imd_documents_raise_instead_of_returning_no_alerts(document):
    with pytest.raises(ProviderUnavailableError):
        parse_imd_alerts(document)


def test_imd_provider_matches_by_geometry_and_admin_name():
    provider = ImdAlertsProvider()
    provider.base_url = "https://imd.example/warnings.xml"
    provider.cacheable = False
    with respx.mock:
        route = respx.get(provider.base_url).respond(200, text=CAP_DOCUMENT)
        out = provider.feed_near(*POINT, 2_000, admin={"district": "Bengaluru Urban"})

    assert route.call_count == 1
    assert out["count"] == 2
    assert out["matched_count"] == 2
    assert {alert["matched_by"] for alert in out["matched"]} == {"geometry", "admin_name"}
    assert out["provenance"]["notes"]["format"] == "cap"
    assert out["provenance"]["notes"]["matched"] == 2


# -- CWC parsing --------------------------------------------------------------
def test_gauge_rows_accept_aliases_and_count_unusable_rows():
    gauges, skipped = parse_cwc_gauges(__import__("json").dumps(GAUGE_DOCUMENT))

    assert skipped == 1
    assert len(gauges) == 2
    rated = gauges[0]
    assert rated["station"] == "Halasur"
    assert rated["level_m"] == 5.5
    assert rated["severity"] == 0.75
    assert gauges[1]["severity"] is None, "a gauge without thresholds cannot be classified"


def test_gauge_severity_uses_the_published_thresholds():
    assert gauge_severity(6.0, 5.0, 6.0) == 1.0
    assert gauge_severity(4.9, 5.0, 6.0) == 0.0
    assert gauge_severity(5.5, 5.0, 6.0) == pytest.approx(0.75)
    assert gauge_severity(3.0, None, None) is None


def test_cwc_provider_reports_an_unrated_gauge_without_a_fake_severity():
    provider = CwcGaugesProvider()
    provider.base_url = "https://cwc.example/gauges.json"
    provider.cacheable = False
    with respx.mock:
        respx.get(provider.base_url).respond(200, json=GAUGE_DOCUMENT)
        out = provider.feed_near(*POINT, 2_000)

    assert out["count"] == 2
    assert out["max_severity"] == 0.75
    assert out["nearest"]["station"] == "Halasur"
    assert out["unrated"] == 1
    assert any("could not be matched" in warning for warning in out["warnings"])

    unrated_only = {"gauges": [GAUGE_DOCUMENT["gauges"][1]]}
    with respx.mock:
        respx.get("https://cwc.example/unrated.json").respond(200, json=unrated_only)
        provider.base_url = "https://cwc.example/unrated.json"
        unrated = provider.feed_near(*POINT, 2_000)
    assert unrated["max_severity"] is None
    assert any("no warning/danger threshold" in warning for warning in unrated["warnings"])


# -- Bhuvan parsing -----------------------------------------------------------
def test_bhuvan_features_map_susceptibility_and_inundation():
    features = parse_bhuvan_features(__import__("json").dumps(BHUVAN_DOCUMENT))
    assert len(features) == 5
    assert features[0]["hazard"] == "flood"
    assert features[0]["severity"] == 0.75
    assert features[1]["hazard"] == "landslide"
    assert features[1]["severity"] == 0.5
    # A mapped inundation extent is an observation that water reached the cell.
    assert features[2]["hazard"] == "flood"
    assert features[2]["inundation"] is True
    assert features[2]["severity"] == 1.0
    assert features[4]["hazard"] == "unclassified"


def test_bhuvan_provider_filters_to_intersecting_polygons():
    provider = BhuvanHazardsProvider()
    provider.base_url = "https://bhuvan.example/hazards.geojson"
    provider.cacheable = False
    with respx.mock:
        respx.get(provider.base_url).respond(200, json=BHUVAN_DOCUMENT)
        out = provider.feed_near(*POINT, 2_000)

    assert out["features"] == 5
    assert out["matched"] == 4, "the distant flood polygon must not be matched"
    assert out["unclassified"] == 1
    assert out["hazards"]["flood"]["max_severity"] == 1.0
    assert out["hazards"]["landslide"]["max_severity"] == 0.5
    assert any("did not declare a hazard type" in warning for warning in out["warnings"])


def test_a_wfs_url_gets_a_bbox_and_never_duplicates_its_own_parameters():
    provider = BhuvanHazardsProvider()
    provider.base_url = (
        "https://bhuvan.example/geoserver/wfs?service=WFS&request=GetFeature"
        "&typeName=flood:inundation&outputFormat=application/json"
    )
    provider.cacheable = False
    with respx.mock:
        route = respx.route(method="GET", host="bhuvan.example").respond(200, json={"type": "FeatureCollection", "features": []})
        provider.feed_near(*POINT, 2_000)

    params = route.calls.last.request.url.params
    assert "bbox" in params
    assert params["bbox"].endswith("EPSG:4326")
    assert params.get_list("outputFormat") == ["application/json"], "outputFormat was sent twice"
    assert params["srsName"] == "EPSG:4326"
    assert params["typeName"] == "flood:inundation"


# -- configuration and blending ----------------------------------------------
def test_an_unconfigured_feed_says_so_and_makes_no_request():
    for provider, env in (
        (ImdAlertsProvider(), "IMD_ALERTS_URL"),
        (CwcGaugesProvider(), "CWC_GAUGES_URL"),
        (BhuvanHazardsProvider(), "BHUVAN_HAZARDS_URL"),
    ):
        assert env in (provider.auth_problem() or "")
        assert provider.healthcheck().state == "unconfigured"


def test_gather_reports_every_feed_as_unconfigured_when_none_is_configured():
    bundle = live_alerts.gather_live_alerts(*POINT, 2_000)
    assert bundle["configured"] is False
    assert {entry["status"] for entry in bundle["feeds"].values()} == {"unconfigured"}
    assert any("No live Indian hazard feed is configured" in warning for warning in bundle["warnings"])


class _Feed:
    def __init__(self, *, configured: bool, payload: dict[str, Any] | None = None, error: Exception | None = None):
        self.configured = configured
        self.payload = payload or {}
        self.error = error

    def auth_problem(self):
        return None if self.configured else "FEED_URL is not configured."

    def feed_near(self, lon, lat, radius_m, *, admin=None):
        if self.error is not None:
            raise self.error
        return self.payload


def test_gather_keeps_a_failed_configured_feed_as_unavailable(monkeypatch):
    feeds = {
        "imd_alerts": _Feed(configured=True, error=TimeoutError("mirror gave up")),
        "cwc_gauges": _Feed(configured=False),
        "bhuvan_hazards": _Feed(configured=False),
    }
    monkeypatch.setattr(live_alerts, "get_provider", lambda key: feeds[key])

    bundle = live_alerts.gather_live_alerts(*POINT, 2_000)

    assert bundle["feeds"]["imd"]["status"] == "unavailable"
    assert "TimeoutError" in bundle["feeds"]["imd"]["reason"]
    assert bundle["configured"] is True
    assert any("IMD warnings" in warning for warning in bundle["warnings"])


def test_live_alerts_raise_the_matching_hazard_factor():
    alerts = {
        "feeds": {
            "imd": {
                "provider": "imd_alerts",
                "status": "available",
                "matched": [
                    {
                        "title": "Heavy Rainfall warning",
                        "tags": ["rain"],
                        "severity_score": 0.75,
                        "matched_by": "geometry",
                    }
                ],
            },
            "cwc": {
                "provider": "cwc_gauges",
                "status": "available",
                "count": 1,
                "max_severity": 1.0,
                "nearest": {
                    "station": "Halasur",
                    "river": "Pennar",
                    "level_m": 6.2,
                    "warning_level_m": 5.0,
                    "danger_level_m": 6.0,
                    "distance_m": 850.0,
                },
            },
            "bhuvan": {
                "provider": "bhuvan_hazards",
                "status": "available",
                "features": 4,
                "hazards": {"flood": {"max_severity": 0.8, "features": 2, "classes": ["High"]}},
            },
        }
    }
    flood = assess_context(full_context(alerts=alerts), ["flood"])[0]

    imd = factor(flood, "imd_heavy_rain_alert")
    assert imd is not None and imd.available is True
    assert imd.severity == 0.75
    assert imd.observed["matched_alerts"] == 1
    assert "imd_alerts" in flood.details["live_alerts"]["feeds_used"]

    gauge = factor(flood, "cwc_river_gauge_level")
    assert gauge is not None and gauge.available is True
    assert gauge.severity == 1.0
    assert gauge.observed["nearest_station"] == "Halasur"

    inundation = factor(flood, "mapped_flood_inundation")
    assert inundation is not None and inundation.available is True
    assert inundation.severity == 0.8
    assert inundation.observed["classes"] == ["High"]


def test_unconfigured_feeds_add_no_factor_and_failed_feeds_do():
    unconfigured = {
        "feeds": {
            "imd": {"provider": "imd_alerts", "status": "unconfigured"},
            "cwc": {"provider": "cwc_gauges", "status": "unconfigured"},
            "bhuvan": {"provider": "bhuvan_hazards", "status": "unconfigured"},
        }
    }
    flood = assess_context(full_context(alerts=unconfigured), ["flood"])[0]
    names = {entry.name for entry in flood.factors}
    assert "imd_heavy_rain_alert" not in names
    assert "cwc_river_gauge_level" not in names
    assert "mapped_flood_inundation" not in names

    failed = {
        "feeds": {
            "imd": {"provider": "imd_alerts", "status": "unavailable", "reason": "timeout"},
            "cwc": {"provider": "cwc_gauges", "status": "unconfigured"},
            "bhuvan": {"provider": "bhuvan_hazards", "status": "unconfigured"},
        }
    }
    flood = assess_context(full_context(alerts=failed), ["flood"])[0]
    gap = factor(flood, "imd_heavy_rain_alert")
    assert gap is not None and gap.available is False
    assert gap.source == "imd_alerts"
    assert "timeout" in gap.note
