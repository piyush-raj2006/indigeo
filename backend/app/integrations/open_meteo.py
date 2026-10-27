from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .base import Provider, ProviderResult

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# Verified against the live APIs: the archive endpoint rejects cape/soil-moisture/ET
# as *daily* variables, so they are requested hourly and averaged instead.
ARCHIVE_DAILY = [
    "precipitation_sum",
    "precipitation_hours",
    "rain_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
]
FORECAST_DAILY = ARCHIVE_DAILY
SOIL_HOURLY = [
    "soil_moisture_0_to_1cm",
    "soil_moisture_1_to_3cm",
    "soil_moisture_3_to_9cm",
    "soil_moisture_9_to_27cm",
]
CAPE_HOURLY = ["cape"]

DAILY_VARIABLES = ARCHIVE_DAILY  # kept for backwards-compatible imports


class OpenMeteoProvider(Provider):
    """ERA5-based daily history and up to 16-day forecasts.

    Reanalysis is modelled output, not gauge observations, and is labelled as such
    in every payload it returns.
    """

    key = "open_meteo"
    base_url = ARCHIVE
    license = "Open-Meteo.com: free for non-commercial use with attribution"
    attribution = "Weather data (c) Open-Meteo.com, model: ECMWF ERA5 / ICON global"
    min_interval_seconds = 0.15

    def _validate(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        if payload.get("error"):
            raise ValueError(f"Open-Meteo error: {payload.get('reason', payload)}")
        if "daily" not in payload and "hourly" not in payload:
            raise ValueError("Open-Meteo response has no daily or hourly block")

    def history(
        self,
        lon: float,
        lat: float,
        start: date,
        end: date,
        *,
        variables: list[str] | None = None,
    ) -> ProviderResult:
        params = {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "Asia/Kolkata",
            "daily": ",".join(variables or ARCHIVE_DAILY),
        }
        return self.get_json(ARCHIVE, params=params, validator=self._validate)

    def hourly(
        self,
        lon: float,
        lat: float,
        start: date,
        end: date,
        *,
        variables: list[str],
    ) -> ProviderResult:
        params = {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "Asia/Kolkata",
            "hourly": ",".join(variables),
        }
        return self.get_json(ARCHIVE, params=params, validator=self._validate)

    def forecast(self, lon: float, lat: float, *, days: int = 7, past_days: int = 3) -> ProviderResult:
        params = {
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "forecast_days": max(1, min(days, 16)),
            "past_days": max(0, min(past_days, 92)),
            "timezone": "Asia/Kolkata",
            "daily": ",".join(FORECAST_DAILY),
        }
        return self.get_json(FORECAST, params=params, validator=self._validate, cache_ttl=1800)

    def recent_observation_window(self, lon: float, lat: float, days: int = 45) -> dict[str, Any]:
        end = date.today() - timedelta(days=2)  # ERA5 lands with a short delay
        start = end - timedelta(days=days)
        result = self.history(lon, lat, start, end)
        daily = result.payload.get("daily", {}) if isinstance(result.payload, dict) else {}
        times = daily.get("time", [])
        soil = _daily_means(self._soil_moisture(lon, lat, start, end))
        series = [
            {
                "date": times[i],
                "precipitation_mm": _num(_get(daily, "precipitation_sum", i)),
                "rain_mm": _num(_get(daily, "rain_sum", i)),
                "precipitation_hours": _num(_get(daily, "precipitation_hours", i)),
                "tmax_c": _num(_get(daily, "temperature_2m_max", i)),
                "tmin_c": _num(_get(daily, "temperature_2m_min", i)),
                "wind_max_kmh": _num(_get(daily, "wind_speed_10m_max", i)),
                "gust_max_kmh": _num(_get(daily, "wind_gusts_10m_max", i)),
                "soil_moisture_m3m3": soil.get(times[i]),
            }
            for i in range(len(times))
        ]
        return {
            "provenance": result.provenance(),
            "series": series,
            "modelled_reanalysis": True,
            "caveat": "ERA5 reanalysis approximates rainfall; it is not a rain-gauge observation.",
        }

    def _soil_moisture(self, lon: float, lat: float, start: date, end: date) -> dict[str, Any]:
        """Soil moisture only comes back hourly, so it is fetched in <=10 day slices."""
        values_by_day: dict[str, list[float]] = {}
        cursor = start
        layer = "soil_moisture_0_to_1cm"
        while cursor <= end:
            stop = min(cursor + timedelta(days=9), end)
            try:
                result = self.hourly(lon, lat, cursor, stop, variables=[layer])
            except Exception:
                break
            hourly = result.payload.get("hourly", {}) if isinstance(result.payload, dict) else {}
            values = hourly.get(layer) or []
            for ts, value in zip(hourly.get("time", []), values):
                day = str(ts)[:10]
                bucket = values_by_day.setdefault(day, [])
                if value is not None:
                    bucket.append(float(value))
            cursor = stop + timedelta(days=1)
        return {"values": values_by_day}

    def healthcheck(self):
        from .base import ProviderStatus

        try:
            end = date.today() - timedelta(days=3)
            result = self.history(77.5946, 12.9716, end - timedelta(days=3), end)
            rows = len(result.payload.get("daily", {}).get("time", []))
            return ProviderStatus(
                self.key, "healthy" if rows else "degraded", f"probe returned {rows} days", False, ARCHIVE,
                None, result.elapsed_ms, result.http_status,
            )
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", False, ARCHIVE)


def _daily_means(soil: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for day, values in (soil.get("values") or {}).items():
        if values:
            out[day] = round(sum(values) / len(values), 4)
    return out


def _get(block: dict[str, Any], key: str, index: int) -> Any:
    values = block.get(key) or []
    if index < len(values):
        return values[index]
    return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "OpenMeteoProvider",
    "DAILY_VARIABLES",
    "ARCHIVE_DAILY",
    "FORECAST_DAILY",
    "SOIL_HOURLY",
    "CAPE_HOURLY",
    "ARCHIVE",
    "FORECAST",
]
