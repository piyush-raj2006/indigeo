from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

TMP_ROOT = Path(tempfile.mkdtemp(prefix="indigeo-tests-"))

os.environ["DATABASE_URL"] = f"sqlite:///{(TMP_ROOT / 'rel.db').as_posix()}"
os.environ["SPATIAL_DUCKDB_PATH"] = (TMP_ROOT / "spatial.duckdb").as_posix()
os.environ["DATA_DIR"] = str(TMP_ROOT / "data")
os.environ["PROVIDER_CACHE_DIR"] = str(TMP_ROOT / "cache")
os.environ["DEM_DIR"] = str(TMP_ROOT / "dem")
os.environ["OSM_EXTRACT_DIR"] = str(TMP_ROOT / "osm_extracts")
os.environ["EARTHDATA_TOKEN"] = ""
# Live Indian feeds have no default endpoint; clearing them keeps every test run
# deterministic even when the developer's own .env configures a real feed.
for _feed in ("IMD_ALERTS_URL", "CWC_GAUGES_URL", "BHUVAN_HAZARDS_URL", "CWC_API_KEY", "BHUVAN_API_KEY"):
    os.environ[_feed] = ""
os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
# Tests swap spatial stores per case, so a process-wide location cache would leak between
# them. The cache itself is covered directly in test_core_cache.py.
os.environ["CACHE_ENABLED"] = "false"

for _sub in ("uploads", "layers", "dem", "dem/local", "osm_extracts", "http_cache"):
    (TMP_ROOT / "data" / _sub).mkdir(parents=True, exist_ok=True)
(TMP_ROOT / "osm_extracts").mkdir(parents=True, exist_ok=True)
(TMP_ROOT / "artifacts").mkdir(parents=True, exist_ok=True)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _configure_app():
    from app.core.config import get_settings

    settings = get_settings()
    settings.artifacts_dir = TMP_ROOT / "artifacts"
    yield settings


_store_counter = iter(range(1, 10_000))


@pytest.fixture()
def store():
    from app.gis.spatial import DuckDBSpatialStore

    engine = DuckDBSpatialStore(path=TMP_ROOT / f"spatial-{next(_store_counter)}.duckdb")
    engine.bootstrap()
    yield engine
    engine.close()


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    return TMP_ROOT


from _geo_fixtures import square_wkb


@pytest.fixture()
def geo_store(tmp_path, monkeypatch):
    """A throwaway spatial store with two districts (one overlapping) and an outline.

    Terrain sampling reaches AWS tiles, so it is stubbed out: this fixture exists so
    boundary and hazard-pipeline tests can run with no network at all.
    """
    from app.core.database import init_schema
    from app.gis.spatial import DuckDBSpatialStore, reset_spatial_store
    from app.services import locations

    init_schema()
    monkeypatch.setattr(
        locations,
        "terrain_at",
        lambda lon, lat, radius_m: {"available": False, "summary": None, "reason": "stubbed in tests", "zoom": None},
    )
    engine = DuckDBSpatialStore(path=tmp_path / "geo_store.duckdb")
    engine.bootstrap()
    engine.replace_layer(
        "admin_districts",
        [square_wkb(78.0, 21.0), square_wkb(78.5, 21.2), square_wkb(92.5, 11.5, 0.2)],
        [
            {"district": "Testpur", "district_code": "0101", "state_name": "Test Pradesh", "state_code": "01"},
            {"district": "Overlap City", "district_code": "0102", "state_name": "Test Pradesh", "state_code": "01"},
            {
                "district": "Andaman Isle",
                "district_code": "0201",
                "state_name": "Andaman and Nicobar Islands",
                "state_code": "02",
            },
        ],
    )
    engine.replace_layer(
        "admin_states",
        [square_wkb(78.0, 21.0, 2.0), square_wkb(92.5, 11.5, 0.3)],
        [{"state": "Test Pradesh", "state_code": "01"}, {"state": "Andaman and Nicobar Islands", "state_code": "02"}],
    )
    engine.replace_layer("india_outline", [square_wkb(68.0, 6.0, 29.4)], [{"name": "India", "iso": "IN"}])

    previous = reset_spatial_store(engine)
    yield engine
    reset_spatial_store(previous)
    engine.close()
