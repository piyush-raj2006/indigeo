/**
 * Types mirroring the responses the API actually returns.
 *
 * Each shape below was read off a live response recorded in
 * `../data/api_samples/` (see the README), not inferred from a spec. Optional fields are
 * optional because the engine omits them when a source did not answer — a missing
 * `severity` means "not measured", and the UI must render it as a gap.
 */

export type Band = "low" | "moderate" | "high" | "very_high" | "extreme" | "unassessed";

export interface Provenance {
  provider: string;
  endpoint: string;
  fetched_at: string | null;
  http_status: number | null;
  elapsed_ms: number | null;
  from_cache: boolean;
  license?: string | null;
  attribution?: string | null;
}

export interface Factor {
  name: string;
  source: string;
  weight: number;
  available: boolean;
  severity?: number;
  observed?: Record<string, number | string | null>;
  note?: string;
}

export interface HazardResult {
  hazard: string;
  score: number | null;
  band: Band;
  confidence: number;
  evidence_kind: string;
  method: string;
  data_completeness: {
    factors_total: number;
    factors_available: number;
    unavailable_sources: string[];
    confidence: number;
  };
  factors: Factor[];
  warnings: string[];
  provenance: Provenance[];
  details: Record<string, unknown>;
}

export interface AdminContext {
  lon: number;
  lat: number;
  label: string;
  state: string | null;
  state_code: string | null;
  district: string | null;
  district_code: string | null;
  inside_india: boolean;
  boundary_matched: boolean;
  boundary_uncertain: boolean;
  boundary_distance_m: number | null;
  warnings: string[];
  provenance: Provenance[];
}

export interface Recommendation {
  hazard: string;
  action: string;
  category: string;
  horizon: string;
  rationale: string;
  evidence: Record<string, unknown>;
  authority: string | null;
  guidance_url: string | null;
  limitations: string;
  requires_professional_assessment: boolean;
  score: number | null;
  method: string;
}

export interface InfrastructureLayerOutcome {
  status: string;
  count?: number;
  truncated?: boolean;
  reason?: string;
  /** "local" when a regional OSM extract answered, "overpass" for the public API. */
  origin?: string;
}

export interface LocalExtractInfo {
  name: string;
  file: string;
  vintage: string | null;
  source: string | null;
  bbox: number[];
}

export interface InfrastructureSummary {
  queried: boolean;
  deadline_seconds: number | null;
  elapsed_ms: number | null;
  answered: number;
  layers: Record<string, InfrastructureLayerOutcome>;
  warnings: string[];
  /** Layer counts by origin, e.g. { local: 3, overpass: 1 }. */
  origins?: Record<string, number>;
  /** Per-origin availability; "available" only when every layer it served answered. */
  origin_states?: Record<string, string>;
  local?: {
    status: string | null;
    extracts?: LocalExtractInfo[];
    query_bbox?: number[] | null;
    reason?: string | null;
  } | null;
}

export interface ScoringProxy {
  proxy: string;
  source: string;
  /** False when the factor's source did not answer; its weight was dropped, not imputed. */
  used: boolean;
}

export interface Scoring {
  mode: "spatial_heuristics" | "ml";
  engine: string;
  engine_version: string;
  /** Empty means no model was called. It is not a missing field. */
  ml_models_used: string[];
  heuristic_proxies: Record<string, ScoringProxy[]>;
  note?: string;
}

