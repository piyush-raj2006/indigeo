from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..core.config import get_settings
from ..core.errors import ProviderUnavailableError
from ..core.logging import get_logger
from .coords import meters_to_degrees

log = get_logger("gis.dem")

TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_SIZE = 256
MAX_TILES_PER_REQUEST = 36
MIN_ZOOM = 9
MAX_ZOOM = 15  # terrarium production tiles stop at z15
PROVIDER = "aws_terrain_tiles"
# Challenger Deep is about -10,935 m, so anything below this is a void-fill artefact
# in the source raster rather than a surveyed elevation.
OCEAN_FLOOR_M = -11_000.0


def deg_to_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2**z

    def lon_of(px: float) -> float:
        return px / n * 360.0 - 180.0

    def lat_of(py: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (py / n)))))

    return lon_of(x), lat_of(y + 1), lon_of(x + 1), lat_of(y)


def equator_metres_per_pixel(zoom: int) -> float:
    return 156_543.03392 / (2**zoom)


def _mercator_y(lat: float, n: float) -> float:
    clamped = max(-85.0, min(85.0, lat))
    return (1.0 - math.asinh(math.tan(math.radians(clamped))) / math.pi) / 2.0 * n


def window_half_degrees(radius_m: float, lat: float) -> tuple[float, float]:
    """Half-widths (lon, lat) in degrees that really cover ``radius_m`` metres.

    Longitude needs the cos(latitude) correction or an east-west window silently
    samples less ground than was asked for.
    """
    dlon, dlat = meters_to_degrees(lat, radius_m)
    return dlon + 0.01, dlat + 0.01


def tile_axes(radius_m: float, lat: float, zoom: int) -> tuple[int, int]:
    """Worst-case tile counts along each axis for load_field's grid formula."""
    dlon, dlat = window_half_degrees(radius_m, lat)
    n = 2**zoom
    across_x = math.ceil(2 * dlon / 360.0 * n) + 1
    y_top = _mercator_y(lat + dlat, n)
    y_bottom = _mercator_y(lat - dlat, n)
    across_y = math.ceil(y_bottom - y_top) + 1
    return across_x, across_y


def pick_zoom(radius_m: float, lat: float) -> int:
    """Finest zoom whose tile grid still fits the per-request budget.

    A larger radius needs a coarser zoom; the loop walks down from the sharpest
    tile and stops at the first one small enough to fetch.
    """
    for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        axes_x, axes_y = tile_axes(radius_m, lat, zoom)
        if axes_x * axes_y <= MAX_TILES_PER_REQUEST:
            return zoom
    return MIN_ZOOM


@dataclass
class DemField:
    """A stitched block of terrarium tiles addressed by exact Web-Mercator pixel index.

    Degrees are never linear in Mercator, so the field is indexed in tile pixels
    rather than an approximate degree step.
    """

    zoom: int
    origin_tile_x: int
    origin_tile_y: int
    elevation: np.ndarray
    nodata_count: int
    tiles_fetched: int
    tiles_missing: int
    masked_voids: int = 0
    source: str = TERRARIUM_URL
    warnings: list[str] = field(default_factory=list)

    @property
    def height(self) -> int:
        return self.elevation.shape[0]

    @property
    def width(self) -> int:
        return self.elevation.shape[1]

    def metres_per_pixel(self, lat: float) -> float:
        return 156_543.03392 * math.cos(math.radians(lat)) / (2**self.zoom)

    def _index(self, lon: float, lat: float) -> tuple[float, float] | None:
        n = 2**self.zoom
        x_px = (lon + 180.0) / 360.0 * n * TILE_SIZE
        y_px = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n * TILE_SIZE
        col = x_px - self.origin_tile_x * TILE_SIZE
        row = y_px - self.origin_tile_y * TILE_SIZE
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
        return slope_deg(self, lon, lat)

    def summary(self, lon: float, lat: float, radius_m: float) -> dict[str, object]:
        return summarise_field(self, lon, lat, radius_m)


