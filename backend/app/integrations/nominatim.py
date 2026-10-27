from __future__ import annotations

from typing import Any

from ..core.config import get_settings
from .base import Provider, ProviderResult

SEARCH_FIELDS = "display_name,place_id,licence,osm_type,osm_id,lat,lon,boundingbox,class,type,importance,address,icon"


class NominatimProvider(Provider):
    """OpenStreetMap geocoding. Free-text search plus reverse lookup of a coordinate."""

    key = "nominatim"
    base_url = "https://nominatim.openstreetmap.org"
    license = "ODbL 1.0 (OpenStreetMap contributors)"
    attribution = "Geocoding (c) OpenStreetMap contributors, ODbL - https://osm.org/copyright"
    min_interval_seconds = get_settings().nominatim_min_interval_seconds

    def _validate(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("error"):
            raise ValueError(f"Nominatim error: {payload['error']}")
        if not isinstance(payload, list | dict):
            raise ValueError("Unexpected Nominatim payload shape")

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        countrycodes: str = "in",
        viewbox: tuple[float, float, float, float] | None = None,
        feature_type: str | None = None,
        polygon: bool = False,
        address_details: bool = True,
    ) -> ProviderResult:
        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "limit": max(1, min(int(limit), 20)),
            "countrycodes": countrycodes,
            "addressdetails": 1 if address_details else 0,
            "polygon_geojson": 1 if polygon else 0,
            "extratags": 0,
            "namedetails": 0,
        }
        if feature_type:
            params["featureType"] = feature_type
        if viewbox:
            params["viewbox"] = ",".join(str(v) for v in viewbox)
        return self.get_json(f"{self.base_url}/search", params=params, validator=self._validate)

    def reverse(self, lon: float, lat: float) -> ProviderResult:
        params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1}
        return self.get_json(f"{self.base_url}/reverse", params=params, validator=self._validate)

    def lookup_osm_id(self, osm_type: str, osm_id: int, *, polygon: bool = True) -> ProviderResult:
        params = {"osm_ids": f"{osm_type[0].upper()}{osm_id}", "format": "jsonv2", "polygon_geojson": 1 if polygon else 0}
        return self.get_json(f"{self.base_url}/lookup", params=params, validator=self._validate)

    def healthcheck(self):
        from .base import ProviderStatus

        url = f"{self.base_url}/status"
        try:
            # /status answers text/plain, so it goes through the text fetcher.
            text, result = self.get_text(url, cache_ttl=0)
            body = text.strip()
            if body == "OK":
                return ProviderStatus(self.key, "healthy", "status=OK", False, url, None, result.elapsed_ms, 200)
            return ProviderStatus(self.key, "degraded", f"status={body[:80]}", False, url, None, result.elapsed_ms, 200)
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", False, url)


__all__ = ["NominatimProvider", "SEARCH_FIELDS"]
