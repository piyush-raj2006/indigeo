from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from ..core.config import get_settings
from ..core.errors import ProviderAuthError, ProviderUnavailableError
from .base import Provider, ProviderResult, ProviderStatus

API_ROOT = "https://firms.modaps.eosdis.nasa.gov/api"

# Source names accepted by the FIRMS area API, verified against the live error responses:
# an unknown source returns "Invalid source." while a known source only complains about the key.
KNOWN_SOURCES = {
    "VIIRS_SNPP_NRT": "NOAA-20/NOAA-21 VIIRS nadir, near real-time (<3h)",
    "VIIRS_SNPP_SP": "Suomi-NPP VIIRS spot, near real-time",
    "VIIRS_SNPP_DAY_NIST": "Suomi-NPP VIIRS day/night",
    "MODIS_Terra_CorrectedReflectance_TrueColor": "Terra MODIS 1km, near real-time",
    "MODIS_Aqua_CorrectedReflectance_TrueColor": "Aqua MODIS 1km, near real-time",
}

INDIA_BBOX = "68.0,6.0,98.0,38.0"


class FirmsProvider(Provider):
    """NASA FIRMS active-fire points.

    Requires a free MAP_KEY. Without one this adapter reports ``unconfigured``;
    it never substitutes synthetic hotspots for real observations.
    """

    key = "firms"
    base_url = f"{API_ROOT}/area"
    license = "NASA FIRMS data are open; citation required"
    attribution = "Fire data via NASA FIRMS (firms.modaps.eosdis.nasa.gov)"
    requires_auth = True
    min_interval_seconds = 1.0

    def credential_env(self) -> str | None:
        return "FIRMS_MAP_KEY"

    def auth_problem(self) -> str | None:
        if not self.map_key:
            return (
                "FIRMS_MAP_KEY is not set. A free map key is issued at "
                "https://firms.modaps.eosdis.nasa.gov/mapkey/ (email + one click). "
                "Active-fire data stays unavailable until it is provided."
            )
        return None

    def __init__(self) -> None:
        super().__init__()
        self.map_key: str | None = get_settings().firms_map_key

    def _url(self, source: str, bbox: str, days: int) -> str:
        return f"{API_ROOT}/area/csv/{self.map_key}/{source}/{bbox}/{int(days)}"

    def hotspots_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        *,
        days: int = 1,
        source: str = "VIIRS_SNPP_NRT",
    ) -> tuple[list[dict[str, Any]], ProviderResult]:
        """Fire detections in a bbox for the last ``days`` days (1-7 for NRT sources)."""
        if source not in KNOWN_SOURCES:
            raise ProviderUnavailableError(
                self.key,
                f"Unknown FIRMS source {source!r}. Known sources: {', '.join(sorted(KNOWN_SOURCES))}.",
            )
        days = max(1, min(int(days), 7))
        bbox = f"{west},{south},{east},{north}"
        text, result = self.get_text(self._url(source, bbox, days), cache_ttl=300)
        # The key travels in the URL; provenance must never repeat it.
        result.endpoint = f"{API_ROOT}/area/csv/<MAP_KEY>/{source}/{bbox}/{days}"
        rows = _parse_csv(text)
        result.notes = {
            "source": source,
            "source_description": KNOWN_SOURCES[source],
            "bbox": bbox,
            "days": days,
            "row_count": len(rows),
        }
        return rows, result

    def hotspots_over_india(self, *, days: int = 1, source: str = "VIIRS_SNPP_NRT") -> tuple[list[dict[str, Any]], ProviderResult]:
        west, south, east, north = (float(v) for v in INDIA_BBOX.split(","))
        return self.hotspots_bbox(west, south, east, north, days=days, source=source)

    def healthcheck(self) -> ProviderStatus:
        if not self.map_key:
            return ProviderStatus(
                self.key,
                "unconfigured",
                "FIRMS_MAP_KEY is not set; active-fire layer unavailable (no synthetic fallback).",
                True,
                self.base_url,
            )
        try:
            rows, result = self.hotspots_bbox(76.5, 8.5, 77.5, 9.5, days=1)
            return ProviderStatus(
                self.key,
                "healthy",
                f"probe returned {len(rows)} detections",
                True,
                result.endpoint,
                datetime.now(timezone.utc).isoformat(),
                result.elapsed_ms,
                result.http_status,
            )
        except ProviderAuthError as exc:
            return ProviderStatus(self.key, "unconfigured", exc.message, True, self.base_url)
        except ProviderUnavailableError as exc:
            rejected = exc.details.get("provider_status") in {400, 401, 403}
            state = "unconfigured" if rejected else "unavailable"
            detail = (
                "FIRMS rejected the configured MAP_KEY (the retired anonymous key returns "
                '"Invalid MAP_KEY."). Request a free key at firms.modaps.eosdis.nasa.gov/mapkey/.'
                if rejected
                else exc.message
            )
            return ProviderStatus(self.key, state, detail, True, self.base_url)
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", True, self.base_url)


def _parse_csv(text: str) -> list[dict[str, Any]]:
    """FIRMS ships CSV, not JSON. Blank/`No data` bodies parse to an empty list."""
    stripped = text.strip()
    if not stripped or "," not in stripped.splitlines()[0]:
        return []
    reader = csv.DictReader(io.StringIO(stripped))
    rows: list[dict[str, Any]] = []
    for raw in reader:
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
        for key in ("latitude", "longitude", "brightness_ti4", "confidence", "frp", "scan", "track"):
            if row.get(key):
                try:
                    row[key] = float(row[key])
                except ValueError:
                    pass
        if row.get("latitude") is not None:
            rows.append(row)
    return rows


__all__ = ["FirmsProvider", "KNOWN_SOURCES", "INDIA_BBOX"]
