from __future__ import annotations

import json
import math
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry
from shapely.wkb import loads as wkb_loads

from ..core.config import get_settings
from ..core.errors import AppError, NotFoundError, ValidationError
from ..core.logging import get_logger

log = get_logger("gis.spatial")

IDENT_RE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
MAX_BBOX_FEATURES = 5000


def safe_table_name(table: str) -> str:
    cleaned = table.strip().lower()
    if not cleaned or not all(ch in IDENT_RE for ch in cleaned) or len(cleaned) > 60:
        raise ValidationError(
            "Layer names may only use letters, digits and underscores (max 60 characters).",
            details={"table": table},
        )
    return cleaned


def _meters_to_deg(lat: float, metres: float) -> tuple[float, float]:
    from .coords import meters_to_degrees

    return meters_to_degrees(lat, metres)


class LayerStats(dict):
    pass


class SpatialStore(ABC):
    """Spatial query layer shared by the DuckDB (local) and PostGIS (production) backends."""

    backend: str

    @abstractmethod
    def bootstrap(self) -> None: ...

    @abstractmethod
    def list_layers(self) -> list[str]: ...

    @abstractmethod
    def drop_layer(self, table: str) -> None: ...

    @abstractmethod
    def replace_layer(self, table: str, geometries_wkb: list[bytes], attrs: list[dict]) -> LayerStats: ...

    @abstractmethod
    def _fetch(self, sql: str, params: list[Any]) -> list[tuple]: ...

    @abstractmethod
    def count(self, table: str) -> int: ...

    @abstractmethod
    def extent(self, table: str) -> list[float] | None: ...

    def features_containing(self, table: str, lon: float, lat: float) -> list[dict]:
        raise NotImplementedError

    def features_within_radius(self, table: str, lon: float, lat: float, radius_m: float, limit: int = 200) -> list[dict]:
        raise NotImplementedError

    def nearest_features(self, table: str, lon: float, lat: float, limit: int = 5, max_distance_m: float = 50_000) -> list[dict]:
        raise NotImplementedError

    def features_in_bbox(self, table: str, bbox: tuple[float, float, float, float], limit: int = 2000) -> list[dict]:
        raise NotImplementedError

    def features_intersecting(self, table: str, geometry_wkb: bytes, limit: int = 500) -> list[dict]:
        raise NotImplementedError

    def distance_to_boundary(self, table: str, lon: float, lat: float, max_distance_m: float = 200_000) -> float | None:
        raise NotImplementedError


