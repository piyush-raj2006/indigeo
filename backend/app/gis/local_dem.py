"""Local high-resolution DEM rasters (SRTM 30 m, ISRO Bhuvan GeoTIFF) with rasterio.

The Terrarium tile pyramid is a useful global fallback, but at 30-90 m it smooths the
local relief that actually drives landslide and runoff severity. This module reads the
deployment's own GeoTIFFs instead - windowed, in EPSG:4326, at native resolution - and
reports the raster's name, vintage and declared source for every value it samples.

Same rule as everywhere else in this build: a raster is used only if the manifest lists it
and its bounds fully contain the query window. Otherwise the caller is told why and falls
back to Terrarium, with the fallback reason in the response.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..core.config import get_settings
from ..core.logging import get_logger
from ..gis.coords import meters_to_degrees

log = get_logger("gis.local_dem")

# Same physical bounds the Terrarium decoder enforces. SRTM void fill is -32768 and is
# normally declared as nodata; these catch rasters whose nodata tag is missing.
OCEAN_FLOOR_M = -11_000.0
MAX_LAND_M = 9_000.0  # Everest is 8,849 m; anything higher is fill, not survey.


@dataclass
class RasterSpec:
    name: str
    path: Path
    bbox: tuple[float, float, float, float] | None
    source: str | None = None
    vintage: str | None = None
    license: str | None = None
    vertical_datum: str | None = None
    resolution_m: float | None = None
    notes: str | None = None
    _bounds: tuple[float, float, float, float] | None = None

    @property
    def resolved(self) -> bool:
        return self.path.exists()

    def bounds_4326(self) -> tuple[float, float, float, float] | None:
        if self.bbox is not None:
            return self.bbox
        if self._bounds is not None:
            return self._bounds
        self._bounds = _raster_bounds_4326(self.path)
        return self._bounds

    def contains(self, box: tuple[float, float, float, float]) -> bool:
        outer = self.bounds_4326()
        if outer is None:
            return False
        west, south, east, north = outer
        qw, qs, qe, qn = box
        return qw >= west and qs >= south and qe <= east and qn <= north


@dataclass
class LocalDemField:
    """A window of a local raster, addressed by its affine transform.

    Mirrors the sampling surface of :class:`app.gis.dem.DemField` so the hazard assessors
    do not care which DEM answered.
    """

    elevation: np.ndarray
    transform: Any  # rasterio Affine, pixel -> EPSG:4326
    raster: str
    source: str | None
    vintage: str | None
    license: str | None
    vertical_datum: str | None
    resolution_m: float
    endpoint: str
    nodata_count: int
    masked_impossible: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def height(self) -> int:
        return int(self.elevation.shape[0])

    @property
    def width(self) -> int:
        return int(self.elevation.shape[1])

    def metres_per_pixel(self, lat: float) -> float:
        return abs(self.transform.a) * 111_320.0 * max(0.05, math.cos(math.radians(lat)))

    def metres_per_pixel_y(self, lat: float) -> float:
        return abs(self.transform.e) * 110_574.0

    def _index(self, lon: float, lat: float) -> tuple[float, float] | None:
        col, row = ~self.transform @ (lon, lat)
        if col < 0 or row < 0 or col > self.width - 1 or row > self.height - 1:
            return None
        return row, col

    def _bilinear(self, row: float, col: float) -> float | None:
        if row < 0 or col < 0 or row > self.height - 1 or col > self.width - 1:
            return None
        r0, c0 = int(math.floor(row)), int(math.floor(col))
        r1, c1 = min(r0 + 1, self.height - 1), min(c0 + 1, self.width - 1)
        dr, dc = row - r0, col - c0
        e = self.elevation
        value = (
            e[r0, c0] * (1 - dr) * (1 - dc)
            + e[r1, c0] * dr * (1 - dc)
            + e[r0, c1] * (1 - dr) * dc
            + e[r1, c1] * dr * dc
        )
        return None if np.isnan(value) else float(value)

    def elevation_at(self, lon: float, lat: float) -> float | None:
        idx = self._index(lon, lat)
        return None if idx is None else self._bilinear(*idx)

    def slope_deg_at(self, lon: float, lat: float) -> float | None:
        from .dem import slope_deg

        return slope_deg(self, lon, lat)

    def summary(self, lon: float, lat: float, radius_m: float) -> dict[str, Any]:
        from .dem import summarise_field

        return summarise_field(self, lon, lat, radius_m)


class LocalDemSource:
    """Manifest reader for local rasters; re-reads the manifest when it changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple[float, int, str] | None = None
        self._rasters: list[RasterSpec] = []
        self._last_read = 0.0

    def _settings_path(self) -> Path:
        settings = get_settings()
        return Path(settings.dem_dir) / "local" / settings.dem_manifest_name

    def rasters(self) -> list[RasterSpec]:
        path = self._settings_path()
        try:
            stat = path.stat()
            stamp = (stat.st_mtime, stat.st_size, str(path))
        except OSError:
            self._rasters = []
            self._stamp = None
            return []
        if stamp == self._stamp:
            return self._rasters
        with self._lock:
            if stamp == self._stamp:
                return self._rasters
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Local DEM manifest unreadable", extra={"fields": {"path": str(path), "error": str(exc)}})
                self._rasters = []
                self._stamp = None
                return []
            entries = raw.get("rasters", []) if isinstance(raw, dict) else raw
            specs: list[RasterSpec] = []
            for index, entry in enumerate(entries if isinstance(entries, list) else []):
                spec = _spec_from_entry(entry, path.parent)
                if spec is not None:
                    specs.append(spec)
                else:
                    log.warning("Skipped a local DEM manifest entry", extra={"fields": {"index": index}})
            self._rasters = specs
            self._stamp = stamp
            return self._rasters

    def covering(self, box: tuple[float, float, float, float]) -> list[RasterSpec]:
        covering = [spec for spec in self.rasters() if spec.contains(box)]
        # Two stable sorts: newest vintage first, then finest resolution first.
        covering.sort(key=lambda spec: spec.vintage or "", reverse=True)
        covering.sort(key=lambda spec: spec.resolution_m or 1e9)
        return covering

    def status(self) -> dict[str, Any]:
        rasters = self.rasters()
        if not get_settings().dem_local_enabled:
            return {"configured": False, "status": "disabled", "count": 0}
        if not rasters:
            return {
                "configured": False,
                "status": "unconfigured",
                "count": 0,
                "reason": f"No local DEM manifest at {self._settings_path()}.",
            }
        readable = [spec for spec in rasters if spec.resolved]
        return {"configured": bool(readable), "status": "available" if readable else "unavailable", "count": len(readable)}

    def healthcheck(self) -> dict[str, Any]:
        status = self.status()
        if status["status"] != "available":
            return status
        readable = [spec for spec in self.rasters() if spec.resolved and spec.bounds_4326() is not None]
        return {
            **status,
            "status": "available" if readable else "degraded",
            "rasters": [
                {
                    "name": spec.name,
                    "file": spec.path.name,
                    "bbox": [round(v, 6) for v in (spec.bounds_4326() or ())] or None,
                    "resolution_m": spec.resolution_m,
                    "vintage": spec.vintage,
                }
                for spec in readable[:10]
            ],
        }