export interface Analysis {
  analysis_uuid: string;
  status: string;
  created_at: string;
  location: {
    lat: number;
    lon: number;
    radius_m: number;
    /** "polygon" for a drawn/uploaded selection; absent on records stored before areas existed. */
    geometry_kind?: "point" | "polygon";
    /** WKT of the drawn boundary, stored for GIS readers; null for point runs. */
    wkt?: string | null;
    admin: AdminContext;
  };
  overall: {
    /** "unknown" only for a drawn area where no sample scored at all; it is not a band. */
    band: Band | "unknown";
    hazards_assessed: string[];
    hazards_unassessed: string[];
    highest: string | null;
    highest_score: number | null;
    mean_confidence: number | null;
    confidence_by_hazard: Record<string, number>;
    /** Drawn-area runs only: how many samples landed in each overall band. */
    sample_bands?: Record<string, number>;
  };
  hazards: HazardResult[];
  /** The hazard names the run asked for; `hazards` above are the scored results. */
  requested_hazards?: string[];
  recommendations: Recommendation[];
  data_completeness: {
    sources: Record<string, string>;
    sources_available: string[];
    unavailable_layers: string[];
    provenance: Provenance[];
    evidence_warnings: string[];
  };
  limitations: string[];
  /** Absent on records stored before the read-back carried it; never assume it exists. */
  request?: Record<string, unknown>;
  /** Absent on records stored before the scoring block existed; derive from `hazards` then. */
  scoring?: Scoring;
  model_versions: { engine: string; engine_version: string; ml_models_used: string[] };
  is_synthetic: boolean;
  duration_ms: number;
  infrastructure_summary?: InfrastructureSummary;
  /** Present only on drawn-area runs (and absent on older point records). */
  selection?: AreaSelection | null;
  area?: AreaBlock | null;
  districts?: AreaDistrict[] | null;
  states?: AreaState[] | null;
  live_feeds?: LiveFeeds | null;
  map_overlays?: MapOverlays | null;
}

export interface AnalysisSummary {
  analysis_uuid: string;
  status: string;
  created_at: string;
  lat: number;
  lon: number;
  radius_m: number;
  geometry_kind?: "point" | "polygon" | string;
  /** Set only for drawn-area runs. */
  area_km2?: number | null;
  /** Null when the run failed before any hazard produced a band. */
  overall_band: Band | null;
  hazards: string[];
  warnings: number;
  duration_ms: number;
  requested_by: string | null;
}

export type JobStatus = "queued" | "running" | "completed" | "failed" | "interrupted";

/** A queued/running analysis. `result` stays empty until the run really finished. */
export interface AnalysisJob {
  job_uuid: string;
  kind: string;
  status: JobStatus;
  progress: number;
  message: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  elapsed_ms: number | null;
  request: {
    /** "polygon" when the job carries a drawn/uploaded geometry instead of lat/lon. */
    mode?: "point" | "polygon";
    lat: number | null;
    lon: number | null;
    radius_m: number | null;
    polygon: GeoJsonAreaGeometry | null;
    hazards: string[] | null;
    requested_by: string | null;
    include_infrastructure: boolean;
  };
  result: {
    analysis_uuid?: string;
    mode?: "point" | "polygon";
    /** Null for a point job: not applicable, not zero. */
    area_km2?: number | null;
    sample_count?: number | null;
    overall_band?: Band;
    hazards_assessed?: number;
    recommendations?: number;
    duration_ms?: number;
    include_infrastructure?: boolean;
    infrastructure_status?: string | null;
    infrastructure_layers?: Record<string, InfrastructureLayerOutcome>;
  };
  result_ref: string | null;
  error: string | null;
  poll: string;
  note: string;
}

export interface ProviderRow {
  provider: string;
  state: string;
  requires_auth: boolean;
  endpoint: string;
  http_status: number | null;
  latency_ms: number | null;
  detail: string;
}

export interface ProviderReport {
  observed_at: string | null;
  mode: string;
  providers: ProviderRow[];
  note: string;
}

export interface PlaceHit {
  label: string;
  kind: string;
  source: string;
  lat: number;
  lon: number;
  admin: Record<string, string | null>;
  exact: boolean;
  provenance: Record<string, string | null>;
}

export interface PlaceSearch {
  query: string;
  interpretation: string;
  results: PlaceHit[];
  warnings: string[];
  provenance: Provenance[];
  attribution: string | null;
}