class DuckDBSpatialStore(SpatialStore):
    """Zero-install spatial engine. Geometry via duckdb-spatial, distances computed geodetically."""

    backend = "duckdb"

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self.path = Path(path or settings.spatial_duckdb_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = None
        self._bootstrap_done = False

    # -- connection -------------------------------------------------------
    @property
    def con(self):
        import duckdb

        if self._con is None:
            self._con = duckdb.connect(str(self.path))
            self._con.execute("INSTALL spatial")
            self._con.execute("LOAD spatial")
            self._con.execute("SET geometry_always_xy = true")
        return self._con

    def bootstrap(self) -> None:
        with self._lock:
            if self._bootstrap_done:
                return
            self.con.execute("SELECT * FROM duckdb_settings() LIMIT 1")
            self.con.execute(
                "CREATE TABLE IF NOT EXISTS layer_registry("
                "table_name VARCHAR PRIMARY KEY, geometry_type VARCHAR, srid INTEGER, "
                "feature_count BIGINT, min_lon DOUBLE, min_lat DOUBLE, max_lon DOUBLE, max_lat DOUBLE, "
                "attrs_sample JSON, created_at TIMESTAMP)"
            )
            self._bootstrap_done = True

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None
                self._bootstrap_done = False

    def _fetch(self, sql: str, params: list[Any]) -> list[tuple]:
        with self._lock:
            return self.con.execute(sql, params).fetchall()

    def _run(self, sql: str, params: list[Any] | None = None) -> None:
        with self._lock:
            self.con.execute(sql, params or [])

    # -- layer management -------------------------------------------------
    def list_layers(self) -> list[str]:
        self.bootstrap()
        rows = self._fetch("SELECT table_name FROM layer_registry ORDER BY table_name", [])
        return [r[0] for r in rows]

    def drop_layer(self, table: str) -> None:
        name = safe_table_name(table)
        self._run(f'DROP TABLE IF EXISTS "{name}"')
        self._run("DELETE FROM layer_registry WHERE table_name = ?", [name])

    def replace_layer(self, table: str, geometries_wkb: list[bytes], attrs: list[dict]) -> LayerStats:
        """Atomically rebuild a layer from WKB geometries plus attribute dicts."""
        import pandas as pd

        name = safe_table_name(table)
        if len(geometries_wkb) != len(attrs):
            raise AppError("Geometry/attribute length mismatch while writing layer.")

        self.bootstrap()
        prepared_attrs = [json.dumps(a, ensure_ascii=False, default=str) for a in attrs]
        frame = pd.DataFrame({"fid": range(len(geometries_wkb)), "wkb": list(geometries_wkb), "attrs": prepared_attrs})

        with self._lock:
            staging = f"_{name}_staging"
            self.con.execute(f'DROP TABLE IF EXISTS "{staging}"')
            self.con.execute(
                f'CREATE TABLE "{staging}"(fid BIGINT, geom GEOMETRY, attrs JSON)'
            )
            self.con.register("wkb_frame", frame)
            self.con.execute(
                f'INSERT INTO "{staging}" (fid, geom, attrs) SELECT fid, ST_GeomFromWKB(wkb), attrs FROM wkb_frame'
            )
            self.con.unregister("wkb_frame")
            self.con.execute(f'DROP TABLE IF EXISTS "{name}"')
            self.con.execute(f'ALTER TABLE "{staging}" RENAME TO "{name}"')
            try:
                self.con.execute(f'CREATE INDEX "rt_{name}" ON "{name}" USING RTREE (geom)')
                indexed = True
            except Exception as exc:  # pragma: no cover - index support varies by version
                log.warning("RTREE index unavailable for %s: %s", name, exc)
                indexed = False

        extent = self.extent(name)
        geometry_type = None
        if attrs:
            geometry_type = self._geometry_types(name)
        stats = LayerStats(
            table=name,
            features=len(geometries_wkb),
            extent=extent,
            rtree_index=indexed,
            geometry_type=geometry_type,
        )
        sample = prepared_attrs[0] if prepared_attrs else "{}"
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO layer_registry "
                "(table_name, geometry_type, srid, feature_count, min_lon, min_lat, max_lon, max_lat, attrs_sample, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, current_timestamp)",
                [
                    name,
                    geometry_type,
                    4326,
                    len(geometries_wkb),
                    extent[0] if extent else None,
                    extent[1] if extent else None,
                    extent[2] if extent else None,
                    extent[3] if extent else None,
                    sample,
                ],
            )
        return stats

    def _geometry_types(self, name: str) -> str | None:
        rows = self._fetch(f'SELECT DISTINCT ST_GeometryType(geom) FROM "{name}" LIMIT 5', [])
        types = {str(r[0]).split(":")[-1] for r in rows if r[0]}
        return "|".join(sorted(types)) if types else None

    def count(self, table: str) -> int:
        name = safe_table_name(table)
        rows = self._fetch(f'SELECT count(*) FROM "{name}"', [])
        return int(rows[0][0]) if rows else 0

    def extent(self, table: str) -> list[float] | None:
        name = safe_table_name(table)
        rows = self._fetch(
            f'SELECT min(ST_XMin(geom)), min(ST_YMin(geom)), max(ST_XMax(geom)), max(ST_YMax(geom)) '
            f'FROM "{name}" WHERE geom IS NOT NULL',
            [],
        )
        values = rows[0] if rows else None
        if not values or values[0] is None:
            return None
        return [float(v) for v in values]

    def has_layer(self, table: str) -> bool:
        return table in self.list_layers()

    # -- queries ----------------------------------------------------------
    def _rows_near(self, name: str, lon: float, lat: float, radius_m: float, extra_where: str, params: list[Any]):
        dlon, dlat = _meters_to_deg(lat, radius_m)
        bbox = box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        sql = (
            f'SELECT fid, ST_AsWKB(geom), attrs FROM "{name}" '
            f'WHERE geom && ST_GeomFromWKB(?) {extra_where}'
        )
        return self._fetch(sql, [shapely.wkb.dumps(bbox)] + params)

    def _candidate_rows(self, name: str, lon: float, lat: float, radius_m: float, where: str | None = None):
        extra = f"AND {where}" if where else ""
        params: list[Any] = []
        if where and "?" in where:
            raise AppError("Inline filter must not contain placeholders; use the params form.")
        return self._rows_near(name, lon, lat, radius_m, extra, params)

    def _materialise(self, rows, lon: float, lat: float, radius_m: float | None, limit: int, mode: str):
        out: list[dict] = []
        for fid, wkb, attrs in rows:
            if wkb is None:
                continue
            try:
                geom = wkb_loads(wkb)
            except Exception:
                continue
            distance = _geodesic_distance_m(geom, lon, lat)
            if mode == "within" and radius_m is not None and distance > radius_m:
                continue
            out.append(
                {
                    "fid": int(fid),
                    "distance_m": round(distance, 1),
                    "geometry": _geom_to_geojson(geom),
                    "attributes": _parse_attrs(attrs),
                }
            )
        if mode == "nearest":
            out.sort(key=lambda item: item["distance_m"])
        return out[:limit]

    def features_containing(self, table: str, lon: float, lat: float, limit: int = 25) -> list[dict]:
        name = safe_table_name(table)
        if not self.has_layer(name):
            raise NotFoundError(f"Spatial layer '{table}' is not loaded.", details={"layer": name})
        rows = self._fetch(
            f'SELECT fid, ST_AsWKB(geom), attrs FROM "{name}" '
            "WHERE ST_Contains(geom, ST_Point(?, ?)) LIMIT ?",
            [lon, lat, limit],
        )
        results: list[dict] = []
        for fid, wkb, attrs in rows:
            geom = wkb_loads(wkb)
            results.append(
                {
                    "fid": int(fid),
                    "distance_m": _boundary_distance_m(geom, lon, lat),
                    "geometry": _geom_to_geojson(geom),
                    "attributes": _parse_attrs(attrs),
                }
            )
        return results

    def features_within_radius(self, table: str, lon: float, lat: float, radius_m: float, limit: int = 200) -> list[dict]:
        name = safe_table_name(table)
        rows = self._candidate_rows(name, lon, lat, radius_m)
        return self._materialise(rows, lon, lat, radius_m, limit, "within")

    def nearest_features(
        self, table: str, lon: float, lat: float, limit: int = 5, max_distance_m: float = 50_000, search_m: float | None = None
    ) -> list[dict]:
        name = safe_table_name(table)
        window = search_m or max(2_000.0, max_distance_m)
        for _ in range(4):
            rows = self._candidate_rows(name, lon, lat, window)
            found = self._materialise(rows, lon, lat, None, 10**9, "nearest")
            if len(found) >= limit or window >= 400_000:
                return [f for f in found[:limit] if f["distance_m"] <= max_distance_m]
            window *= 4
        return []

    def features_in_bbox(
        self,
        table: str,
        bbox: tuple[float, float, float, float],
        limit: int = 2000,
        max_area_deg2: float | None = None,
    ) -> list[dict]:
        """Map payload. Features larger than max_area_deg2 are clipped to the bbox to keep tiles small."""
        name = safe_table_name(table)
        if not self.has_layer(name):
            raise NotFoundError(f"Spatial layer '{table}' is not loaded.", details={"layer": name})
        min_lon, min_lat, max_lon, max_lat = bbox
        window = box(min_lon, min_lat, max_lon, max_lat)
        rows = self._fetch(
            f'SELECT fid, ST_AsWKB(geom), attrs FROM "{name}" WHERE geom && ST_GeomFromWKB(?) LIMIT ?',
            [shapely.wkb.dumps(window), int(limit) + 1],
        )
        out: list[dict] = []
        for fid, wkb, attrs in rows[:limit]:
            if wkb is None:
                continue
            try:
                geom = wkb_loads(wkb)
            except Exception:
                continue
            clipped = geom
            if max_area_deg2 is not None and geom.area > max_area_deg2:
                try:
                    clipped = geom.intersection(window)
                except Exception:
                    clipped = geom
            if clipped.is_empty:
                continue
            out.append(
                {
                    "fid": int(fid),
                    "geometry": _geom_to_geojson(clipped),
                    "attributes": _parse_attrs(attrs),
                }
            )
        return out

    def features_intersecting(self, table: str, geometry_wkb: bytes, limit: int = 500) -> list[dict]:
        """Features that overlap a supplied polygon, with the overlap area in km^2."""
        name = safe_table_name(table)
        if not self.has_layer(name):
            raise NotFoundError(f"Spatial layer '{table}' is not loaded.", details={"layer": name})
        rows = self._fetch(
            f'SELECT fid, ST_AsWKB(geom), attrs FROM "{name}" '
            "WHERE geom && ST_GeomFromWKB(?) AND ST_Intersects(geom, ST_GeomFromWKB(?)) LIMIT ?",
            [geometry_wkb, geometry_wkb, int(limit)],
        )
        selector = wkb_loads(geometry_wkb)
        lon0, lat0 = selector.representative_point().x, selector.representative_point().y
        out: list[dict] = []
        for fid, wkb, attrs in rows:
            if wkb is None:
                continue
            try:
                geom = wkb_loads(wkb)
                overlap = geom.intersection(selector)
            except Exception:
                continue
            if overlap.is_empty:
                continue
            out.append(
                {
                    "fid": int(fid),
                    "geometry": _geom_to_geojson(overlap),
                    "attributes": _parse_attrs(attrs),
                    "overlap_km2": round(_geodesic_area_km2(overlap, lon0, lat0), 2),
                }
            )
        out.sort(key=lambda item: item["overlap_km2"], reverse=True)
        return out

    def column_values(self, table: str, json_path: str, limit: int = 500) -> list[Any]:
        name = safe_table_name(table)
        rows = self._fetch(
            f'SELECT DISTINCT json_extract_string(attrs, ?) FROM "{name}" LIMIT ?',
            [json_path, limit],
        )
        return [r[0] for r in rows if r[0] is not None]

    def distance_to_boundary(self, table: str, lon: float, lat: float, max_distance_m: float = 200_000) -> float | None:
        """Metre distance from a point to the outline of a polygon layer.

        Unlike ``nearest_features`` this reports distance to the boundary even when
        the point is inside the polygon, which is what a coastline distance needs.
        ``None`` means "no boundary within ``max_distance_m``", which is a measurement;
        a layer that could not be read at all raises instead, so the two never look alike.
        """
        name = safe_table_name(table)
        if not self.has_layer(name):
            raise NotFoundError(f"Spatial layer '{table}' is not loaded.", details={"layer": name})
        rows = self._fetch(
            f'SELECT ST_AsWKB(geom) FROM "{name}" WHERE geom IS NOT NULL',
            [],
        )
        best: float | None = None
        failures = 0
        for (wkb,) in rows:
            if wkb is None:
                continue
            try:
                geom = wkb_loads(wkb)
                distance = _geodesic_distance_m(geom.boundary, lon, lat)
            except Exception:
                failures += 1
                continue
            best = distance if best is None else min(best, distance)
        if best is None:
            if failures:
                raise AppError(
                    f"Could not measure any geometry in layer '{table}'.",
                    details={"layer": table, "geometries_failed": failures, "geometries_total": len(rows)},
                )
            return None
        if best > max_distance_m:
            return None
        return round(best, 1)

    def summary(self) -> dict[str, Any]:
        # The RTREE index is what keeps radius/bbox predicates off a full scan; an ingest
        # that could not create one still succeeds, so its absence has to be observable.
        # ``None`` means the catalogue could not be read, which is not "no index".
        try:
            indexed: set[str] | None = {row[0] for row in self._fetch("SELECT table_name FROM duckdb_indexes()", [])}
        except Exception:
            indexed = None
        layers = []
        for table in self.list_layers():
            layers.append(
                {
                    "table": table,
                    "features": self.count(table),
                    "extent": self.extent(table),
                    "indexed": None if indexed is None else table in indexed,
                }
            )
        return {"backend": self.backend, "path": str(self.path), "layers": layers}


