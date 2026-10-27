"""Place resolution: free text, coordinates, PIN codes and drawn polygons.

Everything a hazard analysis needs about *where* it is being run comes from here,
including how sure we are about that. Boundaries in this build come from OSM-derived
polygons, so a point that falls outside every polygon is reported as an uncertain
match with a distance rather than a silent miss.
"""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import or_, select

from ..core.cache import MISS, TTLCache, cache_for
from ..core.config import get_settings
from ..core.database import session_scope
from ..core.errors import InvalidCoordinates, NotFoundError, ValidationError
from ..core.logging import get_logger
from ..gis.coords import INDIA_BBOX, LatLng, is_indian_bbox, meters_to_degrees, parse_coordinates, parse_radius_m
from ..gis.dem import dem_for_point
from ..gis.spatial import SpatialStore, get_spatial_store
from ..models.catalog import AdminUnit

log = get_logger("services.locations")

MAX_POLYGON_VERTICES = 5000
MAX_POLYGON_AREA_KM2 = 250_000.0
PIN_RE = re.compile(r"^\s*(\d{6})\s*$")
_KIND_ORDER = {"state": 0, "ut": 0, "district": 1, "town": 2, "village": 3}


@dataclass
class PlaceContext:
    """What we know about a point, and how sure we are about it."""

    lon: float
    lat: float
    label: str
    state: str | None = None
    state_code: str | None = None
    district: str | None = None
    district_code: str | None = None
    inside_india: bool = True
    boundary_matched: bool = False
    boundary_uncertain: bool = False
    boundary_distance_m: float | None = None
    warnings: list[str] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lon": round(self.lon, 6),
            "lat": round(self.lat, 6),
            "label": self.label,
            "state": self.state,
            "state_code": self.state_code,
            "district": self.district,
            "district_code": self.district_code,
            "inside_india": self.inside_india,
            "boundary_matched": self.boundary_matched,
            "boundary_uncertain": self.boundary_uncertain,
            "boundary_distance_m": self.boundary_distance_m,
            "warnings": self.warnings,
            "provenance": self.provenance,
        }


def store() -> SpatialStore:
    return get_spatial_store()


