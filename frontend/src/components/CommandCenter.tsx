"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import dynamic from "next/dynamic";

import type { MapFocus, MapStatus, DrawMode } from "@/components/MapCanvas";
import AnalysisReport from "@/components/AnalysisReport";
import { DEFAULT_LAYER_VISIBILITY, type LayerVisibility } from "@/components/MapLayerControl";
import { ActionButton, Badge, Card, ErrorNote, Prose } from "@/components/ui";
import { API_BASE, API_IS_LOOPBACK, ApiError, api, type AnalysisSelectionBody } from "@/lib/api";
import { bandLabel, bandTone, sourceTone } from "@/lib/bands";
import {
  assertSelectionWithinLimits,
  buildSelectionExport,
  downloadJson,
  featureKey,
  formatBounds,
  geometryBounds,
  geometryVertexCount,
  GeoJsonSelectionError,
  mergeFeatureGeometries,
  parseGeoJsonSelection,
  selectionView,
} from "@/lib/geojson";
import type {
  Analysis,
  AnalysisJob,
  GeoFeature,
  GeoJsonAreaGeometry,
  HazardMeta,
  PlaceHit,
  PointResolution,
} from "@/lib/types";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

/** True for hosts that only make sense on a dev machine or its LAN: RFC1918, link-local, mDNS. */
function isPrivateHost(host: string): boolean {
  if (LOOPBACK_HOSTS.has(host)) return true;
  if (host.endsWith(".local") || host.endsWith(".localhost")) return true;
  if (host.startsWith("10.") || host.startsWith("192.168.") || host.startsWith("169.254.")) return true;
  const second = /^172\.(\d{1,3})\./.exec(host);
  if (second) {
    const octet = Number(second[1]);
    if (octet >= 16 && octet <= 31) return true;
  }
  const v6 = host.replace(/^\[|\]$/g, "").toLowerCase();
  return v6.startsWith("fc") || v6.startsWith("fd") || v6.startsWith("fe80");
}

// The host serving the page only exists in the browser. Reading it through useSyncExternalStore
// (server snapshot null) keeps hydration consistent without a setState-in-effect round trip.
const subscribePageHost = () => () => {};
const readPageHost = () => window.location.hostname;
const readPageHostOnServer = () => null;

// Leaflet touches `window` on import, so the map is loaded only in the browser.
const BrowserMap = dynamic(() => import("@/components/MapCanvas"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-[12px] text-slate-500">
      Loading the map library in the browser…
    </div>
  ),
});

const RADIUS_CHOICES = ["1 km", "3 km", "5 km", "10 km"] as const;

type SelectionMode = "point" | "area";
type AreaTool = "draw" | "district" | "upload";

const AREA_TOOLS: { key: AreaTool; label: string; hint: string }[] = [
  { key: "draw", label: "Draw polygon", hint: "click corners on the map, then finish" },
  { key: "district", label: "Pick districts", hint: "click stored boundaries; they merge into one MultiPolygon" },
  { key: "upload", label: "Upload GeoJSON", hint: "one Polygon or MultiPolygon feature" },
];

interface PolygonSelection {
  geometry: GeoJsonAreaGeometry;
  origin: "draw" | "district" | "upload";
}

const PRESETS: { label: string; lat: number; lon: number; why: string }[] = [
  { label: "Nagpur", lat: 21.1458, lon: 79.0931, why: "inland, drought-led (probed 2026-10-25)" },
  { label: "Chennai", lat: 13.0878, lon: 80.2785, why: "coastal, cyclone and flood drivers" },
  { label: "Port Blair", lat: 11.6234, lon: 92.7265, why: "island, shoreline DEM readings flagged" },
];

const INPUT_CLASS =
  "rounded-lg border border-slate-700/80 bg-slate-950/80 px-2.5 py-1.5 text-[13px] text-slate-100 outline-none transition placeholder:text-slate-600 focus:border-sky-500/80 focus:ring-2 focus:ring-sky-500/15";

function TerrainRead({ terrain }: { terrain: PointResolution["terrain"] }) {
  if (terrain === undefined) {
    return (
      <Prose tone="text-amber-300/90">
        This lookup returned no terrain block, so no elevation was sampled. The engine samples the DEM only when a
        radius is sent with the request.
      </Prose>
    );
  }
  if (!terrain.available || terrain.summary === null) {
    return (
      <Prose tone="text-red-300/90">
        Terrain unavailable: {terrain.reason ?? "the terrain block reported no reason"}.
        {terrain.fallback_reason ? ` ${terrain.fallback_reason}` : ""} The engine scores without it and lowers
        confidence rather than guessing an elevation.
      </Prose>
    );
  }
  const seen = terrain.summary;
  const localRaster = terrain.source === "local_raster" && terrain.raster;
  return (
    <ul className="space-y-0.5 text-[12px] leading-relaxed text-slate-400">
      <li>
        elevation at point {seen.elevation_at_point_m} m · window {seen.elevation_min_m}–{seen.elevation_max_m} m ·
        relief {seen.elevation_relief_m} m · slope {seen.slope_deg}°
        {seen.downslope_azimuth_deg !== null && seen.downslope_azimuth_deg !== undefined
          ? ` (downslope ${seen.downslope_azimuth_deg}°)`
          : ""}
      </li>
      <li>
        {seen.samples} samples at {terrain.nominal_resolution_m} m nominal ·{" "}
        {localRaster ? (
          <>
            local raster {terrain.raster?.name}
            {terrain.raster?.vintage ? ` (${terrain.raster.vintage})` : ""}, {terrain.nodata_pixels} nodata,{" "}
            {terrain.impossible_bathymetry_pixels} impossible pixels masked
          </>
        ) : (
          <>
            zoom {terrain.zoom}, {terrain.tiles_fetched}/{terrain.tiles_planned} tiles, {terrain.nodata_pixels} void,{" "}
            {terrain.impossible_bathymetry_pixels} bathymetry pixels masked
          </>
        )}{" "}
        · sampled radius {seen.sampled_radius_m} m
      </li>
      {terrain.fallback_reason && (
        <li className="text-amber-300/90">Local raster not used: {terrain.fallback_reason}</li>
      )}
      {seen.bathymetry_only && (
        <li className="text-amber-300">
          The whole window reads at or below sea level: this is water, so elevation-derived factors are labelled as a
          shoreline reading, not a measured land height.
        </li>
      )}
      {seen.local_low_point && <li className="text-amber-300">The point sits at the window’s low side.</li>}
    </ul>
  );
}

