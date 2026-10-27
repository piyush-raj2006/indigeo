from __future__ import annotations

import math

import numpy as np
import pytest

from app.gis.dem import (
    MAX_TILES_PER_REQUEST,
    TILE_SIZE,
    DemField,
    pick_zoom,
    tile_axes,
    window_half_degrees,
)


def _pixel_to_lonlat(x_px: float, y_px: float, zoom: int) -> tuple[float, float]:
    n = 2**zoom * TILE_SIZE
    lon = x_px / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y_px / n)))))
    return lon, lat


def _plane(rows: int, cols: int, slope_per_row: float) -> DemField:
    grid = (np.arange(rows, dtype=float)[:, None] * slope_per_row).repeat(cols, axis=1)
    return DemField(
        zoom=13,
        origin_tile_x=6000,
        origin_tile_y=3000,
        elevation=grid,
        nodata_count=0,
        tiles_fetched=1,
        tiles_missing=0,
    )


def test_point_at_the_origin_tile_corner_reads_the_first_row():
    field = _plane(256, 256, 10.0)
    lon, lat = _pixel_to_lonlat(6000 * TILE_SIZE, 3000 * TILE_SIZE, 13)
    assert field.elevation_at(lon, lat) == pytest.approx(0.0, abs=1e-6)


def test_indexing_is_exact_in_mercator_not_linear_degrees():
    """A degree-linear approximation drifts with latitude; the pixel index must not."""
    field = _plane(512, 512, 1.0)
    for offset in (0, 128, 384, 511):
        lon, lat = _pixel_to_lonlat((6000 * TILE_SIZE) + 100, (3000 * TILE_SIZE) + offset, 13)
        assert field.elevation_at(lon, lat) == pytest.approx(float(offset), abs=1e-6)


def test_bilinear_between_rows():
    field = _plane(256, 256, 10.0)
    lon, lat = _pixel_to_lonlat(6000 * TILE_SIZE, 3000 * TILE_SIZE + 10.5, 13)
    assert field.elevation_at(lon, lat) == pytest.approx(105.0, abs=0.5)


def test_outside_the_field_is_none_not_clamped():
    field = _plane(256, 256, 10.0)
    east, north = _pixel_to_lonlat(6000 * TILE_SIZE + 300, 3000 * TILE_SIZE, 13)
    assert field.elevation_at(east, north) is None


def test_slope_of_a_ramp_matches_the_gradient():
    field = _plane(256, 256, 30.0)  # 30 m rise per pixel
    lon, lat = _pixel_to_lonlat(6000 * TILE_SIZE + 128, 3000 * TILE_SIZE + 128, 13)
    spacing = field.metres_per_pixel(lat)
    expected = math.degrees(math.atan(30.0 / spacing))
    assert field.slope_deg_at(lon, lat) == pytest.approx(expected, rel=0.05)


def test_flat_field_has_no_slope():
    field = _plane(256, 256, 0.0)
    lon, lat = _pixel_to_lonlat(6000 * TILE_SIZE + 128, 3000 * TILE_SIZE + 128, 13)
    assert field.slope_deg_at(lon, lat) == pytest.approx(0.0, abs=1e-6)


def test_summary_reports_relief_and_sampled_extent():
    field = _plane(256, 256, 5.0)
    lon, lat = _pixel_to_lonlat(6000 * TILE_SIZE + 128, 3000 * TILE_SIZE + 128, 13)
    summary = field.summary(lon, lat, 5000)
    assert summary["data_gap"] is False
    assert summary["samples"] == 81
    assert summary["elevation_relief_m"] > 0
    assert summary["sampled_radius_m"] < 5000  # quantised to the grid, reported honestly
    assert summary["nominal_resolution_m"] == pytest.approx(round(field.metres_per_pixel(lat), 1), abs=0.05)


def test_summary_flags_bathymetry_only_windows():
    """Ocean tiles carry negative seafloor values; they are not usable land elevation."""
    grid = np.full((256, 256), -2500.0)
    field = DemField(13, 6000, 3000, grid, 0, 1, 0)
    lon, lat = _pixel_to_lonlat(6000 * TILE_SIZE + 128, 3000 * TILE_SIZE + 128, 13)
    assert field.summary(lon, lat, 5000)["bathymetry_only"] is True


def test_nodata_pixels_do_not_poison_the_neighbourhood():
    grid = np.full((256, 256), 300.0)
    grid[120:140, 120:140] = np.nan
    field = DemField(13, 6000, 3000, grid, int(np.isnan(grid).sum()), 1, 0)
    hole_lon, hole_lat = _pixel_to_lonlat(6000 * TILE_SIZE + 130, 3000 * TILE_SIZE + 130, 13)
    good_lon, good_lat = _pixel_to_lonlat(6000 * TILE_SIZE + 60, 3000 * TILE_SIZE + 60, 13)
    assert field.elevation_at(hole_lon, hole_lat) is None
    assert field.elevation_at(good_lon, good_lat) == pytest.approx(300.0)


def test_pick_zoom_resolves_finer_for_smaller_radius():
    assert pick_zoom(500, 20.0) > pick_zoom(5000, 20.0) > pick_zoom(50_000, 20.0)


@pytest.mark.parametrize("lat", (7.0, 13.0, 22.0, 31.0, 36.5))
@pytest.mark.parametrize("radius", (100, 500, 2000, 5000, 20_000, 50_000, 100_000))
def test_pick_zoom_always_fits_the_tile_budget(lat, radius):
    """The zoom must never hand load_field a grid it will refuse to fetch."""
    zoom = pick_zoom(radius, lat)
    axes_x, axes_y = tile_axes(radius, lat, zoom)
    assert axes_x * axes_y <= MAX_TILES_PER_REQUEST


def test_window_padding_really_covers_the_radius_west_east():
    """Degree padding without the cos correction under-samples at height."""
    dlon, dlat = window_half_degrees(5000, 32.5)
    assert 2 * dlon * 111_320 * math.cos(math.radians(32.5)) >= 2 * 5000 * 0.99
    assert 2 * dlat * 111_320 >= 2 * 5000 * 0.99


def test_coarse_latitudes_get_a_coarser_zoom_for_the_same_radius():
    assert pick_zoom(20_000, 7.0) >= pick_zoom(20_000, 36.0)
