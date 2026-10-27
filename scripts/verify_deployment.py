#!/usr/bin/env python3
"""Pre-deployment checklist for IndiGeo.

Checks, in order: configuration, API reachability and health, the async job routes, CORS
preflight, TLS material, the frontend build, and the spatial layers (presence, extent and
index state). It only reads what a deployed instance answers.

Deliberately out of scope: probing data providers. A provider being unreachable is a gap
that a report states and lowers confidence for; it is not a deployment failure. Probe those
separately with ``scripts/check_providers.py`` or ``GET /api/v1/providers?refresh=true``.

    python scripts/verify_deployment.py \
        --api-base https://api.example.org/api/v1 \
        --frontend-origin https://app.example.org

Exit code is 1 if any check FAILs, 0 otherwise (WARNs are allowed through).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

PASS, WARN, FAIL, NOTE = "PASS", "WARN", "FAIL", "NOTE"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class Checklist:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def _add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))
        line = f"{level:4}  {name}"
        print(f"{line}  {detail}" if detail else line)

    def ok(self, name: str, detail: str = "") -> None:
        self._add(PASS, name, detail)

    def warn(self, name: str, detail: str = "") -> None:
        self._add(WARN, name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self._add(FAIL, name, detail)

    def note(self, name: str, detail: str = "") -> None:
        self._add(NOTE, name, detail)

    def failures(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[0] == FAIL]

    def warnings(self) -> list[tuple[str, str, str]]:
        return [row for row in self.rows if row[0] == WARN]


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; no interpolation, no export handling beyond a leading one."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def http(method: str, url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0):
    """Return (status, headers, body-bytes). Raises ConnectionError on transport failure."""
    req = request.Request(url, method=method, headers=headers or {})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except error.HTTPError as exc:  # a 4xx/5xx answer is still an answer
        return exc.code, dict(exc.headers or {}), exc.read()
    except (error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectionError(str(reason)) from exc


def cors_origins(values: dict[str, str]) -> list[str]:
    raw = values.get("CORS_ORIGINS", "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in raw.split(",") if part.strip()]


def is_loopback(origin: str) -> bool:
    return (urlsplit(origin).hostname or "").lower() in LOOPBACK_HOSTS


def tls_days_left(host: str, port: int) -> tuple[int, str]:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            certificate = tls.getpeercert()
    not_after = certificate.get("notAfter", "")
    expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (expiry - datetime.now(timezone.utc)).days, expiry.isoformat()


def check_configuration(check: Checklist, values: dict[str, str], api_base: str) -> None:
    environment = values.get("ENVIRONMENT", "development")
    production = environment in {"production", "staging"}
    if "ENVIRONMENT" in values:
        check.ok("environment", f"ENVIRONMENT={environment}")
    else:
        check.warn("environment", "ENVIRONMENT not set; the API falls back to development and logs no production warning")

    origins = cors_origins(values)
    if not origins:
        check.fail(
            "CORS origins configured",
            "CORS_ORIGINS is empty; every browser call from another origin will be blocked",
        )
    elif "*" in origins:
        check.warn(
            "CORS origins configured",
            "CORS_ORIGINS contains '*' - accepted only because credentials are disabled; list real origins",
        )
    elif production and all(is_loopback(origin) for origin in origins):
        check.fail(
            "CORS origins allow the deployed frontend",
            f"environment={environment} but CORS_ORIGINS only lists {', '.join(origins)}; "
            "set it to the public frontend origin",
        )
    else:
        check.ok("CORS origins configured", ", ".join(origins))

    public_api = values.get("NEXT_PUBLIC_API_BASE", "").strip()
    if not public_api:
        check.fail(
            "NEXT_PUBLIC_API_BASE configured",
            "unset: the bundle falls back to the loopback dev endpoint",
        )
    elif production and is_loopback(public_api):
        check.fail(
            "NEXT_PUBLIC_API_BASE is reachable by browsers",
            f"{public_api} is loopback while environment={environment}; remote browsers will call their own machine",
        )
    else:
        check.ok("NEXT_PUBLIC_API_BASE configured", public_api)

    agent = values.get("NOMINATIM_USER_AGENT", "").strip()
    if not agent or "set a real contact" in agent:
        check.warn(
            "Nominatim user agent",
            "NOMINATIM_USER_AGENT has no real contact; the usage policy requires one",
        )
    else:
        check.ok("Nominatim user agent", agent)

    if not values.get("FIRMS_MAP_KEY", "").strip():
        check.warn(
            "Fire detections (optional)",
            "FIRMS_MAP_KEY is empty; fire will be reported as a gap, which is honest but loses a hazard",
        )
    else:
        check.ok("Fire detections (optional)", "FIRMS_MAP_KEY is set (value not printed)")

    if production and values.get("CACHE_ENABLED", "true").strip().lower() in {"false", "0", "no"}:
        check.warn(
            "Location cache",
            "CACHE_ENABLED=false in production; every search and point resolve hits Nominatim again",
        )

    check.note("API endpoint under test", api_base)


def check_health(check: Checklist, api_base: str, values: dict[str, str], timeout: float) -> dict | None:
    try:
        status, _, body = http("GET", f"{api_base}/health", timeout=timeout)
    except ConnectionError as exc:
        check.fail(
            "API reachable",
            f"{exc}. Check that the container/process is up, the port is published and the "
            "reverse proxy passes /api/v1 through",
        )
        return None

    if status != 200:
        check.fail("API reachable", f"GET /health answered {status}: {body[:200].decode(errors='replace')}")
        return None
    check.ok("API reachable", "GET /health answered 200")

    try:
        health = json.loads(body)
    except json.JSONDecodeError:
        check.fail("API health payload", "not JSON; something other than the API answered")
        return None

    database = health.get("database") or {}
    if database.get("ok"):
        check.ok("Relational store", f"dialect={database.get('dialect')}, probe={database.get('probe')}")
    else:
        check.fail("Relational store", f"probe failed: {database.get('probe')}")

    spatial = health.get("spatial") or {}
    backend = spatial.get("backend")
    if backend == "duckdb":
        check.ok("Spatial backend", "duckdb (local file; PostGIS is not implemented in this build)")
    elif backend:
        check.fail("Spatial backend", f"{backend} is selected but spatial_postgis.py does not exist; set SPATIAL_BACKEND=duckdb")
    else:
        check.fail("Spatial backend", "health did not report a spatial backend")

    expected_environment = values.get("ENVIRONMENT")
    if expected_environment and health.get("environment") != expected_environment:
        check.warn(
            "API environment",
            f"API reports {health.get('environment')!r} but the env file says {expected_environment!r}; "
            "the running process may predate the current .env",
        )

    return health


def check_jobs_route(check: Checklist, api_base: str, timeout: float) -> None:
    try:
        status, _, body = http("GET", f"{api_base}/analysis/jobs?limit=1", timeout=timeout)
    except ConnectionError as exc:
        check.fail("Async job routes", f"could not reach the API: {exc}")
        return
    if status == 200 and b'"jobs"' in body:
        check.ok("Async job routes", "GET /analysis/jobs is present (this build has the job queue)")
    elif status in {404, 405}:
        check.fail(
            "Async job routes",
            f"GET /analysis/jobs answered {status}: the running image predates the job queue. Rebuild and redeploy",
        )
    else:
        check.fail("Async job routes", f"GET /analysis/jobs answered {status}: {body[:200].decode(errors='replace')}")


def check_layers(check: Checklist, health: dict) -> None:
    spatial = health.get("spatial") or {}
    layers: dict[str, int] = spatial.get("layers") or {}
    extents: dict[str, list[float] | None] = spatial.get("extents") or {}
    indexed: dict[str, bool | None] = spatial.get("indexed") or {}

    if not layers:
        check.fail(
            "Spatial layers loaded",
            "zero layers: boundary matching and the coastline distance will be reported as gaps. "
            "Seed /data with scripts/run_ingest.py boundaries",
        )
        return
    summary = ", ".join(f"{name}={count}" for name, count in sorted(layers.items()))
    check.ok("Spatial layers loaded", summary)

    missing_index = [name for name, flag in indexed.items() if flag is False]
    unknown_index = [name for name, flag in indexed.items() if flag is None]
    if missing_index:
        check.warn(
            "Spatial indexes",
            f"no RTREE index on {', '.join(missing_index)}; radius/bbox predicates fall back to a scan",
        )
    elif unknown_index:
        check.warn("Spatial indexes", f"index state unreadable for {', '.join(unknown_index)}")
    elif indexed:
        check.ok("Spatial indexes", f"RTREE present on all {len(indexed)} layers")

    outline = extents.get("india_outline")
    if not outline:
        if "india_outline" in layers:
            check.warn("National outline extent", "layer present but its extent is null")
        else:
            check.fail(
                "National outline extent",
                "india_outline is missing; coastline distance cannot be measured and is reported as a gap",
            )
    else:
        min_lon, min_lat, max_lon, max_lat = outline
        if 60 <= min_lon and max_lon <= 100 and 5 <= min_lat and max_lat <= 40:
            check.ok("National outline extent", f"lon {min_lon}..{max_lon}, lat {min_lat}..{max_lat}")
        else:
            check.warn(
                "National outline extent",
                f"extent lon {min_lon}..{max_lon}, lat {min_lat}..{max_lat} does not look like India",
            )


def check_cors(check: Checklist, api_base: str, frontend_origin: str, timeout: float) -> None:
    headers = {
        "Origin": frontend_origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "content-type",
    }
    try:
        status, response_headers, body = http("OPTIONS", f"{api_base}/health", headers=headers, timeout=timeout)
    except ConnectionError as exc:
        check.fail("CORS preflight", f"could not reach the API: {exc}")
        return

    allow_origin = response_headers.get("Access-Control-Allow-Origin") or response_headers.get(
        "access-control-allow-origin"
    )
    if allow_origin in {frontend_origin, "*"}:
        check.ok("CORS preflight", f"OPTIONS from {frontend_origin} allowed ({allow_origin})")
    else:
        check.fail(
            "CORS preflight",
            f"OPTIONS from {frontend_origin} answered {status} without Access-Control-Allow-Origin. "
            "Add that origin to CORS_ORIGINS and restart the API",
        )
        if body:
            check.note("preflight body", body[:160].decode(errors="replace"))

    allow_methods = response_headers.get("Access-Control-Allow-Methods") or response_headers.get(
        "access-control-allow-methods"
    )
    if allow_origin and allow_methods and "POST" not in allow_methods:
        check.warn("CORS methods", f"POST is not allowed: {allow_methods}")


def check_tls(check: Checklist, api_base: str, frontend_origin: str) -> None:
    for label, url in (("API", api_base), ("frontend", frontend_origin)):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            if host in LOOPBACK_HOSTS:
                check.warn(
                    f"{label} TLS",
                    f"{url} is plain HTTP on loopback - fine behind a local reverse proxy, never as the public URL",
                )
            else:
                check.fail(
                    f"{label} TLS",
                    f"{url} is public HTTP; cookies/keys and hazard reports would travel in clear text",
                )
            continue
        try:
            days, expiry = tls_days_left(host, parts.port or 443)
        except ssl.SSLCertVerificationError as exc:
            check.fail(f"{label} TLS", f"certificate verification failed for {host}: {exc.verify_message}")
            continue
        except (OSError, ValueError) as exc:
            check.fail(f"{label} TLS", f"could not inspect the certificate for {host}: {exc}")
            continue
        if days < 0:
            check.fail(f"{label} TLS", f"certificate for {host} expired on {expiry}")
        elif days < 14:
            check.warn(f"{label} TLS", f"certificate for {host} expires in {days} day(s) ({expiry})")
        else:
            check.ok(f"{label} TLS", f"valid certificate for {host}, {days} day(s) left")


CHUNK_SRC = re.compile(r'src="(/_next/[^"]+\.js)"')


def check_frontend(check: Checklist, frontend_origin: str, api_base: str, timeout: float) -> None:
    try:
        status, response_headers, body = http("GET", frontend_origin + "/", timeout=timeout)
    except ConnectionError as exc:
        check.fail("Frontend reachable", f"{exc}")
        return
    if status != 200:
        check.fail("Frontend reachable", f"GET / answered {status}")
        return
    content_type = response_headers.get("Content-Type") or response_headers.get("content-type") or ""
    if "text/html" not in content_type:
        check.warn("Frontend reachable", f"200 but Content-Type is {content_type!r}, not HTML")
    else:
        check.ok("Frontend reachable", frontend_origin)

    expected = urlsplit(api_base).netloc
    dev_default = "127.0.0.1:8123"
    html = body.decode("utf-8", errors="replace")
    documents = [("page", html)]

    chunk_paths = list(dict.fromkeys(CHUNK_SRC.findall(html)))[:25]
    for path in chunk_paths:
        try:
            chunk_status, _, chunk_body = http("GET", frontend_origin + path, timeout=timeout)
        except ConnectionError:
            continue
        if chunk_status == 200:
            documents.append((path, chunk_body.decode("utf-8", errors="replace")))

    expected_hits = [name for name, text in documents if expected and expected in text]
    stale_hits = [name for name, text in documents if dev_default in text]

    if expected_hits:
        check.ok("Bundle API origin", f"{expected} found in {expected_hits[0]}")
    else:
        check.warn(
            "Bundle API origin",
            f"could not find {expected} in the page or {len(documents) - 1} script chunk(s); "
            "confirm NEXT_PUBLIC_API_BASE was set at build time",
        )

    if stale_hits and expected != dev_default:
        check.fail(
            "Bundle has no dev endpoint",
            f"the served bundle still contains {dev_default} ({stale_hits[0]}); rebuild the frontend "
            "with NEXT_PUBLIC_API_BASE set to the public API origin",
        )
    else:
        check.ok("Bundle has no dev endpoint")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IndiGeo pre-deployment checklist")
    parser.add_argument("--api-base", help="Default: NEXT_PUBLIC_API_BASE from the env file")
    parser.add_argument("--frontend-origin", help="Default: the first non-loopback CORS origin")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="Default: <repo>/.env")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument("--skip-tls", action="store_true", help="Skip certificate inspection")
    parser.add_argument("--skip-frontend", action="store_true", help="Skip the frontend checks")
    args = parser.parse_args(argv)

    env_path = Path(args.env_file)
    values = read_env_file(env_path)
    for key in ("ENVIRONMENT", "CORS_ORIGINS", "NEXT_PUBLIC_API_BASE", "NOMINATIM_USER_AGENT", "FIRMS_MAP_KEY"):
        if not values.get(key) and os.environ.get(key):
            values[key] = os.environ[key]

    api_base = (args.api_base or values.get("NEXT_PUBLIC_API_BASE") or "http://127.0.0.1:8123/api/v1").rstrip("/")
    origins = cors_origins(values)
    frontend_origin = (
        args.frontend_origin
        or next((origin for origin in origins if not is_loopback(origin)), None)
        or (origins[0].rstrip("/") if origins else "http://127.0.0.1:3000")
    ).rstrip("/")

    print(f"IndiGeo deployment check - {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"env file: {env_path} ({'found' if env_path.is_file() else 'missing, using process environment'})")
    print()

    check = Checklist()
    check_configuration(check, values, api_base)

    print()
    health = check_health(check, api_base, values, args.timeout)
    if health is not None:
        check_jobs_route(check, api_base, args.timeout)
        check_layers(check, health)
        check_cors(check, api_base, frontend_origin, args.timeout)

    print()
    if not args.skip_tls:
        check_tls(check, api_base, frontend_origin)
    if not args.skip_frontend:
        check_frontend(check, frontend_origin, api_base, args.timeout)

    check.note(
        "Provider probing",
        "not part of this checklist: run scripts/check_providers.py or GET /providers?refresh=true separately",
    )

    failures, warnings = check.failures(), check.warnings()
    print()
    print(f"{len(check.rows) - len(failures) - len(warnings)} passed, {len(warnings)} warning(s), {len(failures)} failure(s)")
    if failures:
        print("Not deployable as configured:")
        for _, name, detail in failures:
            print(f"  - {name}: {detail}")
        return 1
    print("No blocking failures. Resolve the warnings before exposing this to users.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
