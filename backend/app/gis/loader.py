from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import Point, mapping

from ..core.errors import ValidationError

EPSG_4326 = "EPSG:4326"

COMMON_EPSG = {
    4326: "WGS 84 (lon/lat degrees)",
    32643: "UTM 43N (metres)",
    32644: "UTM 44N (metres)",
    32645: "UTM 45N (metres)",
    32646: "UTM 46N (metres)",
    32647: "UTM 47N (metres)",
    32648: "UTM 48N (metres)",
    32649: "UTM 49N (metres)",
    32650: "UTM 50N (metres)",
    32651: "UTM 51N (metres)",
    32652: "UTM 52N (metres)",
    32697: "WGS 84 / UTM zone 43N",
    7755: "WGS 84 / India North leg (LL/ISL77E)",
    7756: "WGS 84 / India South leg (LL/ISL80E)",
    32614: "UTM 14N",
}


@dataclass
class LoadReport:
    source: str
    driver: str | None = None
    layer: str | None = None
    crs_input: str | None = None
    crs_input_name: str | None = None
    reprojected: bool = False
    feature_count: int = 0
    geometry_type: str | None = None
    repaired: int = 0
    dropped_invalid: int = 0
    missing_values: dict[str, int] = field(default_factory=dict)
    bbox: list[float] | None = None
    columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


def _detect_crs(gdf: gpd.GeoDataFrame, report: LoadReport) -> gpd.GeoDataFrame:
    crs = gdf.crs
    if crs is None:
        # Unlabelled data: infer from the numeric range, but never silently - record the guess.
        bounds = gdf.total_bounds
        spans_xy = (bounds[2] - bounds[0], bounds[3] - bounds[1])
        if all(-180 <= v <= 180 for v in bounds) and all(-90 <= v <= 90 for v in (bounds[1], bounds[3])):
            report.crs_input = "inferred:EPSG:4326"
            report.warnings.append(
                "File declared no CRS. Values fall inside the lon/lat envelope, so WGS 84 was assumed. "
                "Verify before using this layer for measurements."
            )
            gdf = gdf.set_crs(EPSG_4326)
        elif max(spans_xy) > 1000:
            raise ValidationError(
                "Cannot import a layer with no coordinate reference system and metre-scale extents. "
                "Re-export with an EPSG code (for example EPSG:32643) or as WGS 84.",
                details={"bounds": [float(b) for b in bounds]},
            )
        else:
            raise ValidationError(
                "The file has no CRS and its coordinates do not look like longitude/latitude.",
                details={"bounds": [float(b) for b in bounds]},
            )
    else:
        epsg_code = crs.to_epsg() if hasattr(crs, "to_epsg") else None
        report.crs_input = f"EPSG:{epsg_code}" if epsg_code else str(crs)
        report.crs_input_name = getattr(crs, "name", None)
    if gdf.crs is not None and not gdf.crs.equals(EPSG_4326):
        gdf = gdf.to_crs(EPSG_4326)
        report.reprojected = True
    return gdf


def _layer_names(path: Path, info: dict) -> list[str]:
    from pyogrio import list_layers

    layers = info.get("layers")
    if layers:
        return [str(name) for name in layers]
    array = list_layers(path)
    if array is None or len(array) == 0:
        return []
    if getattr(array, "dtype", None).names:  # structured array
        return [str(row["layer_name"]) for row in array]
    return [str(row[0]) for row in array]


def load_vector(
    path: str | Path,
    *,
    layer: str | None = None,
    max_features: int | None = None,
    repair: bool = True,
) -> tuple[gpd.GeoDataFrame, LoadReport]:
    """Read any supported vector file, force it to EPSG:4326 and report what was done."""
    path = Path(path)

    report = LoadReport(source=str(path))
    try:
        from pyogrio import read_info

        info = read_info(path)
        report.driver = info.get("driver")
    except Exception:
        info = {}

    try:
        if layer is None:
            names = _layer_names(path, info)
            if not names:
                raise ValidationError("No readable layers found in the file.", details={"file": path.name})
            if len(names) > 1:
                report.warnings.append(
                    f"File contains {len(names)} layers; reading '{names[0]}'. "
                    "Choose a layer explicitly when importing the others."
                )
            layer = names[0]
        gdf = gpd.read_file(path, layer=layer)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"Could not read this file as vector GIS data ({type(exc).__name__}: {str(exc)[:120]}).",
            details={"file": path.name, "error": str(exc)[:300]},
        ) from exc

    report.layer = layer
    if gdf.empty:
        report.feature_count = 0
        report.warnings.append("Layer contains no features.")
        return gdf, report

    gdf = _detect_crs(gdf, report)

    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        if repair:
            repaired = gdf.copy()
            repaired["geometry"] = repaired.geometry.make_valid()
            still_bad = ~repaired.geometry.is_valid
            report.repaired = int(invalid.sum()) - int(still_bad.sum())
            report.dropped_invalid = int(still_bad.sum())
            gdf = repaired[~still_bad].copy()
            if still_bad.any():
                report.warnings.append(
                    f"{int(still_bad.sum())} feature(s) could not be repaired and were dropped."
                )
        else:
            report.warnings.append(f"{int(invalid.sum())} invalid geometries kept unrepaired.")

    gdf = gdf[~gdf.geometry.is_empty].copy()
    report.feature_count = int(len(gdf))
    if max_features is not None and len(gdf) > max_features:
        gdf = gdf.iloc[:max_features].copy()
        report.warnings.append(f"Truncated to {max_features} features for this operation.")

    geom_types = {str(t) for t in gdf.geometry.geom_type if t is not None}
    report.geometry_type = "|".join(sorted(geom_types)) if geom_types else None
    if len(geom_types) > 1:
        report.warnings.append("Mixed geometry types: " + ", ".join(sorted(geom_types)))

    if len(gdf):
        report.bbox = [round(float(v), 6) for v in gdf.total_bounds]
    report.columns = [c for c in gdf.columns if c != "geometry"]
    nulls = gdf[report.columns].isna().sum()
    report.missing_values = {str(k): int(v) for k, v in nulls.items() if v > 0}
    return gdf, report


