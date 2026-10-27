from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.config import get_settings
from ..core.errors import (
    AppError,
    ArchiveUnsafeError,
    UnsupportedUploadError,
    UploadTooLargeError,
    ValidationError,
)
from ..core.logging import get_logger
from .loader import LoadReport, geojson_feature, load_vector, read_csv_points, to_wkb_records

log = get_logger("gis.upload")

SUPPORTED_SUFFIXES = {".geojson", ".json", ".shp.zip", ".zip", ".gpkg", ".gpkg", ".gdb.zip", ".csv", ".tsv", ".kml", ".kmz", ".gml"}
SHAPEFILE_MEMBERS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qxz", ".qpj", ".qix", ".qix"}
DANGEROUS_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".jar", ".py", ".pyc", ".sh", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".wasm", ".gz", ".tar", ".rar", ".7z", ".zip",
}
CODE_LIKE_RE = re.compile(r"<script|<!DOCTYPE html|javascript:", re.IGNORECASE)


@dataclass
class LayerInspection:
    name: str
    driver: str | None
    feature_count: int
    geometry_type: str | None
    crs_input: str | None
    crs_name: str | None
    reprojected: bool
    repaired: int
    dropped_invalid: int
    bbox: list[float] | None
    columns: list[str]
    missing_values: dict[str, int]
    sample_rows: list[dict[str, Any]]
    warnings: list[str]


@dataclass
class UploadInspection:
    filename: str
    size_bytes: int
    container: str
    status: str  # ready|rejected|needs_choice
    layers: list[LayerInspection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    error_details: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    preview: dict[str, Any] = field(default_factory=dict)
    load_reports: list[LoadReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["load_reports"] = [r.as_dict() for r in self.load_reports]
        return data


def _suffix(filename: str) -> str:
    lowered = filename.lower()
    for candidate in (".shp.zip", ".gdb.zip", ".kml", ".kmz", ".gpkg", ".gpkg", ".geojson", ".json", ".csv", ".tsv", ".gml", ".zip"):
        if lowered.endswith(candidate):
            return candidate
    return Path(lowered).suffix


def save_upload_bytes(content: bytes, filename: str, *, staging_dir: Path) -> Path:
    settings = get_settings()
    if not content:
        raise ValidationError("Uploaded file is empty.")
    if len(content) > settings.max_upload_bytes:
        raise UploadTooLargeError(
            "File exceeds the upload limit.",
            details={"size_bytes": len(content), "limit_bytes": settings.max_upload_bytes},
        )
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:80] or "upload"
    staging_dir.mkdir(parents=True, exist_ok=True)
    target = staging_dir / f"{Path(stem).stem}{_suffix(stem) or '.bin'}"
    target.write_bytes(content)
    return target


def _assert_zip_safe(path: Path) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo], int]:
    settings = get_settings()
    if not zipfile.is_zipfile(path):
        raise ValidationError("File has a .zip name but is not a valid ZIP archive.", details={"file": path.name})
    archive = zipfile.ZipFile(path)
    members = archive.infolist()
    errors: list[str] = []
    if len(members) > settings.max_archive_members:
        archive.close()
        raise ArchiveUnsafeError(
            "Archive contains too many members.",
            details={"members": len(members), "limit": settings.max_archive_members},
        )
    total_uncompressed = 0
    for member in members:
        name = PurePosixPath(member.filename)
        if name.is_absolute() or ".." in name.parts or member.filename.startswith(("\\", "/")):
            archive.close()
            raise ArchiveUnsafeError("Archive member path escapes the extraction directory.", details={"member": member.filename})
        if any(part.startswith("~") for part in name.parts):
            archive.close()
            raise ArchiveUnsafeError("Archive member path uses a home-relative path.", details={"member": member.filename})
        if member.is_dir():
            continue
        if name.suffix.lower() in DANGEROUS_SUFFIXES and name.suffix.lower() not in SHAPEFILE_MEMBERS:
            errors.append(f"Blocked member type: {member.filename}")
        if member.file_size > settings.max_member_bytes:
            archive.close()
            raise ArchiveUnsafeError(
                "A single archive member is larger than the per-file limit.",
                details={"member": member.filename, "size": member.file_size, "limit": settings.max_member_bytes},
            )
        total_uncompressed += member.file_size
        ratio = (member.compress_size or 1)
        if member.file_size > 10_000_000 and total_uncompressed / max(1, ratio) > 100:
            errors.append(f"Possible zip bomb around {member.filename}")
    if total_uncompressed > settings.max_total_uncompressed_bytes:
        archive.close()
        raise ArchiveUnsafeError(
            "Archive decompressed size exceeds the allowed total.",
            details={"uncompressed_bytes": total_uncompressed, "limit": settings.max_total_uncompressed_bytes},
        )
    if errors:
        archive.close()
        raise ArchiveUnsafeError("Archive rejected for safety reasons.", details={"issues": errors[:10]})
    return archive, members, total_uncompressed


