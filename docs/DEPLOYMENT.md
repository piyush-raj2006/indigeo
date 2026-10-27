# Deploying IndiGeo

Three things had to change before this stack could leave `localhost`:

1. **The API origin is configuration, not code.** `frontend/src/lib/api.ts` reads
   `NEXT_PUBLIC_API_BASE`, applies real timeouts, and turns transport, timeout, 504 and 429
   failures into user-facing messages instead of a spinner that never stops.
2. **Slow spatial work no longer blocks a request.** OSM layers run concurrently inside a
   wall-clock budget, and the lifeline path is submitted as a polled job
   (`POST /api/v1/analysis/jobs`).
3. **Deployment artifacts exist**: container images, a compose file, `.env.example`, and
   `scripts/verify_deployment.py` for the pre-deployment checklist.

What is *not* here, and is not pretended to be: PostGIS, Redis, or trained ML models. See
[Known limits](#known-limits).

---

## 1. Local development (no containers)

```bash
# API on 8123
cd backend
../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8123

# frontend on 3000 (second terminal)
cd frontend
npm install
npm run dev
```

The API reads `../.env` if it exists; without one it uses local defaults, including
`CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000`. Copy `.env.example` to `.env`
only when you need to override something.

Production-shaped build, still on the host:

```bash
cd frontend
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8123/api/v1 npm run build
npm start        # node .next/standalone/server.js — the same entrypoint the image runs
```

`next start` is not supported once `output: "standalone"` is set; `npm start` runs the
standalone server, and the `postbuild` step copies `public/` and `.next/static` into it.

`NEXT_PUBLIC_*` is **inlined at build time**. Changing the API origin means rebuilding the
frontend; restarting it does nothing.

---

## 2. Configuration: dev vs production

Values marked **PRODUCTION:** in `.env.example` are the ones that must differ.

| Setting | Development | Production |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` — makes the API warn if CORS still lists localhost |
| `CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | the exact public frontend origin, e.g. `https://app.example.org` |
| `NEXT_PUBLIC_API_BASE` | `http://127.0.0.1:8123/api/v1` | public API origin, e.g. `https://api.example.org/api/v1` |
| `API_BIND` / `WEB_BIND` (compose) | loopback | loopback behind a TLS reverse proxy |
| `NOMINATIM_USER_AGENT` | any string | a real contact, as the usage policy requires |
| `FIRMS_MAP_KEY` | empty (fire reported as a gap) | a free personal key if fire detections are wanted |

`CORS_ORIGINS`, `OVERPASS_MIRRORS` and `API_KEYS` accept **CSV or JSON**; both parse. The API
logs a warning at start-up when `ENVIRONMENT` is not local/development/test but every origin is
a localhost one, so a misconfigured deployment is visible in `docker compose logs api`.

A missing or failed source never becomes a value. `FIRMS_MAP_KEY` empty means fire is reported
as an unassessed gap with a warning, and the confidence drops; it does not mean "no fires".

---

## 3. Build and run with Docker

The images are written but **have not been built on the machine this repository was developed
on — Docker is not installed there.** Treat the commands below as the intended procedure, not
as something already demonstrated here.

```bash
cp .env.example .env
# edit ENVIRONMENT, CORS_ORIGINS, NEXT_PUBLIC_API_BASE, NOMINATIM_USER_AGENT

docker compose build          # NEXT_PUBLIC_API_BASE is a build arg for the web image
docker compose up -d
docker compose logs -f api
```

Production uses the checked-in template, which sets `ENVIRONMENT=production` and the
`ENV_FILE` the API container reads:

```bash
# 1. replace app.yourdomain.com / api.yourdomain.com in .env.production with real DNS names
# 2. build (NEXT_PUBLIC_API_BASE is baked in here) and start
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
docker compose --env-file .env.production logs -f api

# 3. optional TLS terminator (needs DNS for both hosts and ports 80/443 reachable)
docker compose --env-file .env.production --profile proxy up -d
```

The `proxy` profile runs Caddy with `deploy/Caddyfile`: `APP_HOST` proxies to `web:3000`
and `API_HOST` proxies to `api:8000`, with automatic certificates. Without the profile,
point your own reverse proxy at the loopback ports below. The web image build fails if
`NEXT_PUBLIC_API_BASE` is empty or a loopback URL, so a dev bundle cannot ship by accident.

Two services, because the data tier is embedded:

| Service | Port (host) | What it is |
|---|---|---|
| `api` | `127.0.0.1:8000` | uvicorn, **one worker**, state under the `/data` bind mount |
| `web` | `127.0.0.1:3000` | Next.js standalone server (`node server.js`) |

Why one API worker: DuckDB is a single-writer file and jobs run on an in-process thread pool.
A second uvicorn worker would open a competing writer and duplicate the job executor. Concurrency
inside the process comes from `ANALYSIS_WORKERS` (thread slots).

### Seed the data directory

`./data` is bind-mounted at `/data`. It must contain the spatial file built on the host:

```bash
.venv/Scripts/python.exe scripts/run_ingest.py boundaries
# -> data/indigeo_spatial.duckdb (district/state polygons, india_outline, OSM layer)
```

An empty `data/` is a valid but degraded boot: `/health` reports zero layers, and every report
says the coastline could not be measured instead of inventing a distance. The DEM under
`data/dem/` fills lazily as terrain tiles are fetched and cached.

The container runs as uid 10001, so a Linux host needs the bind mount writable by it:

```bash
sudo chown -R 10001:10001 data
```

Keep `data/` on a local disk. DuckDB and SQLite use POSIX locking that is unreliable over NFS
or SMB shares.

### Put TLS in front

Both ports bind to loopback on purpose. Terminate TLS at a reverse proxy; Caddy example:

```caddyfile
api.example.org {
    reverse_proxy 127.0.0.1:8000
}

app.example.org {
    reverse_proxy 127.0.0.1:3000
}
```

Separate hostnames keep the browser origin (`app.example.org`) and the API origin
(`api.example.org`) distinct, which is exactly what the `CORS_ORIGINS` allow-list is for.
If you insist on one hostname, you must also serve the API under it and change
`NEXT_PUBLIC_API_BASE` to that path — `CORS_ORIGINS` then lists that single origin.

---

## 4. Pre-deployment checklist

```bash
# local stack
.venv/Scripts/python.exe scripts/verify_deployment.py \
  --api-base http://127.0.0.1:8123/api/v1 \
  --frontend-origin http://127.0.0.1:3000

# deployed (reads the production config, then probes the live origins)
.venv/Scripts/python.exe scripts/verify_deployment.py \
  --env-file .env.production \
  --api-base https://api.yourdomain.com/api/v1 \
  --frontend-origin https://app.yourdomain.com
```

Exit code is 1 if anything FAILs. WARNs are allowed through but are printed.

What it checks, and why each one matters:

| Group | Check |
|---|---|
| Configuration | `CORS_ORIGINS` present and, in production, not localhost-only; `NEXT_PUBLIC_API_BASE` set and not loopback; Nominatim contact; optional FIRMS key; cache not disabled |
| API | `GET /health` answers 200; relational probe succeeds; spatial backend is `duckdb` |
| Job routes | `GET /analysis/jobs` exists — a 404 proves the running image predates the job queue |
| Layers | Layer counts; RTREE index presence per layer; `india_outline` exists and its extent looks like India |
| CORS | a real `OPTIONS` preflight from the frontend origin must echo `Access-Control-Allow-Origin` |
| TLS | certificate verifies, is not expired, has ≥14 days left; public plain-HTTP fails |
| Frontend | `GET /` returns HTML, the baked bundle contains the configured API host and no longer contains `127.0.0.1:8123` |

Provider probing is deliberately **not** part of this checklist: a provider outage is a reported
gap that lowers confidence, not a deployment failure. Use
`python scripts/check_providers.py` or `GET /api/v1/providers?refresh=true` for that, knowing
the Overpass part can take minutes.

---

## 5. What to expect at runtime

- **Locations** (`/locations/search`, `/locations/point`) are served from a per-process TTL+LRU
  cache (`CACHE_ENABLED`, `*_CACHE_TTL_SECONDS`, `*_CACHE_MAX_ENTRIES`). Hits say
  `cache: {hit: true, age_seconds: ...}`. Hazard evidence is never cached this way; provider
  responses live in the disk cache under `PROVIDER_CACHE_DIR`.
- **Lifelines** (`include_infrastructure=true`): four OSM layers queried concurrently under
  `OVERPASS_DEADLINE_SECONDS` (90 s default). A layer that misses the budget is reported
  `unavailable` with elapsed time — never as zero features. Serial mirror-walking measured
  504–624 s; the concurrent path measured ~91 s end to end for the same request.
- **Job queue**: `POST /analysis/jobs` answers 202 immediately; poll
  `GET /analysis/jobs/{uuid}`. Jobs left running by a killed process are marked `interrupted` at
  the next start-up, so a poller cannot wait forever.
- **UI**: when the served page talks to a loopback API from a non-loopback host, a banner says
  so. Timeouts and unreachable-API errors surface as toasts, not silent spinners.

---

## 6. Known limits

- **PostGIS is not implemented.** `backend/app/gis/spatial_postgis.py` does not exist, so
  `SPATIAL_BACKEND=postgis` fails at import. That is why the compose file has no database
  service; adding an idle Postgres container would advertise a backend this build does not use.
- **No Redis.** Caching is in-process, which is consistent with the single-worker design but
  resets on restart. Horizontal scale-out requires both PostGIS and an external cache/queue.
- **ML models are not trained yet.** Reports state `rule engine 1.0.0 — ML models used: none`.
- **Docker images are unbuilt here** (Docker absent on the development machine). The Dockerfiles
  and compose file were written against the files that exist; the build itself is unverified.
- `GET /providers?refresh=true` and `scripts/check_providers.py` make real outbound calls; they
  are operational diagnostics, not health checks.
