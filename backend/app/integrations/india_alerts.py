"""Live Indian hazard feeds: IMD bulletins, CWC river gauges, Bhuvan/NDMA hazard maps.

None of these publishers offers a stable, key-free endpoint that this build can assume.
Each connector therefore stays **unconfigured** until the operator sets the URL they are
entitled to query:

===========================  ====================  ==============================
connector key                environment variable  expected payload
===========================  ====================  ==============================
``imd_alerts``               ``IMD_ALERTS_URL``    CAP XML, RSS/Atom or alert JSON
``cwc_gauges``               ``CWC_GAUGES_URL``    gauge JSON (``CWC_API_KEY`` optional)
``bhuvan_hazards``           ``BHUVAN_HAZARDS_URL``GeoJSON / WFS GetFeature JSON
===========================  ====================  ==============================

An unconfigured feed is reported as ``unconfigured`` and adds no factor to the score. It
is never queried at a guessed address, and no alert is ever invented when the feed is
down. When a feed does answer, only alerts whose geometry intersects the analysis circle
or whose area name matches the resolved state/district are used, and every match records
how it was made (``matched_by``).
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.errors import ProviderUnavailableError
from ..core.logging import get_logger
from ..gis.coords import haversine_m
from .base import Provider, ProviderResult, ProviderStatus

log = get_logger("integrations.india_alerts")

# Category words used to decide which hazard an alert can inform. Matching is on the
# alert's own text, so an alert that says nothing recognisable stays visible but is not
# converted into a severity.
_TAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "rain": re.compile(r"\b(heavy|very heavy|extremely heavy)?\s*(rain|rainfall|shower|precipitation)\b", re.I),
    "thunderstorm": re.compile(r"\b(thunder|lightning|hail)\w*\b", re.I),
    "cyclone": re.compile(r"\b(cyclone|depression|deep depression|storm|squall)\w*\b", re.I),
    "heat": re.compile(r"\b(heat\s*wave|heatwave|heat|hot\s*weather)\b", re.I),
    "flood": re.compile(r"\b(flood|inundat|waterlog)\w*\b", re.I),
    "landslide": re.compile(r"\b(landslide|land\s*slide|landslip|slope\s*failure)\w*\b", re.I),
    "fog": re.compile(r"\b(fog|visibility)\b", re.I),
    "cold": re.compile(r"\b(cold\s*wave|coldwave|frost)\b", re.I),
    "wind": re.compile(r"\b(wind|gale)\w*\b", re.I),
}

_HAZARD_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("landslide", ("landslide", "land slide", "landslip", "slope instability", "slope failure")),
    ("flood", ("flood", "inundation", "inundated", "waterlogging")),
)
_INUNDATION_WORDS = ("inundation", "inundated", "flood extent", "flooded area")


class _IndiaFeed(Provider):
    """Shared configuration/caching for a government feed whose URL we do not know."""

    key = "india_feed"
    env_var = ""
    setting_name = ""
    key_setting_name: str | None = None
    requires_auth = False
    cacheable = True

    def __init__(self) -> None:
        super().__init__()
        self.base_url = str(getattr(self._settings, self.setting_name) or "").strip()
        key = getattr(self._settings, self.key_setting_name) if self.key_setting_name else None
        self._api_key = str(key or "").strip()

    def credentials(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def auth_problem(self) -> str | None:
        if not self.base_url:
            return (
                f"{self.env_var} is not configured. No live feed was queried, so no alert "
                "was blended into this score."
            )
        return None

    def credential_env(self) -> str | None:
        return self.env_var

    def _fetch_text(self, params: dict[str, Any] | None = None) -> tuple[str, ProviderResult]:
        url = self.base_url
        if params:
            # httpx replaces a URL's query string when `params` is supplied. A WFS URL
            # carries service/request/typeName in that query, so merge instead of dropping it.
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query.update(params)
            url = urlunsplit(parsed._replace(query=urlencode(query)))
        return self.get_text(
            url,
            cache_ttl=self._settings.live_feed_cache_ttl_seconds,
            timeout=self._settings.live_feed_timeout_seconds,
        )

    def healthcheck(self) -> ProviderStatus:
        if self.auth_problem() is not None:
            return ProviderStatus(self.key, "unconfigured", self.auth_problem() or "", False, None)
        try:
            text, result = self._fetch_text()
            count = len(self._parse(text))
            return ProviderStatus(
                self.key,
                "healthy",
                f"feed parsed ({count} record(s))",
                False,
                self.base_url,
                datetime.now(timezone.utc).isoformat(),
                result.elapsed_ms,
                result.http_status,
            )
        except Exception as exc:
            return ProviderStatus(
                self.key,
                "degraded",
                f"feed configured but unusable: {type(exc).__name__}: {exc}",
                False,
                self.base_url,
            )

    def _parse(self, text: str) -> list[dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError


class ImdAlertsProvider(_IndiaFeed):
    """IMD severe-weather alerts (CAP preferred; RSS/Atom and alert JSON also parsed)."""

    key = "imd_alerts"
    env_var = "IMD_ALERTS_URL"
    setting_name = "imd_alerts_url"
    attribution = "India Meteorological Department (IMD) public warnings"

    def feed_near(
        self,
        lon: float,
        lat: float,
        radius_m: float,
        *,
        admin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text, result = self._fetch_text()
        alerts = parse_imd_alerts(text)
        circle = query_circle(lon, lat, radius_m)
        matched: list[dict[str, Any]] = []
        for alert in alerts:
            match = match_alert(alert, circle, lon, lat, radius_m, admin)
            if match is not None:
                matched.append({**alert, **match})
        provenance = {
            **result.provenance(),
            "notes": {
                **(result.notes or {}),
                "format": alerts[0]["format"] if alerts else "empty",
                "alerts": len(alerts),
                "matched": len(matched),
                "radius_m": radius_m,
            },
        }
        return {
            "alerts": alerts,
            "matched": matched,
            "count": len(alerts),
            "matched_count": len(matched),
            "provenance": provenance,
            "warnings": [],
        }

    def _parse(self, text: str) -> list[dict[str, Any]]:
        return parse_imd_alerts(text)


class CwcGaugesProvider(_IndiaFeed):
    """CWC real-time river gauge levels, matched to the nearest basin gauge."""

    key = "cwc_gauges"
    env_var = "CWC_GAUGES_URL"
    setting_name = "cwc_gauges_url"
    key_setting_name = "cwc_api_key"
    attribution = "Central Water Commission (CWC) river gauge network"

    def feed_near(
        self,
        lon: float,
        lat: float,
        radius_m: float,
        *,
        admin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text, result = self._fetch_text()
        gauges, skipped = parse_cwc_gauges(text)
        search_radius = max(float(radius_m) * 2.0, float(self._settings.cwc_search_radius_m))
        nearby: list[dict[str, Any]] = []
        for gauge in gauges:
            distance = haversine_m(lat, lon, gauge["lat"], gauge["lon"])
            if distance <= search_radius:
                nearby.append({**gauge, "distance_m": round(distance, 1)})
        nearby.sort(key=lambda entry: entry["distance_m"])
        rated = [entry for entry in nearby if entry.get("severity") is not None]
        warnings: list[str] = []
        if skipped:
            warnings.append(
                f"{skipped} gauge row(s) had no usable coordinates and could not be matched to this point."
            )
        if nearby and not rated:
            warnings.append(
                "CWC gauges answered but carry no warning/danger threshold, so their level could not be "
                "classified as a flood state. The reading is reported, not scored."
            )
        return {
            "gauges": nearby,
            "count": len(nearby),
            "max_severity": max((entry["severity"] for entry in rated), default=None),
            "nearest": nearby[0] if nearby else None,
            "unrated": len(nearby) - len(rated),
            "search_radius_m": search_radius,
            "provenance": {
                **result.provenance(),
                "notes": {
                    **(result.notes or {}),
                    "gauges_total": len(gauges),
                    "gauges_within_radius": len(nearby),
                    "search_radius_m": search_radius,
                    "rows_without_coordinates": skipped,
                },
            },
            "warnings": warnings,
        }

    def _parse(self, text: str) -> list[dict[str, Any]]:
        gauges, _ = parse_cwc_gauges(text)
        return gauges


class BhuvanHazardsProvider(_IndiaFeed):
    """ISRO Bhuvan / NDMA hazard maps served as GeoJSON or an OGC WFS GetFeature."""

    key = "bhuvan_hazards"
    env_var = "BHUVAN_HAZARDS_URL"
    setting_name = "bhuvan_hazards_url"
    key_setting_name = "bhuvan_api_key"
    attribution = "ISRO Bhuvan / National Disaster Management Authority hazard maps"

    def feed_near(
        self,
        lon: float,
        lat: float,
        radius_m: float,
        *,
        admin: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = _wfs_bbox_params(self.base_url, lon, lat, radius_m)
        text, result = self._fetch_text(params)
        features = parse_bhuvan_features(text)
        circle = query_circle(lon, lat, radius_m)
        matched = 0
        unclassified = 0
        no_severity = 0
        groups: dict[str, dict[str, Any]] = {}
        for feature in features:
            geometry = feature.get("geometry")
            if geometry is None:
                continue
            try:
                from shapely.geometry import shape

                geom = shape(geometry)
            except Exception:
                continue
            if not geom.intersects(circle):
                continue
            matched += 1
            hazard = feature.get("hazard") or "unclassified"
            if hazard == "unclassified":
                unclassified += 1
                continue
            severity = feature.get("severity")
            if severity is None:
                no_severity += 1
                continue
            group = groups.setdefault(hazard, {"max_severity": 0.0, "features": 0, "classes": []})
            group["features"] += 1
            group["max_severity"] = max(group["max_severity"], float(severity))
            if feature.get("class") is not None:
                group["classes"].append(feature["class"])
        warnings: list[str] = []
        if unclassified:
            warnings.append(
                f"{unclassified} intersecting Bhuvan feature(s) did not declare a hazard type; "
                "they are reported but not scored."
            )
        if no_severity:
            warnings.append(
                f"{no_severity} intersecting Bhuvan feature(s) had no recognised susceptibility class; "
                "their class value was not converted into a number."
            )
        return {
            "hazards": groups,
            "features": len(features),
            "matched": matched,
            "unclassified": unclassified,
            "provenance": {
                **result.provenance(),
                "notes": {
                    **(result.notes or {}),
                    "features": len(features),
                    "intersecting": matched,
                    "radius_m": radius_m,
                    "wfs_bbox_query": params is not None,
                },
            },
            "warnings": warnings,
        }

    def _parse(self, text: str) -> list[dict[str, Any]]:
        return parse_bhuvan_features(text)


# -- IMD parsing --------------------------------------------------------------
def parse_imd_alerts(text: str) -> list[dict[str, Any]]:
    """Parse CAP XML, RSS/Atom or alert JSON. Raises when the format is unrecognised."""
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        raise ProviderUnavailableError("imd_alerts", "IMD answered with an empty document.")
    if stripped[0] in "[{":
        return _parse_json_alerts(json.loads(stripped))
    if not stripped.startswith("<"):
        raise ProviderUnavailableError(
            "imd_alerts", f"Unrecognised IMD payload (starts with {stripped[:40]!r})."
        )
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise ProviderUnavailableError("imd_alerts", f"IMD XML did not parse: {exc}") from exc
    if _local(root.tag) in {"alert", "cap"} or root.findall(".//{*}alert"):
        return _parse_cap(root)
    if _local(root.tag) in {"rss", "rdf"}:
        return _parse_rss(root)
    if _local(root.tag) == "feed":
        return _parse_atom(root)
    raise ProviderUnavailableError(
        "imd_alerts", f"Unrecognised IMD XML root element <{_local(root.tag)}>."
    )


def _parse_cap(root: ET.Element) -> list[dict[str, Any]]:
    alerts = root.findall(".//{*}alert") or ([root] if _local(root.tag) == "alert" else [])
    out: list[dict[str, Any]] = []
    for alert in alerts:
        infos = alert.findall("{*}info") or alert.findall(".//{*}info")
        info = infos[0] if infos else None
        event = _text(info, "event") if info is not None else None
        category = _text(info, "category") if info is not None else None
        severity_raw = _text(info, "severity") if info is not None else None
        headline = _text(info, "headline") if info is not None else None
        description = _text(info, "description") if info is not None else None
        areas = _all_text(alert, "areaDesc")
        polygons = _all_text(alert, "polygon")
        geometry = _cap_polygon(polygons[0]) if polygons else None
        identifier = _text(alert, "identifier") or _text(alert, "id")
        title = headline or event or identifier
        if not title and not description:
            continue
        record = {
            "id": identifier,
            "title": title,
            "description": description,
            "event": event,
            "category": category,
            "severity": severity_raw,
            "severity_score": _severity_score(severity_raw),
            "urgency": _text(info, "urgency") if info is not None else None,
            "certainty": _text(info, "certainty") if info is not None else None,
            "onset": _text(info, "onset") or _text(info, "effective") if info is not None else None,
            "expires": _text(info, "expires") if info is not None else None,
            "areas": areas,
            "url": _text(alert, "web") or _text(alert, "uri"),
            "geometry": geometry,
            "format": "cap",
        }
        record["tags"] = classify_alert(record)
        out.append(record)
    return out


def _parse_rss(root: ET.Element) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in root.findall(".//{*}item"):
        title = _text(item, "title")
        description = _text(item, "description") or _text(item, "summary")
        if not title and not description:
            continue
        categories = [value for value in _all_text(item, "category") if value]
        link = _link(item) or _text(item, "guid")
        record = {
            "id": _text(item, "guid") or link or title,
            "title": title,
            "description": description,
            "event": categories[0] if categories else None,
            "category": ", ".join(categories) if categories else None,
            "severity": None,
            "severity_score": None,
            "onset": _text(item, "pubDate") or _text(item, "updated"),
            "expires": None,
            "areas": categories,
            "url": link,
            "geometry": None,
            "format": "rss",
        }
        record["tags"] = classify_alert(record)
        out.append(record)
    return out


def _parse_atom(root: ET.Element) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in root.findall("{*}entry"):
        title = _text(entry, "title")
        summary = _text(entry, "summary") or _text(entry, "content")
        if not title and not summary:
            continue
        categories = [el.attrib.get("term", "") for el in entry.findall("{*}category")]
        record = {
            "id": _text(entry, "id"),
            "title": title,
            "description": summary,
            "event": categories[0] if categories else None,
            "category": ", ".join(filter(None, categories)) or None,
            "severity": None,
            "severity_score": None,
            "onset": _text(entry, "updated") or _text(entry, "published"),
            "expires": None,
            "areas": [value for value in categories if value],
            "url": _link(entry),
            "geometry": None,
            "format": "atom",
        }
        record["tags"] = classify_alert(record)
        out.append(record)
    return out


def _parse_json_alerts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = (
            payload.get("alerts")
            or payload.get("features")
            or payload.get("data")
            or payload.get("items")
        )
        if items is None and any(key in payload for key in ("title", "event", "headline")):
            items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        items = None
    if not isinstance(items, list):
        raise ProviderUnavailableError(
            "imd_alerts", "IMD JSON has no alerts/features array this connector recognises."
        )
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # GeoJSON features carry the alert fields under properties.
        props = item.get("properties") if isinstance(item.get("properties"), dict) else item
        geometry = item.get("geometry") if isinstance(item.get("geometry"), dict) else props.get("geometry")
        title = props.get("title") or props.get("headline") or props.get("event")
        description = props.get("description") or props.get("summary") or props.get("details")
        if not title and not description:
            continue
        severity_raw = props.get("severity") or props.get("severity_level")
        areas = props.get("areas") or props.get("area") or props.get("areaDesc")
        if isinstance(areas, str):
            areas = [part.strip() for part in areas.split(",") if part.strip()]
        if not isinstance(areas, list):
            areas = []
        record = {
            "id": props.get("id") or props.get("identifier"),
            "title": title,
            "description": description,
            "event": props.get("event"),
            "category": props.get("category"),
            "severity": severity_raw,
            "severity_score": _severity_score(severity_raw),
            "urgency": props.get("urgency"),
            "certainty": props.get("certainty"),
            "onset": props.get("onset") or props.get("effective") or props.get("valid_from"),
            "expires": props.get("expires") or props.get("valid_to"),
            "areas": [str(value) for value in areas],
            "url": props.get("url") or props.get("link"),
            "geometry": geometry,
            "format": "json",
        }
        record["tags"] = classify_alert(record)
        out.append(record)
    return out


def classify_alert(alert: dict[str, Any]) -> list[str]:
    text = " ".join(
        str(alert.get(key) or "")
        for key in ("title", "description", "event", "category")
    )
    tags = [name for name, pattern in _TAG_PATTERNS.items() if pattern.search(text)]
    return tags


def _severity_score(value: Any) -> float | None:
    from ..services.risk import ordinal_severity

    return ordinal_severity(value)


def _cap_polygon(raw: str) -> dict[str, Any] | None:
    """CAP polygons are ``lat,lon`` pairs separated by whitespace."""
    points: list[list[float]] = []
    for pair in raw.split():
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        points.append([lon, lat])
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    return {"type": "Polygon", "coordinates": [points]}


def _text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    for child in element.iter():
        if _local(child.tag) == name.lower() and (child.text or "").strip():
            return child.text.strip()
    return None


def _all_text(element: ET.Element, name: str) -> list[str]:
    return [
        child.text.strip()
        for child in element.iter()
        if _local(child.tag) == name.lower() and (child.text or "").strip()
    ]


def _link(element: ET.Element) -> str | None:
    for child in element.iter():
        if _local(child.tag) != "link":
            continue
        if (child.text or "").strip():
            return child.text.strip()
        if child.attrib.get("href"):
            return child.attrib["href"]
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


# -- spatial matching ---------------------------------------------------------
def query_circle(lon: float, lat: float, radius_m: float):
    """A local equirectangular circle; accurate to well under a pixel at analysis radii."""
    from shapely import affinity
    from shapely.geometry import Point

    base = Point(0.0, 0.0).buffer(1.0, quad_segs=48)
    scaled = affinity.scale(
        base,
        xfact=max(1e-6, radius_m / (111_320.0 * max(0.05, math.cos(math.radians(lat))))),
        yfact=max(1e-6, radius_m / 110_574.0),
        origin=(0.0, 0.0),
    )
    return affinity.translate(scaled, xoff=lon, yoff=lat)


def match_alert(
    alert: dict[str, Any],
    circle,
    lon: float,
    lat: float,
    radius_m: float,
    admin: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return ``{matched_by, matched_area}`` when the alert applies here, else ``None``."""
    geometry = alert.get("geometry")
    if isinstance(geometry, dict):
        try:
            from shapely.geometry import shape

            geom = shape(geometry)
            if geom.geom_type == "Point":
                distance = haversine_m(lat, lon, float(geom.y), float(geom.x))
                if distance <= radius_m * 1.5:
                    return {"matched_by": "geometry", "matched_area": alert.get("title")}
            elif geom.intersects(circle):
                return {"matched_by": "geometry", "matched_area": alert.get("title")}
            return None
        except Exception:
            return None
    admin_names = _admin_names(admin)
    for area in alert.get("areas") or []:
        normalised = _normalise_name(area)
        if not normalised:
            continue
        for candidate in admin_names:
            if normalised == candidate or (
                len(normalised) >= 4 and (normalised in candidate or candidate in normalised)
            ):
                return {"matched_by": "admin_name", "matched_area": area}
    return None