def _extract_selected_members(archive: zipfile.ZipFile, members: list[zipfile.ZipInfo], dest: Path, allowed: set[str]) -> list[str]:
    extracted = []
    for member in members:
        suffix = PurePosixPath(member.filename).suffix.lower()
        if suffix in allowed and not member.is_dir():
            target = dest / PurePosixPath(member.filename).name
            with archive.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 256)
            extracted.append(target.name)
    return extracted


def _describe(gdf, report: LoadReport) -> LayerInspection:
    columns = [c for c in gdf.columns if c != "geometry"]
    sample: list[dict[str, Any]] = []
    from .loader import _jsonable

    for _, row in gdf.head(5).iterrows():
        sample.append({str(col): _jsonable(row[col]) for col in columns[:20]})
    return LayerInspection(
        name=report.layer or "layer",
        driver=report.driver,
        feature_count=report.feature_count,
        geometry_type=report.geometry_type,
        crs_input=report.crs_input,
        crs_name=report.crs_input_name,
        reprojected=report.reprojected,
        repaired=report.repaired,
        dropped_invalid=report.dropped_invalid,
        bbox=report.bbox,
        columns=columns,
        missing_values=report.missing_values,
        sample_rows=sample,
        warnings=report.warnings,
    )


def _resolve_read_target(path: Path) -> tuple[Path, str, Path | None]:
    """Extract archives safely and return (readable path, container label, temp dir to clean up)."""
    suffix = _suffix(path.name)
    if suffix == ".zip":
        archive, members, _ = _assert_zip_safe(path)
        try:
            names = {PurePosixPath(m.filename).suffix.lower() for m in members}
            if ".shp" in names:
                workdir = Path(tempfile.mkdtemp(prefix="indigeo_upload_", dir=str(get_settings().uploads_dir)))
                extracted = _extract_selected_members(archive, members, workdir, SHAPEFILE_MEMBERS)
                shp_name = next(n for n in extracted if n.lower().endswith(".shp"))
                return workdir / shp_name, "shapefile_zip", workdir
            if ".gdb" in names or any(n.endswith(".gdbtable") for n in names):
                raise UnsupportedUploadError(
                    "File Geodatabase ZIPs are not readable by the open pipeline here. "
                    "Convert the .gdb to GeoPackage or Shapefile and upload again."
                )
            raise UnsupportedUploadError("ZIP does not contain a Shapefile (.shp). Provide a GeoPackage or GeoJSON instead.")
        finally:
            archive.close()
    if suffix == ".kmz":
        archive, members, _ = _assert_zip_safe(path)
        try:
            kml_member = next((m for m in members if PurePosixPath(m.filename).suffix.lower() == ".kml"), None)
            if kml_member is None:
                raise ValidationError("KMZ archive has no .kml inside.")
            workdir = Path(tempfile.mkdtemp(prefix="indigeo_upload_", dir=str(get_settings().uploads_dir)))
            extracted = _extract_selected_members(archive, [kml_member], workdir, {".kml"})
            return workdir / extracted[0], "kmz", workdir
        finally:
            archive.close()
    return path, suffix, None


