# Data sources

Every source IndiGeo can talk to, with the status we actually observed. Nothing here
is a claim inherited from documentation alone: each row was probed from this machine, on the
date given, and the probe is reproducible with `python scripts/check_providers.py`
(it prints the JSON; `data/provider_status.json` is that output redirected, and the script
does not write the file itself). The hazard engine's own live end-to-end numbers come from
`python scripts/probe_hazard_engine.py`.

Probe run: **2026-10-25**. `state` is the value the API returns to clients
(`healthy | degraded | unavailable | unconfigured | unverified`).

## Verified working (no key needed)

| Provider | Key | Endpoint | State observed | What it gives us | Licence / attribution |
|---|---|---|---|---|---|
| USGS ComCat | `usgs_earthquakes` | `https://earthquake.usgs.gov/fdsnws/event/1/query` | healthy (HTTP 200) | Historical + recent seismicity | USGS public domain |
| Open-Meteo archive | `open_meteo` | `https://archive-api.open-meteo.com/v1/archive` | healthy (4 days returned) | ERA5 reanalysis daily weather | Open-Meteo, CC-BY for output |
| Open-Meteo forecast | `open_meteo` | `https://api.open-meteo.com/v1/forecast` | healthy | 1-16 day model forecast | as above |
| Nominatim | `nominatim` | `https://nominatim.openstreetmap.org` | healthy (`status=OK`) | Free-text geocode + reverse | ODbL, OSM contributors |
| NASA CMR metadata | `nasa_earthdata` | `https://cmr.earthdata.nasa.gov/search/collections.umm-json` | degraded (31 hits, public) | Dataset discovery only | NASA open metadata |
| Overpass API | `osm_overpass` | mirror failover | healthy via `maps.mail.ru` (200, 12.5 s at 21:09; 21 s at 16:48; three mirrors timed out for 485 s at ~20:30 - see the cost profile below) | OSM infrastructure near a point | ODbL, OSM contributors |
| AWS terrain tiles | `dem` | `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png` | healthy (tiles decode) | Elevation, slope, hillshade | AWS open terrain data |

## Needs a credential we do not have in this environment

| Provider | Env var | State observed | Consequence if unset |
|---|---|---|---|
| NASA FIRMS active fires | `FIRMS_MAP_KEY` | unconfigured | Fire module reports no observations. **No synthetic hotspots are substituted.** |
| NASA Earthdata protected data | `EARTHDATA_TOKEN` (+ `EARTHDATA_PROBE_URL`) | unconfigured / unverified | Only CMR metadata is used; no driven products (GPM IMERG, SNODAS, GRACE, flood extent). |
| Census / PIN / village registers | (none available) | not integrated | District admin codes come from the boundary set instead. |

## Boundary data actually loaded

| Source | URL | Observed | Licence |
|---|---|---|---|
| District + state polygons | `https://raw.githubusercontent.com/udit-001/india-maps-data/refs/heads/main/geojson/india.geojson` | 4,089,052 bytes, 760 features, Polygon/MultiPolygon, props `district, dt_code, st_nm, st_code, year` | ODbL (OSM-derived) |
| Country composite | `https://raw.githubusercontent.com/yashveeeeeeer/india-geodata/refs/heads/main/data/administrative/country/india-composite.geojson` | 10,761,294 bytes | CC-BY-4.0 |

Reproduce with `python scripts/run_ingest.py`.

## Local data and live national feeds

These paths are **operator-supplied**. The repository ships no extract, raster or feed URL:
each is used only when the deployment lists it, and the analysis states which origin answered.

### Local OSM extracts (`osm_local`)

`OSM_EXTRACT_DIR/manifest.json` is authoritative. A file that is not listed is never read.

```json
{
  "extracts": [
    {
      "name": "karnataka-2026-10",
      "path": "karnataka-2026-10.osm.pbf",
      "bbox": [74.0, 11.5, 78.6, 18.5],
      "vintage": "2026-10-01",
      "source": "Geofabrik extract downloaded by the operator",
      "license": "ODbL 1.0",
      "layers": { "hospitals": "points", "bridges": "lines" }
    }
  ]
}
```

- Formats are whatever the installed GDAL/pyogrio drivers open. `pyogrio` ships the OSM
  driver here, so a Geofabrik `.pbf` works without `pyrosm` or `osmnx`; GeoJSON,
  GeoPackage, FlatGeobuf and Shapefile also work.