# -- in-process cache ------------------------------------------------------
def _memory(cache: TTLCache, key: tuple[Any, ...], producer: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Replay a recent response, labelling it as a replay rather than a new measurement.

    Only location lookups go through here: administrative boundaries and the DEM are static,
    and the Nominatim call underneath is already disk-cached with its own timestamp. Weather,
    fire detections and hazards are never cached, because those change minute to minute.
    """
    settings = get_settings()
    if not settings.cache_enabled:
        return producer()
    envelope = cache.get(key)
    if envelope is not MISS:
        payload = copy.deepcopy(envelope["value"])
        payload["cache"] = {
            "hit": True,
            "stored_at": envelope["stored_at"],
            "age_seconds": round(time.time() - envelope["stored_epoch"], 1),
            "note": "Served from this process's memory cache; the observations and their timestamps "
            "inside are the ones recorded when it was produced, not a new measurement.",
        }
        return payload
    payload = producer()
    cache.set(
        key,
        {
            "value": copy.deepcopy(payload),
            "stored_at": datetime.now(timezone.utc).isoformat(),
            "stored_epoch": time.time(),
        },
    )
    payload["cache"] = {"hit": False, "note": "Produced by this request."}
    return payload


def _geocode_cache() -> TTLCache:
    settings = get_settings()
    return cache_for(
        "locations.search",
        max_entries=settings.geocode_cache_max_entries,
        ttl_seconds=settings.geocode_cache_ttl_seconds,
    )


def _point_cache() -> TTLCache:
    settings = get_settings()
    return cache_for(
        "locations.point",
        max_entries=settings.point_cache_max_entries,
        ttl_seconds=settings.point_cache_ttl_seconds,
    )


# -- administrative containment -------------------------------------------
def admin_context(lon: float, lat: float, *, layer_districts: str = "admin_districts", layer_states: str = "admin_states") -> PlaceContext:
    """Resolve the administrative envelope of a point, with explicit uncertainty."""
    engine = store()
    label_bits: list[str] = []
    ctx = PlaceContext(lon=lon, lat=lat, label="", inside_india=True)

    outline = _safe_contain(engine, "india_outline", lon, lat)
    ctx.inside_india = bool(outline) or _inside_bbox(lon, lat)

    if not engine.has_layer(layer_districts):
        ctx.warnings.append("No administrative boundaries are loaded in this instance; run the ingest first.")
        ctx.label = f"{lat:.4f}, {lon:.4f}"
        return ctx

    matches = _safe_contain(engine, layer_districts, lon, lat)
    if matches:
        attrs = matches[0]["attributes"]
        ctx.boundary_matched = True
        ctx.district = attrs.get("district")
        ctx.district_code = attrs.get("district_code")
        ctx.state = attrs.get("state_name")
        ctx.state_code = attrs.get("state_code")
        if len(matches) > 1:
            ctx.warnings.append(
                f"{len(matches)} district polygons contain this point (overlapping boundaries); using {ctx.district}."
            )
        if attrs.get("name_missing"):
            ctx.boundary_uncertain = True
            ctx.warnings.append("The containing district polygon has no name upstream.")
        label_bits = [ctx.district, ctx.state]
    else:
        nearest = engine.nearest_features(layer_districts, lon, lat, limit=1, max_distance_m=250_000)
        if nearest:
            attrs = nearest[0]["attributes"]
            ctx.boundary_uncertain = True
            ctx.boundary_distance_m = nearest[0]["distance_m"]
            ctx.district = attrs.get("district")
            ctx.district_code = attrs.get("district_code")
            ctx.state = attrs.get("state_name")
            ctx.state_code = attrs.get("state_code")
            label_bits = [ctx.district, ctx.state]
            ctx.warnings.append(
                f"The point falls outside every district polygon; the nearest is {ctx.district} "
                f"about {int(ctx.boundary_distance_m or 0)} m away. Treat the administrative label as approximate."
            )
        else:
            ctx.warnings.append("No district polygon is near this point; the location is outside loaded coverage.")

    states = _safe_contain(engine, layer_states, lon, lat)
    if states and not ctx.state:
        ctx.state = states[0]["attributes"].get("state")
        ctx.state_code = states[0]["attributes"].get("state_code")
        label_bits = [ctx.state]

    if not ctx.inside_india:
        ctx.warnings.append("This coordinate is outside the India bounding box; IndiGeo only analyses Indian locations.")

    named = ", ".join(str(bit) for bit in label_bits if bit)
    ctx.label = f"{named} ({lat:.4f}, {lon:.4f})" if named else f"{lat:.4f}, {lon:.4f}"
    return ctx


def _safe_contain(engine: SpatialStore, layer: str, lon: float, lat: float) -> list[dict]:
    if not engine.has_layer(layer):
        return []
    try:
        return engine.features_containing(layer, lon, lat, limit=5)
    except Exception as exc:  # a broken layer must not fail the whole lookup
        log.warning("Containment query failed on %s: %s", layer, exc)
        return []


def _inside_bbox(lon: float, lat: float) -> bool:
    return INDIA_BBOX[0] <= lon <= INDIA_BBOX[2] and INDIA_BBOX[1] <= lat <= INDIA_BBOX[3]


# -- local register --------------------------------------------------------
def _local_matches(query: str, limit: int) -> list[dict[str, Any]]:
    norm = _normalise(query)
    if len(norm) < 2:
        return []
    like = f"%{norm}%"
    with session_scope() as session:
        rows = session.execute(
            select(AdminUnit)
            .where(
                or_(
                    AdminUnit.name_norm == norm,
                    AdminUnit.name_norm.startswith(norm),
                    AdminUnit.name_norm.like(like),
                )
            )
            .limit(max(limit * 4, 20))
        ).scalars().all()
    scored: list[tuple[int, dict[str, Any]]] = []
    for unit in rows:
        exact = unit.name_norm == norm
        prefix = unit.name_norm.startswith(norm)
        rank = (_KIND_ORDER.get(unit.kind, 5), 0 if exact else 1 if prefix else 2)
        scored.append(
            (
                rank[0] * 10 + rank[1],
                {
                    "label": unit.name,
                    "kind": unit.kind,
                    "source": "administrative_register",
                    "lat": unit.lat,
                    "lon": unit.lon,
                    "admin": {
                        "state_code": unit.state_code,
                        "district_code": unit.district_code,
                        "parent_code": unit.parent_code,
                    },
                    "exact": exact,
                    "provenance": {"dataset": unit.source, **(unit.provenance or {})},
                },
            )
        )
    scored.sort(key=lambda item: item[0])
    return [payload for _, payload in scored[:limit]]


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


# -- remote geocoding ------------------------------------------------------
def _remote_candidates(query: str, limit: int, *, pin: str | None = None) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
    from ..integrations.base import get_provider

    warnings: list[str] = []
    try:
        provider = get_provider("nominatim")
        if pin:
            result = provider.get_json(
                f"{provider.base_url}/search",
                params={
                    "postalcode": pin,
                    "countrycodes": "in",
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "limit": max(1, min(limit, 20)),
                },
            )
        else:
            result = provider.search(query, limit=limit, viewbox=(INDIA_BBOX[0], INDIA_BBOX[3], INDIA_BBOX[2], INDIA_BBOX[1]))
    except Exception as exc:
        warnings.append(f"Geocoding service unavailable ({type(exc).__name__}); results cover loaded administrative names only.")
        return [], warnings, None

    features = result.payload if isinstance(result.payload, list) else []
    out: list[dict[str, Any]] = []
    for hit in features:
        address = hit.get("address") or {}
        out.append(
            {
                "label": hit.get("display_name"),
                "kind": _kind_from_nominatim(hit, address),
                "source": "openstreetmap_nominatim",
                "lat": float(hit["lat"]) if hit.get("lat") else None,
                "lon": float(hit["lon"]) if hit.get("lon") else None,
                "admin": {
                    "state": address.get("state"),
                    "state_code": None,
                    "district": address.get("district") or address.get("county"),
                    "village": address.get("village") or address.get("town") or address.get("city"),
                    "postcode": address.get("postcode"),
                },
                "osm": {"type": hit.get("osm_type"), "id": hit.get("osm_id"), "boundingbox": hit.get("boundingbox")},
                "importance": hit.get("importance"),
                "provenance": result.provenance(),
            }
        )
    return out, warnings, result.provenance()


def _kind_from_nominatim(hit: dict[str, Any], address: dict[str, Any]) -> str:
    if str(hit.get("class")) == "boundary":
        ftype = str(hit.get("type") or "")
        if "admin_level" in ftype or ftype in {"administrative", "state"}:
            return "state"
    if address.get("state") and not (address.get("district") or address.get("city") or address.get("village")):
        return "state"
    if address.get("district"):
        return "district"
    if address.get("city"):
        return "town"
    if address.get("town"):
        return "town"
    if address.get("village") or address.get("hamlet"):
        return "village"
    return "place"


# -- public API ------------------------------------------------------------
def search_places(query: str, *, limit: int = 8, allow_remote: bool = True) -> dict[str, Any]:
    """Search by place, town, village, district, state, PIN code or "lat, lon"."""
    query = (query or "").strip()
    if not query:
        raise ValidationError("A search query is required.", details={"query": query})
    limit = max(1, min(int(limit), 20))
    key = (_normalise(query), limit, bool(allow_remote))
    return _memory(_geocode_cache(), key, lambda: _search_places(query, limit, allow_remote))


def _search_places(query: str, limit: int, allow_remote: bool) -> dict[str, Any]:
    try:
        point = parse_coordinates(query)
    except InvalidCoordinates:
        point = None

    if point is not None:
        resolved = resolve_point(point.lat, point.lon, allow_remote=allow_remote)
        return {
            "query": query,
            "interpretation": "coordinates",
            "results": [
                {
                    "label": resolved["place"]["label"],
                    "kind": "coordinate",
                    "source": "coordinate_parse",
                    "lat": point.lat,
                    "lon": point.lon,
                    "admin": resolved["place"],
                }
            ],
            "warnings": resolved["warnings"],
            "provenance": resolved["provenance"],
            "resolved": resolved,
        }

    pin_match = PIN_RE.match(query)
    pin = pin_match.group(1) if pin_match else None

    results = _local_matches(query, limit)
    warnings: list[str] = []
    geocode_provenance: dict[str, Any] | None = None
    if allow_remote and (len(results) < limit or pin):
        remote, remote_warnings, geocode_provenance = _remote_candidates(query, limit - len(results), pin=pin)
        warnings.extend(remote_warnings)
        results = _merge_dedupe(results, remote, limit)
    if pin and not results:
        warnings.append(
            f"PIN code {pin} returned no match. This build has no offline PIN gazetteer; "
            "search by district or paste coordinates instead."
        )

    return {
        "query": query,
        "interpretation": "pin_code" if pin else "free_text",
        "results": results,
        "warnings": warnings,
        "provenance": [geocode_provenance] if geocode_provenance else [],
        "attribution": "Geocoding (c) OpenStreetMap contributors, ODbL" if geocode_provenance else None,
    }


def _merge_dedupe(local: list[dict], remote: list[dict], limit: int) -> list[dict]:
    seen: set[tuple[float, float]] = set()
    out: list[dict] = []
    for item in local + remote:
        if item.get("lat") is None or item.get("lon") is None:
            continue
        key = (round(float(item["lat"]), 3), round(float(item["lon"]), 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda item: (0 if item.get("exact") else 1, _KIND_ORDER.get(item.get("kind", "place"), 5)))
    return out[:limit]


def reverse_location(lon: float, lat: float, *, allow_remote: bool = True) -> dict[str, Any]:
    """Nearest named places for a coordinate: local boundaries first, OSM names second."""
    LatLng(lat, lon)
    resolved = resolve_point(lat, lon, allow_remote=allow_remote)
    return resolved


def _remote_reverse(lon: float, lat: float) -> tuple[dict[str, Any] | None, list[str], dict[str, Any] | None]:
    from ..integrations.base import get_provider

    try:
        provider = get_provider("nominatim")
        result = provider.reverse(lon, lat)
    except Exception as exc:
        return None, [f"Reverse geocoding unavailable ({type(exc).__name__}); administrative labels only."], None
    payload = result.payload
    if not isinstance(payload, dict) or not payload.get("display_name"):
        return None, [], None
    address = payload.get("address") or {}
    return (
        {
            "label": payload["display_name"],
            "kind": _kind_from_nominatim(payload, address),
            "source": "openstreetmap_nominatim",
            "lat": float(payload["lat"]) if payload.get("lat") else lat,
            "lon": float(payload["lon"]) if payload.get("lon") else lon,
            "admin": {
                "state": address.get("state"),
                "district": address.get("district") or address.get("county"),
                "village": address.get("village") or address.get("town") or address.get("city"),
                "subdistrict": address.get("subdistrict"),
                "postcode": address.get("postcode"),
            },
            "osm": {"type": payload.get("osm_type"), "id": payload.get("osm_id")},
            "provenance": result.provenance(),
        },
        [],
        result.provenance(),
    )


def resolve_point(lat: float, lon: float, *, allow_remote: bool = True, radius_m: float | None = None) -> dict[str, Any]:
    LatLng(lat, lon)
    # Rounded to ~1 m: finer than the ~30 m DEM cell, so a replay is the same lookup.
    key = (
        round(float(lat), 5),
        round(float(lon), 5),
        bool(allow_remote),
        None if radius_m is None else round(float(radius_m), 1),
    )
    return _memory(_point_cache(), key, lambda: _resolve_point(lat, lon, allow_remote, radius_m))


def _resolve_point(lat: float, lon: float, allow_remote: bool, radius_m: float | None) -> dict[str, Any]:
    point = LatLng(lat, lon)
    ctx = admin_context(point.lon, point.lat)
    remote: dict[str, Any] | None = None
    if allow_remote:
        remote, remote_warnings, provenance = _remote_reverse(point.lon, point.lat)
        ctx.warnings.extend(remote_warnings)
        if provenance:
            ctx.provenance.append(provenance)
    if remote:
        nearest_name = (
            remote["admin"].get("village")
            or remote["admin"].get("subdistrict")
            or remote["admin"].get("district")
            or remote["label"].split(",")[0]
        )
        if nearest_name and nearest_name.lower() not in ctx.label.lower():
            ctx.label = f"{nearest_name}, {ctx.label}"
    payload: dict[str, Any] = {
        "place": ctx.as_dict(),
        "warnings": list(ctx.warnings),
        "provenance": list(ctx.provenance),
        "osm_reverse": remote,
    }
    if radius_m is not None:
        payload["terrain"] = terrain_at(point.lon, point.lat, radius_m)
        payload["radius_m"] = radius_m
    return payload


def resolve_area(
    *,
    lat: float | None = None,
    lon: float | None = None,
    radius: str | float | None = None,
    polygon: dict[str, Any] | str | None = None,
    query: str | None = None,
    allow_remote: bool = True,
) -> dict[str, Any]:
    """Resolve any supported selection: text, coordinate+radius, or a drawn polygon."""
    if query and (lat is None or lon is None):
        found = search_places(query, limit=1, allow_remote=allow_remote)
        if not found["results"]:
            raise NotFoundError(f"No place matched '{query}'.", details={"query": query})
        first = found["results"][0]
        lat, lon = first.get("lat"), first.get("lon")
        if lat is None or lon is None:
            raise ValidationError(f"'{query}' resolved without coordinates.", details={"match": first})

    if polygon is not None:
        return _resolve_polygon(_load_polygon(polygon), allow_remote=allow_remote)

    if lat is None or lon is None:
        raise ValidationError("Provide lat/lon, a query, or a polygon.")
    LatLng(float(lat), float(lon))
    radius_m = parse_radius_m(radius, default=5000.0)
    resolved = resolve_point(float(lat), float(lon), allow_remote=allow_remote, radius_m=radius_m)
    resolved["selection"] = {"mode": "radius", "radius_m": radius_m, "lat": float(lat), "lon": float(lon)}
    return resolved


def _count_vertices(geom) -> int:
    def _ring_count(ring) -> int:
        return len(ring.coords)

    polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    return sum(_ring_count(p.exterior) + sum(_ring_count(r) for r in p.interiors) for p in polygons)


def _load_polygon(polygon: dict[str, Any] | str):
    from shapely.geometry import shape

    raw = json.loads(polygon) if isinstance(polygon, str) else polygon
    if raw.get("type") == "FeatureCollection":
        features = raw.get("features") or []
        if not features:
            raise ValidationError("FeatureCollection has no features.")
        raw = features[0].get("geometry") or {}
    geometry = raw.get("geometry", raw) if "geometry" in raw else raw
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValidationError(
            "Only Polygon or MultiPolygon selections are supported.",
            details={"type": geometry.get("type")},
        )
    try:
        geom = shape(geometry)
    except Exception as exc:
        raise ValidationError(f"Unparseable polygon: {exc}") from exc
    if not geom.is_valid:
        from shapely import make_valid

        geom = make_valid(geom)
    if geom.geom_type == "GeometryCollection":
        from shapely.geometry import MultiPolygon

        geom = MultiPolygon([g for g in geom.geoms if g.geom_type == "Polygon"])
    if geom.is_empty:
        raise ValidationError("Polygon is empty after validation.")
    if _count_vertices(geom) > MAX_POLYGON_VERTICES:
        raise ValidationError(
            "Polygon has too many vertices for a nationwide analysis.",
            details={"vertices": _count_vertices(geom), "max": MAX_POLYGON_VERTICES},
        )
    min_lon, min_lat, max_lon, max_lat = geom.bounds
    if not is_indian_bbox(min_lon, min_lat, max_lon, max_lat):
        raise ValidationError("The selection lies outside India.", details={"bounds": geom.bounds})
    return geom


def _utm_crs_for(lon: float, lat: float) -> str:
    zone = max(1, min(60, int((lon + 180.0) / 6.0) + 1))
    return f"EPSG:{32600 + zone}" if lat >= 0 else f"EPSG:{32700 + zone}"


def _resolve_polygon(geom, *, allow_remote: bool) -> dict[str, Any]:
    from shapely import wkb as _wkb

    import geopandas as gpd

    frame = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    projected = frame.to_crs(_utm_crs_for(float(geom.centroid.x), float(geom.centroid.y)))
    area_km2 = float(projected.geometry.area.iloc[0] / 1e6)
    perimeter_km = float(projected.geometry.length.iloc[0] / 1e3)
    if area_km2 > MAX_POLYGON_AREA_KM2:
        raise ValidationError(
            f"Polygon covers {round(area_km2)} km2, above the {int(MAX_POLYGON_AREA_KM2)} km2 per-request limit.",
            details={"area_km2": round(area_km2)},
        )

    point = geom.representative_point()
    engine = store()
    districts = engine.features_intersecting("admin_districts", _wkb.dumps(geom), limit=200) if engine.has_layer("admin_districts") else []
    states = engine.features_intersecting("admin_states", _wkb.dumps(geom), limit=50) if engine.has_layer("admin_states") else []

    ctx = admin_context(float(point.y), float(point.x))
    ctx.boundary_matched = bool(districts)
    names = [
        str(d["attributes"].get("district"))
        for d in districts[:5]
        if d["attributes"].get("district") and not d["attributes"].get("name_missing")
    ]
    if names:
        ctx.label = ", ".join(names) + ("..." if len(districts) > 5 else "")

    clipped_area = float(sum(d["overlap_km2"] for d in districts))
    uncovered = max(0.0, area_km2 - clipped_area)
    uncovered_fraction = uncovered / max(area_km2, 1e-9)
    warnings = list(ctx.warnings)
    if districts:
        warnings.append(
            f"Selection covers {round(area_km2)} km2 across {len(districts)} district polygon(s); "
            f"{round(uncovered)} km2 ({round(uncovered_fraction * 100)}%) fell outside every district boundary."
        )
    else:
        warnings.append("The polygon does not intersect any loaded district boundary; results will be boundary-free.")
    if uncovered_fraction > 0.02:
        ctx.boundary_uncertain = True
    min_lon, min_lat, max_lon, max_lat = geom.bounds
    if not (INDIA_BBOX[0] <= min_lon and max_lon <= INDIA_BBOX[2] and INDIA_BBOX[1] <= min_lat and max_lat <= INDIA_BBOX[3]):
        warnings.append("Part of the selection falls outside the India bounding box.")

    sample_radius = max(1000.0, min(50_000.0, (area_km2**0.5) * 1000.0))
    remote, remote_warnings, provenance = (None, [], None)
    if allow_remote:
        remote, remote_warnings, provenance = _remote_reverse(float(point.x), float(point.y))
        warnings.extend(remote_warnings)
    if provenance:
        ctx.provenance.append(provenance)

    return {
        "place": ctx.as_dict(),
        "selection": {
            "mode": "polygon",
            "area_km2": round(area_km2, 2),
            "perimeter_km": round(perimeter_km, 2),
            "bounds": [round(v, 5) for v in geom.bounds],
            "representative_point": {"lat": round(float(point.y), 6), "lon": round(float(point.x), 6)},
            "vertices": _count_vertices(geom),
            # Shapely emits coordinate tuples; the API and the stored record must be JSON arrays.
            "geometry": json.loads(json.dumps(geom.__geo_interface__)),
        },
        "districts": [
            {
                "district": d["attributes"].get("district"),
                "district_code": d["attributes"].get("district_code"),
                "state": d["attributes"].get("state_name"),
                "overlap_km2": d["overlap_km2"],
                # The upstream file has polygons without a name; they are kept as separate
                # overlaps but must not masquerade as place names.
                "name_missing": bool(d["attributes"].get("name_missing")),
            }
            for d in districts
        ],
        "states": [
            {"state": s["attributes"].get("state"), "state_code": s["attributes"].get("state_code"), "overlap_km2": s["overlap_km2"]}
            for s in states
        ],
        "warnings": warnings,
        "provenance": list(ctx.provenance),
        "terrain": terrain_at(float(point.x), float(point.y), sample_radius),
        "osm_reverse": remote,
    }


def terrain_at(lon: float, lat: float, radius_m: float) -> dict[str, Any]:
    field_, meta = dem_for_point(lon, lat, radius_m)
    out = dict(meta)
    if field_ is None:
        out.update({"available": False, "summary": None, "reason": "No elevation tiles could be retrieved."})
        return out
    out["summary"] = field_.summary(lon, lat, radius_m)
    if out["summary"].get("bathymetry_only"):
        out["warnings"] = list(out.get("warnings", [])) + [
            "This window is ocean: the terrain model carries bathymetry, not land elevation."
        ]
    return out


def search_by_bounds(min_lon: float, min_lat: float, max_lon: float, max_lat: float, *, limit: int = 500) -> dict[str, Any]:
    """District/state polygons inside a map viewport, kept small for tiles."""
    engine = store()
    if not engine.has_layer("admin_districts"):
        raise NotFoundError("Administrative boundaries are not loaded.", details={"layer": "admin_districts"})
    dlon, dlat = meters_to_degrees(min_lat, 200.0)
    window = (min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat)
    features = engine.features_in_bbox("admin_districts", window, limit=limit, max_area_deg2=6.0)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {**feature["attributes"], "fid": feature["fid"]},
            }
            for feature in features
        ],
        "number_returned": len(features),
        "limit": limit,
        "truncated": len(features) >= limit,
    }


__all__ = [
    "PlaceContext",
    "admin_context",
    "resolve_area",
    "resolve_point",
    "reverse_location",
    "search_by_bounds",
    "search_places",
    "terrain_at",
    "MAX_POLYGON_AREA_KM2",
    "MAX_POLYGON_VERTICES",
]