def slope_aspect(field, lon: float, lat: float) -> tuple[float, float | None] | None:
    """Slope in degrees and downslope azimuth, from the central difference of the surface.

    Works for both :class:`DemField` and the local raster field: both expose the same
    ``_index``/``_bilinear`` sampling surface. On a flat window the direction is undefined,
    so it is returned as ``None`` rather than an arbitrary compass bearing.
    """
    idx = field._index(lon, lat)
    if idx is None:
        return None
    row, col = idx
    west = field._bilinear(row, col - 1)
    east = field._bilinear(row, col + 1)
    north = field._bilinear(row - 1, col)
    south = field._bilinear(row + 1, col)
    if None in (west, east, north, south):
        return None
    dx = field.metres_per_pixel(lat)
    dy = field.metres_per_pixel_y(lat) if hasattr(field, "metres_per_pixel_y") else dx
    dz_dx = (east - west) / (2 * dx)
    dz_dy = (north - south) / (2 * dy)
    gradient = math.hypot(dz_dx, dz_dy)
    slope = math.degrees(math.atan(gradient))
    aspect = None if gradient == 0.0 else math.degrees(math.atan2(-dz_dx, -dz_dy)) % 360.0
    return slope, aspect


def slope_deg(field, lon: float, lat: float) -> float | None:
    result = slope_aspect(field, lon, lat)
    return None if result is None else result[0]


def summarise_field(field, lon: float, lat: float, radius_m: float) -> dict[str, object]:
    """Window statistics shared by the Terrarium and local-raster sampling surfaces."""
    centre = field._index(lon, lat)
    if centre is None:
        return {"radius_m": radius_m, "samples": 0, "elevation_mean_m": None, "data_gap": True}
    mpp = field.metres_per_pixel(lat)
    row, col = centre
    reach = int(min(row, field.height - 1 - row, col, field.width - 1 - col))
    half_px = max(1, min(int(radius_m / mpp), reach))
    offsets = np.linspace(-half_px, half_px, 9)
    samples = [
        value
        for dr in offsets
        for dc in offsets
        if (value := field._bilinear(centre[0] + dr, centre[1] + dc)) is not None
    ]
    sampled_radius_m = round(half_px * mpp, 1)
    result: dict[str, object] = {
        "radius_m": radius_m,
        "sampled_radius_m": sampled_radius_m,
        "samples": len(samples),
        "nominal_resolution_m": round(mpp, 1),
        "derivative_note": (
            "Slope, aspect and gradient are derivatives of the DEM surface, not a "
            "hydrological flow-accumulation model."
        ),
    }
    if not samples:
        result.update({"elevation_mean_m": None, "data_gap": True})
        return result
    array = np.asarray(samples, dtype=float)
    here = field.elevation_at(lon, lat)
    result.update(
        {
            "elevation_mean_m": round(float(array.mean()), 1),
            "elevation_min_m": round(float(array.min()), 1),
            "elevation_max_m": round(float(array.max()), 1),
            "elevation_relief_m": round(float(array.max() - array.min()), 1),
            "elevation_at_point_m": None if here is None else round(here, 1),
            "local_low_point": here is not None and here <= float(np.percentile(array, 15)),
            # A window with no pixel above sea level is open water. Terrarium's
            # bathymetry is neither land elevation nor usable flood terrain, and
            # deep void-fill artefacts are masked to nodata, so test the max
            # rather than the mean.
            "bathymetry_only": bool(float(array.max()) <= 0.0),
            "data_gap": False,
        }
    )
    derivatives = slope_aspect(field, lon, lat)
    if derivatives is not None:
        slope, aspect = derivatives
        result["slope_deg"] = round(slope, 2)
        result["downslope_azimuth_deg"] = None if aspect is None else round(aspect, 1)
        result["terrain_gradient_m_per_m"] = round(math.tan(math.radians(slope)), 4)
    else:
        result["slope_deg"] = None
        result["downslope_azimuth_deg"] = None
        result["terrain_gradient_m_per_m"] = None
    return result


