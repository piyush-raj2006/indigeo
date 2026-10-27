from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .base import Provider, ProviderResult

INDIA_BBOX = (68.0, 6.0, 98.0, 38.0)  # west, south, east, north


class UsgsEarthquakeProvider(Provider):
    """USGS ComCat: authoritative historical seismicity. Hazard, not prediction."""

    key = "usgs_earthquakes"
    base_url = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    license = "USGS data are public domain unless otherwise noted"
    attribution = "Earthquake data (c) USGS Earthquake Hazards Program"
    cacheable = True

    def _validate(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
            raise ValueError("Expected a GeoJSON FeatureCollection from ComCat")

    def query_india(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        min_magnitude: float = 4.5,
        bbox: tuple[float, float, float, float] = (68.0, 6.0, 98.0, 38.0),
        limit: int = 20_000,
    ) -> ProviderResult:
        """ComCat takes min/max lat/lon, not a single bbox parameter (verified live)."""
        end = end or date.today()
        start = start or (end - timedelta(days=365 * 20))
        west, south, east, north = bbox
        params = {
            "format": "geojson",
            "starttime": start.isoformat(),
            "endtime": end.isoformat(),
            "minmagnitude": min_magnitude,
            "minlongitude": west,
            "minlatitude": south,
            "maxlongitude": east,
            "maxlatitude": north,
            "orderby": "time",
            "limit": limit,
        }
        return self.get_json(self.base_url, params=params, validator=self._validate)

    def query_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        *,
        days: int = 365,
        min_magnitude: float = 2.5,
    ) -> ProviderResult:
        return self.query_india(
            start=date.today() - timedelta(days=days),
            end=date.today(),
            min_magnitude=min_magnitude,
            bbox=(west, south, east, north),
            limit=5000,
        )

    def healthcheck(self):
        from .base import ProviderStatus

        try:
            # A year of M4.5+ around the India neighbourhood reliably has events;
            # a 30-day window is often legitimately empty.
            result = self.query_india(min_magnitude=4.5, start=date.today() - timedelta(days=365))
            count = len(result.payload.get("features", [])) if isinstance(result.payload, dict) else 0
            state = "healthy" if count else "degraded"
            return ProviderStatus(
                self.key, state, f"{count} events (M>=4.5, last 365 days in the India envelope)",
                False, self.base_url, result.fetched_at.isoformat(), result.elapsed_ms, result.http_status,
            )
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", False, self.base_url)


__all__ = ["UsgsEarthquakeProvider", "INDIA_BBOX"]
