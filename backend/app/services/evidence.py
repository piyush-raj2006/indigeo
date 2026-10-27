from __future__ import annotations

"""Evidence gathering for the hazard engine.

This module is the only part of the hazard pipeline that touches the network or the
spatial store. It collects everything the assessors need into one serialisable
:class:`EvidenceContext` and records, per source, whether it actually answered. An
assessor never reaches out to a provider itself, which keeps the scoring deterministic
and testable and makes every external dependency an explicit, honest availability flag.

Nothing here invents a substitute value when a source is down: the source is marked
unavailable with the reason, and the dependent hazard loses confidence rather than
silently gaining a fabricated factor.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..core.config import get_settings
from ..core.logging import get_logger
from ..integrations.base import get_provider
from ..integrations.firms import FirmsProvider
from ..integrations.open_meteo import OpenMeteoProvider
from ..integrations.osm_infra import OverpassProvider
from ..integrations.usgs import UsgsEarthquakeProvider
from .lifelines import LIFELINE_LAYERS, lifelines_near, origin_states
from .live_alerts import gather_live_alerts

log = get_logger("services.evidence")

# Radius the DEM/weather window is sampled over, and the wider box used for regional
# seismicity and fire counts.
LOCAL_RADIUS_M = 5_000.0
REGION_HALF_DEG = 2.0  # ~220 km box around the point for seismicity/fire context
# How far the coastline search looks. Beyond this the distance is unknown, which is not
# the same statement as "the coastline layer failed".
COAST_SEARCH_MAX_M = 1_000_000.0
# Overlay caps: the map draws a sample of a regional query, and the payload says how many
# observations the sample was drawn from rather than pretending it is the whole set.
FIRE_OVERLAY_LIMIT = 500
SEISMIC_OVERLAY_LIMIT = 25


@dataclass
class EvidenceContext:
    lon: float
    lat: float
    radius_m: float
    admin: dict[str, Any] = field(default_factory=dict)
    terrain: dict[str, Any] = field(default_factory=dict)
    weather_recent: dict[str, Any] = field(default_factory=dict)
    weather_forecast: dict[str, Any] = field(default_factory=dict)
    seismicity: dict[str, Any] = field(default_factory=dict)
    infrastructure: dict[str, Any] = field(default_factory=dict)
    fires: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    source_status: dict[str, str] = field(default_factory=dict)  # provider -> available|unavailable|...
    coast_distance_m: float | None = None
    coast_measured: bool = False
    provenance: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def source_available(self, provider_key: str) -> bool:
        return self.source_status.get(provider_key) == "available"

    def as_dict(self) -> dict[str, Any]:
        return {
            "lon": self.lon,
            "lat": self.lat,
            "radius_m": self.radius_m,
            "admin": self.admin,
            "terrain": self.terrain,
            "weather_recent": self.weather_recent,
            "weather_forecast": self.weather_forecast,
            "seismicity": self.seismicity,
            "infrastructure": self.infrastructure,
            "fires": self.fires,
            "alerts": self.alerts,
            "source_status": self.source_status,
            "coast_distance_m": self.coast_distance_m,
            "coast_measured": self.coast_measured,
            "provenance": self.provenance,
            "warnings": self.warnings,
        }


def gather_evidence(
    lon: float,
    lat: float,
    *,
    radius_m: float = LOCAL_RADIUS_M,
    admin: dict[str, Any] | None = None,
    allow_remote: bool = True,
    include_infrastructure: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> EvidenceContext:
    """Collect DEM, weather, seismicity, infrastructure and fire evidence for a point.

    ``include_infrastructure`` gates the Overpass lookups. Nothing in the hazard scoring
    reads them, and measured live they dominate the run time: with a warm DEM tile cache a
    full analysis is 3-4 s, while the same point with the four OSM layers taken serially
    across mirrors took 504-571 s and still failed outright on a later attempt. Synchronous
    callers skip them; the dashboard asks for them separately.
    """
    from ..services.locations import terrain_at  # local import avoids a cycle
    from ..gis.spatial import get_spatial_store

    def emit(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    ctx = EvidenceContext(lon=lon, lat=lat, radius_m=radius_m, admin=admin or {})

    # Terrain resolves from a local raster first, then the Terrarium tile pyramid. Whichever
    # path answered names itself in the provenance table; a fallback is stated in warnings.
    emit("terrain")
    try:
        ctx.terrain = terrain_at(lon, lat, radius_m)
        ctx.source_status["dem"] = "available" if ctx.terrain.get("available") else "unavailable"
        terrain_provenance = ctx.terrain.get("provenance")
        if isinstance(terrain_provenance, dict):
            ctx.provenance.append(terrain_provenance)
        if ctx.terrain.get("fallback_reason"):
            ctx.warnings.append(f"Terrain: {ctx.terrain['fallback_reason']}")
    except Exception as exc:  # pragma: no cover - DEM network failure
        ctx.source_status["dem"] = "unavailable"
        ctx.warnings.append(f"Terrain (DEM) unavailable: {type(exc).__name__}.")

    # Coastline distance from the loaded national outline.
    emit("coastline")
    try:
        store = get_spatial_store()
        if store.has_layer("india_outline"):
            ctx.coast_distance_m = store.distance_to_boundary("india_outline", lon, lat, max_distance_m=COAST_SEARCH_MAX_M)
            ctx.coast_measured = True
            ctx.source_status["coastline"] = "available"
            ctx.warnings.append(
                "Coastal distance is measured to India's own coastline outline (mainland plus island "
                "territories). Where the shortest route to the sea leaves Indian territory, as in much "
                "of the north-east, this reads farther than the nearest water actually is."
            )
            if ctx.coast_distance_m is None:
                ctx.warnings.append(
                    f"No coastline within {COAST_SEARCH_MAX_M / 1000:.0f} km of the point; treated as zero coastal exposure, "
                    "not as a missing layer."
                )
        else:
            ctx.source_status["coastline"] = "unavailable"
            ctx.warnings.append("The national coastline outline is not loaded; coastal hazards cannot be measured.")
    except Exception as exc:
        ctx.source_status["coastline"] = "unavailable"
        ctx.warnings.append(f"Coastline distance unavailable: {type(exc).__name__}.")

    if not allow_remote:
        for key in ("open_meteo", "usgs_earthquakes", "osm_overpass", "firms"):
            ctx.source_status.setdefault(key, "unavailable")
        # A configured feed is still a gap here: it was not queried, and saying nothing
        # would let a reader assume no alert existed.
        _apply_alerts(ctx, gather_live_alerts(lon, lat, radius_m, admin=ctx.admin, allow_remote=False))
        return ctx

    # Weather: recent reanalysis window and forward forecast.
    emit("weather")
    meteo: OpenMeteoProvider = get_provider("open_meteo")  # type: ignore[assignment]
    try:
        ctx.weather_recent = meteo.recent_observation_window(lon, lat, days=45)
        ctx.provenance.append(ctx.weather_recent["provenance"])
        ctx.source_status["open_meteo_recent"] = "available"
    except Exception as exc:
        ctx.source_status["open_meteo_recent"] = "unavailable"
        ctx.warnings.append(f"Recent weather unavailable: {type(exc).__name__}.")
    try:
        fc = meteo.forecast(lon, lat, days=7, past_days=0)
        ctx.weather_forecast = {"daily": fc.payload.get("daily", {}), "provenance": fc.provenance()}
        ctx.provenance.append(fc.provenance())
    except Exception as exc:
        ctx.source_status["open_meteo_forecast"] = "unavailable"
        ctx.warnings.append(f"Weather forecast unavailable: {type(exc).__name__}.")
    else:
        ctx.source_status["open_meteo_forecast"] = "available"
    if ctx.source_status["open_meteo_recent"] == "available" or ctx.source_status.get("open_meteo_forecast") == "available":
        ctx.source_status["open_meteo"] = "available"

    # Seismicity: 50 years of significant events in a regional box (historical hazard,
    # never a prediction).
    emit("seismicity")
    usgs: UsgsEarthquakeProvider = get_provider("usgs_earthquakes")  # type: ignore[assignment]
    try:
        result = usgs.query_india(
            start=date.today() - timedelta(days=365 * 50),
            min_magnitude=4.5,
            bbox=(lon - REGION_HALF_DEG, lat - REGION_HALF_DEG, lon + REGION_HALF_DEG, lat + REGION_HALF_DEG),
        )
        ctx.seismicity = _summarise_quakes(result.payload, lon, lat)
        ctx.provenance.append(result.provenance())
        ctx.source_status["usgs_earthquakes"] = "available"
    except Exception as exc:
        ctx.source_status["usgs_earthquakes"] = "unavailable"
        ctx.warnings.append(f"Seismicity data unavailable: {type(exc).__name__}.")

    # Lifeline infrastructure within the analysis radius. Local OSM extracts answer first;
    # Overpass is asked only for the layers the extract could not cover.
    if include_infrastructure:
        emit("lifelines")
        settings = get_settings()
        overpass: OverpassProvider = get_provider("osm_overpass")  # type: ignore[assignment]
        local_provider = None
        if settings.osm_local_enabled:
            try:
                candidate = get_provider("osm_local")
                if getattr(candidate, "extract_count", 0) > 0:
                    local_provider = candidate
            except Exception:  # a broken manifest must not take the whole run down
                local_provider = None
        try:
            infrastructure = lifelines_near(
                lon,
                lat,
                radius_m,
                layers=LIFELINE_LAYERS,
                local=local_provider,
                overpass=overpass,
            )
            ctx.infrastructure = infrastructure
            ctx.provenance.extend(infrastructure.get("provenance", []))
            ctx.warnings.extend(infrastructure.get("warnings", []))
            layers = infrastructure.get("layers", {})
            states = infrastructure.get("origin_states") or origin_states(infrastructure)
            for origin, state in states.items():
                provider_key = {"local": "osm_local", "overpass": "osm_overpass"}.get(origin, f"osm_{origin}")
                ctx.source_status[provider_key] = state

            answered = sum(1 for layer in layers.values() if layer.get("status") == "ok")
            # "available" means every layer answered. A layer cut off at the wall-clock
            # budget is a gap, and saying "available" for a 1-of-4 answer would overstate it.
            if layers and answered == len(layers):
                pass
            elif answered:
                ctx.warnings.append(
                    f"OSM lifelines: {answered} of {len(layers)} layers answered inside the "
                    f"{infrastructure.get('deadline_seconds')} s budget; the rest are reported unavailable, "
                    "not as zero features."
                )
            else:
                ctx.warnings.append(
                    "OSM lifelines: no layer answered inside the budget. The lifeline block is a gap, not a "
                    "finding that no hospitals, bridges or emergency services exist here."
                )
            local_info = infrastructure.get("local") or {}
            if local_info.get("status") == "outside_coverage":
                ctx.warnings.append(
                    "No local OSM extract fully covers this radius, so every lifeline layer was requested "
                    "from Overpass."
                )
            elif local_info.get("status") == "used" and not infrastructure.get("remote_used"):
                names = ", ".join(
                    entry.get("name", "unnamed") for entry in local_info.get("extracts") or []
                )
                ctx.warnings.append(
                    "Lifelines were computed from the local OSM extract(s) "
                    f"{names or 'listed in the manifest'} and no Overpass request was made."
                )
            elif local_info.get("status") == "used" and infrastructure.get("remote_used"):
                ctx.warnings.append(
                    "Lifelines came from the local OSM extract where it covered the radius, and from "
                    "Overpass for the layers it could not answer."
                )
        except Exception as exc:
            ctx.source_status["osm_overpass"] = "unavailable"
            ctx.warnings.append(f"Infrastructure lookup unavailable: {type(exc).__name__}.")
    else:
        ctx.warnings.append(
            "OpenStreetMap lifeline infrastructure was not queried for this run; the empty "
            "infrastructure block is a deliberate omission, not a finding of none."
        )

    # Active fires: only if a FIRMS key is configured, else honestly unavailable.
    emit("fires")
    firms: FirmsProvider = get_provider("firms")  # type: ignore[assignment]
    if firms.auth_problem() is not None:
        ctx.source_status["firms"] = "unconfigured"
        # Keep the reason with the evidence so the map layer can say why it is empty.
        ctx.fires = {"count": 0, "available": False, "reason": firms.auth_problem()}
    else:
        try:
            rows, result = firms.hotspots_bbox(
                lon - REGION_HALF_DEG, lat - REGION_HALF_DEG, lon + REGION_HALF_DEG, lat + REGION_HALF_DEG, days=7
            )
            hotspots = _compact_hotspots(rows, limit=FIRE_OVERLAY_LIMIT)
            ctx.fires = {
                "count": len(rows),
                "available": True,
                "hotspots": hotspots,
                "hotspots_total": len(rows),
                "hotspots_returned": len(hotspots),
                "provenance": result.provenance(),
            }
            ctx.provenance.append(result.provenance())
            ctx.source_status["firms"] = "available"
        except Exception as exc:
            ctx.source_status["firms"] = "unavailable"
            ctx.fires = {"count": 0, "available": False, "reason": f"{type(exc).__name__}: {exc}"}
            ctx.warnings.append(f"Active-fire data unavailable: {type(exc).__name__}.")

    # Live Indian hazard feeds: IMD warnings, CWC river gauges, Bhuvan/NDMA hazard maps.
    emit("alerts")
    _apply_alerts(ctx, gather_live_alerts(lon, lat, radius_m, admin=ctx.admin))

    return ctx


def _apply_alerts(ctx: EvidenceContext, bundle: dict[str, Any]) -> None:
    """Attach the feed bundle, its per-source state and its provenance to the context."""
    ctx.alerts = bundle
    for entry in (bundle.get("feeds") or {}).values():
        ctx.source_status[entry.get("provider", "live_feed")] = entry.get("status", "unavailable")
    ctx.provenance.extend(bundle.get("provenance") or [])
    ctx.warnings.extend(bundle.get("warnings") or [])


def _summarise_quakes(payload: Any, lon: float, lat: float) -> dict[str, Any]:
    """Reduce a ComCat FeatureCollection to observed regional seismicity statistics."""
    from ..gis.coords import haversine_m

    features = payload.get("features", []) if isinstance(payload, dict) else []
    records: list[tuple[float, float | None]] = []  # (magnitude, distance_m or None)
    events: list[dict[str, Any]] = []
    for feature in features:
        props = feature.get("properties", {}) if isinstance(feature, dict) else {}
        coords = (feature.get("geometry", {}) or {}).get("coordinates") if isinstance(feature, dict) else None
        mag = props.get("mag")
        if mag is None:
            continue
        distance = None
        if coords and len(coords) >= 2:
            distance = haversine_m(lat, lon, coords[1], coords[0])
            events.append(
                {
                    "magnitude": round(float(mag), 2),
                    "distance_km": round(distance / 1000.0, 1),
                    # Epicentre coordinates are what a map overlay draws; the statistics
                    # above are derived from the same features.
                    "lat": round(float(coords[1]), 5),
                    "lon": round(float(coords[0]), 5),
                    "place": props.get("place"),
                    "time": props.get("time"),
                    "url": props.get("url"),
                }
            )
        records.append((float(mag), distance))
    events.sort(key=lambda e: (e["distance_km"], -e["magnitude"]))
    magnitudes = [m for m, _ in records]
    distances = [d for _, d in records if d is not None]
    near_mags = [m for m, d in records if d is not None and d <= 150_000]
    return {
        "total_events": len(magnitudes),
        "events_within_150km": len(near_mags),
        "nearest_event_km": round(min(distances) / 1000.0, 1) if distances else None,
        "max_magnitude": round(max(magnitudes), 2) if magnitudes else None,
        "max_magnitude_within_150km": round(max(near_mags), 2) if near_mags else None,
        "nearest_events": events[:SEISMIC_OVERLAY_LIMIT],
        "events_returned": min(len(events), SEISMIC_OVERLAY_LIMIT),
        "window_years": 50,
        "lookback_note": "USGS ComCat historical seismicity; a hazard climatology, not a forecast.",
    }


def _compact_hotspots(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Keep the fields the map can draw, capped, with no invented positions.

    FIRMS rows without a usable coordinate are skipped rather than placed at a default.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        lat, lon = row.get("latitude"), row.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "brightness": row.get("brightness_ti4") or row.get("brightness"),
                "confidence": row.get("confidence"),
                "frp": row.get("frp"),
                "acq_date": row.get("acq_date"),
                "acq_time": row.get("acq_time"),
                "satellite": row.get("satellite"),
                "instrument": row.get("instrument"),
            }
        )
        if len(out) >= limit:
            break
    return out


__all__ = ["EvidenceContext", "gather_evidence", "LOCAL_RADIUS_M", "REGION_HALF_DEG", "COAST_SEARCH_MAX_M"]
