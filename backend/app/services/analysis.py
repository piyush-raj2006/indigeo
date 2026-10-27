from __future__ import annotations

"""Point hazard analysis: gather evidence, score, advise, persist.

This is the orchestration layer the API talks to. It composes the three layers beneath
it - ``evidence`` (the only network/spatial access), ``hazard`` (rule scoring) and
``recommendations`` (advice) - and writes the outcome to the relational store so an
analysis stays auditable after the providers have moved on.

Two honesty rules carry through the whole path:

* ``is_synthetic`` is always False here. A failure to reach a source is stored as a
  declared gap with reduced confidence, never backfilled with invented observations.
* if the run itself fails, the analysis is persisted with ``status="failed"`` and the
  error text before the exception is re-raised, so a broken run is still visible.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import math
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from ..core.database import session_scope
from ..core.errors import NotFoundError, ValidationError
from ..core.logging import get_logger
from ..gis.coords import LatLng, parse_radius_m
from ..models.hazard import HazardAnalysis, Recommendation
from .evidence import COAST_SEARCH_MAX_M, LOCAL_RADIUS_M, gather_evidence
from .hazard import HAZARDS, assess_context
from .recommendations import recommend
from .risk import HazardSignal, band_for, overall_band

log = get_logger("services.analysis")

ENGINE_NAME = "rule_engine"
ENGINE_VERSION = "1.0.0"
# The evidence window the assessors actually read: 45 days of reanalysis behind the
# point and a 7-day forecast ahead of it.
LOOKBACK_DAYS = 45
FORECAST_DAYS = 7

# A drawn area is assessed at a handful of point readings, not as an interpolated surface.
# Weather, seismicity, fire and live-alert evidence is gathered once; terrain and coastline
# distance are measured at every sample. Five keeps a nationwide request bounded.
AREA_SAMPLE_LIMIT = 5
AREA_SAMPLE_RADIUS_MIN_M = 1_000.0
AREA_SAMPLE_RADIUS_MAX_M = 15_000.0
LIVE_FEED_SOURCES = {"imd_alerts", "cwc_gauges", "bhuvan_hazards"}
FEED_OVERLAY_LIMIT = 25
OSM_OVERLAY_LIMIT = 300


def _window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=LOOKBACK_DAYS), now + timedelta(days=FORECAST_DAYS)


def _overall(signals: list[HazardSignal]) -> dict[str, Any]:
    assessed = [s for s in signals if s.has_evidence]
    return {
        "band": overall_band(signals),
        "hazards_assessed": [s.hazard for s in assessed],
        "hazards_unassessed": [s.hazard for s in signals if not s.has_evidence],
        "highest": max(assessed, key=lambda s: s.score).hazard if assessed else None,
        "highest_score": max((s.score for s in assessed), default=None),
        "mean_confidence": round(sum(s.confidence for s in signals) / len(signals), 3) if signals else 0.0,
        "confidence_by_hazard": {s.hazard: s.confidence for s in signals},
    }


def _scoring(signals: list[HazardSignal], ml_models_used: list[str]) -> dict[str, Any]:
    """State how the numbers were produced, in the payload rather than in a docstring.

    Every hazard here is a weighted read of measured evidence (``method="rule_engine"``).
    ``ml_models_used`` is only populated when an assessor actually calls a model, so an
    empty list is a statement that no inference took place, not a missing field. The
    proxy list names the factors each hazard tried to read and whether they answered.
    """
    return {
        "mode": "ml" if ml_models_used else "spatial_heuristics",
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "ml_models_used": list(ml_models_used),
        "heuristic_proxies": {
            s.hazard: [
                {"proxy": f.name, "source": f.source, "used": f.available} for f in s.factors
            ]
            for s in signals
        },
        "note": (
            "Every score is a weighted read of measured spatial, terrain and weather evidence. "
            "No ML model is trained on real labels in this build, so nothing here is an inference. "
            "A factor whose source did not answer is dropped and lowers confidence; it is never imputed."
            if not ml_models_used
            else "Scores combine rule-based factors with the listed ML models; each hazard still states its method."
        ),
    }


def _completeness(ctx, signals: list[HazardSignal]) -> dict[str, Any]:
    unavailable = sorted({source for s in signals for source in s.unavailable_sources} | {k for k, v in ctx.source_status.items() if v != "available"})
    return {
        "sources": dict(ctx.source_status),
        "sources_available": sorted(k for k, v in ctx.source_status.items() if v == "available"),
        "unavailable_layers": unavailable,
        "provenance": list(ctx.provenance),
        "evidence_warnings": list(ctx.warnings),
    }


def _limitations(ctx, place, signals: list[HazardSignal]) -> list[str]:
    """Every caveat the engine is aware of, de-duplicated in first-seen order.

    The boundary warnings come first: advice is addressed to a ward or district, so a label
    that was matched only by nearest polygon is a limitation of the advice, not of the data.
    """
    seen: dict[str, None] = {}
    for warning in list(place.warnings) + list(ctx.warnings) + [w for s in signals for w in s.warnings]:
        seen.setdefault(warning, None)
    return list(seen)


# -- live Indian feeds -------------------------------------------------------
def _compact_alert(alert: dict[str, Any]) -> dict[str, Any]:
    description = alert.get("description")
    return {
        "id": alert.get("id"),
        "title": alert.get("title"),
        "description": description[:600] if isinstance(description, str) else description,
        "event": alert.get("event"),
        "severity": alert.get("severity"),
        "severity_score": alert.get("severity_score"),
        "onset": alert.get("onset"),
        "expires": alert.get("expires"),
        "areas": list(alert.get("areas") or [])[:10],
        "tags": list(alert.get("tags") or []),
        "matched_by": alert.get("matched_by"),
        "geometry": alert.get("geometry"),
        "url": alert.get("url"),
        "format": alert.get("format"),
    }


def _compact_gauge(gauge: dict[str, Any]) -> dict[str, Any]:
    return {
        "station": gauge.get("station"),
        "river": gauge.get("river"),
        "lat": gauge.get("lat"),
        "lon": gauge.get("lon"),
        "level_m": gauge.get("level_m"),
        "warning_level_m": gauge.get("warning_level_m"),
        "danger_level_m": gauge.get("danger_level_m"),
        "trend": gauge.get("trend"),
        "observed_at": gauge.get("observed_at"),
        "severity": gauge.get("severity"),
        "distance_km": (
            round(gauge["distance_m"] / 1000.0, 1) if gauge.get("distance_m") is not None else None
        ),
    }


def _feed_summary(ctx, signals: list[HazardSignal]) -> dict[str, Any]:
    """The IMD/CWC/Bhuvan status and matched records, in one auditable block.

    Every configured feed appears with its own status even when it failed. A feed that was
    never gathered is reported as such rather than labelled unconfigured by assumption.
    """
    bundle = ctx.alerts or {}
    if not bundle:
        return {
            "feeds": {},
            "configured": False,
            "unconfigured": [],
            "unavailable": [],
            "blended_into": {},
            "warnings": [],
            "note": (
                "This run's evidence context carried no live-feed block, so no IMD, CWC or Bhuvan "
                "status can be reported. That is a gap in this run, not a finding of no alerts."
            ),
        }

    feeds: dict[str, Any] = {}
    for key, entry in (bundle.get("feeds") or {}).items():
        summary: dict[str, Any] = {
            "label": entry.get("label"),
            "provider": entry.get("provider"),
            "env_var": entry.get("env_var"),
            "status": entry.get("status"),
            "reason": entry.get("reason"),
        }
        if key == "imd":
            summary["alerts_total"] = entry.get("count")
            summary["matched_count"] = entry.get("matched_count")
            summary["items"] = [_compact_alert(alert) for alert in (entry.get("matched") or [])[:FEED_OVERLAY_LIMIT]]
        elif key == "cwc":
            summary["gauges_within_radius"] = entry.get("count")
            summary["search_radius_m"] = entry.get("search_radius_m")
            summary["max_severity"] = entry.get("max_severity")
            summary["unrated"] = entry.get("unrated")
            summary["gauges"] = [_compact_gauge(gauge) for gauge in (entry.get("gauges") or [])[:FEED_OVERLAY_LIMIT]]
        elif key == "bhuvan":
            summary["features_in_feed"] = entry.get("features")
            summary["matched_features"] = entry.get("matched")
            summary["unclassified_features"] = entry.get("unclassified")
            summary["hazards"] = entry.get("hazards") or {}
        feeds[key] = summary

    blended: dict[str, set[str]] = {}
    for signal in signals:
        for factor in signal.factors:
            if factor.available and factor.source in LIVE_FEED_SOURCES:
                blended.setdefault(signal.hazard, set()).add(factor.source)

    return {
        "feeds": feeds,
        "configured": bool(bundle.get("configured")),
        "unconfigured": list(bundle.get("unconfigured") or []),
        "unavailable": list(bundle.get("unavailable") or []),
        "blended_into": {hazard: sorted(sources) for hazard, sources in sorted(blended.items())},
        "warnings": list(bundle.get("warnings") or []),
        "note": (
            "Feeds are enabled per deployment through IMD_ALERTS_URL, CWC_GAUGES_URL and "
            "BHUVAN_HAZARDS_URL. An unconfigured feed adds no factor; a configured feed that "
            "failed is reported as a gap and lowers the confidence of the hazards that read it."
        ),
    }


# -- map overlays ------------------------------------------------------------
def _firms_reason(ctx) -> str | None:
    status = ctx.source_status.get("firms")
    if status == "available":
        return None
    if status == "unconfigured":
        return (ctx.fires or {}).get("reason") or (
            "FIRMS is not configured on this deployment (FIRMS_MAP_KEY is unset); no hotspots are shown."
        )
    return "FIRMS did not answer for this run; no hotspots are shown rather than an empty hot-fire finding."


def _map_overlays(
    ctx,
    *,
    scope: str,
    samples: list[dict[str, Any]],
    geometry: dict[str, Any] | None,
    radius_m: float | None,
    include_infrastructure: bool,
) -> dict[str, Any]:
    """Everything the map can draw for this run, plus what it deliberately cannot.

    The layer catalogue is part of the payload so the browser never has to guess whether an
    empty layer means "nothing was found" or "nothing was measured". Unavailable layers
    carry the reason; no overlay is synthesised to fill a toggle.
    """
    events = list((ctx.seismicity or {}).get("nearest_events") or [])
    fires = list((ctx.fires or {}).get("hotspots") or [])
    fire_total = int((ctx.fires or {}).get("hotspots_total") or len(fires))
    infrastructure = ctx.infrastructure or {}
    layers = infrastructure.get("layers") or {}

    osm_features: list[dict[str, Any]] = []
    osm_layers: dict[str, Any] = {}
    for name, entry in layers.items():
        osm_layers[name] = {
            key: entry.get(key)
            for key in ("status", "count", "truncated", "reason", "origin", "role")
            if key in entry
        }
        if entry.get("status") != "ok":
            continue
        for feature in entry.get("features") or []:
            if feature.get("lat") is None or feature.get("lon") is None:
                continue
            osm_features.append(
                {
                    "layer": name,
                    "lat": feature.get("lat"),
                    "lon": feature.get("lon"),
                    "name": feature.get("name"),
                    "kind": feature.get("kind"),
                    "osm_id": feature.get("osm_id"),
                }
            )
    osm_features = osm_features[:OSM_OVERLAY_LIMIT]

    feeds = (ctx.alerts or {}).get("feeds") or {}
    live_alerts: dict[str, Any] = {}
    imd = feeds.get("imd") or {}
    if imd.get("status") == "available":
        live_alerts["imd"] = [
            _compact_alert(alert) for alert in (imd.get("matched") or [])[:FEED_OVERLAY_LIMIT]
        ]
    cwc = feeds.get("cwc") or {}
    if cwc.get("status") == "available":
        live_alerts["cwc"] = [
            _compact_gauge(gauge) for gauge in (cwc.get("gauges") or [])[:FEED_OVERLAY_LIMIT]
        ]
    bhuvan = feeds.get("bhuvan") or {}
    if bhuvan.get("status") == "available":
        live_alerts["bhuvan"] = {
            "features_in_feed": bhuvan.get("features"),
            "matched_features": bhuvan.get("matched"),
            "hazards": bhuvan.get("hazards") or {},
            "geometry_available": False,
            "note": (
                "The Bhuvan connector scores intersecting susceptibility groups but does not "
                "retain their geometries, so no polygon is drawn for them."
            ),
        }

    terrain_samples = [sample for sample in samples if sample.get("terrain_available")]
    source_status = dict(ctx.source_status or {})
    live_statuses = {key: (entry or {}).get("status") for key, entry in feeds.items()}
    osm_answered = [name for name, entry in layers.items() if entry.get("status") == "ok"]

    catalogue: list[dict[str, Any]] = [
        {
            "id": "selection",
            "label": "Selection boundary",
            "kind": "polygon" if scope == "area" else "radius",
            "available": True,
            "feature_count": 1,
            "note": (
                "The drawn polygon as submitted; its vertices are returned in selection.geometry."
                if scope == "area"
                else f"The analysis circle around the point ({round((radius_m or 0) / 1000.0, 1)} km)."
            ),
        },
        {
            "id": "hazard_samples",
            "label": "Hazard sample points",
            "kind": "samples",
            "available": bool(samples),
            "feature_count": len(samples),
            "note": (
                "Discrete point readings, not an interpolated heat surface. "
                f"{len(samples)} sample(s) at {round((radius_m or 0) / 1000.0, 1)} km radius."
            ),
        },
        {
            "id": "seismic_events",
            "label": "Historical earthquakes (USGS, M4.5+, 50 yr)",
            "kind": "points",
            "available": source_status.get("usgs_earthquakes") == "available",
            "feature_count": len(events),
            "note": (
                "Epicentres from the USGS ComCat catalogue; historical hazard, not a forecast."
                if events
                else "ComCat answered; no M4.5+ epicentre in the 50-year window fell inside the regional box."
            ),
            "reason": (
                None
                if source_status.get("usgs_earthquakes") == "available"
                else "USGS ComCat did not answer for this run; no epicentres are shown."
            ),
        },
        {
            "id": "firms_hotspots",
            "label": "NASA FIRMS active-fire hotspots (7 d)",
            "kind": "points",
            "available": source_status.get("firms") == "available",
            "feature_count": len(fires),
            "note": (
                f"{len(fires)} of {fire_total} detection(s) in the regional box are returned for drawing."
                if fires
                else "FIRMS answered; no active-fire detection fell inside the regional box in the last 7 days."
            )
            if source_status.get("firms") == "available"
            else None,
            "reason": _firms_reason(ctx),
        },
        {
            "id": "elevation_samples",
            "label": "Elevation samples (DEM)",
            "kind": "samples",
            "available": bool(terrain_samples),
            "feature_count": len(terrain_samples),
            "note": (
                "DEM readings at the sampled points. No elevation raster or gradient surface is "
                "streamed to the browser, so this layer is points, not a continuous gradient."
            ),
            "reason": (
                None if terrain_samples else "Terrain did not answer at any sampled point in this run."
            ),
        },
        {
            "id": "osm_infrastructure",
            "label": "OSM lifeline infrastructure",
            "kind": "points",
            "available": bool(osm_answered),
            "feature_count": len(osm_features),
            "layers": osm_layers,
            "note": (
                f"{len(osm_answered)} of {len(layers)} lifeline layer(s) answered; "
                f"{len(osm_features)} feature(s) with coordinates are returned for drawing."
                if include_infrastructure
                else None
            ),
            "reason": (
                None
                if osm_answered
                else (
                    "Lifeline infrastructure was not requested for this run. Re-run with "
                    "include_infrastructure=true (queued job) to populate it."
                    if not include_infrastructure
                    else "No OSM lifeline layer answered for this run; the block is a gap, not a finding of none."
                )
            ),
        },
        {
            "id": "live_alerts",
            "label": "Live IMD / CWC alerts",
            "kind": "mixed",
            "available": any(status == "available" for status in live_statuses.values()),
            "feature_count": len(live_alerts.get("imd") or []) + len(live_alerts.get("cwc") or []),
            "feeds": live_statuses,
            "note": (
                "IMD alert polygons and CWC gauge points that matched this selection."
                if live_alerts
                else "No configured feed returned a match for this selection."
            ),
            "reason": (
                None
                if any(status == "available" for status in live_statuses.values())
                else (
                    "No live Indian feed is configured, so there is nothing to draw. This is a "
                    "deployment gap, not evidence that no warning exists."
                    if feeds and not any(status == "available" for status in live_statuses.values())
                    else "No live-feed block was gathered for this run."
                )
            ),
        },
        {
            "id": "seismic_faults",
            "label": "Seismic fault lines",
            "kind": "lines",
            "available": False,
            "feature_count": 0,
            "reason": (
                "No fault-line dataset is ingested in this build. Earthquake risk is scored from "
                "USGS epicentre climatology, not from mapped faults, so the layer stays off rather "
                "than being drawn from invented linework."
            ),
        },
    ]

    return {
        "scope": scope,
        "geometry": geometry,
        "radius_m": radius_m,
        "samples": samples,
        "seismic_events": events,
        "fire_hotspots": fires,
        "osm_infrastructure": {
            "queried": bool(include_infrastructure),
            "layers": osm_layers,
            "features": osm_features,
            "features_returned": len(osm_features),
        },
        "live_alerts": live_alerts,
        "layers": catalogue,
        "notes": [
            "Hazard sample points are discrete readings; the map draws no interpolated surface between them.",
            "Historical epicentres are a hazard climatology, not a prediction or a fault trace.",
            "Elevation is reported as DEM readings at sample points; no elevation gradient raster is served.",
        ],
    }


# -- drawn-area analysis -----------------------------------------------------
def _sample_points(geometry, limit: int = AREA_SAMPLE_LIMIT) -> list[dict[str, float]]:
    """Deterministic interior sample points: the representative point plus spread grid points.

    Every candidate is checked against the drawn geometry, so a sample can never fall outside
    the selection. MultiPolygon selections may sample more than one part.
    """
    from shapely.geometry import Point

    representative = geometry.representative_point()
    minx, miny, maxx, maxy = geometry.bounds
    width, height = maxx - minx, maxy - miny
    grid = (
        (0.5, 0.5),
        (0.2, 0.2),
        (0.8, 0.2),
        (0.2, 0.8),
        (0.8, 0.8),
        (0.5, 0.15),
        (0.5, 0.85),
        (0.15, 0.5),
        (0.85, 0.5),
    )
    interior = []
    for fx, fy in grid:
        candidate = Point(minx + fx * width, miny + fy * height)
        if geometry.contains(candidate):
            interior.append(candidate)
    interior.sort(key=lambda point: representative.distance(point), reverse=True)
    points = [representative, *interior[: max(0, limit - 1)]]
    return [{"lat": round(float(point.y), 6), "lon": round(float(point.x), 6)} for point in points]


def _area_sample_radius(area_km2: float, sample_count: int) -> float:
    """Radius each sample reads for terrain and coastline, derived from the selection size."""
    if area_km2 <= 0:
        return AREA_SAMPLE_RADIUS_MIN_M
    spacing = (area_km2**0.5) * 1000.0 / math.sqrt(max(1, sample_count))
    return round(max(AREA_SAMPLE_RADIUS_MIN_M, min(AREA_SAMPLE_RADIUS_MAX_M, spacing)), 1)


def _sample_context(base, lon: float, lat: float, radius_m: float):
    """A sample's view of the shared evidence, with terrain and coastline measured here.

    Weather, seismicity, fire and live-alert evidence are the same regional readings for
    every sample and are reused by reference. Only the local surface is re-measured, so the
    response can say exactly which factors varied between samples.
    """
    from dataclasses import replace

    from ..gis.spatial import get_spatial_store
    from .locations import terrain_at

    ctx = replace(
        base,
        lon=lon,
        lat=lat,
        radius_m=radius_m,
        source_status=dict(base.source_status),
        warnings=list(base.warnings),
        provenance=list(base.provenance),
    )
    try:
        ctx.terrain = terrain_at(lon, lat, radius_m)
        ctx.source_status["dem"] = "available" if ctx.terrain.get("available") else "unavailable"
    except Exception as exc:
        ctx.terrain = {"available": False, "summary": None, "reason": f"{type(exc).__name__}: {exc}"}
        ctx.source_status["dem"] = "unavailable"

    try:
        engine = get_spatial_store()
        if engine.has_layer("india_outline"):
            ctx.coast_distance_m = engine.distance_to_boundary(
                "india_outline", lon, lat, max_distance_m=COAST_SEARCH_MAX_M
            )
            ctx.coast_measured = True
            ctx.source_status["coastline"] = "available"
            if ctx.coast_distance_m is None:
                ctx.warnings.append(
                    f"No coastline within {COAST_SEARCH_MAX_M / 1000:.0f} km of the sample at "
                    f"({lat:.4f}, {lon:.4f}); coastal exposure is zero there, not unmeasured."
                )
        else:
            ctx.coast_distance_m = None
            ctx.coast_measured = False
            ctx.source_status["coastline"] = "unavailable"
    except Exception as exc:
        ctx.coast_distance_m = None
        ctx.coast_measured = False
        ctx.source_status["coastline"] = "unavailable"
        ctx.warnings.append(f"Coastline distance unavailable at a sample: {type(exc).__name__}.")
    return ctx


def _band_counts(scores: list[float]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for score in scores:
        band = band_for(score)
        counts[band] = counts.get(band, 0) + 1
    return counts


def _aggregate_area(
    sample_signals: list[list[HazardSignal]],
) -> tuple[list[dict[str, Any]], list[HazardSignal]]:
    """Per-hazard range across samples, plus the worst sample for each hazard.

    The worst sampled point drives the headline score and the advice. It is not an average:
    an average would hide a hazard that is severe anywhere inside the selection.
    """
    if not sample_signals:
        return [], []
    hazards = [signal.hazard for signal in sample_signals[0]]
    summaries: list[dict[str, Any]] = []
    worst: list[HazardSignal] = []
    for index, hazard in enumerate(hazards):
        column = [signals[index] for signals in sample_signals]
        assessed = [signal for signal in column if signal.has_evidence]
        scores = [signal.score for signal in assessed]
        peak = max(assessed, key=lambda signal: signal.score) if assessed else column[0]
        worst.append(peak)
        summaries.append(
            {
                "hazard": hazard,
                "samples_total": len(column),
                "samples_assessed": len(assessed),
                "unassessed_samples": len(column) - len(assessed),
                "min_score": round(min(scores), 1) if scores else None,
                "mean_score": round(sum(scores) / len(scores), 1) if scores else None,
                "max_score": round(max(scores), 1) if scores else None,
                "band_at_min": band_for(min(scores)) if scores else "unassessed",
                "band_at_max": band_for(max(scores)) if scores else "unassessed",
                "bands": _band_counts(scores),
                "mean_confidence": (
                    round(sum(signal.confidence for signal in column) / len(column), 3) if column else None
                ),
                "method": peak.method,
                "evidence_kind": peak.evidence_kind,
            }
        )
    return summaries, worst


def _area_overall(
    sample_signals: list[list[HazardSignal]],
    summaries: list[dict[str, Any]],
    worst_signals: list[HazardSignal],
) -> dict[str, Any]:
    order = [signal.hazard for signal in worst_signals]
    assessed = {signal.hazard for signals in sample_signals for signal in signals if signal.has_evidence}
    confidences = [signal.confidence for signals in sample_signals for signal in signals] or [0.0]
    peak = max(
        (summary for summary in summaries if summary["max_score"] is not None),
        key=lambda summary: summary["max_score"],
        default=None,
    )
    top_score = peak["max_score"] if peak else None
    sample_bands: dict[str, int] = {}
    for signals in sample_signals:
        band = overall_band(signals)
        sample_bands[band] = sample_bands.get(band, 0) + 1
    return {
        "band": band_for(top_score) if top_score is not None else "unknown",
        "hazards_assessed": [hazard for hazard in order if hazard in assessed],
        "hazards_unassessed": [hazard for hazard in order if hazard not in assessed],
        "highest": peak["hazard"] if peak else None,
        "highest_score": top_score,
        "mean_confidence": round(sum(confidences) / len(confidences), 3),
        "confidence_by_hazard": {
            summary["hazard"]: summary["mean_confidence"] for summary in summaries
        },
        "sample_bands": sample_bands,
    }


def _sample_rows(sample_points, sample_ctxs, sample_signals) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for point, ctx, signals in zip(sample_points, sample_ctxs, sample_signals):
        assessed = [signal for signal in signals if signal.has_evidence]
        score = max((signal.score for signal in assessed), default=None)
        terrain = ctx.terrain or {}
        summary = terrain.get("summary") or {}
        rows.append(
            {
                **point,
                "score": score,
                "band": band_for(score) if score is not None else "unassessed",
                "hazards_assessed": len(assessed),
                "terrain_available": bool(terrain.get("available")),
                "terrain_source": terrain.get("source"),
                "elevation_at_point_m": summary.get("elevation_at_point_m"),
                "relief_m": summary.get("elevation_relief_m"),
                "coast_distance_m": ctx.coast_distance_m,
            }
        )
    return rows


def _area_hazard_rows(worst_signals, summaries) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal, summary in zip(worst_signals, summaries):
        row = signal.as_dict()
        row["details"] = {**row.get("details", {}), "area": summary}
        if summary["samples_assessed"] > 1:
            row["warnings"] = [
                *row.get("warnings", []),
                f"Area score is the highest of {summary['samples_assessed']} sampled points "
                f"({summary['min_score']}–{summary['max_score']}); it is not an interior average.",
            ]
        rows.append(row)
    return rows


def _area_completeness(sample_ctxs, worst_signals) -> dict[str, Any]:
    base = sample_ctxs[0]
    completeness = _completeness(base, worst_signals)
    states = [ctx.source_status.get("dem") for ctx in sample_ctxs]
    if all(state == "available" for state in states):
        completeness["sources"]["dem"] = "available"
    elif any(state == "available" for state in states):
        completeness["sources"]["dem"] = "degraded"
    else:
        completeness["sources"]["dem"] = "unavailable"
    completeness["sources_available"] = sorted(
        source for source, state in completeness["sources"].items() if state == "available"
    )
    completeness["unavailable_layers"] = sorted(
        {source for signal in worst_signals for source in signal.unavailable_sources}
        | {source for source, state in completeness["sources"].items() if state != "available"}
    )
    missing = sum(1 for state in states if state != "available")
    if missing:
        completeness["evidence_warnings"].append(
            f"Terrain answered at {len(states) - missing} of {len(states)} sampled points; hazards that "
            "read terrain are scored only where it answered and carry the gap in confidence."
        )
    return completeness


def _area_limitations(area_warnings, sample_ctxs, worst_signals, sample_radius_m, sample_count) -> list[str]:
    seen: dict[str, None] = {}
    for warning in (
        list(area_warnings or [])
        + [warning for ctx in sample_ctxs for warning in ctx.warnings]
        + [warning for signal in worst_signals for warning in signal.warnings]
    ):
        seen.setdefault(warning, None)
    seen.setdefault(
        f"The selection is assessed at {sample_count} interior point samples with a {round(sample_radius_m / 1000.0, 1)} km "
        "reading radius. Each hazard reports its worst sampled point, not a continuous surface or an interior "
        "average. Weather, seismicity, fire and live-alert evidence were gathered once at the selection's "
        "representative point; terrain and coastline distance were measured at every sample.",
        None,
    )
    return list(seen)


def run_point_analysis(
    lat: float,
    lon: float,
    *,
    radius_m: float | None = None,
    hazards: list[str] | tuple[str, ...] | None = None,
    requested_by: str | None = None,
    persist: bool = True,
    include_infrastructure: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Assess every hazard at one coordinate and store the auditable result.

    Lifeline infrastructure (Overpass) is off by default: no hazard factor reads it and it
    is the slowest, least reliable source in the build, so a synchronous analysis waits on
    evidence that cannot change the score. Ask for it when the caller can tolerate minutes.
    """
    point = LatLng(lat, lon)
    radius = parse_radius_m(radius_m) if radius_m is not None else LOCAL_RADIUS_M
    selected = _validate_hazards(hazards)
    started = datetime.now(timezone.utc)
    analysis_uuid = str(uuid4())
    window_start, window_end = _window()
    # No assessor calls a model in this build, so this stays empty until one does.
    ml_models_used: list[str] = []

    from .locations import admin_context

    def emit(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    try:
        emit("admin context")
        place = admin_context(point.lon, point.lat)
        ctx = gather_evidence(
            point.lon, point.lat, radius_m=radius, admin=place.as_dict(),
            include_infrastructure=include_infrastructure, on_progress=on_progress,
        )
        emit("scoring")
        signals = assess_context(ctx, selected)
        emit("recommendations")
        rows = recommend(signals)
    except Exception as exc:
        if persist:
            _persist(
                analysis_uuid=analysis_uuid,
                geometry_kind="point",
                lon=point.lon,
                lat=point.lat,
                radius=radius,
                selected=selected,
                admin={},
                window=(window_start, window_end),
                model_versions={"engine": ENGINE_NAME, "engine_version": ENGINE_VERSION, "ml_models_used": ml_models_used},
                results={},
                completeness={},
                limitations=[],
                started=started,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                requested_by=requested_by,
            )
        raise

    live_feeds = _feed_summary(ctx, signals)
    assessed = [signal for signal in signals if signal.has_evidence]
    peak_score = max((signal.score for signal in assessed), default=None)
    terrain_summary = (ctx.terrain or {}).get("summary") or {}
    sample_point = {
        "lat": round(point.lat, 6),
        "lon": round(point.lon, 6),
        "score": peak_score,
        "band": band_for(peak_score) if peak_score is not None else "unassessed",
        "hazards_assessed": len(assessed),
        "terrain_available": bool((ctx.terrain or {}).get("available")),
        "terrain_source": (ctx.terrain or {}).get("source"),
        "elevation_at_point_m": terrain_summary.get("elevation_at_point_m"),
        "relief_m": terrain_summary.get("elevation_relief_m"),
        "coast_distance_m": ctx.coast_distance_m,
    }
    overlays = _map_overlays(
        ctx,
        scope="point",
        samples=[sample_point],
        geometry=None,
        radius_m=radius,
        include_infrastructure=include_infrastructure,
    )

    results = {
        "overall": _overall(signals),
        "hazards": [s.as_dict() for s in signals],
        "recommendations": rows,
        "scoring": _scoring(signals, ml_models_used),
        "evidence": {
            "terrain": ctx.terrain,
            "coast_distance_m": ctx.coast_distance_m,
            "seismicity": ctx.seismicity,
            "fires": ctx.fires,
            "infrastructure": ctx.infrastructure,
            "alerts": ctx.alerts,
            "weather_recent": _compact_recent(ctx.weather_recent),
            "weather_forecast": ctx.weather_forecast,
        },
        "live_feeds": live_feeds,
        "map_overlays": overlays,
        "request": {
            "mode": "point",
            "allow_remote": True,
            "radius_m": radius,
            "include_infrastructure": include_infrastructure,
        },
    }
    payload = {
        "analysis_uuid": analysis_uuid,
        "status": "completed",
        "created_at": started.isoformat(),
        "location": {
            "lat": round(point.lat, 6),
            "lon": round(point.lon, 6),
            "radius_m": radius,
            "geometry_kind": "point",
            "admin": place.as_dict(),
        },
        "overall": results["overall"],
        "hazards": results["hazards"],
        "recommendations": rows,
        "data_completeness": _completeness(ctx, signals),
        "limitations": _limitations(ctx, place, signals),
        "scoring": results["scoring"],
        "infrastructure_summary": _infrastructure_summary(results),
        "live_feeds": live_feeds,
        "map_overlays": overlays,
        "request": results["request"],
        "model_versions": {"engine": ENGINE_NAME, "engine_version": ENGINE_VERSION, "ml_models_used": ml_models_used},
        "is_synthetic": False,
        "duration_ms": None,
    }
    if persist:
        payload["duration_ms"] = _persist(
            analysis_uuid=analysis_uuid,
            geometry_kind="point",
            lon=point.lon,
            lat=point.lat,
            radius=radius,
            selected=selected,
            admin=place.as_dict(),
            window=(window_start, window_end),
            model_versions=payload["model_versions"],
            results=results,
            completeness=payload["data_completeness"],
            limitations=payload["limitations"],
            started=started,
            status="completed",
            error=None,
            requested_by=requested_by,
            recommendation_rows=rows,
        )
    return payload


def run_area_analysis(
    polygon: dict[str, Any] | str,
    *,
    hazards: list[str] | tuple[str, ...] | None = None,
    requested_by: str | None = None,
    persist: bool = True,
    include_infrastructure: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Assess a drawn GeoJSON polygon at interior sample points and store the result.

    Selection validation, area/perimeter, intersecting districts and the representative
    point come from :func:`locations.resolve_area`; nothing here re-implements them. The
    regional evidence (weather, seismicity, fire, live alerts) is gathered once at the
    representative point and the local surface is measured at every sample. That split is
    stated in the limitations of the run rather than left for a reader to assume.
    """
    from shapely.geometry import shape

    from .locations import resolve_area

    selected = _validate_hazards(hazards)
    started = datetime.now(timezone.utc)
    analysis_uuid = str(uuid4())
    window_start, window_end = _window()
    ml_models_used: list[str] = []

    def emit(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    emit("selection")
    # Boundaries only: an analysis does not need a remote reverse-geocode of the polygon,
    # and skipping it keeps the selection step deterministic and offline-capable.
    area = resolve_area(polygon=polygon, allow_remote=False)
    geometry = shape(area["selection"]["geometry"])
    admin = area["place"]
    representative = area["selection"]["representative_point"]
    sample_points = _sample_points(geometry)
    sample_radius = _area_sample_radius(float(area["selection"]["area_km2"]), len(sample_points))

    try:
        ctx = gather_evidence(
            representative["lon"],
            representative["lat"],
            radius_m=sample_radius,
            admin=admin,
            include_infrastructure=include_infrastructure,
            on_progress=on_progress,
        )
        emit("samples")
        sample_ctxs = [
            _sample_context(ctx, point["lon"], point["lat"], sample_radius) for point in sample_points
        ]
        emit("scoring")
        sample_signals = [assess_context(sample_ctx, selected) for sample_ctx in sample_ctxs]
        summaries, worst_signals = _aggregate_area(sample_signals)
        emit("recommendations")
        rows = recommend(worst_signals)
    except Exception as exc:
        if persist:
            _persist(
                analysis_uuid=analysis_uuid,
                geometry_kind="polygon",
                lon=representative["lon"],
                lat=representative["lat"],
                radius=sample_radius,
                wkt=geometry.wkt,
                selected=selected,
                admin=admin,
                window=(window_start, window_end),
                model_versions={
                    "engine": ENGINE_NAME,
                    "engine_version": ENGINE_VERSION,
                    "ml_models_used": ml_models_used,
                },
                results={},
                completeness={},
                limitations=[],
                started=started,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                requested_by=requested_by,
            )
        raise

    sample_rows = _sample_rows(sample_points, sample_ctxs, sample_signals)
    hazard_rows = _area_hazard_rows(worst_signals, summaries)
    overall = _area_overall(sample_signals, summaries, worst_signals)
    completeness = _area_completeness(sample_ctxs, worst_signals)
    limitations = _area_limitations(
        area.get("warnings"), sample_ctxs, worst_signals, sample_radius, len(sample_points)
    )
    live_feeds = _feed_summary(ctx, worst_signals)
    overlays = _map_overlays(
        ctx,
        scope="area",
        samples=sample_rows,
        geometry=area["selection"].get("geometry"),
        radius_m=sample_radius,
        include_infrastructure=include_infrastructure,
    )
    scoring = _scoring(worst_signals, ml_models_used)
    scoring["aggregation"] = "worst_sampled_point"
    scoring["note"] = (
        f"{scoring['note']} For a drawn area, each hazard's headline score is the highest of its sampled "
        "points and the full range is reported in details.area."
    )
    area_block = {
        "mode": "polygon",
        "area_km2": area["selection"]["area_km2"],
        "perimeter_km": area["selection"]["perimeter_km"],
        "bounds": area["selection"]["bounds"],
        "vertices": area["selection"]["vertices"],
        "representative_point": representative,
        "sample_radius_m": sample_radius,
        "samples_total": len(sample_rows),
        "samples": sample_rows,
        "hazard_summary": summaries,
        "aggregation": "worst_sampled_point",
        "aggregation_note": (
            "Per hazard, the headline score and band are the highest scoring sample. The range across "
            "samples is reported in the hazard's details.area; it is not an interior average."
        ),
        "districts": area["districts"],
        "states": area["states"],
    }
    results = {
        "overall": overall,
        "hazards": hazard_rows,
        "recommendations": rows,
        "scoring": scoring,
        "evidence": {
            "terrain": ctx.terrain,
            "coast_distance_m": ctx.coast_distance_m,
            "seismicity": ctx.seismicity,
            "fires": ctx.fires,
            "infrastructure": ctx.infrastructure,
            "alerts": ctx.alerts,
            "weather_recent": _compact_recent(ctx.weather_recent),
            "weather_forecast": ctx.weather_forecast,
        },
        "selection": area["selection"],
        "area": area_block,
        "live_feeds": live_feeds,
        "map_overlays": overlays,
        "request": {
            "mode": "polygon",
            # The selection is resolved from loaded boundaries only; no reverse-geocode call.
            "allow_remote": False,
            "sample_radius_m": sample_radius,
            "samples": len(sample_points),
            "include_infrastructure": include_infrastructure,
        },
    }
    payload = {
        "analysis_uuid": analysis_uuid,
        "status": "completed",
        "created_at": started.isoformat(),
        "location": {
            "lat": representative["lat"],
            "lon": representative["lon"],
            "radius_m": sample_radius,
            "geometry_kind": "polygon",
            "admin": admin,
        },
        "selection": area["selection"],
        "area": area_block,
        "districts": area["districts"],
        "states": area["states"],
        "overall": overall,
        "hazards": hazard_rows,
        "recommendations": rows,
        "data_completeness": completeness,
        "limitations": limitations,
        "scoring": scoring,
        "infrastructure_summary": _infrastructure_summary(results),
        "live_feeds": live_feeds,
        "map_overlays": overlays,
        "request": results["request"],
        "model_versions": {
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            "ml_models_used": ml_models_used,
        },
        "is_synthetic": False,
        "duration_ms": None,
    }
    if persist:
        payload["duration_ms"] = _persist(
            analysis_uuid=analysis_uuid,
            geometry_kind="polygon",
            lon=representative["lon"],
            lat=representative["lat"],
            radius=sample_radius,
            wkt=geometry.wkt,
            selected=selected,
            admin=admin,
            window=(window_start, window_end),
            model_versions=payload["model_versions"],
            results=results,
            completeness=payload["data_completeness"],
            limitations=payload["limitations"],
            started=started,
            status="completed",
            error=None,
            requested_by=requested_by,
            recommendation_rows=rows,
        )
    return payload



def _compact_recent(recent: dict[str, Any]) -> dict[str, Any]:
    """Keep the daily series out of the stored result; it is summarised in the factors."""
    return {k: v for k, v in (recent or {}).items() if k != "series"} | {
        "series_days": len((recent or {}).get("series", []))
    }


def _infrastructure_summary(results: dict[str, Any]) -> dict[str, Any]:
    """Per-layer outcome of the lifeline lookup, without the feature payload.

    The stored evidence holds every hospital and bridge returned; this is the part a
    reader needs to judge it: which layers answered, how many features, and what was cut
    off at the wall-clock budget.
    """
    infrastructure = ((results or {}).get("evidence") or {}).get("infrastructure") or {}
    if not infrastructure:
        return {"queried": False}
    layers = infrastructure.get("layers") or {}
    return {
        "queried": True,
        "deadline_seconds": infrastructure.get("deadline_seconds"),
        "elapsed_ms": infrastructure.get("elapsed_ms"),
        "answered": sum(1 for layer in layers.values() if layer.get("status") == "ok"),
        "origins": dict(infrastructure.get("origins") or {}),
        "origin_states": dict(infrastructure.get("origin_states") or {}),
        "local": infrastructure.get("local"),
        "layers": {
            name: {
                key: layer.get(key)
                for key in ("status", "count", "truncated", "reason", "origin")
                if key in layer
            }
            for name, layer in layers.items()
        },
        "warnings": list(infrastructure.get("warnings") or []),
    }


def _validate_hazards(hazards: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not hazards:
        return HAZARDS
    unknown = [h for h in hazards if h not in HAZARDS]
    if unknown:
        raise ValidationError(
            f"Unknown hazard(s): {', '.join(unknown)}.",
            details={"unknown_hazards": unknown, "known_hazards": list(HAZARDS)},
        )
    return tuple(hazards)


def _persist(
    *,
    analysis_uuid: str,
    geometry_kind: str,
    lon: float | None,
    lat: float | None,
    radius: float | None,
    selected: tuple[str, ...],
    admin: dict[str, Any],
    window: tuple[datetime, datetime],
    model_versions: dict[str, Any],
    results: dict[str, Any],
    completeness: dict[str, Any],
    limitations: list[str],
    started: datetime,
    status: str,
    error: str | None,
    requested_by: str | None = None,
    recommendation_rows: list[dict[str, Any]] | None = None,
    wkt: str | None = None,
) -> float:
    """Write the analysis (and its advice) and return how long the run took."""
    duration_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000.0, 1)
    with session_scope() as session:
        analysis = HazardAnalysis(
            analysis_uuid=analysis_uuid,
            status=status,
            hazards=list(selected),
            geometry_kind=geometry_kind,
            lon=None if lon is None else round(lon, 6),
            lat=None if lat is None else round(lat, 6),
            radius_m=radius,
            wkt=wkt,
            srid=4326,
            admin_context=admin,
            window_start=window[0],
            window_end=window[1],
            results=results,
            data_completeness=completeness,
            model_versions=model_versions,
            warnings=limitations,
            error=error,
            duration_ms=duration_ms,
            requested_by=requested_by,
            is_synthetic=False,
        )
        session.add(analysis)
        session.flush()
        for row in recommendation_rows or []:
            session.add(
                Recommendation(
                    analysis_id=analysis.id,
                    analysis_uuid=analysis_uuid,
                    hazard=row["hazard"],
                    action=row["action"],
                    category=row["category"],
                    horizon=row["horizon"],
                    rationale=row["rationale"],
                    evidence=row["evidence"],
                    authority=row["authority"],
                    guidance_url=row["guidance_url"],
                    limitations=row["limitations"],
                    requires_professional_assessment=row["requires_professional_assessment"],
                    score=row["score"],
                    method=row["method"],
                )
            )
    return duration_ms


def get_analysis(analysis_uuid: str) -> dict[str, Any]:
    """Read a stored analysis back, with its recommendation rows."""
    with session_scope() as session:
        analysis = session.scalar(select(HazardAnalysis).where(HazardAnalysis.analysis_uuid == analysis_uuid))
        if analysis is None:
            raise NotFoundError(f"No hazard analysis with id {analysis_uuid!r}.", details={"analysis_uuid": analysis_uuid})
        rows = session.scalars(select(Recommendation).where(Recommendation.analysis_uuid == analysis_uuid)).all()
        return {
            "analysis_uuid": analysis.analysis_uuid,
            "status": analysis.status,
            "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
            "location": {
                "lat": analysis.lat,
                "lon": analysis.lon,
                "radius_m": analysis.radius_m,
                "geometry_kind": analysis.geometry_kind,
                "wkt": analysis.wkt,
                "admin": analysis.admin_context,
            },
            "selection": (analysis.results or {}).get("selection"),
            "area": (analysis.results or {}).get("area"),
            "districts": (analysis.results or {}).get("districts"),
            "states": (analysis.results or {}).get("states"),
            # ``hazards`` must stay the scored results, the same shape POST returns; the
            # requested names are their own field. Returning names here made the stored
            # record page read ``.hazard``/``.band`` off strings and crash.
            "hazards": (analysis.results or {}).get("hazards", []),
            "requested_hazards": analysis.hazards,
            "request": (analysis.results or {}).get("request", {}),
            "overall": (analysis.results or {}).get("overall", {}),
            "recommendations": [
                {
                    "hazard": r.hazard,
                    "action": r.action,
                    "category": r.category,
                    "horizon": r.horizon,
                    "rationale": r.rationale,
                    "evidence": r.evidence,
                    "authority": r.authority,
                    "guidance_url": r.guidance_url,
                    "limitations": r.limitations,
                    "requires_professional_assessment": r.requires_professional_assessment,
                    "score": r.score,
                    "method": r.method,
                }
                for r in rows
            ],
            "data_completeness": analysis.data_completeness,
            "limitations": analysis.warnings,
            # Absent on records stored before this block existed; the UI derives the same
            # list from the stored factors in that case.
            "scoring": (analysis.results or {}).get("scoring"),
            "infrastructure_summary": _infrastructure_summary(analysis.results or {}),
            # Present on runs recorded after these blocks existed; older records return None
            # rather than an empty block that would read as "no feeds were available".
            "live_feeds": (analysis.results or {}).get("live_feeds"),
            "map_overlays": (analysis.results or {}).get("map_overlays"),
            "model_versions": analysis.model_versions,
            "is_synthetic": analysis.is_synthetic,
            "requested_by": analysis.requested_by,
            "duration_ms": analysis.duration_ms,
            "error": analysis.error,
        }


def recent_analyses(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent analyses, newest first, without their bulky result blobs."""
    with session_scope() as session:
        rows = session.scalars(
            select(HazardAnalysis).order_by(HazardAnalysis.created_at.desc()).limit(max(1, min(int(limit), 200)))
        ).all()
        return [
            {
                "analysis_uuid": a.analysis_uuid,
                "status": a.status,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "lat": a.lat,
                "lon": a.lon,
                "radius_m": a.radius_m,
                "geometry_kind": a.geometry_kind,
                "area_km2": ((a.results or {}).get("area") or {}).get("area_km2"),
                "overall_band": (a.results or {}).get("overall", {}).get("band"),
                "hazards": a.hazards,
                "warnings": len(a.warnings or []),
                "duration_ms": a.duration_ms,
                "requested_by": a.requested_by,
            }
            for a in rows
        ]


__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "get_analysis",
    "recent_analyses",
    "run_area_analysis",
    "run_point_analysis",
]
