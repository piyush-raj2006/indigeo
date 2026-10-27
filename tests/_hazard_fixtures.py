from __future__ import annotations

"""Shared synthetic EvidenceContext builders for the hazard-engine tests.

These are literal, hand-written evidence payloads. Keeping them here lets the assessor
and recommendation tests run entirely offline, and keeps the distinction clear between a
fixture (fabricated evidence for testing rules) and a provider test (which must hit or
stub real endpoints).
"""

from typing import Any

from app.services.evidence import EvidenceContext


def terrain_summary(**overrides: Any) -> dict[str, Any]:
    base = {
        "radius_m": 5000.0,
        "samples": 81,
        "nominal_resolution_m": 30.0,
        "elevation_mean_m": 8.0,
        "elevation_min_m": 2.0,
        "elevation_max_m": 21.0,
        "elevation_relief_m": 19.0,
        "elevation_at_point_m": 6.5,
        "local_low_point": False,
        "bathymetry_only": False,
        "slope_deg": 1.2,
        "data_gap": False,
    }
    base.update(overrides)
    return base


SOURCE_STATUS: dict[str, str] = {
    "dem": "available",
    "coastline": "available",
    "open_meteo": "available",
    "open_meteo_recent": "available",
    "open_meteo_forecast": "available",
    "usgs_earthquakes": "available",
    "osm_overpass": "available",
    "firms": "available",
}


def full_context(**overrides: Any) -> EvidenceContext:
    """A coastal, well-instrumented context: every source answered."""
    base: dict[str, Any] = {
        "lon": 80.27,
        "lat": 13.08,
        "radius_m": 5000.0,
        "terrain": {"available": True, "summary": terrain_summary()},
        "coast_distance_m": 1_700.0,
        "weather_recent": {
            "series": [
                {
                    "date": f"2026-09-{day:02d}",
                    "precipitation_mm": 4.0,
                    "tmax_c": 34.0,
                    "gust_max_kmh": 30.0,
                    "soil_moisture_m3m3": 0.22,
                }
                for day in range(1, 29)
            ],
            "modelled_reanalysis": True,
        },
        "weather_forecast": {
            "daily": {
                "precipitation_sum": [8.0, 12.0, 0.0, 2.0, 0.0, 1.0, 0.0],
                "wind_speed_10m_max": [28.0, 31.0, 22.0, 20.0, 19.0, 21.0, 20.0],
                "wind_gusts_10m_max": [45.0, 52.0, 38.0, 33.0, 30.0, 31.0, 30.0],
                "temperature_2m_max": [35.0, 36.2, 34.0, 33.1, 33.5, 34.2, 33.0],
            }
        },
        "seismicity": {
            "total_events": 6,
            "events_within_150km": 2,
            "nearest_event_km": 96.4,
            "max_magnitude": 6.1,
            "max_magnitude_within_150km": 5.4,
            "window_years": 50,
        },
        "fires": {"count": 3},
        "source_status": dict(SOURCE_STATUS),
        "provenance": [{"provider": "usgs_earthquakes", "endpoint": "https://earthquake.usgs.gov/fdsnws/event/1/query"}],
    }
    base.update(overrides)
    return EvidenceContext(**base)


def empty_context(**overrides: Any) -> EvidenceContext:
    """Every external source failed to answer."""
    base: dict[str, Any] = {
        "lon": 79.0,
        "lat": 15.0,
        "radius_m": 5000.0,
        "terrain": {"available": False, "summary": None},
        "coast_distance_m": None,
        "weather_recent": {},
        "weather_forecast": {},
        "seismicity": {},
        "fires": {},
        "source_status": {key: "unavailable" if key != "firms" else "unconfigured" for key in SOURCE_STATUS},
    }
    base.update(overrides)
    return EvidenceContext(**base)


def factor(signal, name):
    return next((f for f in signal.factors if f.name == name), None)