export interface TerrainSummary {
  radius_m: number;
  sampled_radius_m: number;
  samples: number;
  nominal_resolution_m: number;
  elevation_mean_m: number | null;
  elevation_min_m: number | null;
  elevation_max_m: number | null;
  elevation_relief_m: number | null;
  elevation_at_point_m: number | null;
  local_low_point: boolean;
  bathymetry_only: boolean;
  data_gap: boolean;
  slope_deg: number | null;
  downslope_azimuth_deg?: number | null;
  /** tan(slope): the surface gradient, not a flow-accumulation value. */
  terrain_gradient_m_per_m?: number | null;
  derivative_note?: string;
}

export interface PointResolution {
  place: AdminContext;
  warnings: string[];
  provenance: Provenance[];
  osm_reverse: {
    label: string;
    kind: string;
    source: string;
    lat: number;
    lon: number;
    admin: Record<string, string | null>;
    osm: { type: string; id: number };
    provenance: Provenance;
  } | null;
  terrain?: {
    /** "local_raster" for an SRTM/Bhuvan GeoTIFF, otherwise the Terrarium tile URL. */
    source: string;
    dem_origin?: "local_raster" | "terrarium_tiles" | null;
    zoom: number | null;
    tiles_planned: number | null;
    nominal_resolution_m: number | null;
    tiles_fetched: number | null;
    tiles_missing: number | null;
    nodata_pixels: number | null;
    impossible_bathymetry_pixels: number | null;
    available: boolean;
    summary: TerrainSummary | null;
    reason?: string;
    /** Set only when a local raster was preferred but could not answer. */
    fallback_reason?: string | null;
    local_dem?: { status: string; reason?: string; raster?: Record<string, unknown> } | null;
    raster?: {
      name: string;
      file: string;
      bbox: number[];
      resolution_m: number | null;
      vintage: string | null;
      source: string | null;
      vertical_datum: string | null;
      declared_license: string | null;
    } | null;
    warnings?: string[];
  };
  radius_m?: number;
}

export interface Health {
  app: string;
  version: string;
  environment: string;
  engine: { name: string; version: string; ml_models_used: string[] };
  database: { ok: boolean; dialect: string; probe: number };
  spatial: {
    backend: string;
    layers: Record<string, number>;
    extents: Record<string, [number, number, number, number]>;
  };
  hazards_assessed: string[];
  caches?: {
    enabled: boolean;
    namespaces: Record<string, { name: string; entries: number; hits: number; misses: number; evictions: number; expirations: number }>;
    note: string;
  };
  note: string;
}

export interface LayerSummary {
  backend: string;
  path: string;
  layers: { table: string; features: number; extent: [number, number, number, number] | null }[];
}

export interface HazardMeta {
  hazards: string[];
  band_thresholds: [number, string][];
  band_labels: Record<string, string>;
  band_order: Record<string, number>;
  authorities: Record<string, [string, string]>;
  data_gaps: string[];
}

export interface GeoFeature {
  type: "Feature";
  properties: Record<string, string | number | boolean | null>;
  geometry: { type: string; coordinates: unknown } | null;
}

export interface GeoFeatureCollection {
  type: "FeatureCollection";
  features: GeoFeature[];
  layer: string;
  crs: string;
  bbox: [number, number, number, number];
  returned: number;
  truncated: boolean;
  limit: number;
  clipping: { requested: number | null; applied: boolean; note: string };
  note: string;
}

export interface ErrorEnvelope {
  error: { code: string; message: string; retryable?: boolean; details?: unknown };
}

// ---------------------------------------------------------------------------
// GeoJSON selections. The API accepts a bare Polygon/MultiPolygon geometry, a Feature
// wrapping one, or a FeatureCollection holding exactly one such feature; it rejects
// anything else rather than picking one silently.
// ---------------------------------------------------------------------------
export type GeoJsonPosition = [number, number];

export interface GeoJsonPolygon {
  type: "Polygon";
  /** First ring is the outer boundary, the rest are holes. Coordinates are [lon, lat]. */
  coordinates: GeoJsonPosition[][];
}