def _parse_attrs(attrs: Any) -> dict:
    if isinstance(attrs, dict):
        return attrs
    if isinstance(attrs, str):
        try:
            return json.loads(attrs)
        except json.JSONDecodeError:
            return {"raw": attrs}
    return {}


def _geom_to_geojson(geom: BaseGeometry) -> dict:
    from shapely.geometry import mapping

    return mapping(geom)


def _azimuthal_project(coords, lon0: float, lat0: float):
    """Local azimuthal equidistant projection around (lon0, lat0): metre-accurate regionally."""
    import numpy as np
    from pyproj import CRS, Transformer

    aeqd = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs +ellps=WGS84"
    )
    transformer = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
    x, y = transformer.transform(np.asarray(coords[:, 0]), np.asarray(coords[:, 1]))
    return np.column_stack([x, y])


_aeqd_cache: dict[tuple[float, float], Any] = {}


def _geodesic_distance_m(geom: BaseGeometry, lon: float, lat: float) -> float:
    """Metre distance from a point to a geometry, via a local azimuthal equidistant CRS."""
    point = Point(lon, lat)
    if geom.contains(point):
        return 0.0
    key = (round(lon, 2), round(lat, 2))
    transformer = _aeqd_cache.get(key)
    if transformer is None:
        from pyproj import CRS, Transformer

        aeqd = CRS.from_proj4(
            f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs +ellps=WGS84"
        )
        transformer = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
        _aeqd_cache[key] = transformer

    def _project(coords):
        import numpy as np

        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.stack([x, y], axis=-1)

    try:
        return float(shapely.transform(geom, _project).distance(Point(0, 0)))
    except Exception as exc:
        log.debug("Projection distance failed, falling back to planar: %s", exc)
        return geom.distance(point) * 111_320.0