- A query is answered locally only if an extract's bbox **fully contains** the lookup
  window. Partial overlap is a miss, because counting only the features inside the file
  would understate the lifelines in the radius.
- `layers` maps a lifeline to a source layer. Without it, the GDAL OSM `points`/`lines`
  layers are used, or the layer whose fields carry the tags for that lifeline.
- A `.pbf` is read across every layer that can carry the tags — hospitals, schools,
  first responders and water structures are often mapped as areas, so `points` **and**
  `multipolygons` are read and merged (water structures also read `lines`). The GDAL OSM
  driver promotes only a fixed set of tags to columns: `bridge`, `amenity` and most others
  arrive in `other_tags` as `"key"=>"value"` pairs, which are parsed. Without that parse a
  real extract answers `ok` with zero features, which reads as "no hospitals here".
- The local matcher uses the same tag set as the Overpass selectors, so which origin
  answered does not change what a lifeline means. Both query all geometry types (`nwr`):
  a node-only selector reports neighbourhoods as having no hospitals when the hospital is
  a building footprint. `emergency` accepts `ambulance_station` (current), the deprecated
  `ambulance_service`, and the legacy `amenity=fire_station` / `amenity=police` form.
- Per-layer `status` is `ok`, `outside_coverage` or `unavailable`; every layer carries
  `origin: "local"`. Layers the extract cannot serve are the only ones sent to Overpass
  (`origin: "overpass"`), under the same 90 s budget. Nothing is backfilled across origins.
- Provenance uses `provider: osm_local`, a `file://` endpoint, `http_status: null`,
  `from_cache: true` and notes with the extract name, layer, vintage, source and feature
  count. `origin_states` reports `available` only when every layer an origin served answered.
- Without a manifest the provider reports `unconfigured`; the lookup is exactly the
  Overpass path that existed before, and the response says so.
- Tests: `tests/test_integrations_local_osm.py` (synthetic GeoJSON manifest, GDAL
  `other_tags` parsing, `.pbf` layer selection and points/areas merging). Verified against
  a real 2026-10 Geofabrik Monaco `.pbf`: 46 bridges, 6 hospitals, 10 first-responder
  sites, 12 schools served in 3.4 s with no network.

### Local raster DEMs (`dem_local`)

`DEM_DIR/local/manifest.json` lists SRTM 30 m, ISRO Bhuvan or equivalent GeoTIFFs.

```json
{
  "rasters": [
    {
      "name": "srtm-30m-india",
      "path": "srtm_30m_india.tif",
      "bbox": [68.0, 6.0, 98.0, 36.0],
      "source": "SRTM 1 arc-second, downloaded by the operator",
      "vintage": "2014-09-24",
      "license": "check the distributor's redistribution terms",
      "vertical_datum": "EGM96",
      "resolution_m": 30
    }
  ]
}
```

- The finest covering raster is sampled at native resolution through `rasterio`
  (windowed read; `WarpedVRT` when the file is not EPSG:4326). `DEM_MAX_WINDOW_PIXELS`
  caps the in-memory window, not the source resolution.
- The response records `source: "local_raster"`, `dem_origin: "local_raster"`, `zoom: null`,
  zero tiles, the raster name/file/bbox/resolution/vintage/source/vertical datum, and
  provenance with a `file://` endpoint. `nominal_resolution_m` is computed from the pixel
  size with the cos(latitude) correction.
- `summary` carries `slope_deg`, `downslope_azimuth_deg` (central differences) and
  `terrain_gradient_m_per_m` (tan slope). These are surface derivatives, **not** a
  hydrological flow-accumulation model; the response says that in `derivative_note`.
- Pixels outside -11,000..9,000 m are masked to nodata and counted
  (`impossible_bathymetry_pixels`); a window that is wholly below sea level is
  `bathymetry_only` and is never used as land elevation.
- When no raster fully covers the window, the engine falls back to Terrarium and reports
  `dem_origin: "terrarium_tiles"`, `local_dem.status`
  (`disabled|unconfigured|outside_coverage|unavailable`) and a human-readable
  `fallback_reason`. The fallback is stated, never silent.
- Tests: `tests/test_gis_local_dem.py` (synthetic GeoTIFF with a known gradient).

### Live Indian hazard feeds

No public machine-readable IMD/CWC/Bhuvan endpoint was verified from this machine, so no
URL is hardcoded. Each connector remains `unconfigured` and adds **no factor** until the
operator supplies the URL they are entitled to query.

