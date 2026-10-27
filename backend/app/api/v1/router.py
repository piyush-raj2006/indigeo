from __future__ import annotations

"""HTTP surface for IndiGeo.

Every endpoint here maps onto a service that already exists and has been measured against
live sources. Two rules govern the shape of this layer:

* Availability is reported, never implied. A response carries the per-source status,
  provenance and limitations that the engine produced; a source that did not answer shows
  as a gap instead of a zero.
* Nothing is queued, faked or cached behind the caller's back. The slow evidence (OSM
  lifelines through Overpass) is opt-in per request because it is measured at minutes; it
  can be run inline or submitted to the explicit job queue, and either way the provider
  snapshot is returned with the timestamp of the probe that produced it rather than
  presented as a live check.

The handlers are plain ``def`` on purpose: the services use synchronous httpx clients and a
blocking spatial store, so FastAPI must run them in its worker threadpool rather than on the
event loop.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, model_validator

from ...core.cache import cache_stats
from ...core.config import get_settings
from ...core.errors import ValidationError
from ...gis.coords import parse_radius_m
from ...gis.spatial import get_spatial_store, safe_table_name
from ...integrations.base import all_providers
from ...services.analysis import (
    ENGINE_NAME,
    ENGINE_VERSION,
    get_analysis,
    recent_analyses,
    run_area_analysis,
    run_point_analysis,
)
from ...services.hazard import HAZARDS
from ...services.jobs import get_job, recent_jobs, submit_analysis_job
from ...services.locations import resolve_point, reverse_location, search_places
from ...services.recommendations import AUTHORITIES, BAND_LABELS, BAND_ORDER
from ...services.risk import BAND_THRESHOLDS

router = APIRouter()

_SETTINGS = get_settings()
_SNAPSHOT = Path(_SETTINGS.data_dir) / "provider_status.json"


class PointAnalysisRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: str | float | None = Field(
        default=None,
        description="Sample radius: a number of metres or a string such as '3 km'. Default 5000 m.",
    )
    hazards: list[str] | None = Field(default=None, description="Restrict to these hazards; default is all eight.")
    requested_by: str | None = None
    include_infrastructure: bool = Field(
        default=False,
        description="Query OSM lifelines through Overpass. Measured at 500+ s and frequently unavailable; "
        "no hazard factor consumes the result, so it is off by default.",
    )


class AnalysisRequest(BaseModel):
    """One analysis request: either a point with an optional radius, or a drawn boundary.

    ``geometry`` accepts any GeoJSON object that carries one Polygon or MultiPolygon -
    the geometry itself, a Feature, or a FeatureCollection whose first feature has one.
    Nothing is inferred from a mixed payload: sending both lat/lon and geometry, or a
    radius alongside a boundary, is rejected rather than silently preferring one.
    """

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_m: str | float | None = Field(
        default=None,
        description="Point selections only: metres or a string such as '3 km'. Default 5000 m.",
    )
    geometry: dict[str, Any] | None = Field(
        default=None,
        description="GeoJSON Polygon/MultiPolygon, Feature, or FeatureCollection. Replaces lat/lon.",
    )
    hazards: list[str] | None = Field(default=None, description="Restrict to these hazards; default is all eight.")
    requested_by: str | None = None
    include_infrastructure: bool = Field(
        default=False,
        description="Query OSM lifelines through Overpass. Measured at 500+ s and frequently unavailable; "
        "no hazard factor consumes the result, so it is off by default.",
    )

    @model_validator(mode="after")
    def _one_selection(self) -> "AnalysisRequest":
        has_point = self.lat is not None or self.lon is not None
        if self.geometry is not None and has_point:
            raise ValueError("Send either lat/lon or geometry, not both.")
        if self.geometry is None and not has_point:
            raise ValueError("Send either lat/lon or a GeoJSON geometry.")
        if has_point and (self.lat is None or self.lon is None):
            raise ValueError("Both lat and lon are required for a point selection.")
        if self.geometry is not None and self.radius_m is not None:
            raise ValueError("radius_m applies to point selections only; remove it when sending geometry.")
        return self


@router.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    """What this instance can actually serve right now, without touching any provider."""
    store = get_spatial_store()
    spatial = store.summary()
    try:
        from ...core.database import get_engine

        with get_engine().connect() as connection:
            database = connection.exec_driver_sql("select 1").scalar()
            dialect = connection.dialect.name
        database_ok = True
    except Exception as exc:
        database, dialect, database_ok = f"unavailable: {type(exc).__name__}", None, False
    return {
        "app": _SETTINGS.app_name,
        "version": _SETTINGS.app_version,
        "environment": _SETTINGS.environment,
        "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION, "ml_models_used": []},
        "database": {"ok": database_ok, "dialect": dialect, "probe": database},
        "spatial": {
            "backend": spatial["backend"],
            "layers": {row["table"]: row["features"] for row in spatial["layers"]},
            "extents": {row["table"]: row["extent"] for row in spatial["layers"]},
            "indexed": {row["table"]: row.get("indexed") for row in spatial["layers"]},
        },
        "hazards_assessed": list(HAZARDS),
        "caches": {
            "enabled": _SETTINGS.cache_enabled,
            "namespaces": cache_stats(),
            "note": "Per-process TTL/LRU caches for location lookups only. Hazard evidence is "
            "never cached; provider responses stay in the disk cache under provider_cache_dir.",
        },
        "note": "Provider availability is reported by /providers; this endpoint deliberately makes no "
        "outbound calls so that a health check cannot be mistaken for a data-source check.",
    }


@router.get("/providers", tags=["system"])
def providers(
    refresh: bool = Query(default=False, description="Probe every provider now (Overpass alone can take minutes)."),
) -> dict[str, Any]:
    """Last recorded provider states, or a live sweep when ``refresh`` is set."""
    if refresh:
        rows = []
        for provider in all_providers():
            try:
                status = provider.healthcheck()
                row = {
                    "provider": status.provider,
                    "state": status.state,
                    "requires_auth": status.requires_auth,
                    "endpoint": status.endpoint,
                    "http_status": status.http_status,
                    "latency_ms": round(status.latency_ms) if status.latency_ms else None,
                    "detail": status.detail,
                }
            except Exception as exc:  # a probe must never crash the sweep
                row = {"provider": provider.key, "state": "unavailable", "detail": f"probe crashed: {type(exc).__name__}: {exc}"}
            rows.append(row)
        try:
            _SNAPSHOT.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return {"observed_at": datetime.now(timezone.utc).isoformat(), "mode": "live sweep", "providers": rows}

    if not _SNAPSHOT.exists():
        return {
            "observed_at": None,
            "mode": "none recorded",
            "providers": [],
            "note": "No probe has been stored. Pass refresh=true to probe now, or run "
            "python scripts/check_providers.py.",
        }
    modified = datetime.fromtimestamp(_SNAPSHOT.stat().st_mtime, tz=timezone.utc)
    return {
        "observed_at": modified.isoformat(),
        "mode": "snapshot",
        "providers": json.loads(_SNAPSHOT.read_text(encoding="utf-8")),
        "note": f"Recorded {modified.isoformat()} by a probe from this machine, not measured at request time.",
    }


@router.get("/hazards", tags=["meta"])
def hazards() -> dict[str, Any]:
    """The hazard set, how bands are cut, and who the advice is attributed to."""
    return {
        "hazards": list(HAZARDS),
        "band_thresholds": BAND_THRESHOLDS,
        "band_labels": BAND_LABELS,
        "band_order": BAND_ORDER,
        "authorities": AUTHORITIES,
        "data_gaps": [
            "BIS seismic zoning map (earthquake scores use epicentre climatology instead)",
            "River and floodplain polygons (flood scores use rainfall plus local terrain depression)",
            "Storm-surge hydrodynamics (coastal flood is a proximity plus elevation proxy)",
            "Cyclone track climatology (cyclone score uses forecast wind plus coastal proximity)",
            "NDVI vegetation stress and groundwater/reservoir storage (drought uses rainfall and soil moisture)",
            "Land cover / fuel load (wildfire uses fire detections plus reanalysis dryness)",
            "Lithology and land cover (landslide uses slope, relief and rainfall only)",
        ],
    }


@router.post("/analysis", tags=["analysis"])
def analysis_create(body: AnalysisRequest) -> dict[str, Any]:
    """Assess either a point/radius or a drawn polygon in one call.

    Point selections keep the measured 0.2-4 s synchronous path. A Polygon/MultiPolygon
    is sampled at interior points and scored per hazard at the worst sample; both shapes
    return the same envelope (hazards, band, provenance, live feeds, map overlays), with
    ``selection.mode`` saying which was run. Use the job queue when lifelines are requested.
    """
    if body.geometry is not None:
        return run_area_analysis(
            body.geometry,
            hazards=body.hazards,
            requested_by=body.requested_by,
            include_infrastructure=body.include_infrastructure,
        )
    assert body.lat is not None and body.lon is not None  # guaranteed by AnalysisRequest
    return run_point_analysis(
        body.lat,
        body.lon,
        radius_m=parse_radius_m(body.radius_m) if body.radius_m is not None else None,
        hazards=body.hazards,
        requested_by=body.requested_by,
        include_infrastructure=body.include_infrastructure,
    )


@router.post("/analysis/point", tags=["analysis"])
def analysis_point(body: PointAnalysisRequest) -> dict[str, Any]:
    """Assess every hazard at one coordinate and store the auditable result."""
    return run_point_analysis(
        body.lat,
        body.lon,
        radius_m=parse_radius_m(body.radius_m) if body.radius_m is not None else None,
        hazards=body.hazards,
        requested_by=body.requested_by,
        include_infrastructure=body.include_infrastructure,
    )


@router.post("/analysis/jobs", status_code=202, tags=["analysis"])
def analysis_job_create(body: AnalysisRequest) -> dict[str, Any]:
    """Queue the same analysis on a worker thread and return a pollable job record.

    Accepts the same point or polygon payload as ``POST /analysis``. The synchronous
    endpoint stays the default for a warm point; this one exists for the lifeline path,
    measured at 504-624 s, which no gateway will hold open.
    """
    return submit_analysis_job(
        lat=body.lat,
        lon=body.lon,
        radius_m=body.radius_m,
        hazards=body.hazards,
        requested_by=body.requested_by,
        include_infrastructure=body.include_infrastructure,
        polygon=body.geometry,
    )


@router.get("/analysis/jobs", tags=["analysis"])
def analysis_jobs(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    jobs = recent_jobs(limit=limit)
    return {"count": len(jobs), "jobs": jobs}


# Registered before /analysis/{analysis_uuid} so "jobs" is not read as an analysis id.
@router.get("/analysis/jobs/{job_uuid}", tags=["analysis"])
def analysis_job_read(job_uuid: str) -> dict[str, Any]:
    """Current job state. ``result_ref`` is set only after the analysis is really stored."""
    return get_job(job_uuid)


@router.get("/analysis/{analysis_uuid}", tags=["analysis"])
def analysis_read(analysis_uuid: str) -> dict[str, Any]:
    """Read back a stored analysis exactly as it was produced, provenance included."""
    return get_analysis(analysis_uuid)


@router.get("/analyses", tags=["analysis"])
def analyses(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    return {"count": limit, "analyses": recent_analyses(limit=limit)}


@router.get("/locations/search", tags=["locations"])
def locations_search(
    q: str = Query(min_length=2, description="Place name, e.g. 'Nagpur' or '600028'"),
    limit: int = Query(default=8, ge=1, le=50),
    allow_remote: bool = Query(default=True, description="Consult Nominatim as well as the loaded boundaries"),
) -> dict[str, Any]:
    return search_places(q, limit=limit, allow_remote=allow_remote)


@router.get("/locations/reverse", tags=["locations"])
def locations_reverse(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    allow_remote: bool = True,
) -> dict[str, Any]:
    return reverse_location(lon, lat, allow_remote=allow_remote)


@router.get("/locations/point", tags=["locations"])
def locations_point(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_m: str | float | None = None,
    allow_remote: bool = True,
) -> dict[str, Any]:
    """Admin context, terrain block and boundary confidence for one point (no hazard scoring)."""
    return resolve_point(lat, lon, allow_remote=allow_remote, radius_m=parse_radius_m(radius_m) if radius_m is not None else None)


@router.get("/layers", tags=["meta"])
def layers() -> dict[str, Any]:
    """Spatial layers loaded in this instance and their extents."""
    return get_spatial_store().summary()


@router.get("/layers/{table}/geojson", tags=["meta"])
def layers_geojson(
    table: str,
    bbox: str = Query(description="min_lon,min_lat,max_lon,max_lat in EPSG:4326 degrees."),
    limit: int = Query(default=3000, ge=1, le=20_000),
    clip_above_area_deg2: float | None = Query(
        default=None,
        gt=0,
        description="Draw features larger than this many square degrees clipped to the window. "
        "Used to keep a national outline small at district zoom; it changes the geometry sent "
        "to the browser and is reported back in 'clipping'.",
    ),
) -> dict[str, Any]:
    """Vector payload for the web map: one loaded layer, intersected with a bounding box.

    The geometry returned is what is stored (clipped to the window only when
    ``clip_above_area_deg2`` says so). If the window holds more features than ``limit``,
    the response says it is truncated rather than quietly looking like the whole area.
    """
    parts = [piece.strip() for piece in bbox.split(",")]
    if len(parts) != 4:
        raise ValidationError(
            "bbox must have exactly four values: min_lon,min_lat,max_lon,max_lat.",
            details={"bbox": bbox, "got": len(parts)},
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(piece) for piece in parts)
    except (ValueError, TypeError) as exc:
        raise ValidationError(
            "bbox must be four comma-separated numbers: min_lon,min_lat,max_lon,max_lat.",
            details={"bbox": bbox},
        ) from exc
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise ValidationError("bbox longitude must be within -180..180.", details={"bbox": bbox})
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise ValidationError("bbox latitude must be within -90..90.", details={"bbox": bbox})
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValidationError("bbox must be ordered min_lon,min_lat,max_lon,max_lat.", details={"bbox": bbox})

    name = safe_table_name(table)
    store = get_spatial_store()
    # One extra row is requested so that a full page cannot be mistaken for the whole window.
    fetched = store.features_in_bbox(name, (min_lon, min_lat, max_lon, max_lat), limit + 1, clip_above_area_deg2)
    truncated = len(fetched) > limit
    features = [
        {"type": "Feature", "properties": {"fid": row["fid"], **row["attributes"]}, "geometry": row["geometry"]}
        for row in fetched[:limit]
    ]
    return {
        "type": "FeatureCollection",
        "features": features,
        "layer": name,
        "crs": "EPSG:4326",
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "returned": len(features),
        "truncated": truncated,
        "limit": limit,
        "clipping": {
            "requested": clip_above_area_deg2,
            "applied": clip_above_area_deg2 is not None,
            "note": (
                "Features larger than the threshold are the intersection of the stored boundary "
                "with the requested window; their outer edges are the window, not the boundary."
                if clip_above_area_deg2 is not None
                else "No clipping applied; geometries are the stored boundaries as loaded."
            ),
        },
        "note": (
            "Boundaries are the loaded administrative layer; this endpoint serves stored geometry only "
            "and runs no hazard analysis. See /layers for what this instance has and where it came from."
        ),
    }