def read_csv_points(path: str | Path, *, lat_col: str | None, lon_col: str | None, max_rows: int = 200_000) -> tuple[gpd.GeoDataFrame, LoadReport]:
    """Build a point layer from a CSV holding coordinates or a documented geometry column."""
    path = Path(path)
    report = LoadReport(source=str(path), driver="CSV")
    try:
        frame = pd.read_csv(path, nrows=max_rows)
    except Exception as exc:
        raise ValidationError("CSV could not be parsed.", details={"file": path.name, "error": str(exc)[:200]}) from exc

    columns = {c.lower().strip(): c for c in frame.columns}
    lat_name = lat_col or next((columns[k] for k in ("lat", "latitude", "y") if k in columns), None)
    lon_name = lon_col or next((columns[k] for k in ("lon", "long", "lng", "longitude", "x") if k in columns), None)

    if lat_name and lon_name:
        numeric_lat = pd.to_numeric(frame[lat_name], errors="coerce")
        numeric_lon = pd.to_numeric(frame[lon_name], errors="coerce")
        mask = numeric_lat.between(-90, 90) & numeric_lon.between(-180, 180) & numeric_lat.notna() & numeric_lon.notna()
        bad = int((~mask).sum())
        if bad:
            report.warnings.append(f"{bad} row(s) had missing or out-of-range coordinates and were skipped.")
        kept = frame[mask].copy()
        if kept.empty:
            raise ValidationError(
                "No row in the CSV has a usable latitude/longitude pair.",
                details={"rows": int(len(frame)), "lat_column": lat_name, "lon_column": lon_name},
            )
        geometry = [Point(float(lon), float(lat)) for lon, lat in zip(numeric_lon[mask], numeric_lat[mask])]
        gdf = gpd.GeoDataFrame(kept, geometry=geometry, crs=EPSG_4326)
        report.geometry_type = "Point"
        report.layer = f"{lat_name},{lon_name}"
    else:
        raise ValidationError(
            "CSV needs latitude/longitude columns (lat/lon, latitude/longitude, y/x) or a WKT column named 'geometry'.",
            details={"columns": list(frame.columns)},
        )

    report.feature_count = int(len(gdf))
    if len(gdf):
        report.bbox = [round(float(v), 6) for v in gdf.total_bounds]
        report.crs_input = "declared:EPSG:4326"
        report.crs_input_name = "CSV coordinates treated as WGS 84 lon/lat"
    report.columns = [c for c in gdf.columns if c != "geometry"]
    nulls = gdf[report.columns].isna().sum()
    report.missing_values = {str(k): int(v) for k, v in nulls.items() if v > 0}
    return gdf, report


def to_wkb_records(gdf: gpd.GeoDataFrame, *, attr_columns: list[str] | None = None, max_features: int = 200_000) -> tuple[list[bytes], list[dict]]:
    if gdf.crs is None or not gdf.crs.equals(EPSG_4326):
        gdf = gdf.to_crs(EPSG_4326)
    gdf = gdf.head(max_features)
    attr_names = attr_columns or [c for c in gdf.columns if c != "geometry"]
    attrs = [
        {"fid": i, **{str(col): _jsonable(row[col]) for col in attr_names if col in gdf.columns}}
        for i, (_, row) in enumerate(gdf.iterrows())
    ]
    wkbs = [shapely.wkb.dumps(geom) if geom is not None else None for geom in gdf.geometry]
    pairs = [(w, a) for w, a in zip(wkbs, attrs) if w is not None]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, list, dict, int, float)):
        if isinstance(value, float) and value != value:
            return None
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def geojson_feature(gdf: gpd.GeoDataFrame, limit: int = 500) -> dict:
    from shapely.geometry import mapping as _mapping

    features = []
    for _, row in gdf.head(limit).iterrows():
        geom = row.geometry
        if geom is None or (hasattr(geom, "is_empty") and geom.is_empty):
            continue
        props = {str(c): _jsonable(row[c]) for c in gdf.columns if c != "geometry"}
        features.append({"type": "Feature", "geometry": _mapping(geom), "properties": props})
    return {"type": "FeatureCollection", "features": features}


__all__ = ["LoadReport", "load_vector", "read_csv_points", "to_wkb_records", "geojson_feature", "EPSG_4326", "COMMON_EPSG", "mapping"]