| Connector | Env vars | Accepted payload | Matching |
|---|---|---|---|
| `imd_alerts` | `IMD_ALERTS_URL` | CAP XML, RSS/Atom or alert JSON | CAP polygon/point intersects the radius, or area name matches the resolved state/district (`matched_by` says which) |
| `cwc_gauges` | `CWC_GAUGES_URL`, optional `CWC_API_KEY` | gauge/stations JSON with level + coordinates | nearest gauge within `max(2 x radius, CWC_SEARCH_RADIUS_M)` |
| `bhuvan_hazards` | `BHUVAN_HAZARDS_URL`, optional `BHUVAN_API_KEY` | GeoJSON FeatureCollection or WFS GetFeature JSON | intersecting polygons grouped by flood/landslide |

- **IMD**: CAP polygons are stated `lat,lon` and are converted to GeoJSON `lon,lat`.
  Severity is mapped from the published vocabulary (`Extreme`=1.0, `Severe`/`High`=0.75,
  `Moderate`=0.5, `Low`=0.2). An alert with no recognisable category is kept but adds 0.
- **CWC**: a gauge is scored against its own warning/danger thresholds. Rows without
  coordinates are counted and skipped; a gauge without thresholds is reported but has
  `severity: null` and does not contribute a number.
- **Bhuvan**: a WFS URL gets `bbox`/`srsName` appended while its existing
  `service`/`request`/`typeName`/`outputFormat` query is preserved (httpx would otherwise
  replace the query). A plain GeoJSON URL is downloaded once and filtered locally.
  Susceptibility classes use the same ordinal mapping; a mapped flood-inundation extent
  with no class is scored 1.0 because it is an observation that water reached the cell.
- Blending into hazards: `flood` uses IMD rain/thunderstorm/flood alerts, the CWC gauge
  and mapped inundation; `coastal_flood` uses inundation and coastal IMD warnings;
  `cyclone` uses IMD cyclone/wind alerts; `landslide` uses Bhuvan susceptibility and IMD
  rain; `heatwave` uses IMD heat alerts. `details.live_alerts` lists which feeds were used.
  Absence of an intersecting polygon is **not** evidence of low susceptibility, and a
  configured feed that failed lowers confidence instead of scoring zero.
- Every matched value is traceable to `provider: imd_alerts|cwc_gauges|bhuvan_hazards`
  plus the exact request in the provenance table.
- Tests: `tests/test_integrations_india_alerts.py` (CAP/RSS/JSON, gauge aliases, WFS bbox,
  unconfigured/failed states, hazard blending).

## Corrections found by probing (do not "fix" these back)

1. **USGS takes `minlongitude/minlatitude/maxlongitude/maxlatitude`, not `bbox`.**
   A `bbox` parameter returns HTTP 400 with `Unknown parameter "bbox"`.
2. **Open-Meteo refuses `cape`, `soil_moisture_0_to_1cm_mean` and `water_evaporation_sum`
   as `daily` variables** on the archive endpoint
   (`Cannot initialize ForecastVariableDaily from invalid String value ...`).
   They are available hourly; the adapter fetches hourly slices and averages them.
3. **FIRMS no longer accepts the anonymous `FIRMS_PUBLIC` key.** Live responses return
   HTTP 400 `Invalid MAP_KEY.` for any key-less request, and `Invalid source.` for an
   unknown source name. A free personal map key is required.
4. **Nominatim `/status` answers `text/plain`, not JSON** - the health probe must not
   parse it as JSON.
5. **CMR `collections.json` returns an Atom-style `feed.entry[]`; `collections.umm-json`
   returns `{hits, took, items}`** - the adapters use the latter so hit counts exist.
6. **Overpass public instances are frequently saturated.** Observed in one run:
   `overpass-api.de` 502 "at capacity", `overpass.private.coffee` no answer,
   `overpass.openstreetmap.fr` 403 "only available to white-listed usages",
   `overpass.kumi.systems` no answer, `maps.mail.ru` 200. The adapter therefore walks a
   mirror list and records which mirror answered plus which ones failed.
7. **A public metadata search is not proof of authentication.** Earthdata reports
   `unverified` whenever a token exists but no protected URL has been named, instead of
   inferring success from CMR.