def _decode_terrarium(path: Path) -> tuple[np.ndarray, int]:
    """Decode an RGB terrarium tile into metres.

    Returns the grid and the number of pixels masked as impossible. High-zoom AWS
    terrarium ocean tiles in the Arabian Sea / Bay of Bengal decode below the real
    ocean floor (observed down to -16,576 m at z15 near 10N 72E, where the true
    depth is about -2,100 m). Those are void-fill artefacts, not bathymetry, so they
    become nodata instead of being reported as an elevation.
    """
    import rasterio

    with rasterio.open(path) as src:
        data = src.read()
    red, green, blue = data[0].astype(np.int32), data[1].astype(np.int32), data[2].astype(np.int32)
    elevation = (red * 256 + green + blue / 256) - 32768
    mask = src.mask.astype(bool) if src.count > 3 else np.ones_like(elevation, dtype=bool)
    out = elevation.astype(float)
    out[~mask] = np.nan
    # NaN fails the `<` comparison, so this counts only finite sub-floor pixels.
    impossible = out < OCEAN_FLOOR_M
    out[impossible] = np.nan
    return out, int(impossible.sum())


def load_field(min_lon: float, min_lat: float, max_lon: float, max_lat: float, zoom: int) -> DemField | None:
    """Fetch and stitch terrarium tiles covering a bbox. Returns None when nothing is retrievable."""
    import httpx

    settings = get_settings()
    cache_root = Path(settings.dem_dir) / "terrarium"
    cache_root.mkdir(parents=True, exist_ok=True)

    x0, y0 = deg_to_tile(min_lon, max_lat, zoom)
    x1, y1 = deg_to_tile(max_lon, min_lat, zoom)
    xs = list(range(int(math.floor(x0)), int(math.floor(x1)) + 1))
    ys = list(range(int(math.floor(y0)), int(math.floor(y1)) + 1))
    if len(xs) * len(ys) > MAX_TILES_PER_REQUEST:
        raise ProviderUnavailableError(PROVIDER, f"Requested DEM window needs {len(xs) * len(ys)} tiles at z{zoom}.")

    n = 2**zoom
    if max(xs) >= n or max(ys) >= n or min(xs) < 0 or min(ys) < 0:
        return None
    grid = np.full((len(ys) * TILE_SIZE, len(xs) * TILE_SIZE), np.nan, dtype=float)
    fetched = missing = masked_voids = 0
    errors: list[str] = []

    with httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=True) as client:
        for iy, ty in enumerate(ys):
            for ix, tx in enumerate(xs):
                tile_path = cache_root / str(zoom) / str(tx) / f"{ty}.png"
                url = TERRARIUM_URL.format(z=zoom, x=tx, y=ty)
                if not tile_path.exists():
                    tile_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        response = client.get(url, headers={"User-Agent": settings.nominatim_user_agent})
                        if response.status_code == 200 and response.content[:4] == b"\x89PNG":
                            tile_path.write_bytes(response.content)
                        elif response.status_code == 404:
                            missing += 1
                            continue
                        else:
                            errors.append(f"{url} -> HTTP {response.status_code}")
                            continue
                    except httpx.HTTPError as exc:
                        errors.append(f"{url} -> {type(exc).__name__}")
                        continue
                try:
                    decoded, voids = _decode_terrarium(tile_path)
                    masked_voids += voids
                    grid[iy * TILE_SIZE : (iy + 1) * TILE_SIZE, ix * TILE_SIZE : (ix + 1) * TILE_SIZE] = decoded
                    fetched += 1
                except Exception as exc:  # pragma: no cover - depends on GDAL build
                    errors.append(f"decode {tile_path.name}: {type(exc).__name__}: {exc}")

    if fetched == 0:
        log.warning("DEM tiles unavailable", extra={"fields": {"errors": errors[:5]}})
        return None

    if masked_voids:
        log.info(
            "Masked impossible terrarium bathymetry pixels",
            extra={"fields": {"count": masked_voids, "zoom": zoom, "floor_m": OCEAN_FLOOR_M}},
        )
    field_ = DemField(
        zoom=zoom,
        origin_tile_x=int(xs[0]),
        origin_tile_y=int(ys[0]),
        elevation=grid,
        nodata_count=int(np.isnan(grid).sum()),
        tiles_fetched=fetched,
        tiles_missing=missing,
        masked_voids=masked_voids,
        warnings=errors[:5],
    )
    return field_