_SOURCE = LocalDemSource()


def local_dem_healthcheck() -> dict[str, Any]:
    return _SOURCE.healthcheck()


def local_dem_for_point(lon: float, lat: float, radius_m: float) -> tuple[LocalDemField | None, dict[str, Any]]:
    """Sample the finest local raster covering the radius, or explain why not."""
    settings = get_settings()
    meta: dict[str, Any] = {
        "source": "local_raster",
        "dem_origin": "local_raster",
        "available": False,
        "zoom": None,
        "tiles_planned": 0,
        "tiles_fetched": 0,
        "tiles_missing": 0,
        "nodata_pixels": None,
        "impossible_bathymetry_pixels": 0,
        "status": "unconfigured",
    }
    if not settings.dem_local_enabled:
        meta.update(status="disabled", reason="Local DEM rasters are disabled (DEM_LOCAL_ENABLED=false).")
        return None, meta

    rasters = _SOURCE.rasters()
    if not rasters:
        meta["reason"] = f"No local DEM manifest at {_SOURCE._settings_path()}."
        return None, meta

    dlon, dlat = meters_to_degrees(lat, radius_m)
    box = (lon - dlon - 0.01, lat - dlat - 0.01, lon + dlon + 0.01, lat + dlat + 0.01)
    covering = _SOURCE.covering(box)
    if not covering:
        meta.update(
            status="outside_coverage",
            reason=(
                "No local DEM raster fully contains the sampling window "
                f"{tuple(round(v, 4) for v in box)}; available rasters: "
                + ", ".join(spec.name for spec in rasters[:4])
                + "."
            ),
        )
        return None, meta

    failures: list[str] = []
    for spec in covering:
        try:
            field_ = _read_window(spec, box, settings.dem_max_window_pixels)
        except Exception as exc:
            failures.append(f"{spec.name}: {type(exc).__name__}: {exc}")
            continue
        if field_ is None:
            failures.append(f"{spec.name}: no pixels in the sampling window")
            continue
        meta.update(
            available=True,
            status="used",
            nominal_resolution_m=round(field_.resolution_m, 2),
            nodata_pixels=field_.nodata_count,
            impossible_bathymetry_pixels=field_.masked_impossible,
            raster={
                "name": spec.name,
                "file": spec.path.name,
                "bbox": [round(v, 6) for v in (spec.bounds_4326() or ())],
                "resolution_m": spec.resolution_m,
                "vintage": spec.vintage,
                "source": spec.source,
                "vertical_datum": spec.vertical_datum,
                "declared_license": spec.license,
            },
            provenance={
                "provider": "dem_local",
                "endpoint": _file_uri(spec.path),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "http_status": None,
                "elapsed_ms": 0.0,
                "from_cache": True,
                "license": spec.license,
                "attribution": spec.source or "operator-supplied DEM raster",
                "notes": {
                    "origin": "local_raster",
                    "raster": spec.name,
                    "resolution_m": round(field_.resolution_m, 2),
                    "vintage": spec.vintage,
                    "vertical_datum": spec.vertical_datum,
                    "source": spec.source or "not recorded in the DEM manifest",
                    "license": spec.license or "not recorded in the DEM manifest",
                },
            },
            derivative_note=(
                "Slope, aspect and gradient are derivatives of this raster's surface, not a "
                "hydrological flow-accumulation model."
            ),
        )
        if field_.warnings:
            meta["warnings"] = field_.warnings
        return field_, meta

    meta.update(status="unavailable", reason="; ".join(failures[:3]) or "Local DEM rasters could not be read.")
    return None, meta