8. **Terrarium high-zoom ocean tiles carry impossible bathymetry.** At 10 N 72 E the same
   pixel decodes to ~ -2,086 m at zoom 8-10 (the true Arabian Sea depth) but to -9,954 m at
   z11, -13,645 m at z13 and -16,576 m at z15 - deeper than Challenger Deep (-10,935 m), so
   it is void-fill, not survey. `_decode_terrarium` masks any pixel below `OCEAN_FLOOR_M`
   (-11,000 m) to nodata and counts the masked pixels in the response. A window with no
   pixel above sea level is reported as `bathymetry_only` and never feeds flood/landslide
   scoring.
9. **Zoom is bounded by the tile budget, not by a fixed pick.** `pick_zoom` walks down from
   z15 and returns the finest zoom whose tile grid still fits `MAX_TILES_PER_REQUEST` (36).
   The degree padding for the east-west window uses the cos(latitude) correction, otherwise
   the window silently samples less ground than the requested radius near the top of India.
10. **Mixed-element Overpass QL must be a union, not a concatenation.** The
    `water_structures` layer used to emit `node["man_made"~"..."]way["waterway"="dam"](bbox)`,
    which every mirror rejects with **HTTP 400**; the corrected form puts one statement per
    selector inside the union — `( nwr[...](bbox); nwr[...](bbox); )` — and returns
    **HTTP 200 on `maps.mail.ru` and `overpass-api.de`** in the same session. Because a 400
    used to be treated as an unhealthy mirror, one bad query burned the whole mirror list
    and looked exactly like an outage, so 400/406/413/422 now abort failover and are
    reported as `degraded` with the query attached.
11. **A 406 from Overpass was our missing headers, not the server's rule.** A bare `httpx`
    request with the same QL is refused; the provider's `User-Agent` +
    `Accept: application/json` request succeeds. Do not "fix" the mirrors to make that probe
    pass.
12. **`distance_to_sea_km` is really distance to India's own coastline outline.** It is
    measured against `india_outline` (country composite, mainland plus island territories),
    so a point whose shortest route to open water leaves India reads farther than the sea
    actually is. The engine states this in every run's `limitations` instead of implying a
    global measurement.
13. **A node-only lifeline selector reports real places as absent.** In a 2026-10 Geofabrik
    Monaco extract the whole country holds 2 fire stations and 6 police stations, all tagged
    `amenity=fire_station` / `amenity=police`, and hospitals mapped as closed ways; the old
    `node[...]` selectors with only `emergency=*` counted **0** of them. The selectors are
    now `nwr` and include the deprecated `amenity` forms, and the local-extract matcher uses
    the same tag set so both origins mean the same thing. In a 1.4 km radius around Monaco
    centre, `emergency_services` went 0 → 10 and `hospitals` 0 → 6 (one of them an area).

## Cost profile of one analysis (measured, same machine, 2026-10-25)

| Path | Measured |
|---|---|
| `run_point_analysis` without OSM infrastructure (Chennai, warm DEM tile cache) | **3.1 s** |
| `run_point_analysis` without OSM infrastructure (Nagpur, warm DEM tile cache) | **4.4 s** |
| `gather_evidence` with the four OSM layers (Nagpur) | **504.3 s** |
| `gather_evidence` with the four OSM layers (Chennai) | **570.9 s** |
| one OSM layer that ultimately failed (all mirrors, retries) | **220.6 s** |
| `infrastructure_near` for two layers, re-run later the same evening | **485.5 s, both layers `unavailable`** (`overpass-api.de` ReadTimeout, `maps.mail.ru` HTTP 504 then ReadTimeout, `overpass.private.coffee` ReadTimeout) |
| `infrastructure_near` for all four layers at Chennai (21:0x) | **624.2 s - hospitals 50 features via `maps.mail.ru`, emergency_services 0 features (a real zero *for the node-only `emergency` selector in use at the time*; the selector has since been widened to `nwr` plus the legacy `amenity` tags), bridges and water_structures `unavailable` after three mirrors each timed out** |
| `GET /api/v1/providers?refresh=true` (full live sweep, all six providers) | **51.2 s**, Overpass healthcheck alone 17.7 s |
| `POST /api/v1/analysis/point` for Chennai through the running server | **0.27-0.79 s** (warm provider cache), 8 hazards + 13 recommendations, stored |

