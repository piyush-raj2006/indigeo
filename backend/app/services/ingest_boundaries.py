from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import shapely
from shapely.geometry import MultiPolygon, Point, shape

from ..core.config import PROJECT_ROOT, get_settings
from ..core.database import session_scope
from ..core.logging import get_logger
from ..gis.spatial import get_spatial_store
from ..models.catalog import AdminUnit, Dataset, IngestRun

log = get_logger("services.ingest")

DISTRICT_URL = "https://raw.githubusercontent.com/udit-001/india-maps-data/refs/heads/main/geojson/india.geojson"
COUNTRY_URL = "https://raw.githubusercontent.com/yashveeeeeeer/india-geodata/main/data/administrative/country/india-composite.geojson"
ATTRIBUTION = (
    "Administrative boundaries derived from OpenStreetMap data (c) OpenStreetMap contributors, ODbL 1.0 - "
    "https://osm.org/copyright. District codes follow the 2011 Census scheme with revisions up to 2025; "
    "boundary positions in disputed and newly reorganised areas are approximate."
)

UT_NAMES = {
    "andaman and nicobar islands",
    "chandigarh",
    "dadra and nagar haveli and daman and diu",
    "delhi",
    "jammu and kashmir",
    "ladakh",
    "lakshadweep",
    "pondicherry",
    "puducherry",
}

STOPWORDS = {"the", "of", "and"}


