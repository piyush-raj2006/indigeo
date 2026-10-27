from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from ..core.config import get_settings
from ..core.errors import ProviderUnavailableError
from ..core.logging import get_logger
from .base import Provider, ProviderResult, ProviderStatus

log = get_logger("integrations.osm_infra")

# HTTP statuses that mean "this query is wrong", not "this server is busy".
_QUERY_REJECTIONS = {400, 406, 413, 422}

# What each selector is used for in the hazard modules. ``selectors`` is a list because
# Overpass QL needs one statement per element type inside a union: a concatenated
# 'node[...]way[...]' is a syntax error that every mirror rejects with HTTP 400.
INFRA_LAYERS: dict[str, dict[str, Any]] = {
    "bridges": {"selectors": ['way["highway"]["bridge"]'], "kind": "way", "role": "lifeline / crossing"},
    "major_roads": {
        "selectors": ['way["highway"~"^(motorway|trunk|primary)$"]'],
        "kind": "way",
        "role": "evacuation network",
    },
    # `nwr`, not `node`: hospitals and schools are routinely mapped as areas (a closed way
    # or a multipolygon), and a node-only selector reports those neighbourhoods as having
    # none. The local-extract reader matches the same geometry set, so both origins answer
    # the same question.
    "hospitals": {"selectors": ['nwr["amenity"="hospital"]'], "kind": "any", "role": "medical response"},
    "emergency_services": {
        # `emergency=ambulance_station` is the current tag; `ambulance_service` is kept as a
        # deprecated alias. `amenity=fire_station` / `amenity=police` are deprecated but
        # still widespread (verified in a 2026-10 Monaco extract: all 2 fire stations and
        # all 6 police stations used the amenity form), so omitting them undercounts.
        "selectors": [
            'nwr["emergency"~"^(ambulance_station|ambulance_service|fire_station|police)$"]',
            'nwr["amenity"~"^(fire_station|police)$"]',
        ],
        "kind": "any",
        "role": "first responders",
    },
    "schools": {"selectors": ['nwr["amenity"~"^(school|college|kindergarten)$"]'], "kind": "any", "role": "shelter candidates"},
    "water_structures": {
        "selectors": ['nwr["man_made"~"^(reservoir_covered|water_tower|dam)$"]', 'nwr["waterway"="dam"]'],
        "kind": "mixed",
        "role": "flood exposure",
    },
}