So the same evidence is either free or eight minutes and sometimes still fails: no hazard
factor reads OSM infrastructure, and `include_infrastructure` therefore defaults to `False`
in `run_point_analysis`, with the skip declared in the payload (`request.include_infrastructure`)
and in `limitations`. The built endpoint `POST /api/v1/analysis/point` inherits that default:
a caller who sets it `true` waits ~624 s and still receives partial layers, so the frontend
must treat that variant as a background job rather than a request. The synchronous path is
sub-second for a cached point, which is what the map interaction needs.

## Hazard engine verified end-to-end on live sources

`gather_evidence` -> `assess_context` -> `recommend` for three real points, with the loaded
national outline, Terrarium DEM, Open-Meteo and USGS (no synthetic inputs, FIRMS
`unconfigured`). The pre-fix run is kept verbatim at
`data/probes/hazard_engine_2026-10-25T20-12_before_terrain_fix.json`; the post-fix numbers came
from `python scripts/probe_hazard_engine.py`.

| Point | Coast distance | DEM at point | Seismicity (50 y) | Bands that stood out |
|---|---|---|---|---|
| Nagpur 79.0882 21.1458 | 563.8 km (measured, not a gap) | 309.1 m, relief 68.4 m | 4 events, 1 within 150 km, M4.9 within 150 km / M5.8 in window | drought **high** 53.0, earthquake moderate 29.4, coastal flood low 6.4 |
| Chennai 80.2785 13.0878 | 1.8 km | 15.2 m, relief 16.6 m | 1 event, M5.2 | cyclone **high** 55.0, flood moderate 40.4 (227.7 mm rain/15 d), drought low 6.5 |
| Port Blair 92.5715 11.6234 | 3.3 km | **0.0 m - intertidal, see below** | 1,045 events, 429 within 150 km, M6.6 within 150 km / M6.8 in window | earthquake **very high** 81.3, flood **extreme** 99.2, coastal flood very high 70.4 |

The same point assessed twice in one evening gave Port Blair flood 62.5 (`high`) at 20:12 and
99.2 (`extreme`) at 21:00. Both are correct outputs: the 7-day forecast had filled in between
the runs (121.6 mm predicted) and `terrain_depression` changed with the terrain fix below. The
engine is deliberately live-sensitive, so these numbers are a snapshot of the sources at the
timestamp in the payload, not a stable rating - which is why every signal keeps its
observations and provenance rather than only a score.

Two honesty fixes came out of reading those numbers rather than trusting them:

- `terrain_depression` used to be a constant (`1.0` if flagged a low point, else `0.25`) while
  presenting `elevation_at_point_m` and `relief_m` as if they produced it. It is now the
  point's measured position inside the sampled DEM window (`1 - (here - min) / (max - min)`),
  a flat window is scored `0.5` with a note that relative position is unmeasurable, and a
  terrain block without a point elevation is reported unavailable instead of numbered. The
  measured effect: Nagpur's flood score went 9.4 -> 22.0 (it sits 23.6 m below the top of its
  own 5 km window) and Chennai's went 45.7 -> 40.4 (it sits near the top of a 16.6 m window).
- The Port Blair coordinate is in the harbour: at a 2 km radius the whole DEM window reads
  0.0 m and is flagged `bathymetry_only`; at the 5 km radius used for analysis the window
  mixes water (-1.7 m) with land (61.8 m), so `bathymetry_only` stays false and the point's
  own 0.0 m pixel was being used as a land elevation. The severity is still defensible (a
  point at sea level has maximal coastal exposure) but it is now labelled: `_shoreline_note`
  attaches to `low_elevation`, `terrain_depression` and `altitude_mitigation` and repeats in
  `limitations`, saying the reading is shoreline/intertidal, not a measured land height. A
  flood score built on a water pixel is now visibly built on one.

- The same coordinate is corroborated by the boundary layer: `admin_context` reports "outside
  every district polygon; the nearest is South Andaman about 2962 m away". DEM 0.0 m, slope
  0.0 degrees and no containing district are three independent readings agreeing that the
  probe point is harbour water, so the label is now honest rather than a silent approximation.
  `_limitations` therefore starts with the boundary caveats (an approximate district label
  weakens ward-level advice), then the evidence warnings, then the per-hazard ones.


## Map payload served by this backend (measured, same machine, 2026-10-25)

