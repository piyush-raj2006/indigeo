"""Gather the three live Indian hazard feeds into one evidence block.

The feeds are independent and each can be configured, unconfigured or failing on its own.
This module turns all three outcomes into one shape the assessors can read, and records a
single honest warning when none of them is configured, instead of repeating that fact in
four hazard warnings.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ProviderAuthError
from ..core.logging import get_logger
from ..integrations.base import get_provider

log = get_logger("services.live_alerts")

# (bundle key, provider key, environment variable that enables it, feed label)
FEEDS: tuple[tuple[str, str, str, str], ...] = (
    ("imd", "imd_alerts", "IMD_ALERTS_URL", "IMD warnings"),
    ("cwc", "cwc_gauges", "CWC_GAUGES_URL", "CWC river gauges"),
    ("bhuvan", "bhuvan_hazards", "BHUVAN_HAZARDS_URL", "Bhuvan/NDMA hazard maps"),
)


def gather_live_alerts(
    lon: float,
    lat: float,
    radius_m: float,
    *,
    admin: dict[str, Any] | None = None,
    allow_remote: bool = True,
) -> dict[str, Any]:
    """Query every configured feed and keep a per-feed status even when one fails."""
    bundle: dict[str, Any] = {"feeds": {}, "provenance": [], "warnings": []}
    for key, provider_key, env_var, label in FEEDS:
        provider = get_provider(provider_key)
        entry: dict[str, Any] = {"provider": provider_key, "env_var": env_var, "label": label}
        if provider.auth_problem() is not None:
            entry.update(status="unconfigured", reason=provider.auth_problem())
            bundle["feeds"][key] = entry
            continue
        if not allow_remote:
            entry.update(
                status="unavailable",
                reason="Remote access was disabled for this run, so the configured feed was not queried.",
            )
            bundle["feeds"][key] = entry
            continue
        try:
            payload = provider.feed_near(lon, lat, radius_m, admin=admin)
        except ProviderAuthError as exc:
            entry.update(status="unconfigured", reason=exc.message)
        except Exception as exc:
            entry.update(status="unavailable", reason=f"{type(exc).__name__}: {exc}")
        else:
            entry.update(status="available", **payload)
            if payload.get("provenance"):
                bundle["provenance"].append(payload["provenance"])
            bundle["warnings"].extend(payload.get("warnings") or [])
        bundle["feeds"][key] = entry

    unconfigured = [entry["label"] for entry in bundle["feeds"].values() if entry["status"] == "unconfigured"]
    unavailable = [entry["label"] for entry in bundle["feeds"].values() if entry["status"] == "unavailable"]
    bundle["configured"] = len(unconfigured) < len(FEEDS)
    bundle["unconfigured"] = unconfigured
    bundle["unavailable"] = unavailable
    if not bundle["configured"]:
        names = ", ".join(env for _, _, env, _ in FEEDS)
        bundle["warnings"].append(
            "No live Indian hazard feed is configured (" + names + "); no IMD, CWC or Bhuvan "
            "alert was blended into the hazard scores. This is a data gap, not a finding of no alerts."
        )
    elif unavailable:
        bundle["warnings"].append(
            "Configured live feed(s) did not answer: " + ", ".join(unavailable) + ". Their factors are "
            "reported unavailable rather than scored as zero."
        )
    return bundle


__all__ = ["FEEDS", "gather_live_alerts"]