class OverpassProvider(Provider):
    """OpenStreetMap infrastructure lookups through the Overpass API.

    Overpass instances are heavily loaded, so a query walks the configured mirror
    list and records which mirror actually served it. A total failure is reported
    as unavailable - never backfilled with invented features.
    """

    key = "osm_overpass"
    base_url = "https://overpass-api.de/api/interpreter"
    license = "ODbL 1.0 (OpenStreetMap contributors)"
    attribution = "Map data (c) OpenStreetMap contributors, ODbL - https://osm.org/copyright"
    requires_auth = False
    min_interval_seconds = get_settings().overpass_min_interval_seconds
    cacheable = True

    def __init__(self) -> None:
        super().__init__()
        self.mirrors: list[str] = list(get_settings().overpass_mirrors) or [self.base_url]
        self.last_mirror: str | None = None

    # -- query plumbing ---------------------------------------------------
    def _statement(self, body: str, *, timeout: int, out: str) -> str:
        return f"[out:json][timeout:{max(5, min(int(timeout), 60))}];{body};{out};"

    def _cache_lookup(self, query: str) -> ProviderResult | None:
        path = self._cache_path("overpass-query", {"q": query})
        ttl = self._settings.provider_cache_ttl_seconds
        if ttl <= 0 or not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age >= ttl:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
            return None
        return ProviderResult(
            provider=self.key,
            endpoint=f"cache://{path.name}",
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            payload=payload,
            http_status=200,
            elapsed_ms=0.0,
            from_cache=True,
            license=self.license,
            attribution=self.attribution,
            notes={"mirror": "disk-cache", "cache_age_seconds": round(age)},
        )

    def _cache_store(self, query: str, payload: Any) -> None:
        if self._settings.provider_cache_ttl_seconds <= 0:
            return
        path = self._cache_path("overpass-query", {"q": query})
        try:
            path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        except OSError:
            pass

    def run_query(self, query: str, *, cache: bool = True, deadline: float | None = None) -> ProviderResult:
        """POST an Overpass QL statement, failing over across mirrors.

        ``deadline`` is an absolute ``time.monotonic()`` instant. With one set, the walk
        stops before starting a mirror it cannot finish, and each request gets a single
        attempt capped at the remaining budget: inside a fixed budget the next mirror is
        worth more than a second try at the same one.
        """
        if cache:
            hit = self._cache_lookup(query)
            if hit is not None:
                self.last_status = ProviderStatus(self.key, "healthy", "served from cache", False, hit.endpoint)
                return hit

        content = f"data={_urlencode(query)}".encode("utf-8")
        headers = {
            "User-Agent": self._settings.nominatim_user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        failures: list[str] = []
        for mirror in self.mirrors:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 1.0:
                failures.append(f"{_mirror_host(mirror)}: not tried (budget exhausted)")
                break
            bounded: dict[str, Any] = {}
            if remaining is not None:
                bounded = {"timeout": max(1.0, min(self._settings.http_timeout_seconds, remaining)), "attempts": 1}
            try:
                result = self.get_json(
                    mirror, content=content, method="POST", cache_ttl=0, headers=headers, **bounded
                )
            except ProviderUnavailableError as exc:
                rejected = exc.details.get("provider_status") in _QUERY_REJECTIONS
                failures.append(f"{_mirror_host(mirror)}: {exc.message}")
                if rejected:
                    # A rejected query is a defect in this code, not an outage: every mirror
                    # parses the same QL, so failing over only burns minutes and mislabels the
                    # failure as "temporarily unavailable".
                    self.last_status = ProviderStatus(
                        self.key,
                        "degraded",
                        f"Overpass rejected the query syntax ({_mirror_host(mirror)}): {exc.message}",
                        False,
                        mirror,
                    )
                    raise ProviderUnavailableError(
                        self.key,
                        "OpenStreetMap query was rejected as invalid Overpass QL "
                        f"by {_mirror_host(mirror)}; the query was not retried on other mirrors. "
                        f"Query: {query[:200]}",
                        status_code=exc.details.get("provider_status"),
                    ) from exc
                continue
            except Exception as exc:  # timeouts and connection resets move to the next mirror
                failures.append(f"{_mirror_host(mirror)}: {type(exc).__name__}")
                continue
            payload = result.payload
            if not isinstance(payload, dict) or "elements" not in payload:
                failures.append(f"{_mirror_host(mirror)}: response had no 'elements' array")
                continue
            self.last_mirror = mirror
            result.notes = {
                "mirror": _mirror_host(mirror),
                "osm_base": (payload.get("osm3s") or {}).get("timestamp_osm_base"),
                "element_count": len(payload.get("elements", [])),
                "mirrors_tried": [
                    _mirror_host(m) for m in self.mirrors[: self.mirrors.index(mirror) + 1]
                ],
                "query": query,
            }
            if failures:
                result.notes["mirror_failures"] = failures
            self._cache_store(query, payload)
            return result

        self.last_status = ProviderStatus(
            self.key,
            "unavailable",
            "Every Overpass mirror refused or timed out: " + " | ".join(failures),
            False,
            self.mirrors[0],
        )
        budget_note = (
            " The wall-clock budget for this lookup was exhausted."
            if deadline is not None and time.monotonic() >= deadline
            else ""
        )
        raise ProviderUnavailableError(
            self.key,
            "OpenStreetMap infrastructure data is temporarily unavailable ("
            + "; ".join(failures[:3])
            + f").{budget_note} Nothing was substituted for it.",
        )

    # -- domain queries ---------------------------------------------------
    def bbox_of(self, lon: float, lat: float, radius_m: float) -> str:
        from ..gis.coords import meters_to_degrees

        dlon, dlat = meters_to_degrees(lat, radius_m)
        return f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"

    def infrastructure_near(
        self,
        lon: float,
        lat: float,
        radius_m: float,
        *,
        layers: tuple[str, ...] = ("bridges", "hospitals", "emergency_services"),
        limit: int = 500,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Lifeline features within a radius, grouped by layer with provenance.

        The layers are queried concurrently and share one wall-clock budget, because the
        serial walk measured 504-624 s (four layers x four mirrors x retries). A layer that
        does not answer inside the budget is reported unavailable with the elapsed time -
        never as "no features here".
        """
        settings = get_settings()
        budget = settings.overpass_deadline_seconds if deadline_seconds is None else deadline_seconds
        started = time.monotonic()
        deadline = started + budget if budget and budget > 0 else None
        area = self.bbox_of(lon, lat, radius_m)
        out: dict[str, Any] = {"layers": {}, "provenance": [], "warnings": []}

        ready: list[tuple[str, dict[str, Any]]] = []
        for name in layers:
            spec = INFRA_LAYERS.get(name)
            if not spec:
                out["warnings"].append(f"Unknown infrastructure layer {name!r}; skipped.")
                out["layers"][name] = {
                    "features": [],
                    "status": "unavailable",
                    "reason": "unknown layer",
                    "origin": "overpass",
                }
                continue
            ready.append((name, spec))

        def fetch(name: str, spec: dict[str, Any]) -> ProviderResult:
            body = "(" + "".join(f"{selector}({area});" for selector in spec["selectors"]) + ")"
            query = self._statement(body, timeout=25, out=f"out center tags {int(limit)}")
            return self.run_query(query, deadline=deadline)

        results: dict[str, ProviderResult] = {}
        failures: dict[str, str] = {}
        unfinished: set[str] = set()
        if ready:
            workers = max(1, min(int(settings.overpass_layer_concurrency), len(ready)))
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="overpass-layer")
            try:
                futures = {pool.submit(fetch, name, spec): name for name, spec in ready}
                if deadline is None:
                    done, pending = wait(futures)
                else:
                    done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
                unfinished = {futures[future] for future in pending}
                for future in done:
                    name = futures[future]
                    try:
                        results[name] = future.result()
                    except ProviderUnavailableError as exc:
                        failures[name] = exc.message
                    except Exception as exc:
                        failures[name] = f"{type(exc).__name__}: {exc}"
            finally:
                # A blocked HTTP call cannot be interrupted; returning at the deadline
                # matters more than joining these threads.
                pool.shutdown(wait=False, cancel_futures=True)

        for name, spec in ready:
            if name in unfinished:
                reason = (
                    f"did not answer inside the {budget:g} s budget "
                    f"({round((time.monotonic() - started) * 1000.0)} ms elapsed)"
                )
                out["warnings"].append(f"{name}: {reason}. Reported unavailable, not as zero features.")
                out["layers"][name] = {
                    "features": [],
                    "status": "unavailable",
                    "reason": reason,
                    "role": spec["role"],
                    "origin": "overpass",
                }
                continue
            if name in failures:
                out["warnings"].append(f"{name}: {failures[name]}")
                out["layers"][name] = {
                    "features": [],
                    "status": "unavailable",
                    "reason": failures[name],
                    "role": spec["role"],
                    "origin": "overpass",
                }
                continue
            result = results.get(name)
            if result is None:  # defensive: a future neither finished nor reported is still a gap
                out["warnings"].append(f"{name}: no result was returned; reported unavailable.")
                out["layers"][name] = {
                    "features": [],
                    "status": "unavailable",
                    "role": spec["role"],
                    "origin": "overpass",
                }
                continue
            features = [_normalise(el) for el in result.payload.get("elements", [])]
            features = [f for f in features if f]
            out["layers"][name] = {
                "features": features,
                "count": len(features),
                "truncated": len(features) >= int(limit),
                "role": spec["role"],
                "status": "ok",
                "origin": "overpass",
            }
            out["provenance"].append(
                {**result.provenance(), "layer": name, "radius_m": radius_m, "notes": result.notes}
            )
        out["attribution"] = self.attribution
        out["license"] = self.license
        out["deadline_seconds"] = budget
        out["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        if unfinished:
            out["warnings"].append(
                "Some layers were cut off at the budget; the request returned without waiting for them."
            )
        return out

    def roads_in_bbox(self, west: float, south: float, east: float, north: float, *, limit: int = 20_000, timeout: int = 55) -> ProviderResult:
        area = f"{south},{west},{north},{east}"
        body = 'way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"]'
        query = self._statement(f"({body}({area});)", timeout=timeout, out=f"out geom tags {int(limit)}")
        return self.run_query(query)

    def healthcheck(self) -> ProviderStatus:
        query = self._statement('node["amenity"="hospital"](12.9,77.5,13.0,77.6)', timeout=10, out="out center tags 3")
        try:
            result = self.run_query(query, cache=False)
            return ProviderStatus(
                self.key,
                "healthy",
                f"mirror {result.notes.get('mirror')} answered with {result.notes.get('element_count')} features",
                False,
                result.endpoint,
                datetime.now(timezone.utc).isoformat(),
                result.elapsed_ms,
                result.http_status,
            )
        except ProviderUnavailableError as exc:
            return ProviderStatus(self.key, "unavailable", exc.message, False, self.mirrors[0])
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", False, self.mirrors[0])


def _normalise(element: dict[str, Any]) -> dict[str, Any] | None:
    tags = element.get("tags") or {}
    lon = element.get("lon")
    lat = element.get("lat")
    if lon is None or lat is None:
        center = element.get("center") or {}
        lon, lat = center.get("lon"), center.get("lat")
    if lon is None or lat is None:
        geometry = _line_geometry(element.get("geometry"))
        if geometry is None:
            return None
        return {
            "osm_id": f"{element.get('type')}/{element.get('id')}",
            "kind": element.get("type"),
            "name": tags.get("name"),
            "tags": tags,
            "geometry": geometry,
        }
    return {
        "osm_id": f"{element.get('type')}/{element.get('id')}",
        "kind": element.get("type"),
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "name": tags.get("name"),
        "tags": tags,
    }


def _line_geometry(geometry: Any) -> dict[str, Any] | None:
    if not isinstance(geometry, list):
        return None
    coords = [[round(float(pt["lon"]), 6), round(float(pt["lat"]), 6)] for pt in geometry if isinstance(pt, dict)]
    if len(coords) < 2:
        return None
    return {"type": "LineString", "coordinates": coords}


def _urlencode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _mirror_host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


__all__ = ["OverpassProvider", "INFRA_LAYERS"]