`GET /api/v1/layers/{table}/geojson?bbox=min_lon,min_lat,max_lon,max_lat` exists because the web
map has to draw the loaded boundaries, and there was no endpoint that returned stored geometry.
It serves geometry only - it runs no analysis, and it says so in the response `note`. A full page
is not mistaken for the whole window: the store is asked for `limit + 1` rows and `truncated`
reports the extra one. `clip_above_area_deg2` intersects oversized boundaries with the window and
echoes `clipping.applied` plus a note that the clipped edges are the window, not the boundary.
An unloaded table is a 404 (`NotFoundError`), which required a `has_layer` guard in
`DuckDBSpatialStore.features_in_bbox` - without it DuckDB raised a raw `CatalogException`.

Whole-country window `68.1,6.75,97.4,37.1`, `limit=4000`, `clip_above_area_deg2=0.25`:

| Layer | Bytes | Time |
|---|---|---|
| `admin_states` | 201,418 | 0.013 s |
| `admin_districts` | 1,212,487 | 0.089 s |
| `india_outline` | 9,484,648 | 0.328 s |

So the client asks for districts only at zoom 7 and above (the frontend does this in
`MapCanvas`), and states above that. The basemap is the public OSM raster tile service, used
under their tile usage policy for local testing - it is drawn in the UI footer, not hidden.

## Frontend

`frontend/` is Next.js 16.4.3 (App Router, Turbopack) with `cacheComponents` and
`partialPrefetching` on, React 19.3.0, Tailwind v4, leaflet 1.9.4 + react-leaflet 5.0.0.
Routes: `/` (command center), `/history`, `/sources`, `/analysis/[uuid]`. It talks only to the
REST API; it holds no data of its own, and every number on screen carries the provenance the API
sent with it.

```bash
# backend, from the repository root
cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123
# frontend
cd frontend && npm run dev            # http://localhost:3000
cd frontend && npm run build && npx next start -p 3000   # production build
```

- `NEXT_PUBLIC_API_BASE` defaults to `http://127.0.0.1:8123/api/v1` and is inlined at build time.
- The API's `CORS_ORIGINS` is a list field, so it must be given as **JSON** in the environment -
  comma-separated raises `SettingsError` from `EnvSettingsSource` and the server never starts:
  `CORS_ORIGINS='["http://localhost:3000","http://127.0.0.1:3000"]'`.
  The default already allows `:3000`; serving the build on any other port needs that origin added.
  Verified: the ACAO header echoes the origin and `OPTIONS` preflight returns 200 for GET, POST.
- `/locations/point` returns a `terrain` block **only when `radius_m` is sent**. The UI always
  sends the selected radius and re-reads the point when it changes; the report has a separate
  "no terrain requested" state instead of assuming the block exists (that assumption was a real
  crash: `Cannot read properties of undefined (reading 'available')`).
- Leaflet touches `window` on import, so the map is loaded with `dynamic(..., { ssr: false })`
  from a client component. Deferred parent updates use `queueMicrotask`, not
  `requestAnimationFrame` - a backgrounded tab never fires animation frames, and boundaries
  silently failed to load there.
- Still unverified by eye: `/analysis/[uuid]` is the one PPR route, and Next reveals its streamed
  segment with `requestAnimationFrame`. The in-app browser used for testing reports
  `document.visibilityState === "hidden"`, so the reveal never runs there and the page sits on its
  fallback. The segment itself is correct in the delivered HTML and the API read for that uuid
  returns 200 with CORS for the frontend origin; in a visible tab the boundary resolves. This
  should be re-checked in a real browser before it is called done.

## Explicitly not used

- **GADM**: non-commercial licence, incompatible with a public hazard tool.
- **A bundled nationwide OSM extract**: `geofabrik india-latest.osm.pbf` exists
  (1,711,953,012 bytes verified) but this build does not download it. A deployment may add
  regional extracts through `OSM_EXTRACT_DIR/manifest.json`; otherwise nationwide OSM
  coverage is built incrementally through Overpass and the coverage dashboard reports the
  shortfall honestly.
- **data.gov.in API**: requires an API key we do not hold; no endpoint was invented for it.
- **A hardcoded IMD/CWC/Bhuvan endpoint**: connectors exist but remain `unconfigured`
  until an operator supplies a licensed URL; no warning is ever synthesised from a guessed
  address. The UI links to IMD instead.
- **Cycling 90/85 m DEM tiles**: a local SRTM/Bhuvan raster is used when the operator lists
  one; otherwise the Terrarium tiles are ~30 m at zoom 12 and are labelled nominal, not
  survey-grade.
