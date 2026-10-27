from __future__ import annotations

"""Background analysis jobs.

The synchronous endpoint is right for a cached point (measured 0.2-4 s) and wrong for the
lifeline path, which walks Overpass mirrors and was measured at 504-624 s. Holding an HTTP
connection open for that long is what produces gateway timeouts, so this module runs the
same service call on a worker thread and the caller polls.

Honesty rules, same as everywhere else in the pipeline:

* a job is not a result - ``result`` stays empty until the analysis really finished, and a
  failure carries the exception text rather than an invented payload;
* the analysis itself is persisted by ``run_point_analysis`` exactly as on the synchronous
  path, so the job only points at it (``result_ref``);
* jobs live in the relational store, and a restart marks anything still queued or running as
  ``interrupted`` instead of leaving a poller waiting forever.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from ..core.config import get_settings
from ..core.database import session_scope
from ..core.errors import NotFoundError, ValidationError
from ..core.logging import get_logger
from ..gis.coords import parse_radius_m
from ..models.catalog import utcnow
from ..models.jobs import Job
from .analysis import run_area_analysis, run_point_analysis
from .hazard import HAZARDS

log = get_logger("services.jobs")

# Coarse, hand-set weights for the stages the pipeline actually reports. They are labels for
# the operator, not a measured time estimate, so progress never claims to know a duration.
_STAGE_PROGRESS: dict[str, float] = {
    "admin context": 0.05,
    "terrain": 0.15,
    "coastline": 0.25,
    "weather": 0.40,
    "seismicity": 0.55,
    "lifelines": 0.65,
    "fires": 0.85,
    "scoring": 0.92,
    "recommendations": 0.96,
}

_EXECUTOR: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        workers = max(1, int(get_settings().analysis_workers))
        _EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analysis-job")
        log.info("analysis job executor started with %s worker(s)", workers)
    return _EXECUTOR


def _serialise(job: Job) -> dict[str, Any]:
    elapsed_ms = None
    if job.started_at is not None:
        finished = job.finished_at or utcnow()
        started = job.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=finished.tzinfo)
        elapsed_ms = round((finished - started).total_seconds() * 1000.0, 1)
    status = job.status
    note = {
        "queued": "Waiting for a worker slot. Nothing has been measured yet.",
        "running": "Running. The stage below is the last one that started, not a time estimate.",
        "completed": "Finished. Read the stored analysis at result_ref for the full provenance.",
        "failed": "The run failed; the stored analysis row carries status=failed and this error.",
        "interrupted": "The API restarted while this job was queued or running. Submit it again.",
    }.get(status, "")
    return {
        "job_uuid": job.job_uuid,
        "kind": job.kind,
        "status": status,
        "progress": round(float(job.progress or 0.0), 3),
        "message": job.message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "elapsed_ms": elapsed_ms,
        "request": job.payload or {},
        "result": job.result or {},
        "result_ref": job.result_ref,
        "error": job.error,
        "poll": f"{get_settings().api_prefix}/analysis/jobs/{job.job_uuid}",
        "note": note,
    }


def _read(job_uuid: str) -> Job:
    with session_scope() as session:
        job = session.execute(select(Job).where(Job.job_uuid == job_uuid)).scalar_one_or_none()
        if job is None:
            raise NotFoundError(
                f"No analysis job with id '{job_uuid}'.",
                details={"job_uuid": job_uuid},
            )
        return job


def _update(job_uuid: str, **fields: Any) -> None:
    try:
        with session_scope() as session:
            job = session.execute(select(Job).where(Job.job_uuid == job_uuid)).scalar_one_or_none()
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
    except Exception as exc:  # pragma: no cover - a lost progress write must not kill the run
        log.warning("could not update job %s: %s", job_uuid, exc)


def submit_analysis_job(
    *,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: str | float | None,
    hazards: list[str] | None,
    requested_by: str | None,
    include_infrastructure: bool,
    polygon: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a point or polygon analysis and return the job record (202 semantics)."""
    if hazards:
        unknown = [h for h in hazards if h not in HAZARDS]
        if unknown:
            raise ValidationError(
                f"Unknown hazard(s): {', '.join(unknown)}.",
                details={"unknown_hazards": unknown, "known_hazards": list(HAZARDS)},
            )
    if polygon is None and (lat is None or lon is None):
        raise ValidationError(
            "A job needs either lat and lon or a GeoJSON polygon geometry.",
            details={"lat": lat, "lon": lon, "polygon": polygon},
        )
    radius = parse_radius_m(radius_m) if radius_m is not None else None
    job_uuid = str(uuid4())
    payload = {
        "mode": "polygon" if polygon is not None else "point",
        "lat": lat,
        "lon": lon,
        "radius_m": radius,
        "polygon": polygon,
        "hazards": list(hazards) if hazards else None,
        "requested_by": requested_by,
        "include_infrastructure": include_infrastructure,
    }
    with session_scope() as session:
        session.add(
            Job(
                job_uuid=job_uuid,
                kind="area_analysis" if polygon is not None else "point_analysis",
                status="queued",
                progress=0.0,
                message="queued",
                payload=payload,
                result={},
            )
        )
    _executor().submit(_run, job_uuid)
    return get_job(job_uuid)