def clean_value(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return None if text.lower() in {"", "nan", "none", "null"} else text


def normalise_name(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", text).lower()
    return re.sub(r"\s+", " ", text).strip()


def _state_kind(state_name: str) -> str:
    return "ut" if normalise_name(state_name) in {normalise_name(n) for n in UT_NAMES} else "state"


def download_boundaries(force: bool = False) -> dict[str, Path]:
    """Fetch the two verified boundary files, caching them under data/raw."""
    import httpx

    settings = get_settings()
    targets = {
        "districts": settings.data_dir / "raw" / "india_districts.geojson",
        "country": settings.data_dir / "raw" / "india_country.geojson",
    }
    urls = {"districts": DISTRICT_URL, "country": COUNTRY_URL}
    results: dict[str, Path] = {}
    for key, path in targets.items():
        if path.exists() and path.stat().st_size > 100_000 and not force:
            results[key] = path
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            response = client.get(urls[key], headers={"User-Agent": settings.nominatim_user_agent})
            response.raise_for_status()
            path.write_bytes(response.content)
        log.info("Downloaded boundary file", extra={"fields": {"key": key, "bytes": len(response.content)}})
        results[key] = path
    return results


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ingest_districts(path: Path, country_path: Path | None = None) -> dict[str, Any]:
    frame = gpd.read_file(path)
    frame = frame[frame.geometry.notna()]
    if frame.crs is None or not frame.crs.equals("EPSG:4326"):
        frame = frame.set_crs("EPSG:4326", allow_override=True) if frame.crs is None else frame.to_crs("EPSG:4326")

    invalid = ~frame.geometry.is_valid
    if invalid.any():
        frame.loc[invalid, "geometry"] = frame.loc[invalid, "geometry"].make_valid()
        log.warning("Repaired invalid district geometries", extra={"fields": {"count": int(invalid.sum())}})

    frame["_state_clean"] = frame["st_nm"].map(lambda value: clean_value(value) or "Unknown state")

    attrs: list[dict[str, Any]] = []
    unnamed = 0
    for _, row in frame.iterrows():
        district_name = clean_value(row["district"])
        state_name = clean_value(row["st_nm"]) or "Unknown state"
        if district_name is None:
            unnamed += 1
        attrs.append(
            {
                "district": district_name or "Unnamed area",
                "district_official": district_name,
                "district_code": clean_value(row.get("dt_code")),
                "state_name": state_name,
                "state_code": clean_value(row.get("st_code")),
                "vintage": clean_value(row.get("year")),
                "district_norm": normalise_name(district_name or "unnamed area"),
                "state_norm": normalise_name(state_name),
                "name_missing": district_name is None,
            }
        )
    if unnamed:
        log.warning(
            "Upstream boundary file has unnamed districts",
            extra={"fields": {"count": unnamed, "handling": "labelled 'Unnamed area' and flagged"}},
        )
    wkbs = [shapely.to_wkb(geom) for geom in frame.geometry]
    store = get_spatial_store()
    stats = store.replace_layer("admin_districts", wkbs, attrs)

    state_frame = frame.dissolve(by="_state_clean", as_index=False)
    state_attrs = [
        {
            "state": str(row["_state_clean"]).strip(),
            "state_code": clean_value(row.get("st_code")),
            "kind": _state_kind(row["_state_clean"]),
            "district_count": int((frame["_state_clean"] == row["_state_clean"]).sum()),
            "state_norm": normalise_name(row["_state_clean"]),
        }
        for _, row in state_frame.iterrows()
    ]
    state_wkbs = [shapely.to_wkb(geom) for geom in state_frame.geometry]
    state_stats = store.replace_layer("admin_states", state_wkbs, state_attrs)

    country_frame = gpd.read_file(country_path or (PROJECT_ROOT / "data" / "raw" / "india_country.geojson"))
    country_attrs = [{"name": "India", "iso": "IN", "kind": "country"}]
    country_wkbs = [shapely.to_wkb(g) for g in country_frame.geometry.head(1)]
    country_stats = store.replace_layer("india_outline", country_wkbs, country_attrs)

    registered_districts = _register_admin_units(state_frame, frame)
    admin_counts = {
        "states": int(sum(1 for _, r in state_frame.iterrows() if _state_kind(r["_state_clean"]) == "state")),
        "union_territories": int(sum(1 for _, r in state_frame.iterrows() if _state_kind(r["_state_clean"]) == "ut")),
        "districts": int(len(frame)),
        "districts_named_in_register": registered_districts,
        "unnamed_district_polygons": unnamed,
    }
    _register_datasets(path, stats, state_stats, country_stats, admin_counts)
    return {
        "districts": stats["features"],
        "states_and_uts": state_stats["features"],
        "outline_features": country_stats["features"],
        "district_extent": stats["extent"],
        "rtree": {"districts": stats.get("rtree_index"), "states": state_stats.get("rtree_index")},
        "admin_counts": admin_counts,
        "digest": _file_digest(path),
    }


def _register_admin_units(state_frame: gpd.GeoDataFrame, district_frame: gpd.GeoDataFrame) -> int:
    now = datetime.now(timezone.utc)
    registered = 0
    with session_scope() as session:
        session.query(AdminUnit).delete()
        used_state_codes: set[str] = set()
        for _, row in state_frame.iterrows():
            centroid = row.geometry.representative_point()
            state_name = clean_value(row["_state_clean"]) or "Unknown state"
            st_code = clean_value(row.get("st_code")) or normalise_name(state_name)
            while st_code in used_state_codes:
                st_code = f"{st_code}+"
            used_state_codes.add(st_code)
            session.add(
                AdminUnit(
                    kind=_state_kind(state_name),
                    code=st_code,
                    name=state_name,
                    name_norm=normalise_name(state_name),
                    lon=round(centroid.x, 5),
                    lat=round(centroid.y, 5),
                    source="osm_via_udit_india_maps",
                    provenance={"url": DISTRICT_URL, "retrieved_at": now.isoformat()},
                    extra={"district_count": int((district_frame["_state_clean"] == row["_state_clean"]).sum())},
                )
            )
        used_codes: set[str] = set()
        for _, row in district_frame.iterrows():
            district_name = clean_value(row["district"])
            if not district_name:
                continue
            registered += 1
            centroid = row.geometry.representative_point()
            st_code = clean_value(row.get("st_code")) or "NA"
            raw_dt = clean_value(row.get("dt_code"))
            base = (
                f"{st_code}-{raw_dt}"
                if raw_dt and raw_dt != "0"
                else f"{st_code}-{normalise_name(district_name).replace(' ', '_') or 'unknown'}"
            )
            code, suffix = base, 1
            while code in used_codes:
                suffix += 1
                code = f"{base}#{suffix}"
            used_codes.add(code)
            session.add(
                AdminUnit(
                    kind="district",
                    code=code,
                    name=district_name,
                    name_norm=normalise_name(district_name),
                    parent_code=st_code,
                    state_code=st_code,
                    district_code=raw_dt if raw_dt and raw_dt != "0" else None,
                    lon=round(centroid.x, 5),
                    lat=round(centroid.y, 5),
                    source="osm_via_udit_india_maps",
                    provenance={"url": DISTRICT_URL, "vintage": clean_value(row.get("year")), "retrieved_at": now.isoformat()},
                    extra={"census_code_missing": raw_dt in {None, "", "0"}},
                )
            )
    return registered


def _register_datasets(path: Path, district_stats, state_stats, country_stats, admin_counts: dict[str, int]) -> None:
    now = datetime.now(timezone.utc)
    common = {
        "source": "OpenStreetMap (audit mirror)",
        "license": "ODbL 1.0",
        "attribution": ATTRIBUTION,
        "status": "ready",
        "origin": "provider",
        "temporal_kind": "static",
        "retrieved_at": now,
        "srid": 4326,
    }
    with session_scope() as session:
        rows = [
            Dataset(
                code="admin_districts",
                title="India district boundaries (all states and union territories)",
                category="boundary",
                geometry_type="Polygon|MultiPolygon",
                table_name="admin_districts",
                feature_count=district_stats["features"],
                resolution="vector boundary; positional accuracy inherited from OpenStreetMap",
                bbox_min_lon=district_stats["extent"][0],
                bbox_min_lat=district_stats["extent"][1],
                bbox_max_lon=district_stats["extent"][2],
                bbox_max_lat=district_stats["extent"][3],
                provenance={"upstream_url": DISTRICT_URL, "sha256_16": district_stats.get("digest"), "local_file": str(path)},
                coverage=admin_counts,
                **common,
            ),
            Dataset(
                code="admin_states",
                title="India state and union territory boundaries",
                category="boundary",
                geometry_type="MultiPolygon",
                table_name="admin_states",
                feature_count=state_stats["features"],
                resolution="vector boundary dissolved from district polygons",
                bbox_min_lon=state_stats["extent"][0],
                bbox_min_lat=state_stats["extent"][1],
                bbox_max_lon=state_stats["extent"][2],
                bbox_max_lat=state_stats["extent"][3],
                provenance={"derived_from": "admin_districts", "upstream_url": DISTRICT_URL},
                coverage=admin_counts,
                **common,
            ),
            Dataset(
                code="india_outline",
                title="National outline of India",
                category="boundary",
                geometry_type="MultiPolygon",
                table_name="india_outline",
                feature_count=country_stats["features"],
                resolution="vector boundary",
                bbox_min_lon=country_stats["extent"][0],
                bbox_min_lat=country_stats["extent"][1],
                bbox_max_lon=country_stats["extent"][2],
                bbox_max_lat=country_stats["extent"][3],
                provenance={"upstream_url": COUNTRY_URL, "license_upstream": "CC-BY-4.0"},
                **common,
            ),
        ]
        for row in rows:
            existing = session.query(Dataset).filter(Dataset.code == row.code).one_or_none()
            if existing:
                row.id = existing.id
                session.merge(row)
            else:
                session.add(row)
            session.flush()
            session.add(
                IngestRun(
                    dataset_id=row.id or 0,
                    status="completed",
                    finished_at=datetime.now(timezone.utc),
                    rows_written=row.feature_count,
                    message=f"Loaded layer {row.table_name}",
                    detail={"upstream_url": row.provenance.get("upstream_url"), "extent": row.provenance.get("local_file")},
                )
            )


def coverage_snapshot() -> dict[str, Any]:
    """Actual nationwide coverage from what is loaded, not an aspiration."""
    store = get_spatial_store()
    out: dict[str, Any] = {"spatial_backend": store.backend, "layers": {}}
    for table in store.list_layers():  # type: ignore[attr-defined]
        out["layers"][table] = {"features": store.count(table), "extent": store.extent(table)}  # type: ignore[attr-defined]
    with session_scope() as session:
        from sqlalchemy import func

        out["admin_units"] = dict(
            session.query(AdminUnit.kind, func.count(AdminUnit.id)).group_by(AdminUnit.kind).all()
        )
        out["datasets"] = [
            {
                "code": d.code,
                "status": d.status,
                "features": d.feature_count,
                "source": d.source,
                "license": d.license,
                "retrieved_at": d.retrieved_at.isoformat() if d.retrieved_at else None,
                "hazard": d.hazard,
                "category": d.category,
            }
            for d in session.query(Dataset).order_by(Dataset.category, Dataset.code).all()
        ]
    return out


def run(force_download: bool = False) -> dict[str, Any]:
    from ..core.database import init_schema

    init_schema()
    settings = get_settings()
    (settings.data_dir / "raw").mkdir(parents=True, exist_ok=True)
    paths = download_boundaries(force=force_download)
    result = ingest_districts(paths["districts"])
    result["coverage"] = coverage_snapshot()
    return result


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(run(), indent=2, default=str))