def _geodesic_area_km2(geom: BaseGeometry, lon0: float, lat0: float) -> float:
    """Area via the same local azimuthal equidistant CRS used for distances."""
    import numpy as np
    from pyproj import CRS, Transformer

    def _project(coords):
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.stack([x, y], axis=-1)

    aeqd = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs +ellps=WGS84"
    )
    transformer = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
    try:
        return float(shapely.transform(geom, _project).area) / 1e6
    except Exception as exc:
        log.debug("Projected area failed, falling back to planar degrees: %s", exc)
        return geom.area * 111.32**2 * max(0.05, math.cos(math.radians(lat0)))


def _boundary_distance_m(geom: BaseGeometry, lon: float, lat: float) -> float:
    """0 when inside, otherwise the metre distance to the boundary."""
    if geom.contains(Point(lon, lat)):
        return 0.0
    return _geodesic_distance_m(geom, lon, lat)


_store: SpatialStore | None = None


def get_spatial_store() -> SpatialStore:
    global _store
    settings = get_settings()
    if _store is None:
        if settings.spatial_backend == "postgis":
            from .spatial_postgis import PostGISSpatialStore

            _store = PostGISSpatialStore()
        else:
            _store = DuckDBSpatialStore()
            _store.bootstrap()
    return _store


def reset_spatial_store(store: SpatialStore | None = None) -> SpatialStore | None:
    """Swap the process-wide store (used by tests) and hand back the one replaced."""
    global _store
    previous = _store
    _store = store
    return previous


__all__ = [
    "DuckDBSpatialStore",
    "LayerStats",
    "SpatialStore",
    "get_spatial_store",
    "reset_spatial_store",
    "safe_table_name",
]