def _admin_names(admin: dict[str, Any] | None) -> list[str]:
    if not admin:
        return []
    names: list[str] = []
    for key in ("state", "district", "state_name", "district_name", "label"):
        value = admin.get(key)
        if value:
            names.append(_normalise_name(str(value).split(",")[0]))
    return [name for name in names if name]


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z ]+", " ", value.lower()).strip()


# -- CWC parsing --------------------------------------------------------------
def parse_cwc_gauges(text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse gauge JSON into normalised rows; returns ``(gauges, rows_without_coords)``.

    The connector accepts the key aliases used by CWC exports rather than enforcing one
    schema: a row needs a level and a coordinate; warning/danger levels are optional but
    without them the row is reported and not scored.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderUnavailableError(
            "cwc_gauges", f"CWC payload is not JSON ({len(text)} bytes)."
        ) from exc
    if isinstance(payload, dict):
        rows = (
            payload.get("gauges")
            or payload.get("stations")
            or payload.get("data")
            or payload.get("items")
            or payload.get("features")
        )
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        raise ProviderUnavailableError("cwc_gauges", "CWC JSON has no gauges/stations array.")

    gauges: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        props = row.get("properties") if isinstance(row.get("properties"), dict) else row
        if isinstance(row.get("geometry"), dict) and "geometry" not in props:
            props = {**props, "geometry": row["geometry"]}
        record = _normalise_gauge(props)
        if record is None:
            skipped += 1
            continue
        gauges.append(record)
    return gauges, skipped


def _normalise_gauge(row: dict[str, Any]) -> dict[str, Any] | None:
    geometry = row.get("geometry") if isinstance(row.get("geometry"), dict) else None
    lon = _number(_pick(row, "lon", "longitude", "long"))
    lat = _number(_pick(row, "lat", "latitude"))
    if (lon is None or lat is None) and geometry and geometry.get("type") == "Point":
        coords = geometry.get("coordinates") or []
        if len(coords) >= 2:
            lon, lat = _number(coords[0]), _number(coords[1])
    level = _number(_pick(row, "level", "water_level", "current_level", "waterlevel", "value"))
    if lon is None or lat is None or level is None:
        return None
    warning = _number(_pick(row, "warning_level", "warninglevel", "warning"))
    danger = _number(_pick(row, "danger_level", "dangerlevel", "danger"))
    trend = _pick(row, "trend", "trend_status")
    return {
        "station": _pick(row, "station", "station_name", "site", "name"),
        "river": _pick(row, "river", "river_name", "basin"),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "level_m": round(level, 3),
        "warning_level_m": None if warning is None else round(warning, 3),
        "danger_level_m": None if danger is None else round(danger, 3),
        "trend": str(trend) if trend is not None else None,
        "observed_at": _pick(row, "observed_at", "timestamp", "time", "date", "last_updated"),
        "severity": gauge_severity(level, warning, danger),
    }


def gauge_severity(level: float, warning: float | None, danger: float | None) -> float | None:
    """Classify a gauge reading against its published thresholds.

    Without a threshold the reading cannot be called a flood state, so the result is
    ``None`` rather than a guessed severity.
    """
    if warning is None and danger is None:
        return None
    if danger is not None and level >= danger:
        return 1.0
    if warning is not None and danger is not None and danger > warning:
        if level < warning:
            return 0.0
        return 0.5 + 0.5 * min(1.0, (level - warning) / (danger - warning))
    if warning is not None:
        return 1.0 if level >= warning else 0.0
    return 0.0  # danger threshold exists but the level is below it


# -- Bhuvan parsing -----------------------------------------------------------
def parse_bhuvan_features(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderUnavailableError(
            "bhuvan_hazards", f"Bhuvan payload is not GeoJSON ({len(text)} bytes)."
        ) from exc
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        features = payload.get("features")
    elif isinstance(payload, dict) and isinstance(payload.get("features"), list):
        features = payload["features"]
    elif isinstance(payload, list):
        features = payload
    else:
        features = None
    if not isinstance(features, list):
        raise ProviderUnavailableError("bhuvan_hazards", "Bhuvan JSON has no GeoJSON features array.")
    out: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        text_blob = " ".join(str(props.get(key) or "") for key in ("hazard", "hazard_type", "theme", "layer", "title", "name"))
        hazard = _hazard_of(text_blob)
        class_raw = _pick(props, "susceptibility", "hazard_class", "risk_class", "class", "level", "category", "risk")
        inundation = any(word in text_blob.lower() for word in _INUNDATION_WORDS)
        from ..services.risk import ordinal_severity

        severity = ordinal_severity(class_raw)
        if hazard == "flood" and inundation and severity is None:
            # A mapped inundation extent is an observation that water reached this cell.
            severity = 1.0
        out.append(
            {
                "hazard": hazard,
                "class": class_raw,
                "severity": severity,
                "inundation": inundation,
                "geometry": feature.get("geometry"),
                "properties": props,
            }
        )
    return out


def _hazard_of(text: str) -> str:
    lowered = text.lower()
    for hazard, words in _HAZARD_WORDS:
        if any(word in lowered for word in words):
            return hazard
    return "unclassified"


# -- generic helpers ----------------------------------------------------------
def _pick(mapping: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _wfs_bbox_params(url: str, lon: float, lat: float, radius_m: float) -> dict[str, Any] | None:
    """Add a bbox only to a URL that already declares itself a WFS endpoint.

    Appending WFS parameters to an arbitrary URL would change its meaning; when the URL is
    a plain GeoJSON resource the connector downloads it and filters the features locally.
    """
    parsed = urlsplit(url)
    query = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    if "service" not in query or query["service"].lower() != "wfs":
        return None
    from ..gis.coords import meters_to_degrees

    dlon, dlat = meters_to_degrees(lat, radius_m)
    params: dict[str, Any] = {
        "bbox": f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat},EPSG:4326",
    }
    # Only add parameters the operator's URL does not already declare; httpx merges this
    # dict with the URL's own query and duplicate keys would send both values.
    if "outputformat" not in query:
        params["outputFormat"] = "application/json"
    if "srsname" not in query:
        params["srsName"] = "EPSG:4326"
    return params


__all__ = [
    "BhuvanHazardsProvider",
    "CwcGaugesProvider",
    "ImdAlertsProvider",
    "classify_alert",
    "gauge_severity",
    "match_alert",
    "parse_bhuvan_features",
    "parse_cwc_gauges",
    "parse_imd_alerts",
    "query_circle",
]
