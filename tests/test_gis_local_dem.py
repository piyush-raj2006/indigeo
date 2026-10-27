from __future__ import annotations

"""Local raster DEM sampling and the stated fallback to Terrarium.

The rasters here are synthetic GeoTIFFs written by the test, with a known 10 m rise per
row. That makes the expected elevation, slope and aspect exact, so the assertions check the
sampling maths rather than whatever a real SRTM tile happens to contain.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine, from_origin

from app.core.config import get_settings
from app.gis import dem
from app.gis.dem import slope_aspect
from app.gis.local_dem import LocalDemField, local_dem_for_point, local_dem_healthcheck

PIXEL_DEG = 0.0005
WEST, NORTH = 80.0, 13.5
SIZE = 128
SLOPE_PER_ROW_M = 10.0
# A quarter into a pixel avoids landing exactly on a row/column boundary, which would make
# the bilinear expectation ambiguous. The column is far enough in that the 1 km sampling
# window (which extends west by ~1.9 degrees/100) stays inside the raster.
LON = WEST + 50.25 * PIXEL_DEG
LAT = NORTH - 45.25 * PIXEL_DEG


def _write_dem(root: Path, *, value_per_row: float = SLOPE_PER_ROW_M) -> Path:
    (root / "local").mkdir(parents=True, exist_ok=True)
    raster = root / "local" / "srtm_test.tif"
    grid = (np.arange(SIZE, dtype="float32")[:, None] * value_per_row).repeat(SIZE, axis=1)
    grid[10, 20] = 20_000.0  # above Everest: fill, not survey, and must be masked
    with __import__("rasterio").open(
        raster,
        "w",
        driver="GTiff",
        height=SIZE,
        width=SIZE,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(WEST, NORTH, PIXEL_DEG, PIXEL_DEG),
    ) as dataset:
        dataset.write(grid, 1)
    (root / "local" / "manifest.json").write_text(
        json.dumps(
            {
                "rasters": [
                    {
                        "name": "srtm-test",
                        "path": "srtm_test.tif",
                        "bbox": [WEST, NORTH - SIZE * PIXEL_DEG, WEST + SIZE * PIXEL_DEG, NORTH],
                        "source": "synthetic SRTM test raster",
                        "vintage": "2026-01-01",
                        "license": "test-only",
                        "vertical_datum": "EGM96",
                        "resolution_m": 30,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return raster


@pytest.fixture()
def local_dem(tmp_path, monkeypatch):
    root = tmp_path / "dem"
    _write_dem(root)
    monkeypatch.setattr(get_settings(), "dem_dir", root)
    monkeypatch.setattr(get_settings(), "dem_local_enabled", True)
    return root


def test_a_covering_raster_reports_its_own_provenance(local_dem):
    field, meta = local_dem_for_point(LON, LAT, 1_000)

    assert field is not None
    assert meta["available"] is True
    assert meta["status"] == "used"
    assert meta["source"] == "local_raster"
    # A local raster has no tiles and no zoom; carrying Terrarium's fields would be a lie.
    assert meta["zoom"] is None
    assert meta["tiles_planned"] == 0 and meta["tiles_fetched"] == 0

    provenance = meta["provenance"]
    assert provenance["provider"] == "dem_local"
    assert provenance["endpoint"].startswith("file://")
    assert provenance["license"] == "test-only"
    assert provenance["notes"]["origin"] == "local_raster"
    assert provenance["notes"]["vertical_datum"] == "EGM96"
    assert provenance["notes"]["source"] == "synthetic SRTM test raster"
    assert meta["raster"]["name"] == "srtm-test"
    assert meta["nominal_resolution_m"] == pytest.approx(field.resolution_m, abs=0.01)


def test_elevation_is_sampled_from_the_raster_surface(local_dem):
    field, _ = local_dem_for_point(LON, LAT, 1_000)
    expected = 45.25 * SLOPE_PER_ROW_M
    assert field.elevation_at(LON, LAT) == pytest.approx(expected, abs=0.2)
    assert field.elevation_at(79.0, 12.0) is None


def test_slope_and_aspect_come_from_the_native_pixel_size(local_dem):
    field, _ = local_dem_for_point(LON, LAT, 1_000)
    spacing_y = field.metres_per_pixel_y(LAT)
    expected = math.degrees(math.atan(SLOPE_PER_ROW_M / spacing_y))

    result = slope_aspect(field, LON, LAT)
    assert result is not None
    slope, aspect = result
    assert slope == pytest.approx(expected, rel=1e-3)
    # Elevation rises southwards, so the downslope direction is north (0 degrees).
    assert aspect == pytest.approx(0.0, abs=0.5)


def test_the_summary_carries_derivatives_and_an_honest_caveat(local_dem):
    field, _ = local_dem_for_point(LON, LAT, 1_000)
    summary = field.summary(LON, LAT, 1_000)

    assert summary["data_gap"] is False
    assert summary["elevation_relief_m"] > 0
    assert summary["elevation_at_point_m"] == pytest.approx(452.5, abs=0.2)
    assert summary["downslope_azimuth_deg"] == pytest.approx(0.0, abs=0.5)
    assert summary["terrain_gradient_m_per_m"] > 0
    assert "flow-accumulation" in summary["derivative_note"]


def test_impossible_pixels_are_masked_not_returned_as_elevation(local_dem):
    _, meta = local_dem_for_point(LON, LAT, 1_000)
    assert meta["impossible_bathymetry_pixels"] == 1
    assert meta["nodata_pixels"] >= 1
    assert any("physically possible range" in warning for warning in meta["warnings"])


def test_a_point_outside_every_raster_falls_back_with_a_stated_reason(tmp_path, monkeypatch):
    root = tmp_path / "dem"
    _write_dem(root)
    monkeypatch.setattr(get_settings(), "dem_dir", root)

    def _no_tiles(lon, lat, radius_m):
        return None, {
            "source": dem.TERRARIUM_URL,
            "available": False,
            "zoom": 12,
            "tiles_planned": 4,
            "tiles_fetched": 0,
            "tiles_missing": 4,
            "dem_origin": "terrarium_tiles",
        }

    monkeypatch.setattr(dem, "_terrarium_for_point", _no_tiles)
    field, meta = dem.dem_for_point(77.0, 20.0, 1_000)

    assert field is None
    assert meta["dem_origin"] is None
    assert meta["local_dem"]["status"] == "outside_coverage"
    assert "No local DEM raster fully contains" in meta["fallback_reason"]
    assert meta["tiles_planned"] == 4


def test_a_covered_point_does_not_reach_the_tile_pyramid(local_dem, monkeypatch):
    def _must_not_run(lon, lat, radius_m):  # pragma: no cover - failure path
        raise AssertionError("Terrarium was consulted even though a local raster covered the window")

    monkeypatch.setattr(dem, "_terrarium_for_point", _must_not_run)
    field, meta = dem.dem_for_point(LON, LAT, 1_000)
    assert isinstance(field, LocalDemField)
    assert meta["status"] == "used"
    assert "fallback_reason" not in meta


def test_no_manifest_is_reported_as_unconfigured_not_as_a_dry_point(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "dem_dir", tmp_path / "empty_dem")
    monkeypatch.setattr(
        dem,
        "_terrarium_for_point",
        lambda lon, lat, radius_m: (
            None,
            {
                "source": dem.TERRARIUM_URL,
                "available": False,
                "zoom": 12,
                "tiles_planned": 1,
                "tiles_fetched": 0,
                "tiles_missing": 1,
            },
        ),
    )

    _, meta = dem.dem_for_point(80.0, 13.0, 1_000)
    assert meta["local_dem"]["status"] == "unconfigured"
    assert "No local DEM manifest" in meta["fallback_reason"]
    assert meta["source"] == dem.TERRARIUM_URL


def test_disabling_local_rasters_is_explicit(local_dem, monkeypatch):
    monkeypatch.setattr(get_settings(), "dem_local_enabled", False)
    monkeypatch.setattr(
        dem,
        "_terrarium_for_point",
        lambda lon, lat, radius_m: (None, {"available": False, "zoom": 12, "tiles_planned": 1, "tiles_fetched": 0, "tiles_missing": 1}),
    )
    _, meta = dem.dem_for_point(LON, LAT, 1_000)
    assert meta["local_dem"]["status"] == "disabled"
    assert "disabled" in meta["fallback_reason"]


def test_healthcheck_names_the_readable_rasters(local_dem):
    status = local_dem_healthcheck()
    assert status["status"] == "available"
    assert status["rasters"][0]["name"] == "srtm-test"
    assert status["rasters"][0]["resolution_m"] == 30


def test_a_flat_surface_has_no_defined_aspect():
    field = LocalDemField(
        elevation=np.full((64, 64), 120.0),
        transform=Affine(PIXEL_DEG, 0.0, WEST, 0.0, -PIXEL_DEG, NORTH),
        raster="flat",
        source=None,
        vintage=None,
        license=None,
        vertical_datum=None,
        resolution_m=50.0,
        endpoint="file:///flat.tif",
        nodata_count=0,
    )
    lon, lat = WEST + 0.01, NORTH - 0.01
    result = slope_aspect(field, lon, lat)
    assert result is not None
    slope, aspect = result
    assert slope == pytest.approx(0.0, abs=1e-9)
    assert aspect is None
    summary = field.summary(lon, lat, 1_000)
    assert summary["downslope_azimuth_deg"] is None
    assert summary["terrain_gradient_m_per_m"] == 0.0
