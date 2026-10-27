from __future__ import annotations

"""Analysis jobs: submission, polling, failure and restart reconciliation.

The worker is stubbed so nothing here touches a provider or the spatial store; what is
under test is the job lifecycle and the honesty of the record it returns.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.core.database import session_scope
from app.main import app
from app.models.jobs import Job
from app.services import jobs

client = TestClient(app)


@pytest.fixture(autouse=True)
def _schema():
    from app.core.database import init_schema

    init_schema()


def _analysis_payload(uuid: str = "job-analysis-1") -> dict:
    return {
        "analysis_uuid": uuid,
        "overall": {"band": "moderate"},
        "hazards": [{"hazard": "flood", "band": "moderate"}],
        "recommendations": [{"hazard": "flood"}, {"hazard": "heat"}],
        "duration_ms": 12.5,
        "data_completeness": {"sources": {"osm_overpass": "available"}},
    }


def _poll(job_uuid: str, *, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/analysis/jobs/{job_uuid}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"completed", "failed", "interrupted"}:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_uuid} did not settle: {body}")


def test_a_submitted_job_completes_and_points_at_the_stored_analysis(monkeypatch):
    def fake_run(lat, lon, **kwargs):
        if kwargs.get("on_progress"):
            kwargs["on_progress"]("terrain")
        return _analysis_payload()

    monkeypatch.setattr(jobs, "run_point_analysis", fake_run)

    response = client.post(
        "/api/v1/analysis/jobs",
        json={"lat": 21.1458, "lon": 79.0931, "radius_m": "1 km", "include_infrastructure": True},
    )
    assert response.status_code == 202
    submitted = response.json()
    assert submitted["status"] in {"queued", "running", "completed"}
    if submitted["status"] != "completed":
        assert submitted["result"] == {}, "an unfinished job must not carry a result"

    body = _poll(submitted["job_uuid"])
    assert body["status"] == "completed"
    assert body["progress"] == 1.0
    assert body["result_ref"] == "job-analysis-1"
    assert body["result"]["overall_band"] == "moderate"
    assert body["result"]["include_infrastructure"] is True
    assert body["request"]["radius_m"] == 1000.0
    assert body["elapsed_ms"] is not None


def test_a_failed_job_records_the_error_instead_of_a_result(monkeypatch):
    def boom(lat, lon, **kwargs):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(jobs, "run_point_analysis", boom)

    submitted = client.post("/api/v1/analysis/jobs", json={"lat": 21.1458, "lon": 79.0931}).json()
    body = _poll(submitted["job_uuid"])

    assert body["status"] == "failed"
    assert body["result"] == {}
    assert body["result_ref"] is None
    assert "provider unreachable" in body["error"]


def test_an_unknown_job_is_a_404_with_the_standard_envelope():
    response = client.get("/api/v1/analysis/jobs/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["details"]["job_uuid"] == "does-not-exist"


def test_an_unknown_hazard_is_rejected_before_anything_is_queued():
    response = client.post(
        "/api/v1/analysis/jobs", json={"lat": 21.1458, "lon": 79.0931, "hazards": ["flood", "dragons"]}
    )
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert details["unknown_hazards"] == ["dragons"]


DRAWN_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[78.10, 21.10], [78.40, 21.10], [78.40, 21.40], [78.10, 21.40], [78.10, 21.10]]],
}


def test_a_polygon_job_runs_the_area_engine_and_reports_the_extent(monkeypatch):
    seen: list[dict] = []

    def fake_area(polygon, **kwargs):
        seen.append({"polygon": polygon, **kwargs})
        if kwargs.get("on_progress"):
            kwargs["on_progress"]("samples")
        return {
            "analysis_uuid": "job-area-1",
            "overall": {"band": "high"},
            "hazards": [{"hazard": "flood", "band": "high"}],
            "recommendations": [{"hazard": "flood"}],
            "duration_ms": 21.0,
            "area": {"area_km2": 812.5, "samples": [{"lat": 21.2, "lon": 78.2}, {"lat": 21.3, "lon": 78.3}]},
            "data_completeness": {"sources": {"osm_overpass": "available"}},
        }

    monkeypatch.setattr(jobs, "run_area_analysis", fake_area)
    submitted = client.post(
        "/api/v1/analysis/jobs", json={"geometry": DRAWN_SQUARE, "hazards": ["flood"]}
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["kind"] == "area_analysis"
    assert body["request"]["mode"] == "polygon"
    assert body["request"]["polygon"]["type"] == "Polygon"
    assert body["request"]["radius_m"] is None

    settled = _poll(body["job_uuid"])
    assert settled["status"] == "completed"
    assert settled["result"]["mode"] == "polygon"
    assert settled["result"]["area_km2"] == 812.5
    assert settled["result"]["sample_count"] == 2
    assert settled["result_ref"] == "job-area-1"
    assert seen and seen[0]["polygon"]["type"] == "Polygon"
    assert "lat" not in seen[0], "a polygon job must not reach the point engine"


def test_a_job_without_a_selection_is_rejected_before_it_is_queued():
    response = client.post("/api/v1/analysis/jobs", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["error"]["details"]["errors"]


def test_the_job_list_is_not_read_as_an_analysis_uuid():
    response = client.get("/api/v1/analysis/jobs")
    assert response.status_code == 200
    body = response.json()
    assert "jobs" in body and "count" in body


def test_reconciliation_marks_orphaned_jobs_interrupted():
    orphan = "orphan-" + str(time.time_ns())
    with session_scope() as session:
        session.add(
            Job(
                job_uuid=orphan,
                kind="point_analysis",
                status="running",
                progress=0.65,
                message="lifelines",
                payload={"lat": 21.1458, "lon": 79.0931},
                result={},
            )
        )

    marked = jobs.reconcile_interrupted_jobs()
    assert marked >= 1

    body = client.get(f"/api/v1/analysis/jobs/{orphan}").json()
    assert body["status"] == "interrupted"
    assert "restarted" in body["error"]
    assert body["result"] == {}