def _read_window(spec: RasterSpec, box: tuple[float, float, float, float], max_pixels: int) -> LocalDemField | None:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import Affine
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import from_bounds

    started = time.monotonic()
    west, south, east, north = box
    with rasterio.open(spec.path) as dataset:
        if dataset.crs is None:
            raise ValueError("raster has no declared CRS")
        source = dataset
        vrt = None
        if dataset.crs.to_epsg() != 4326:
            vrt = WarpedVRT(dataset, crs="EPSG:4326", resampling=Resampling.bilinear)
            source = vrt
        try:
            window = from_bounds(west, south, east, north, transform=source.transform)
            window = window.round_offsets().round_lengths()
            if window.width < 1 or window.height < 1:
                return None
            out_width = max(1, min(int(window.width), int(max_pixels)))
            out_height = max(1, min(int(window.height), int(max_pixels)))
            data = source.read(1, window=window, out_shape=(out_height, out_width), masked=True)
            transform = source.window_transform(window) @ Affine.scale(
                window.width / out_width, window.height / out_height
            )
        finally:
            if vrt is not None:
                vrt.close()

    elevation = np.asarray(data.astype("float64").filled(np.nan))
    elevation[~np.isfinite(elevation)] = np.nan
    nodata = int(np.isnan(elevation).sum())
    impossible = (elevation < OCEAN_FLOOR_M) | (elevation > MAX_LAND_M)
    masked_impossible = int(impossible.sum())
    elevation[impossible] = np.nan
    warnings: list[str] = []
    if masked_impossible:
        warnings.append(
            f"{masked_impossible} raster pixel(s) were outside the physically possible range "
            f"({OCEAN_FLOOR_M:.0f}..{MAX_LAND_M:.0f} m) and were masked to nodata."
        )
    if np.isnan(elevation).all():
        raise ValueError("every pixel in the window is nodata after masking")
    resolution_m = abs(transform.a) * 111_320.0 * max(0.05, math.cos(math.radians((south + north) / 2.0)))
    log.info(
        "Sampled local DEM raster",
        extra={
            "fields": {
                "raster": spec.name,
                "pixels": f"{out_width}x{out_height}",
                "resolution_m": round(resolution_m, 2),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        },
    )
    return LocalDemField(
        elevation=elevation,
        transform=transform,
        raster=spec.name,
        source=spec.source,
        vintage=spec.vintage,
        license=spec.license,
        vertical_datum=spec.vertical_datum,
        resolution_m=resolution_m,
        endpoint=_file_uri(spec.path),
        nodata_count=nodata + masked_impossible,
        masked_impossible=masked_impossible,
        warnings=warnings,
    )


def _spec_from_entry(entry: Any, root: Path) -> RasterSpec | None:
    if not isinstance(entry, dict):
        return None
    path_value = entry.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    bbox = entry.get("bbox")
    parsed_bbox: tuple[float, float, float, float] | None = None
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            parsed_bbox = tuple(float(v) for v in bbox)  # type: ignore[assignment]
        except (TypeError, ValueError):
            parsed_bbox = None
    resolution = entry.get("resolution_m")
    try:
        parsed_resolution = float(resolution) if resolution is not None else None
    except (TypeError, ValueError):
        parsed_resolution = None
    return RasterSpec(
        name=str(entry.get("name") or path.stem),
        path=path,
        bbox=parsed_bbox,
        source=str(entry["source"]) if entry.get("source") else None,
        vintage=str(entry["vintage"]) if entry.get("vintage") else None,
        license=str(entry["license"]) if entry.get("license") else None,
        vertical_datum=str(entry["vertical_datum"]) if entry.get("vertical_datum") else None,
        resolution_m=parsed_resolution,
        notes=str(entry["notes"]) if entry.get("notes") else None,
    )


def _raster_bounds_4326(path: Path) -> tuple[float, float, float, float] | None:
    try:
        import rasterio
        from rasterio.warp import transform_bounds

        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                return None
            bounds = dataset.bounds
            if dataset.crs.to_epsg() == 4326:
                return (bounds.left, bounds.bottom, bounds.right, bounds.top)
            west, south, east, north = transform_bounds(
                dataset.crs, "EPSG:4326", bounds.left, bounds.bottom, bounds.right, bounds.top
            )
            return (float(west), float(south), float(east), float(north))
    except Exception:
        return None


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


__all__ = [
    "LocalDemField",
    "LocalDemSource",
    "local_dem_for_point",
    "local_dem_healthcheck",
]