export default function CommandCenter() {
  const [meta, setMeta] = useState<HazardMeta | null>(null);
  const [point, setPoint] = useState<{ lat: number; lon: number } | null>(null);
  const [place, setPlace] = useState<PointResolution | null>(null);
  const [locating, setLocating] = useState(false);
  const [locateError, setLocateError] = useState<string | null>(null);

  const [radius, setRadius] = useState<string>("5 km");
  const [includeInfra, setIncludeInfra] = useState(false);
  const [selectionMode, setSelectionMode] = useState<SelectionMode>("point");
  const [areaTool, setAreaTool] = useState<AreaTool>("draw");
  const [polygon, setPolygon] = useState<PolygonSelection | null>(null);
  const [districtPicks, setDistrictPicks] = useState<GeoFeature[]>([]);
  const [areaError, setAreaError] = useState<string | null>(null);
  const [areaNote, setAreaNote] = useState<string | null>(null);
  const [layerVisibility, setLayerVisibility] = useState<LayerVisibility>(DEFAULT_LAYER_VISIBILITY);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<ApiError | Error | null>(null);

  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<PlaceHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchNote, setSearchNote] = useState<string | null>(null);

  const [manualLat, setManualLat] = useState("");
  const [manualLon, setManualLon] = useState("");

  const [mapStatus, setMapStatus] = useState<MapStatus | null>(null);
  const onMapStatus = useCallback((status: MapStatus) => setMapStatus(status), []);
  const requestToken = useRef(0);
  // Explicit selections move the viewport; a click on the map is already at the clicked spot.
  const [mapFocus, setMapFocus] = useState<MapFocus | null>(null);
  const focusMap = useCallback((lat: number, lon: number, radiusM: number) => {
    setMapFocus((previous) => ({ lat, lon, radiusM, token: (previous?.token ?? 0) + 1 }));
  }, []);

  // The bundle's API origin is fixed at build time, but the host serving this page is only
  // known in the browser.
  const pageHost = useSyncExternalStore(subscribePageHost, readPageHost, readPageHostOnServer);
  const pageIsLoopback = pageHost === null ? null : LOOPBACK_HOSTS.has(pageHost);
  const pageIsPrivate = pageHost === null ? null : isPrivateHost(pageHost);

  const [job, setJob] = useState<AnalysisJob | null>(null);
  const [jobStopped, setJobStopped] = useState(false);
  const jobWatch = useRef(0);

  const [toasts, setToasts] = useState<{ id: number; tone: "error" | "info"; title: string; body: string }[]>([]);
  const toastId = useRef(0);
  const notify = useCallback((tone: "error" | "info", title: string, body: string) => {
    const id = ++toastId.current;
    setToasts((rows) => [...rows.slice(-3), { id, tone, title, body }]);
    if (tone === "info") window.setTimeout(() => setToasts((rows) => rows.filter((row) => row.id !== id)), 8000);
  }, []);
  const dismissToast = useCallback((id: number) => setToasts((rows) => rows.filter((row) => row.id !== id)), []);

  useEffect(() => {
    api
      .hazardMeta()
      .then(setMeta)
      .catch(() => setMeta(null));
  }, []);

  const radiusMeters = useMemo(() => Number.parseFloat(radius) * 1000, [radius]);

  const selectedDistrictFids = useMemo(
    () => new Set(districtPicks.map((feature) => featureKey(feature))),
    [districtPicks],
  );

  /** Every selection change drops the previous result: the map and the report must agree. */
  const resetRun = useCallback(() => {
    jobWatch.current += 1;
    setRunning(false);
    setJob(null);
    setJobStopped(false);
    setAnalysis(null);
    setRunError(null);
  }, []);

  const applySelection = useCallback(
    (geometry: GeoJsonAreaGeometry, origin: PolygonSelection["origin"], note: string) => {
      assertSelectionWithinLimits(geometry);
      setPolygon({ geometry, origin });
      setSelectionMode("area");
      setAreaError(null);
      setAreaNote(note);
      setMapFocus({ ...selectionView(geometry), token: Date.now() });
      resetRun();
    },
    [resetRun],
  );

  const drawnPolygon = useCallback(
    (geometry: GeoJsonAreaGeometry) => {
      try {
        applySelection(geometry, "draw", `drawn polygon · ${geometryVertexCount(geometry)} vertices`);
      } catch (error) {
        setAreaError(error instanceof Error ? error.message : String(error));
      }
    },
    [applySelection],
  );

  const toggleDistrict = useCallback(
    (feature: GeoFeature) => {
      const key = featureKey(feature);
      const next = districtPicks.some((picked) => featureKey(picked) === key)
        ? districtPicks.filter((picked) => featureKey(picked) !== key)
        : [...districtPicks, feature];
      setDistrictPicks(next);
      if (next.length === 0) {
        setPolygon(null);
        setAreaNote(null);
        setAreaError(null);
        return;
      }
      try {
        const geometry = mergeFeatureGeometries(next);
        applySelection(
          geometry,
          "district",
          `${next.length} boundar${next.length === 1 ? "y" : "ies"} merged into one MultiPolygon`,
        );
      } catch (error) {
        setAreaError(error instanceof Error ? error.message : String(error));
      }
    },
    [districtPicks, applySelection],
  );

  const importFile = useCallback(
    async (file: File) => {
      setAreaError(null);
      try {
        const geometry = parseGeoJsonSelection(await file.text());
        applySelection(geometry, "upload", `${file.name} · ${geometryVertexCount(geometry)} vertices`);
        setAreaTool("upload");
        setDistrictPicks([]);
      } catch (error) {
        setAreaError(
          error instanceof GeoJsonSelectionError
            ? error.message
            : `Could not read ${file.name}: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    },
    [applySelection],
  );

  const clearAreaSelection = useCallback(() => {
    setPolygon(null);
    setDistrictPicks([]);
    setAreaError(null);
    setAreaNote(null);
    setAnalysis(null);
    setRunError(null);
  }, []);

  const exportSelection = useCallback(() => {
    const geometry = analysis?.selection?.geometry ?? polygon?.geometry;
    if (!geometry) return;
    const { filename, collection } = buildSelectionExport(geometry, analysis);
    downloadJson(filename, collection);
    notify(
      "info",
      "GeoJSON exported",
      `${geometryVertexCount(geometry)} vertices written to ${filename}${
        analysis ? ` with ${analysis.analysis_uuid}` : " (not analysed yet, so no score is attached)"
      }.`,
    );
  }, [analysis, polygon, notify]);

  const choosePoint = useCallback(
    async (lat: number, lon: number, radiusUsed: string, recenter = true) => {
      const token = ++requestToken.current;
      // A watch belongs to the point it was started for; changing the point abandons it.
      jobWatch.current += 1;
      setRunning(false);
      setJob(null);
      setJobStopped(false);
      setPoint({ lat, lon });
      setSelectionMode("point");
      if (recenter) focusMap(lat, lon, (Number.parseFloat(radiusUsed) || 0) * 1000);
      setAnalysis(null);
      setRunError(null);
      setPlace(null);
      setLocateError(null);
      setLocating(true);
      try {
        // The terrain block only exists when a radius is asked for, so pass the one in the UI.
        const resolved = await api.resolvePoint(Number(lat.toFixed(6)), Number(lon.toFixed(6)), radiusUsed);
        if (token !== requestToken.current) return;
        setPlace(resolved);
      } catch (error) {
        if (token !== requestToken.current) return;
        setLocateError(error instanceof ApiError ? error.message : String(error));
      } finally {
        if (token === requestToken.current) setLocating(false);
      }
    },
    [focusMap],
  );

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setSearchError(null);
    setHits(null);
    try {
      const found = await api.searchPlaces(query.trim());
      setHits(found.results);
      // Show where the first hit is without choosing it: the sample point stays empty until
      // the user picks one, but the map should not keep looking at the previous place.
      const first = found.results[0];
      if (first) focusMap(first.lat, first.lon, 1000);
      setSearchNote(
        `${found.interpretation} · ${found.results.length} hit${found.results.length === 1 ? "" : "s"} · ` +
          (found.attribution ?? "no attribution returned") +
          (found.warnings.length > 0 ? ` · ${found.warnings.join(" ")}` : ""),
      );
    } catch (error) {
      setSearchError(error instanceof ApiError ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  };

  const adoptAnalysis = useCallback((stored: Analysis) => {
    setAnalysis(stored);
    if (stored.selection?.geometry) {
      setPolygon((current) => ({ geometry: stored.selection!.geometry, origin: current?.origin ?? "draw" }));
      setSelectionMode("area");
    }
    // The new run has its own catalogue; start from the honest defaults again.
    setLayerVisibility(DEFAULT_LAYER_VISIBILITY);
    // A failure toast belongs to the attempt that failed, not to this record.
    setToasts((rows) => rows.filter((row) => row.tone !== "error"));
  }, []);

  const watchJob = useCallback(
    async (jobUuid: string) => {
      const token = ++jobWatch.current;
      while (token === jobWatch.current) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (token !== jobWatch.current) return;
        let current: AnalysisJob;
        try {
          current = await api.analysisJob(jobUuid);
        } catch (error) {
          if (token !== jobWatch.current) return;
          const apiError = error instanceof ApiError ? error : new Error(String(error));
          setRunError(apiError);
          setRunning(false);
          notify("error", "Lost contact with the analysis job", apiError.message);
          return;
        }
        if (token !== jobWatch.current) return;
        setJob(current);
        if (current.status === "completed" && current.result_ref) {
          try {
            const stored = await api.analysis(current.result_ref);
            if (token !== jobWatch.current) return;
            adoptAnalysis(stored);
            notify("info", "Analysis ready", `Stored as ${current.result_ref}.`);
          } catch (error) {
            if (token !== jobWatch.current) return;
            const apiError = error instanceof ApiError ? error : new Error(String(error));
            setRunError(apiError);
            notify("error", "The job finished but its record could not be read", apiError.message);
          } finally {
            if (token === jobWatch.current) setRunning(false);
          }
          return;
        }
        if (current.status === "failed" || current.status === "interrupted") {
          const apiError = new ApiError(current.error ?? `The job ended as ${current.status}.`, {
            code: `job_${current.status}`,
            status: 0,
            retryable: current.status === "interrupted",
          });
          setRunError(apiError);
          setRunning(false);
          notify(
            "error",
            current.status === "failed" ? "Analysis job failed" : "Analysis job was interrupted",
            current.error ?? current.note,
          );
          return;
        }
      }
    },
    [notify, adoptAnalysis],
  );

  const stopWatching = () => {
    jobWatch.current += 1;
    setRunning(false);
    setJobStopped(true);
  };

  const run = async () => {
    const pointSelection = selectionMode === "point" ? point : null;
    const areaSelection = selectionMode === "area" ? polygon : null;
    if (!pointSelection && !areaSelection) return;
    jobWatch.current += 1;
    setRunning(true);
    setRunError(null);
    setAnalysis(null);
    setJob(null);
    setJobStopped(false);
    const body: AnalysisSelectionBody = pointSelection
      ? { lat: pointSelection.lat, lon: pointSelection.lon, radius_m: radius, include_infrastructure: includeInfra }
      : { geometry: areaSelection!.geometry, include_infrastructure: includeInfra };
    try {
      if (includeInfra) {
        // The lifeline path is measured in minutes; submit and poll instead of holding a
        // request open until a proxy answers 504.
        const submitted = await api.submitAnalysisJob(body);
        setJob(submitted);
        await watchJob(submitted.job_uuid);
        return;
      }
      const result = await api.analyse(body);
      adoptAnalysis(result);
      notify(
        "info",
        "Analysis ready",
        `${result.hazards.length} hazards stored as ${result.analysis_uuid}` +
          (result.area ? ` · ${result.area.area_km2.toFixed(1)} km² over ${result.area.samples_total} samples` : "") +
          ".",
      );
      setRunning(false);
    } catch (error) {
      const apiError = error instanceof ApiError ? error : new Error(String(error));
      setAnalysis(null);
      setRunError(apiError);
      notify(
        "error",
        apiError instanceof ApiError && apiError.code === "timeout" ? "Analysis timed out" : "Analysis failed",
        apiError.message,
      );
      setRunning(false);
    }
  };

  const mapTone = mapStatus?.busy
    ? sourceTone("degraded")
    : mapStatus?.error
      ? sourceTone("unavailable")
      : sourceTone("available");

  const drawMode: DrawMode =
    selectionMode === "area" && areaTool === "draw"
      ? "polygon"
      : selectionMode === "area" && areaTool === "district"
        ? "district"
        : "off";
  const mapSelection =
    selectionMode === "area" ? (analysis?.selection?.geometry ?? polygon?.geometry ?? null) : null;
  const canRun = selectionMode === "point" ? Boolean(point) : Boolean(polygon);
  const toggleLayer = useCallback(
    (id: string, next: boolean) => setLayerVisibility((current) => ({ ...current, [id]: next })),
    [],
  );
  const areaBounds = polygon ? geometryBounds(polygon.geometry) : null;

  return (
    <div className="mx-auto w-full max-w-[1680px] flex-1 space-y-4 px-4 pb-10 pt-4">
      <div className="glass-card flex flex-wrap items-center gap-3 rounded-xl px-4 py-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-sky-500/15 text-[12px] font-bold tracking-tight text-sky-300 ring-1 ring-inset ring-sky-400/30">
          IG
        </div>
        <div className="min-w-0">
          <h1 className="text-[16px] font-semibold tracking-tight text-slate-50">Hazard command center</h1>
          <p className="text-[11.5px] leading-relaxed text-slate-500">
            Eight-hazard rule engine over live public sources · a source that does not answer is reported as a gap,
            never imputed
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 lg:ml-auto">
          {analysis ? (
            <Badge tone={bandTone(analysis.overall.band).chip}>
              last run · {bandLabel(analysis.overall.band, meta?.band_labels)}
            </Badge>
          ) : (
            <Badge tone={sourceTone("unverified")}>no run yet</Badge>
          )}
          <Badge tone={mapTone}>
            {mapStatus?.busy ? "boundaries loading" : mapStatus?.error ? "boundary layer failed" : "map ready"}
          </Badge>
          <span className="hidden rounded-full bg-slate-900/60 px-2 py-0.5 font-mono text-[10.5px] text-slate-400 ring-1 ring-inset ring-slate-700/70 sm:inline">
            {API_BASE}
          </span>
        </div>
      </div>

      {API_IS_LOOPBACK && pageHost !== null && pageIsPrivate === false && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3.5 py-2.5 text-[12px] leading-relaxed text-amber-200">
          This page is served from <span className="font-mono">{pageHost}</span>, but this build calls{" "}
          <span className="font-mono">{API_BASE}</span> — a loopback address that only resolves on the machine running
          the API. To deploy, rebuild with <span className="font-mono">NEXT_PUBLIC_API_BASE</span> set to the API&apos;s
          public origin (it is baked in at build time) and add this page&apos;s origin to the API&apos;s{" "}
          <span className="font-mono">CORS_ORIGINS</span>.
        </div>
      )}
      {API_IS_LOOPBACK && pageIsLoopback === true && (
        <p className="text-[11.5px] leading-relaxed text-slate-500">
          local build · this bundle calls <span className="font-mono">{API_BASE}</span>, which resolves because the API
          runs on this machine. A deployment must be rebuilt with{" "}
          <span className="font-mono">NEXT_PUBLIC_API_BASE</span> pointing at the public API origin, with that
          page&apos;s origin listed in <span className="font-mono">CORS_ORIGINS</span>.
        </p>
      )}
      {API_IS_LOOPBACK && pageIsLoopback === false && pageIsPrivate === true && (
        <p className="text-[11.5px] leading-relaxed text-slate-500">
          local build · this page is served from <span className="font-mono">{pageHost}</span>, while the bundle calls{" "}
          <span className="font-mono">{API_BASE}</span>. The API is only reachable from the machine running it; for
          other devices use a public <span className="font-mono">NEXT_PUBLIC_API_BASE</span> at build time.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-[390px_minmax(0,1fr)] xl:grid-cols-[420px_minmax(0,1fr)]">
        <div className="space-y-4">
          <Card title="Locate a point" aside={<span className="font-mono">{API_BASE}</span>}>
            <form onSubmit={search} className="flex gap-2">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="district, city or place name"
                className={`min-w-0 flex-1 ${INPUT_CLASS}`}
              />
              <button
                type="submit"
                className="rounded-lg bg-sky-500/90 px-3 py-1.5 text-[12.5px] font-semibold text-slate-950 ring-1 ring-inset ring-sky-400/50 transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
                disabled={searching}
              >
                {searching ? "searching…" : "search"}
              </button>
            </form>

            {searchError && <p className="mt-2 text-[12px] text-red-300">{searchError}</p>}
            {searchNote && <p className="mt-2 text-[11.5px] leading-relaxed text-slate-500">{searchNote}</p>}

            {hits && hits.length === 0 && (
              <div className="mt-2">
                <Prose tone="text-amber-300/90">
                  The register and the geocoder both returned nothing for “{query}”. No position was invented — type a
                  name, pick a spot on the map, or enter coordinates below.
                </Prose>
              </div>
            )}
            {hits && hits.length > 0 && (
              <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto pr-0.5">
                {hits.map((hit) => (
                  <li key={`${hit.source}-${hit.lat}-${hit.lon}`}>
                    <button
                      onClick={() => void choosePoint(hit.lat, hit.lon, radius)}
                      className="w-full rounded-lg border border-slate-800/70 bg-slate-950/50 px-2.5 py-1.5 text-left transition hover:border-sky-500/60 hover:bg-slate-900/60"
                    >
                      <span className="block text-[13px] text-slate-100">{hit.label}</span>
                      <span className="block text-[11px] text-slate-500">
                        {hit.kind} · {hit.source} · {hit.lat.toFixed(4)}, {hit.lon.toFixed(4)}
                        {hit.exact ? "" : " · approximate centroid"}
                        {hit.provenance.vintage ? ` · vintage ${hit.provenance.vintage}` : ""}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <form
              className="mt-3 flex gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                const lat = Number.parseFloat(manualLat);
                const lon = Number.parseFloat(manualLon);
                if (Number.isNaN(lat) || Number.isNaN(lon)) {
                  setLocateError("Both a latitude and a longitude are needed. Nothing was placed.");
                  return;
                }
                void choosePoint(lat, lon, radius);
              }}
            >
              <input
                value={manualLat}
                onChange={(event) => setManualLat(event.target.value)}
                inputMode="decimal"
                placeholder="lat"
                className={`w-24 ${INPUT_CLASS}`}
              />
              <input
                value={manualLon}
                onChange={(event) => setManualLon(event.target.value)}
                inputMode="decimal"
                placeholder="lon"
                className={`w-24 ${INPUT_CLASS}`}
              />
              <button
                type="submit"
                className="rounded-lg border border-slate-700/80 bg-slate-900/60 px-3 py-1.5 text-[12.5px] text-slate-200 transition hover:border-sky-500/70 hover:text-white"
              >
                go
              </button>
            </form>

            <div className="mt-3 flex flex-wrap gap-1.5">
              {PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  onClick={() => void choosePoint(preset.lat, preset.lon, radius)}
                  title={preset.why}
                  className="rounded-full border border-slate-700/80 bg-slate-900/40 px-2.5 py-1 text-[11.5px] text-slate-300 transition hover:border-sky-500/70 hover:text-slate-100"
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
              Click anywhere on the map to place the sample point instead. Presets are coordinates probed against the
              live engine, not sample data.
            </p>
          </Card>

          <Card
            title="Selection"
            aside={<span className="font-mono">{selectionMode === "point" ? `${radius} radius` : "polygon"}</span>}
            accent={selectionMode === "point" ? undefined : bandTone("moderate").bar}
          >
            <div className="grid grid-cols-2 gap-1 rounded-lg border border-slate-800/70 bg-slate-950/60 p-1">
              {(["point", "area"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => setSelectionMode(option)}
                  className={`rounded-md px-2 py-1.5 text-[12px] font-medium transition ${
                    selectionMode === option
                      ? "bg-slate-800 text-slate-100 shadow-sm ring-1 ring-inset ring-slate-700"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {option === "point" ? "Point + radius" : "Drawn area"}
                </button>
              ))}
            </div>

            {selectionMode === "area" && (
              <div className="mt-3 space-y-3">
                <div className="grid grid-cols-3 gap-1">
                  {AREA_TOOLS.map((tool) => (
                    <button
                      key={tool.key}
                      type="button"
                      onClick={() => {
                        setAreaTool(tool.key);
                        setAreaError(null);
                      }}
                      title={tool.hint}
                      className={`rounded-md border px-1.5 py-1.5 text-[11px] font-medium transition ${
                        areaTool === tool.key
                          ? "border-sky-500/60 bg-sky-500/15 text-sky-200"
                          : "border-slate-800/70 bg-slate-950/50 text-slate-400 hover:border-slate-600 hover:text-slate-200"
                      }`}
                    >
                      {tool.label}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] leading-relaxed text-slate-500">
                  {AREA_TOOLS.find((tool) => tool.key === areaTool)?.hint}. The engine assesses up to five interior
                  sample points and reports the worst one per hazard, never an average.
                </p>

                {areaTool === "upload" && (
                  <div>
                    <input
                      ref={fileInput}
                      type="file"
                      accept=".geojson,.json,application/geo+json,application/json"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file) void importFile(file);
                        event.target.value = "";
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => fileInput.current?.click()}
                      className="w-full rounded-lg border border-dashed border-slate-700 bg-slate-950/50 px-3 py-2 text-[12px] text-slate-300 transition hover:border-sky-500/60 hover:text-slate-100"
                    >
                      choose a .geojson file
                    </button>
                    <p className="mt-1 text-[10.5px] leading-relaxed text-slate-600">
                      One Polygon or MultiPolygon, as a bare geometry, a Feature, or a FeatureCollection holding exactly
                      one feature. Multi-feature files are rejected rather than guessed at.
                    </p>
                  </div>
                )}

                {areaError && <ErrorNote title="This selection was not accepted" message={areaError} />}

                {polygon ? (
                  <div className="space-y-2 rounded-lg border border-slate-800/70 bg-slate-950/50 px-2.5 py-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge tone={bandTone("moderate").chip}>
                        {polygon.origin === "district"
                          ? `${districtPicks.length} boundaries`
                          : polygon.origin === "upload"
                            ? "uploaded"
                            : "drawn"}
                      </Badge>
                      <Badge>{geometryVertexCount(polygon.geometry)} vertices</Badge>
                      {analysis?.selection && <Badge>{analysis.selection.area_km2.toFixed(1)} km²</Badge>}
                    </div>
                    {areaNote && <p className="text-[11.5px] leading-relaxed text-slate-400">{areaNote}</p>}
                    {areaBounds && (
                      <p className="font-mono text-[10.5px] text-slate-500">bbox {formatBounds(areaBounds)}</p>
                    )}
                    {!analysis?.selection && (
                      <p className="text-[10.5px] leading-relaxed text-slate-600">
                        Area and perimeter are computed by the engine during the run; nothing is estimated here.
                      </p>
                    )}
                    <div className="flex flex-wrap gap-1.5">
                      <ActionButton onClick={exportSelection}>Export GeoJSON</ActionButton>
                      <ActionButton onClick={clearAreaSelection}>Clear selection</ActionButton>
                    </div>
                  </div>
                ) : (
                  <p className="rounded-lg border border-slate-800/70 bg-slate-950/40 px-2.5 py-2 text-[11.5px] leading-relaxed text-slate-500">
                    No boundary yet. Draw one, pick districts, or upload a GeoJSON file. Nothing is scored until a
                    boundary exists.
                  </p>
                )}

                {districtPicks.length > 0 && (
                  <div className="rounded-lg border border-slate-800/70 bg-slate-950/40 px-2.5 py-2">
                    <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                      Picked boundaries
                    </p>
                    <ul className="mt-1 max-h-28 space-y-0.5 overflow-y-auto text-[11.5px] text-slate-300">
                      {districtPicks.map((feature) => (
                        <li key={featureKey(feature)} className="flex items-center gap-2">
                          <span className="min-w-0 flex-1 truncate">
                            {String(feature.properties.district ?? feature.properties.state ?? "boundary")}
                            {feature.properties.state_name ? `, ${String(feature.properties.state_name)}` : ""}
                          </span>
                          <button
                            type="button"
                            onClick={() => toggleDistrict(feature)}
                            className="text-slate-500 transition hover:text-red-300"
                            aria-label="remove this boundary"
                          >
                            ×
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {selectionMode === "point" && (
              <p className="mt-2 text-[11.5px] leading-relaxed text-slate-500">
                Pick a point on the map or from the search above; its radius window is set below.
              </p>
            )}

            <div className="mt-3 space-y-2.5 border-t border-slate-800/70 pt-3">
              {selectionMode === "point" && (
                <label className="flex items-center gap-2 text-[12.5px] text-slate-300">
                  sample radius
                  <select
                    value={radius}
                    onChange={(event) => {
                      const next = event.target.value;
                      setRadius(next);
                      // The terrain block is sampled for a radius, so changing it re-reads the point.
                      // No recenter: the user is adjusting the sample window, not moving to a new place.
                      if (point) void choosePoint(point.lat, point.lon, next, false);
                    }}
                    className="rounded-lg border border-slate-700/80 bg-slate-950/80 px-2 py-1 text-[12.5px] text-slate-100 outline-none focus:border-sky-500/80"
                  >
                    {RADIUS_CHOICES.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="flex items-start gap-2 text-[12px] leading-relaxed text-slate-400">
                <input
                  type="checkbox"
                  checked={includeInfra}
                  onChange={(event) => setIncludeInfra(event.target.checked)}
                  className="mt-0.5 accent-sky-500"
                />
                <span>
                  also query OSM lifelines (hospitals, emergency services, bridges, water). The serial walk measured
                  504–624 s; it now runs the layers concurrently under a 90 s budget and is submitted as a polled job.
                  A layer that misses the budget is reported unavailable, not as zero features. No hazard factor
                  consumes the result. <span className="text-slate-500">Off by default on purpose.</span>
                </span>
              </label>
              <button
                onClick={() => void run()}
                disabled={!canRun || running}
                className="w-full rounded-lg bg-sky-500 px-3 py-2 text-[13px] font-semibold text-slate-950 shadow-[0_16px_34px_-20px_rgba(14,165,233,0.95)] ring-1 ring-inset ring-sky-400/50 transition hover:bg-sky-400 disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500 disabled:shadow-none disabled:ring-slate-700"
              >
                {running
                  ? includeInfra
                    ? "watching the analysis job…"
                    : "analysing…"
                  : selectionMode === "point"
                    ? "Run 8-hazard analysis"
                    : "Assess this area"}
              </button>
              <p className="text-[10.5px] leading-relaxed text-slate-600">
                The map&apos;s layer toggles and legend reflect what this run measured. Layers the engine did not
                measure stay disabled with the reason shown.
              </p>

              {(running || job) && includeInfra && (
                <div className="rounded-lg border border-slate-800/70 bg-slate-950/60 px-2.5 py-2 text-[11.5px] leading-relaxed text-slate-400">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-slate-200">{job ? job.status : "submitting"}</span>
                    {job && <span>{job.message}</span>}
                    {job && (
                      <span className="ml-auto font-mono text-slate-500">{Math.round(job.progress * 100)}%</span>
                    )}
                  </div>
                  <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded bg-slate-800">
                    <div
                      className="h-full bg-sky-500 transition-all"
                      style={{ width: `${Math.round((job?.progress ?? 0) * 100)}%` }}
                    />
                  </div>
                  {job && (
                    <p className="mt-1.5">
                      {((job.elapsed_ms ?? 0) / 1000).toFixed(1)} s elapsed on the server · polling every 1.5 s ·{" "}
                      <span className="font-mono text-slate-500">{job.poll}</span>
                    </p>
                  )}
                  {job && (
                    <p className="mt-1 text-slate-500">
                      {job.note} The percentage marks the last stage that started.
                    </p>
                  )}
                  {job?.status === "completed" && job.result.infrastructure_layers && (
                    <p className="mt-1 text-slate-500">
                      lifelines:{" "}
                      {Object.entries(job.result.infrastructure_layers)
                        .map(
                          ([name, layer]) =>
                            `${name} ${layer.status}${layer.count === undefined ? "" : ` (${layer.count})`}`,
                        )
                        .join(" · ")}
                    </p>
                  )}
                  {running && (
                    <button
                      onClick={stopWatching}
                      className="mt-1.5 rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 transition hover:border-slate-500"
                    >
                      stop waiting
                    </button>
                  )}
                  {jobStopped && (
                    <p className="mt-1.5 text-amber-300">
                      Stopped watching. The job keeps running on the API; nothing is shown here because this page never
                      received a result. Poll the URL above, or submit the run again.
                    </p>
                  )}
                </div>
              )}

              {runError && (
                <ErrorNote
                  title={runError instanceof ApiError ? `API refused the analysis (${runError.code})` : "Analysis failed"}
                  message={runError.message}
                  hint={
                    runError instanceof ApiError && runError.details !== undefined
                      ? `details: ${JSON.stringify(runError.details).slice(0, 400)}`
                      : undefined
                  }
                />
              )}
            </div>
          </Card>

          {selectionMode === "point" && (
          <Card
            title="Sample point"
            aside={
              <span className="font-mono">
                {point ? `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}` : "none picked"}
              </span>
            }
            accent={place?.terrain?.available ? bandTone("low").bar : undefined}
          >
            {locating && (
              <Prose>
                Resolving admin context and DEM terrain for this point… (measured 0.2–0.3 s from a warm DEM cache, 8.9
                s when the tiles for this window have to be fetched)
              </Prose>
            )}
            {locateError && <ErrorNote title="Could not resolve this point" message={locateError} />}
            {!point && !locating && (
              <Prose>
                No point picked. Nothing is scored until you place one — the map shows stored boundaries only.
              </Prose>
            )}
            {place && (
              <div className="space-y-2">
                <p className="text-[13px] font-medium text-slate-100">{place.place.label}</p>
                <div className="flex flex-wrap gap-1.5">
                  <Badge tone={place.place.inside_india ? bandTone("low").chip : bandTone("very_high").chip}>
                    {place.place.inside_india ? "inside the loaded boundaries" : "outside the loaded boundaries"}
                  </Badge>
                  {place.place.boundary_uncertain && (
                    <Badge tone={bandTone("moderate").chip}>boundary uncertain</Badge>
                  )}
                  {place.terrain === undefined ? (
                    <Badge tone={sourceTone("unverified")}>no terrain requested</Badge>
                  ) : place.terrain.available ? (
                    <Badge tone={bandTone("low").chip}>DEM read</Badge>
                  ) : (
                    <Badge tone={sourceTone("unavailable")}>DEM not read</Badge>
                  )}
                </div>
                <TerrainRead terrain={place.terrain} />
                {place.place.warnings.map((warning) => (
                  <p
                    key={warning}
                    className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[11.5px] leading-relaxed text-amber-200"
                  >
                    {warning}
                  </p>
                ))}
              </div>
            )}

          </Card>
          )}
        </div>

        <div className="min-w-0 space-y-4">
          <div className="glass-card rounded-2xl p-1.5">
            <div className="relative h-[46vh] min-h-[360px] overflow-hidden rounded-xl border border-slate-800/80">
              <BrowserMap
                point={point}
                radiusM={radiusMeters}
                focus={mapFocus}
                onPick={(lat, lon) => void choosePoint(lat, lon, radius, false)}
                onStatus={onMapStatus}
                analysis={analysis}
                selectionGeometry={mapSelection}
                selectionOrigin={polygon?.origin ?? null}
                pointPicking={selectionMode === "point"}
                layerVisibility={layerVisibility}
                onToggleLayer={toggleLayer}
                drawMode={drawMode}
                onPolygonDrawn={drawnPolygon}
                onDistrictToggle={toggleDistrict}
                selectedDistrictFids={selectedDistrictFids}
                bandThresholds={meta?.band_thresholds}
                bandLabels={meta?.band_labels}
              />
              <div className="pointer-events-none absolute right-3 top-3 z-10 flex max-w-[58%] flex-col items-end gap-1.5">
                <div className="rounded-lg border border-slate-700/60 bg-slate-950/80 px-2.5 py-1.5 backdrop-blur">
                  <p className="text-[9.5px] font-semibold uppercase tracking-[0.18em] text-slate-500">
                    {selectionMode === "point" ? "Sample point" : "Area selection"}
                  </p>
                  <p className="font-mono text-[12px] text-slate-100">
                    {selectionMode === "point"
                      ? point
                        ? `${point.lat.toFixed(4)}, ${point.lon.toFixed(4)}`
                        : "click the map to place one"
                      : analysis?.selection
                        ? `${analysis.selection.area_km2.toFixed(1)} km²`
                        : polygon
                          ? `${geometryVertexCount(polygon.geometry)} vertices`
                          : "draw, pick or upload a boundary"}
                  </p>
                </div>
                <div className="flex flex-wrap justify-end gap-1.5">
                  {selectionMode === "point" ? (
                    <Badge tone={sourceTone("unverified")}>{radius} window</Badge>
                  ) : analysis?.area ? (
                    <Badge tone={bandTone("moderate").chip}>{analysis.area.samples_total} samples</Badge>
                  ) : (
                    <Badge tone={sourceTone("unverified")}>not assessed yet</Badge>
                  )}
                  {mapStatus && <Badge>{mapStatus.features} boundary features</Badge>}
                </div>
              </div>
              <div className="pointer-events-none absolute bottom-3 left-3 z-10 max-w-[58%] rounded-lg border border-slate-700/60 bg-slate-950/80 px-2.5 py-1 text-[10.5px] text-slate-400 backdrop-blur">
                sample points are discrete band readings — no interpolated surface is drawn
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-slate-800/70 bg-slate-900/35 px-3 py-2 text-[11px] text-slate-500">
            {mapStatus?.busy ? (
              <span>loading stored boundaries for this window…</span>
            ) : mapStatus?.error ? (
              <span className="text-red-300">boundary layer failed: {mapStatus.error}</span>
            ) : (
              <span>
                boundaries: {mapStatus?.layer ?? "unknown"} · {mapStatus?.features ?? 0} features
                {mapStatus?.truncated ? " (page full — more exist outside the limit)" : ""} · zoom{" "}
                {mapStatus?.zoom ?? "—"}
              </span>
            )}
            <span aria-hidden className="hidden h-3 w-px bg-slate-800 sm:block" />
            <span>district outlines appear at zoom 7+; state outlines above that</span>
            <span aria-hidden className="hidden h-3 w-px bg-slate-800 sm:block" />
            <span>basemap © OpenStreetMap contributors, used under their tile policy for local testing</span>
          </div>

          {analysis ? (
            <AnalysisReport analysis={analysis} labels={meta?.band_labels} />
          ) : (
            <Card title="Report" aside="no run yet">
              <Prose>
                Scores, the factors behind each one, the provenance of every observation and the limitations of the
                method appear here after a run. Earlier runs stay under{" "}
                <a className="text-sky-400 underline" href="/history">
                  History
                </a>
                ; what the engine could and could not reach is on{" "}
                <a className="text-sky-400 underline" href="/sources">
                  Sources
                </a>
                .
              </Prose>
            </Card>
          )}
        </div>
      </div>

      <div className="pointer-events-none fixed bottom-4 right-4 z-[1000] w-[min(92vw,380px)] space-y-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.tone === "error" ? "alert" : "status"}
            className={`pointer-events-auto rounded-xl border px-3 py-2 text-[12px] shadow-2xl backdrop-blur ${
              toast.tone === "error"
                ? "border-red-500/40 bg-red-950/90 text-red-100"
                : "border-sky-500/40 bg-slate-900/90 text-slate-100"
            }`}
          >
            <div className="flex items-start gap-2">
              <p className="font-semibold">{toast.title}</p>
              <button
                onClick={() => dismissToast(toast.id)}
                aria-label="dismiss notification"
                className="ml-auto text-slate-400 transition hover:text-slate-100"
              >
                ×
              </button>
            </div>
            <p className="mt-1 break-words leading-relaxed opacity-90">{toast.body}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