def read_upload(path: Path) -> tuple[object, LoadReport, str]:
    """Read an uploaded file into a GeoDataFrame after the archive safety checks."""
    read_path, container, workdir = _resolve_read_target(path)
    try:
        if container in {".csv", ".tsv"}:
            gdf, report = read_csv_points(read_path, lat_col=None, lon_col=None)
        else:
            gdf, report = load_vector(read_path)
        report.source = str(path)
        report.layer = report.layer or path.name
        return gdf, report, container
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


def inspect_upload(path: Path, *, max_preview_features: int = 300) -> UploadInspection:
    """Validate and describe an uploaded GIS file without importing it."""
    settings = get_settings()
    suffix = _suffix(path.name)
    inspection = UploadInspection(
        filename=path.name,
        size_bytes=path.stat().st_size,
        container=suffix or "unknown",
        status="ready",
    )
    if inspection.size_bytes > settings.max_upload_bytes:
        raise UploadTooLargeError("File exceeds the upload limit.", details={"limit_bytes": settings.max_upload_bytes})

    if suffix not in SUPPORTED_SUFFIXES:
        inspection.status = "rejected"
        inspection.errors.append(
            f"Unsupported file type '{suffix or 'unknown'}'. Supported: GeoJSON, Shapefile ZIP, GeoPackage, CSV, KML/KMZ."
        )
        return inspection

    try:
        gdf, report, container = read_upload(path)
        inspection.container = container
        if container == "shapefile_zip":
            inspection.warnings.append(
                "Shapefile bundle unpacked; geometry, index and .prj sidecar files were read together."
            )
        if CODE_LIKE_RE.search(path.read_bytes()[:2048].decode("utf-8", errors="ignore")):
            inspection.warnings.append(
                "Uploaded text contains markup that looks like HTML/script content; only geometry and attributes were read."
            )

        inspection.layers.append(_describe(gdf, report))
        inspection.load_reports.append(report)
        inspection.preview = geojson_feature(gdf.head(max_preview_features), limit=max_preview_features)

        if report.reprojected:
            inspection.warnings.append(f"Reprojected from {report.crs_input} to EPSG:4326 for storage.")
        if report.repaired:
            inspection.warnings.append(f"{report.repaired} invalid geometries repaired automatically.")
        if report.dropped_invalid:
            inspection.warnings.append(f"{report.dropped_invalid} unrepairable geometries were dropped.")
        if not report.geometry_type:
            inspection.status = "needs_choice"
            inspection.errors.append("No usable geometry column was found.")
        total_missing = sum(report.missing_values.values())
        if total_missing:
            inspection.warnings.append(
                f"{total_missing} missing attribute value(s) across {len(report.missing_values)} column(s); see missing_values."
            )
    except (ValidationError, AppError) as exc:
        inspection.status = "rejected"
        inspection.errors.append(exc.message)
        inspection.codes.append(exc.code.value)
        if getattr(exc, "details", None):
            inspection.error_details.append(exc.details)
        inspection.load_reports.append(LoadReport(source=path.name, warnings=[exc.message]))
    return inspection


