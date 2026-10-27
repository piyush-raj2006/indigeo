"""Local-first lifeline lookup with the Overpass API as the fallback.

The order is deliberate: a regional OSM extract answers in milliseconds and cannot return
a 502, while the public Overpass instances measured 504-624 s for the same four layers and
dropped layers under load. So the engine reads the local extract for every layer it can,
and asks Overpass only for the layers the local data could not answer - a bbox outside
every extract, an unreadable file, or a layer the extract does not carry.

The merged result keeps one statement per layer: where it came from (``origin``), whether
it answered (``status``), and the reason when it did not. Nothing is backfilled across
origins: a layer that neither source answered stays unavailable, and the caller turns that
into a confidence penalty rather than a zero-feature finding.
"""

from __future__ import annotations

import time
from typing import Any

from ..core.config import get_settings
from ..core.logging import get_logger

log = get_logger("services.lifelines")

LIFELINE_LAYERS: tuple[str, ...] = ("hospitals", "emergency_services", "bridges", "water_structures")


def lifelines_near(
    lon: float,
    lat: float,
    radius_m: float,
    *,
    layers: tuple[str, ...] = LIFELINE_LAYERS,
    local: Any | None = None,
    overpass: Any,
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    """Return lifelines for ``layers``, preferring ``local`` and falling back per layer.

    ``local`` and ``overpass`` are provider objects with an ``infrastructure_near`` method.
    They are passed in rather than looked up here so the evidence layer keeps a single
    provider-resolution point (and the existing tests can stub it).
    """
    settings = get_settings()
    budget = settings.overpass_deadline_seconds if deadline_seconds is None else deadline_seconds
    started = time.monotonic()
    out: dict[str, Any] = {
        "layers": {},
        "provenance": [],
        "warnings": [],
        "deadline_seconds": budget,
        "local": None,
        "origins": {},
    }
    pending: list[str] = list(layers)

    local_result: dict[str, Any] | None = None
    has_local = local is not None and getattr(local, "extract_count", 0) > 0
    if has_local:
        try:
            local_result = local.infrastructure_near(lon, lat, radius_m, layers=tuple(layers))
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            out["warnings"].append(f"Local OSM extract lookup failed ({reason}); falling back to Overpass.")
            local_result = None
        if local_result is not None:
            out["provenance"].extend(local_result.get("provenance") or [])
            out["warnings"].extend(local_result.get("warnings") or [])
            out["local"] = {
                "status": local_result.get("local_status"),
                "extracts": local_result.get("extracts") or [],
                "query_bbox": local_result.get("query_bbox"),
                "reason": (local_result.get("warnings") or [None])[0],
            }
            for name in layers:
                entry = (local_result.get("layers") or {}).get(name)
                if entry is None:
                    continue
                if entry.get("status") == "ok":
                    out["layers"][name] = entry
                else:
                    # Keep the local reason for the fallback to inherit if Overpass also
                    # cannot answer; do not expose a failed local read as the layer result.
                    out.setdefault("_local_misses", {})[name] = entry
            pending = [name for name in layers if name not in out["layers"]]
    else:
        out["local"] = {
            "status": "skipped",
            "reason": "No readable local extract is configured; every layer uses Overpass.",
        }

    remote_used = False
    if pending:
        remaining = None
        if budget and budget > 0:
            remaining = max(1.0, budget - (time.monotonic() - started))
        try:
            remote = overpass.infrastructure_near(
                lon,
                lat,
                radius_m,
                layers=tuple(pending),
                deadline_seconds=remaining,
            )
            remote_used = True
            out["provenance"].extend(remote.get("provenance") or [])
            out["warnings"].extend(remote.get("warnings") or [])
            for name in pending:
                entry = (remote.get("layers") or {}).get(name)
                if entry is not None:
                    entry.setdefault("origin", "overpass")
                    out["layers"][name] = entry
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            out["warnings"].append(
                f"Overpass fallback failed for {len(pending)} layer(s) ({reason}); those layers are gaps, not zeros."
            )
            local_misses = out.pop("_local_misses", {})
            for name in pending:
                miss = (local_misses or {}).get(name)
                if miss is not None:
                    entry = dict(miss)
                    entry["status"] = "unavailable"
                    entry["reason"] = (
                        f"local: {miss.get('reason', 'layer unavailable')} | overpass: {reason}"
                    )
                else:
                    entry = {
                        "features": [],
                        "status": "unavailable",
                        "reason": f"overpass fallback failed: {reason}",
                        "origin": "overpass",
                    }
                out["layers"][name] = entry

    out.pop("_local_misses", None)
    # Final layer order follows the request, so callers and stored records stay stable.
    ordered = {name: out["layers"][name] for name in layers if name in out["layers"]}
    out["layers"] = ordered
    out["origin_states"] = origin_states(out)
    out["origins"] = {
        origin: sum(1 for entry in ordered.values() if (entry.get("origin") or "overpass") == origin)
        for origin in {entry.get("origin") or "overpass" for entry in ordered.values()}
    }
    out["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    out["remote_used"] = remote_used
    if remote_used:
        out["attribution"] = getattr(overpass, "attribution", None)
        out["license"] = getattr(overpass, "license", None)
    elif has_local:
        out["attribution"] = getattr(local, "attribution", None)
        out["license"] = getattr(local, "license", None)
    else:
        out["attribution"] = getattr(overpass, "attribution", None)
        out["license"] = getattr(overpass, "license", None)
    return out


def origin_states(infrastructure: dict[str, Any]) -> dict[str, str]:
    """Per-origin state: available only when every layer that origin served answered.

    A result without ``origin`` is treated as Overpass, which is what every layer meant
    before local extracts existed, so stored records and older callers keep their meaning.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in (infrastructure.get("layers") or {}).values():
        origin = entry.get("origin") or "overpass"
        groups.setdefault(origin, []).append(entry)
    states: dict[str, str] = {}
    for origin, entries in groups.items():
        answered = sum(1 for entry in entries if entry.get("status") == "ok")
        if answered == len(entries):
            states[origin] = "available"
        elif answered:
            states[origin] = "partial"
        else:
            states[origin] = "unavailable"
    return states


__all__ = ["LIFELINE_LAYERS", "lifelines_near", "origin_states"]