_cache: dict[tuple[float, float, float, float, int], DemField | None] = {}


def dem_for_point(lon: float, lat: float, radius_m: float) -> tuple[object | None, dict[str, object]]:
    """Resolve elevation from a local raster first, then the Terrarium tile pyramid.

    The local path is preferred when the manifest covers the sampling window: a 30 m SRTM
    or Bhuvan GeoTIFF resolves the slope that drives landslide and runoff severity, while
    the global tile pyramid smooths it. When no local raster covers the window the
    Terrarium path runs and the response carries ``fallback_reason`` - the fallback is
    stated, never silent.
    """
    from .local_dem import local_dem_for_point

    local_field, local_meta = local_dem_for_point(lon, lat, radius_m)
    if local_field is not None:
        return local_field, local_meta

    field_, meta = _terrarium_for_point(lon, lat, radius_m)
    meta["dem_origin"] = "terrarium_tiles" if field_ is not None else None
    fallback = local_meta.get("reason")
    if local_meta.get("status") == "disabled":
        fallback = "Local DEM rasters are disabled; the Terrarium tile pyramid was used."
    elif local_meta.get("status") == "unconfigured":
        fallback = "No local DEM manifest is configured; the Terrarium tile pyramid was used."
    meta["local_dem"] = {
        key: local_meta.get(key) for key in ("status", "reason", "raster") if local_meta.get(key) is not None
    }
    meta["fallback_reason"] = fallback
    return field_, meta


def _terrarium_for_point(lon: float, lat: float, radius_m: float) -> tuple[DemField | None, dict[str, object]]:
    zoom = pick_zoom(radius_m, lat)
    dlon, dlat = window_half_degrees(radius_m, lat)
    key = (round(lon - dlon, 4), round(lat - dlat, 4), round(lon + dlon, 4), round(lat + dlat, 4), zoom)
    if key not in _cache:
        _cache[key] = load_field(key[0], key[1], key[2], key[3], zoom)
    field_ = _cache[key]
    axes_x, axes_y = tile_axes(radius_m, lat, zoom)
    meta: dict[str, object] = {
        "source": TERRARIUM_URL,
        "zoom": zoom,
        "tiles_planned": axes_x * axes_y,
        "nominal_resolution_m": round(equator_metres_per_pixel(zoom) * math.cos(math.radians(lat)), 1),
        "tiles_fetched": getattr(field_, "tiles_fetched", 0),
        "tiles_missing": getattr(field_, "tiles_missing", 0),
        "nodata_pixels": getattr(field_, "nodata_count", None),
        "impossible_bathymetry_pixels": getattr(field_, "masked_voids", 0),
        "available": field_ is not None,
        "provenance": {
            "provider": "dem",
            "endpoint": TERRARIUM_URL,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "http_status": None,
            "elapsed_ms": 0.0,
            "from_cache": False,
            "license": None,
            "attribution": "AWS Terrain Tiles (elevation-tiles-prod); underlying DEM sources are mixed",
            "notes": {
                "origin": "terrarium_tiles",
                "zoom": zoom,
                "tiles_fetched": getattr(field_, "tiles_fetched", 0),
                "tiles_missing": getattr(field_, "tiles_missing", 0),
                "impossible_bathymetry_pixels": getattr(field_, "masked_voids", 0),
            },
        },
    }
    if field_ is not None and field_.warnings:
        meta["warnings"] = field_.warnings
    return field_, meta


__all__ = [
    "DemField",
    "MAX_TILES_PER_REQUEST",
    "MAX_ZOOM",
    "MIN_ZOOM",
    "PROVIDER",
    "TERRARIUM_URL",
    "dem_for_point",
    "equator_metres_per_pixel",
    "load_field",
    "pick_zoom",
    "slope_aspect",
    "slope_deg",
    "summarise_field",
    "tile_axes",
    "tile_bounds",
    "window_half_degrees",
]