export interface GeoJsonMultiPolygon {
  type: "MultiPolygon";
  coordinates: GeoJsonPosition[][][];
}

export type GeoJsonAreaGeometry = GeoJsonPolygon | GeoJsonMultiPolygon;

export interface GeoJsonAreaFeature {
  type: "Feature";
  properties?: Record<string, unknown> | null;
  geometry: GeoJsonAreaGeometry;
}

export interface GeoJsonAreaFeatureCollection {
  type: "FeatureCollection";
  features: GeoJsonAreaFeature[];
}

/** Every shape `POST /analysis` and the jobs endpoint accept as `geometry`. */
export type GeoJsonAreaInput =
  | GeoJsonAreaGeometry
  | GeoJsonAreaFeature
  | GeoJsonAreaFeatureCollection;

// ---------------------------------------------------------------------------
// Drawn-area analysis payload (selection / area / districts / states).
// ---------------------------------------------------------------------------

export interface AreaSelection {
  mode: "polygon";
  area_km2: number;
  perimeter_km: number;
  /** [min_lon, min_lat, max_lon, max_lat] */
  bounds: [number, number, number, number];
  representative_point: { lat: number; lon: number };
  vertices: number;
  geometry: GeoJsonAreaGeometry;
}

export interface AreaSample {
  lat: number;
  lon: number;
  /** Highest hazard score at this sample; null when nothing was assessable there. */
  score: number | null;
  band: Band;
  hazards_assessed: number;
  terrain_available: boolean;
  terrain_source: string | null;
  elevation_at_point_m: number | null;
  relief_m: number | null;
  coast_distance_m: number | null;
}

export interface AreaHazardSummary {
  hazard: string;
  samples_total: number;
  samples_assessed: number;
  unassessed_samples: number;
  min_score: number | null;
  mean_score: number | null;
  max_score: number | null;
  band_at_min: Band;
  band_at_max: Band;
  /** How many samples landed in each band. */
  bands: Record<string, number>;
  mean_confidence: number | null;
  method: string;
  evidence_kind: string;
}

export interface AreaDistrict {
  district: string | null;
  district_code: string | null;
  state: string | null;
  overlap_km2: number;
  /** True when the loaded boundary file has no name for this polygon. */
  name_missing?: boolean;
}

export interface AreaState {
  state: string | null;
  state_code: string | null;
  overlap_km2: number;
}

export interface AreaBlock {
  mode: "polygon";
  area_km2: number;
  perimeter_km: number;
  bounds: [number, number, number, number];
  vertices: number;
  representative_point: { lat: number; lon: number };
  sample_radius_m: number;
  samples_total: number;
  samples: AreaSample[];
  hazard_summary: AreaHazardSummary[];
  /** The headline score per hazard is the worst sample, never an interior average. */
  aggregation: "worst_sampled_point";
  aggregation_note: string;
  districts: AreaDistrict[];
  states: AreaState[];
}

// ---------------------------------------------------------------------------
// Map overlays: everything the map may draw, plus the catalogue saying what it cannot.
// An unavailable layer carries a `reason`; an empty available layer means "measured, none found".
// ---------------------------------------------------------------------------

export interface OverlayCatalogueEntry {
  id:
    | "selection"
    | "hazard_samples"
    | "seismic_events"
    | "firms_hotspots"
    | "elevation_samples"
    | "osm_infrastructure"
    | "live_alerts"
    | "seismic_faults"
    | string;
  label: string;
  kind: "polygon" | "radius" | "points" | "lines" | "samples" | "mixed" | string;
  available: boolean;
  feature_count: number;
  reason?: string | null;
  note?: string | null;
  /** Present on `osm_infrastructure` and `live_alerts` only. */
  layers?: Record<string, OsmLayerOutcome>;
  feeds?: Record<string, string | null>;
}