def import_inspection(inspection: UploadInspection, source_path: Path, *, dataset_code: str, table_name: str, title: str, hazard: str | None = None, category: str = "user_data", actor: str = "anonymous", max_features: int = 200_000) -> dict[str, Any]:
    """Write a validated upload into the spatial store and register its provenance."""
    from datetime import datetime, timezone

    from ..core.database import session_scope
    from ..models.catalog import Dataset
    from .spatial import get_spatial_store

    if inspection.status == "rejected":
        raise ValidationError("Rejected uploads cannot be imported.", details={"errors": inspection.errors})

    gdf, report, _container = read_upload(source_path)
    if len(gdf) > max_features:
        gdf = gdf.head(max_features)
        report.warnings.append(f"Import capped at {max_features} features.")

    wkbs, attrs = to_wkb_records(gdf, max_features=max_features)
    store = get_spatial_store()
    stats = store.replace_layer(table_name, wkbs, attrs)
    bbox = stats.get("extent") or [float(v) for v in gdf.total_bounds]

    provenance = {
        "origin": "user_upload",
        "original_filename": source_path.name,
        "size_bytes": source_path.stat().st_size,
        "container": inspection.container,
        "driver": report.driver,
        "crs_input": report.crs_input,
        "crs_input_name": report.crs_input_name,
        "reprojected_to": "EPSG:4326" if report.reprojected else None,
        "repaired_geometries": report.repaired,
        "dropped_invalid": report.dropped_invalid,
        "missing_values": report.missing_values,
        "imported_by": actor,
        "load_warnings": report.warnings,
    }
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        existing = session.query(Dataset).filter(Dataset.code == dataset_code).one_or_none()
        row = existing or Dataset(code=dataset_code)
        row.title = title
        row.category = category
        row.hazard = hazard
        row.geometry_type = report.geometry_type
        row.srid = 4326
        row.storage = "spatial_store"
        row.table_name = stats["table"]
        row.feature_count = stats["features"]
        row.bbox_min_lon, row.bbox_min_lat, row.bbox_max_lon, row.bbox_max_lat = bbox
        row.resolution = "as supplied by the uploader"
        row.temporal_kind = "static"
        row.retrieved_at = now
        row.source = f"upload:{source_path.name}"
        row.license = "declared by uploader - unverified"
        row.attribution = f"Uploaded by {actor}"
        row.status = "ready"
        row.origin = "user_upload"
        row.provenance = provenance
        row.coverage = {"bbox": bbox, "spatially_indexed": stats.get("rtree_index", False)}
        session.add(row)
        session.flush()
        dataset_id = row.id

    return {
        "dataset_id": dataset_id,
        "dataset_code": dataset_code,
        "table_name": stats["table"],
        "features": stats["features"],
        "bbox": bbox,
        "rtree_index": stats.get("rtree_index", False),
        "provenance": provenance,
        "warnings": report.warnings,
    }


def export_layer(table_name: str, *, fmt: str = "geojson", bbox: tuple[float, float, float, float] | None = None, limit: int = 100_000) -> tuple[str, bytes, str]:
    """Export a stored layer back to a standard GIS format."""
    import geopandas as gpd
    from shapely.geometry import shape

    from .spatial import get_spatial_store

    store = get_spatial_store()
    window = bbox or (-180.0, -90.0, 180.0, 90.0)
    rows = store.features_in_bbox(table_name, window, limit=limit)  # type: ignore[attr-defined]
    if not rows:
        raise ValidationError("Layer is empty for the requested extent.", details={"table": table_name, "bbox": window})
    frame = gpd.GeoDataFrame(
        {**{key: [r["attributes"].get(key) for r in rows] for key in sorted({k for r in rows for k in r["attributes"]})}},
        geometry=[shape(r["geometry"]) for r in rows],
        crs="EPSG:4326",
    )
    fmt = fmt.lower()
    if fmt in {"geojson", "json"}:
        return f"{table_name}.geojson", frame.to_json().encode("utf-8"), "application/geo+json"
    if fmt in {"gpkg", "geopackage"}:
        workdir = Path(tempfile.mkdtemp(prefix="indigeo_export_"))
        target = workdir / f"{table_name}.gpkg"
        frame.to_file(target, driver="GPKG", layer=table_name[:60])
        data = target.read_bytes()
        shutil.rmtree(workdir, ignore_errors=True)
        return f"{table_name}.gpkg", data, "application/geopackage+sqlite3"
    if fmt == "shapefile-zip":
        workdir = Path(tempfile.mkdtemp(prefix="indigeo_export_"))
        shp = workdir / f"{table_name[:14]}.shp"
        frame.to_file(shp, driver="ESRI Shapefile", encoding="utf-8")
        zip_path = workdir / f"{table_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for sibling in workdir.glob(f"{table_name[:14]}.*"):
                archive.write(sibling, sibling.name)
        data = zip_path.read_bytes()
        shutil.rmtree(workdir, ignore_errors=True)
        return f"{table_name}.zip", data, "application/zip"
    raise ValidationError("Unsupported export format.", details={"requested": fmt, "supported": ["geojson", "gpkg", "shapefile-zip"]})


__all__ = [
    "UploadInspection",
    "LayerInspection",
    "inspect_upload",
    "import_inspection",
    "read_upload",
    "save_upload_bytes",
    "export_layer",
]
