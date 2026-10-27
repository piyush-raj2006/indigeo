# IndiGeo frontend

Next.js 16.4.3 (App Router, Turbopack, `cacheComponents` + `partialPrefetching`), React 19.3.0,
Tailwind v4, leaflet 1.9.4 + react-leaflet 5.0.0. It is a thin client over the REST API in
`../backend` - it holds no data, computes no hazard score, and invents no fallback value. Every
number shown on screen is one the API sent, with the provenance that came with it.

## Run it

```bash
# 1. start the API (from the repository root)
cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123

# 2. start the frontend
cd frontend
npm install
npm run dev                      # http://localhost:3000

# or serve the production build
npm run build && npm start      # `next start` is not supported with output: "standalone"
```

The API origin is read from `NEXT_PUBLIC_API_BASE` (default `http://127.0.0.1:8123/api/v1`) and is
inlined at build time, so change it before `build`, not after. When the page is served from a
non-loopback host while the bundle still points at loopback, the UI says so in a banner instead of
failing silently.

The API must allow the browser origin. `CORS_ORIGINS` accepts a comma-separated list or JSON:

```bash
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000 \
  ../.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123
```

Deployment (containers, env files, TLS, verification) is in `../docs/DEPLOYMENT.md`.

## Pages

| Route | What it is |
|---|---|
| `/` | Command center: locate a point (search, preset, manual coordinates or a map click), pick the sample radius, run the 8-hazard analysis, read the report. |
| `/history` | The runs this instance persisted, with the band and warning count recorded at run time. |
| `/sources` | Live provider states, engine facts, loaded spatial layers, boundary provenance, and the named data gaps. |
| `/analysis/[uuid]` | One stored record, read back by id. Nothing is recomputed to fill in a missing one. |

`src/lib/api.ts` is the only place the API is called; `src/lib/types.ts` mirrors the recorded live
responses in `../data/api_samples/`.

## What the UI deliberately says out loud

- A source that did not answer lowers confidence and appears as a gap. It is never replaced with a
  substitute value, and unassessed is shown as unassessed rather than as a low score.
- `also query OSM lifelines` is off by default because that query measured 504-624 s and often
  returned partial or nothing, and no hazard factor consumes the result.
- The map draws the loaded administrative boundaries from `GET /api/v1/layers/{table}/geojson`.
  District outlines appear at zoom 7 and above, state outlines above that; the basemap is the
  public OSM raster tile service, used under their tile usage policy for local testing.
- `rule engine 1.0.0 - ML models used: none` is printed on every report. No model contributes to
  these scores yet.
- Guidance only. The footer and every recommendation repeat that local disaster authority
  instructions override anything here, and engineering-grade advice is flagged as needing a
  licensed professional.

## Checks

```bash
npm run lint      # eslint (flat config)
npx tsc --noEmit  # types against the captured API shapes
npm run build     # 5 routes; /analysis/[uuid] is the only PPR route
```

Verified in a visible (headless, CDP-driven) browser: `/analysis/[uuid]` renders the stored run,
its hazards, provenance, limitations and the lifeline gap with no console errors. Next.js reveals
the partial-prerender segment on an animation frame, so a backgrounded tab — including the in-app
test browser, which reports `document.visibilityState === "hidden"` — can sit on the
"Reading the requested record…" fallback indefinitely. That is a measurement artifact of a hidden
tab, not a failed page.

The record page needs `GET /api/v1/analysis/{uuid}` to return the same shape as a fresh run.
It previously returned the requested hazard *names* under `hazards` and omitted `request`, which
crashed the report; the read-back now carries the scored results, `requested_hazards` and
`request` (`tests/test_services_analysis.py` pins that shape).