export interface SeismicEventOverlay {
  magnitude: number;
  distance_km: number;
  lat: number;
  lon: number;
  place: string | null;
  /** USGS epoch milliseconds. */
  time: number | null;
  url: string | null;
}

export interface FireHotspotOverlay {
  lat: number;
  lon: number;
  brightness: number | null;
  confidence: string | number | null;
  frp: number | null;
  acq_date: string | null;
  acq_time: string | null;
  satellite: string | null;
  instrument: string | null;
}

export interface OsmLayerOutcome {
  status?: string;
  count?: number;
  truncated?: boolean;
  reason?: string;
  origin?: string;
  role?: string;
}

export interface OsmOverlayFeature {
  layer: string;
  lat: number;
  lon: number;
  name: string | null;
  kind: string | null;
  osm_id: number | string | null;
}

export interface LiveAlertOverlay {
  id: string | null;
  title: string | null;
  description: string | null;
  event: string | null;
  severity: string | null;
  severity_score: number | null;
  onset: string | null;
  expires: string | null;
  areas: string[];
  tags: string[];
  matched_by: string | null;
  /** GeoJSON polygon for the warning, when the feed supplied one. */
  geometry: GeoJsonAreaGeometry | GeoJsonPolygon | null;
  url: string | null;
  format: string | null;
}

export interface GaugeOverlay {
  station: string | null;
  river: string | null;
  lat: number | null;
  lon: number | null;
  level_m: number | null;
  warning_level_m: number | null;
  danger_level_m: number | null;
  trend: string | null;
  observed_at: string | null;
  severity: string | null;
  distance_km: number | null;
}

export interface BhuvanOverlay {
  features_in_feed: number | null;
  matched_features: number | null;
  hazards: Record<string, { max_severity: number; features: number; classes: string[] }>;
  /** Always false: the connector scores susceptibility but does not retain geometry. */
  geometry_available: false;
  note: string;
}

export interface MapOverlays {
  scope: "point" | "area";
  geometry: GeoJsonAreaGeometry | null;
  radius_m: number | null;
  samples: AreaSample[];
  seismic_events: SeismicEventOverlay[];
  fire_hotspots: FireHotspotOverlay[];
  osm_infrastructure: {
    queried: boolean;
    layers: Record<string, OsmLayerOutcome>;
    features: OsmOverlayFeature[];
    features_returned: number;
  };
  live_alerts: {
    imd?: LiveAlertOverlay[];
    cwc?: GaugeOverlay[];
    bhuvan?: BhuvanOverlay;
  };
  layers: OverlayCatalogueEntry[];
  notes: string[];
}

// ---------------------------------------------------------------------------
// Live Indian feeds (IMD warnings, CWC gauges, Bhuvan hazard maps).
// A feed that was never configured still appears, with its status and no matches.
// ---------------------------------------------------------------------------

export interface LiveFeedEntry {
  label: string | null;
  provider: string | null;
  env_var: string | null;
  /** available | unconfigured | unavailable | degraded */
  status: string | null;
  reason: string | null;
  alerts_total?: number | null;
  matched_count?: number | null;
  items?: LiveAlertOverlay[];
  gauges_within_radius?: number | null;
  search_radius_m?: number | null;
  max_severity?: number | null;
  unrated?: number | null;
  gauges?: GaugeOverlay[];
  features_in_feed?: number | null;
  matched_features?: number | null;
  unclassified_features?: number | null;
  hazards?: Record<string, { max_severity: number; features: number; classes: string[] }>;
}

export interface LiveFeeds {
  feeds: {
    imd?: LiveFeedEntry;
    cwc?: LiveFeedEntry;
    bhuvan?: LiveFeedEntry;
  };
  configured: boolean;
  /** Labels of feeds whose URL/env var is unset on this deployment. */
  unconfigured: string[];
  /** Labels of configured feeds that did not answer. */
  unavailable: string[];
  /** Hazard name → live-feed sources that actually contributed a factor to it. */
  blended_into: Record<string, string[]>;
  warnings: string[];
  note: string;
}