def _run(job_uuid: str) -> None:
    try:
        with session_scope() as session:
            job = session.execute(select(Job).where(Job.job_uuid == job_uuid)).scalar_one_or_none()
            payload = dict(job.payload or {}) if job is not None else None
    except Exception as exc:  # pragma: no cover - the row was deleted between submit and run
        log.warning("job %s could not be loaded: %s", job_uuid, exc)
        return
    if payload is None:
        return

    _update(job_uuid, status="running", started_at=utcnow(), message="starting", progress=0.02)

    def progress(stage: str) -> None:
        _update(job_uuid, message=stage, progress=_STAGE_PROGRESS.get(stage, 0.5))

    polygon = payload.get("polygon")
    try:
        if polygon is not None:
            analysis = run_area_analysis(
                polygon,
                hazards=payload.get("hazards"),
                requested_by=payload.get("requested_by"),
                include_infrastructure=bool(payload.get("include_infrastructure")),
                on_progress=progress,
            )
        else:
            analysis = run_point_analysis(
                payload["lat"],
                payload["lon"],
                radius_m=payload.get("radius_m"),
                hazards=payload.get("hazards"),
                requested_by=payload.get("requested_by"),
                include_infrastructure=bool(payload.get("include_infrastructure")),
                on_progress=progress,
            )
    except Exception as exc:
        log.exception("analysis job %s failed", job_uuid)
        _update(
            job_uuid,
            status="failed",
            finished_at=utcnow(),
            progress=1.0,
            message="failed",
            error=f"{type(exc).__name__}: {exc}",
            result={},
        )
        return

    include_infrastructure = bool(payload.get("include_infrastructure"))
    infrastructure = analysis.get("infrastructure_summary") or {}
    _update(
        job_uuid,
        status="completed",
        finished_at=utcnow(),
        progress=1.0,
        message="completed",
        result_ref=analysis["analysis_uuid"],
        result={
            "analysis_uuid": analysis["analysis_uuid"],
            "mode": "polygon" if polygon is not None else "point",
            "overall_band": analysis["overall"]["band"],
            "hazards_assessed": len(analysis["hazards"]),
            "recommendations": len(analysis["recommendations"]),
            # Only a polygon run has samples; null says "not applicable", not "zero points".
            "area_km2": (analysis.get("area") or {}).get("area_km2") if polygon is not None else None,
            "sample_count": len((analysis.get("area") or {}).get("samples") or []) if polygon is not None else None,
            "duration_ms": analysis.get("duration_ms"),
            "include_infrastructure": include_infrastructure,
            # null here would read as "queried but unknown"; the run never asked.
            "infrastructure_status": (
                analysis["data_completeness"]["sources"].get("osm_overpass")
                if include_infrastructure
                else "not_requested"
            ),
            "infrastructure_layers": infrastructure.get("layers", {}) if include_infrastructure else {},
        },
    )


def get_job(job_uuid: str) -> dict[str, Any]:
    return _serialise(_read(job_uuid))


def recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = (
            session.execute(select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit))
            .scalars()
            .all()
        )
        return [_serialise(row) for row in rows]


def reconcile_interrupted_jobs() -> int:
    """Mark jobs left queued/running by a previous process as interrupted.

    A thread cannot outlive the process, so those jobs will never finish; leaving them
    ``running`` would make every poller wait forever and lie about the state.
    """
    with session_scope() as session:
        rows = session.execute(select(Job).where(Job.status.in_(("queued", "running")))).scalars().all()
        for job in rows:
            job.status = "interrupted"
            job.finished_at = utcnow()
            job.message = "interrupted"
            job.progress = 1.0
            job.error = "The API restarted while this job was queued or running; submit it again."
        return len(rows)
