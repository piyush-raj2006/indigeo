/**
 * Thin typed client for the IndiGeo REST API.
 *
 * Two rules: never invent a fallback value for data the API did not send, and never let a
 * transport failure look like an empty result. Both are surfaced as `ApiError` so the UI can
 * say plainly that a number was not obtained.
 */

import type {
  Analysis,
  AnalysisJob,
  AnalysisSummary,
  ErrorEnvelope,
  GeoFeatureCollection,
  GeoJsonAreaInput,
  HazardMeta,
  Health,
  LayerSummary,
  PointResolution,
  PlaceSearch,
  ProviderReport,
} from "./types";

export type AnalysisSelectionBody =
  | {
      lat: number;
      lon: number;
      radius_m?: string | number;
      hazards?: string[];
      include_infrastructure?: boolean;
      geometry?: never;
    }
  | {
      geometry: GeoJsonAreaInput;
      hazards?: string[];
      include_infrastructure?: boolean;
      lat?: never;
      lon?: never;
      radius_m?: never;
    };

export const API_BASE: string =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8123/api/v1";

/** True when this build points at an API a remote browser cannot reach. */
export const API_IS_LOOPBACK = /(^|\/\/)(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/.test(API_BASE);

const DEFAULT_TIMEOUT_MS = 20_000;
// The synchronous endpoint runs the whole evidence pipeline. Without lifelines it is
// measured at ~1-4 s; the ceiling here exists to abandon a request that has clearly hung.
export const ANALYSIS_TIMEOUT_MS = 120_000;
// `/providers?refresh=true` walks every provider, including Overpass.
export const PROVIDER_SWEEP_TIMEOUT_MS = 240_000;

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly details: unknown;

  constructor(message: string, init: { code: string; status: number; retryable?: boolean; details?: unknown }) {
    super(message);
    this.name = "ApiError";
    this.code = init.code;
    this.status = init.status;
    this.retryable = init.retryable ?? false;
    this.details = init.details;
  }
}

type RequestInitWithTimeout = RequestInit & { timeoutMs?: number };

async function request<T>(path: string, init?: RequestInitWithTimeout): Promise<T> {
  const timeoutMs = init?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const url = `${API_BASE}${path}`;
  let response: Response;
  try {
    // `timeoutMs` rides along in the init object; fetch ignores properties it does not know.
    response = await fetch(url, {
      ...init,
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    const name = error instanceof Error ? error.name : "";
    if (name === "TimeoutError" || name === "AbortError") {
      throw new ApiError(
        `No answer from ${url} within ${Math.round(timeoutMs / 1000)} s, so this page abandoned the request. ` +
          "The server may still be working; nothing was measured here.",
        { code: "timeout", status: 0, retryable: true, details: { timeout_ms: timeoutMs } },
      );
    }
    throw new ApiError(
      `No answer from the API at ${API_BASE}. Check that it is running, that NEXT_PUBLIC_API_BASE points at it, ` +
        "and that its CORS origin list includes this page's origin. This is a transport failure: nothing was measured." +
        (error instanceof Error && error.message ? ` (${error.message})` : ""),
      { code: "unreachable", status: 0, retryable: true, details: { base: API_BASE } },
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const envelope = (payload ?? {}) as ErrorEnvelope;
    const error = envelope.error;
    const retryable =
      error?.retryable ??
      (response.status === 429 || response.status === 502 || response.status === 503 || response.status === 504);
    let message =
      error?.message ?? `HTTP ${response.status} from ${path}, and the body was not the documented error envelope.`;
    if (response.status === 504) {
      message =
        `Gateway timeout (HTTP 504) for ${path}. A proxy gave up before the API answered; the heavy lifeline ` +
        "lookup belongs on /analysis/jobs, which returns immediately and is polled.";
    } else if (response.status === 429) {
      message = `Rate limited (HTTP 429) by the API or a provider behind it. Wait, then retry. ${message}`;
    }
    throw new ApiError(message, {
      code: error?.code ?? `http_${response.status}`,
      status: response.status,
      retryable,
      details: error?.details ?? (text.slice(0, 400) || null),
    });
  }
  if (payload === null) throw new ApiError(`${path} returned 200 with an unreadable body.`, { code: "bad_json", status: response.status });
  return payload as T;
}

const q = (params: Record<string, string | number | boolean | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
};

export const api = {
  health: () => request<Health>("/health"),

  providers: (refresh = false) =>
    request<ProviderReport>(refresh ? "/providers?refresh=true" : "/providers", {
      timeoutMs: refresh ? PROVIDER_SWEEP_TIMEOUT_MS : undefined,
    }),

  hazardMeta: () => request<HazardMeta>("/hazards"),

  layers: () => request<LayerSummary>("/layers"),

  layerGeoJson: (
    table: string,
    bbox: [number, number, number, number],
    options?: { limit?: number; clipAboveAreaDeg2?: number },
  ) =>
    request<GeoFeatureCollection>(
      `/layers/${encodeURIComponent(table)}/geojson` +
        q({
          bbox: bbox.join(","),
          limit: options?.limit,
          clip_above_area_deg2: options?.clipAboveAreaDeg2,
        }),
    ),

  searchPlaces: (query: string, limit = 8) => request<PlaceSearch>(`/locations/search${q({ q: query, limit })}`),

  resolvePoint: (lat: number, lon: number, radiusM?: string) =>
    request<PointResolution>(`/locations/point${q({ lat, lon, radius_m: radiusM })}`),

  /**
   * One selection per call: either a point with a radius, or a GeoJSON Polygon/MultiPolygon
   * (bare, Feature or single-feature FeatureCollection). The API rejects both at once.
   */
  analyse: (body: AnalysisSelectionBody) =>
    request<Analysis>("/analysis", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: ANALYSIS_TIMEOUT_MS,
    }),

  submitAnalysisJob: (body: AnalysisSelectionBody & { requested_by?: string }) =>
    request<AnalysisJob>("/analysis/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 15_000,
    }),

  analysisJob: (jobUuid: string) =>
    request<AnalysisJob>(`/analysis/jobs/${encodeURIComponent(jobUuid)}`, { timeoutMs: 10_000 }),

  analysisJobs: (limit = 10) => request<{ count: number; jobs: AnalysisJob[] }>(`/analysis/jobs${q({ limit })}`),

  analysis: (uuid: string) => request<Analysis>(`/analysis/${encodeURIComponent(uuid)}`),

  analyses: (limit = 20) => request<{ count: number; analyses: AnalysisSummary[] }>(`/analyses${q({ limit })}`),
};
