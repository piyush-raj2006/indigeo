/**
 * GeoJSON selection helpers: parse an uploaded boundary, merge picked districts into one
 * geometry, and build the export file. Everything here is client-side validation of the
 * *shape* only; the engine remains the authority on area, vertices and India coverage.
 */

import type {
  Analysis,
  GeoFeature,
  GeoJsonAreaFeature,
  GeoJsonAreaFeatureCollection,
  GeoJsonAreaGeometry,
  GeoJsonPosition,
} from "./types";

export const MAX_SELECTION_VERTICES = 5000;
/** Same envelope the API uses for its "selection lies outside India" rejection. */
const INDIA_BBOX: [number, number, number, number] = [68.1, 6.6, 97.4, 37.1];

export class GeoJsonSelectionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GeoJsonSelectionError";
  }
}

function isPosition(value: unknown): value is GeoJsonPosition {
  return (
    Array.isArray(value) &&
    value.length >= 2 &&
    Number.isFinite(value[0]) &&
    Number.isFinite(value[1]) &&
    Number.isFinite(value[2] ?? 0)
  );
}

function closeRing(ring: GeoJsonPosition[]): GeoJsonPosition[] {
  if (ring.length === 0) return ring;
  const [firstLon, firstLat] = ring[0];
  const [lastLon, lastLat] = ring[ring.length - 1];
  return firstLon === lastLon && firstLat === lastLat ? ring : [...ring, [firstLon, firstLat]];
}

function normalizeRing(raw: unknown, index: number): GeoJsonPosition[] {
  if (!Array.isArray(raw)) {
    throw new GeoJsonSelectionError(`Ring ${index + 1} is not an array of positions.`);
  }
  const ring = raw.map((position) => {
    if (!isPosition(position)) {
      throw new GeoJsonSelectionError(
        `Ring ${index + 1} contains a position that is not a [lon, lat] pair of finite numbers.`,
      );
    }
    if (position[0] < -180 || position[0] > 180 || position[1] < -90 || position[1] > 90) {
      throw new GeoJsonSelectionError(
        `Ring ${index + 1} has a position outside the valid longitude/latitude range: [${position[0]}, ${position[1]}].`,
      );
    }
    return [position[0], position[1]] as GeoJsonPosition;
  });
  const closed = closeRing(ring);
  const distinct = new Set(closed.slice(0, -1).map(([lon, lat]) => `${lon},${lat}`)).size;
  if (distinct < 3) {
    throw new GeoJsonSelectionError(
      `Ring ${index + 1} needs at least three distinct corners; it has ${distinct}.`,
    );
  }
  return closed;
}

function normalizePolygonCoordinates(raw: unknown): GeoJsonPosition[][] {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new GeoJsonSelectionError("A Polygon needs at least one ring of coordinates.");
  }
  return raw.map((ring, index) => normalizeRing(ring, index));
}

function normalizeMultiPolygonCoordinates(raw: unknown): GeoJsonPosition[][][] {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new GeoJsonSelectionError("A MultiPolygon needs at least one polygon of coordinates.");
  }
  return raw.map((polygon, index) => {
    if (!Array.isArray(polygon) || polygon.length === 0) {
      throw new GeoJsonSelectionError(`MultiPolygon part ${index + 1} has no rings.`);
    }
    return polygon.map((ring, ringIndex) => normalizeRing(ring, ringIndex));
  });
}

/** Validate one geometry and return a closed-ring copy. Throws `GeoJsonSelectionError`. */
export function normalizeSelectionGeometry(value: unknown): GeoJsonAreaGeometry {
  if (!value || typeof value !== "object") {
    throw new GeoJsonSelectionError("The geometry is missing or is not a JSON object.");
  }
  const geometry = value as { type?: unknown; coordinates?: unknown };
  if (geometry.type === "Polygon") {
    return { type: "Polygon", coordinates: normalizePolygonCoordinates(geometry.coordinates) };
  }
  if (geometry.type === "MultiPolygon") {
    return { type: "MultiPolygon", coordinates: normalizeMultiPolygonCoordinates(geometry.coordinates) };
  }
  throw new GeoJsonSelectionError(
    `Only Polygon or MultiPolygon selections can be assessed; this file contains ${
      typeof geometry.type === "string" ? `a ${geometry.type}` : "no geometry type"
    }.`,
  );
}

/**
 * Parse an uploaded file. A FeatureCollection must hold exactly one feature: silently
 * picking one of several would assess a boundary the user did not choose.
 */
export function parseGeoJsonSelection(text: string): GeoJsonAreaGeometry {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new GeoJsonSelectionError(
      `That file is not valid JSON${error instanceof Error && error.message ? ` (${error.message})` : ""}.`,
    );
  }
  const root = parsed as Record<string, unknown> | null;
  if (!root || typeof root !== "object") {
    throw new GeoJsonSelectionError("The file does not contain a JSON object.");
  }
  if (root.type === "FeatureCollection") {
    const features = root.features;
    if (!Array.isArray(features) || features.length === 0) {
      throw new GeoJsonSelectionError("This FeatureCollection holds no features.");
    }
    if (features.length > 1) {
      throw new GeoJsonSelectionError(
        `This FeatureCollection holds ${features.length} features. Merge them into one Polygon or MultiPolygon first — ` +
          "the upload is not allowed to pick one boundary for you.",
      );
    }
    const feature = features[0] as GeoJsonAreaFeature | undefined;
    return normalizeSelectionGeometry(feature?.geometry);
  }
  if (root.type === "Feature") {
    return normalizeSelectionGeometry((root as unknown as GeoJsonAreaFeature).geometry);
  }
  return normalizeSelectionGeometry(root);
}

/** Stable identity for a stored boundary feature, shared by the map and the picker list. */
export function featureKey(feature: GeoFeature): string {
  const fid = feature.properties.fid;
  if (fid !== undefined && fid !== null) return String(fid);
  const district = feature.properties.district ?? feature.properties.state ?? "boundary";
  return `${district}-${feature.properties.state_code ?? ""}`;
}

export function geometryVertexCount(geometry: GeoJsonAreaGeometry): number {  const countRings = (rings: GeoJsonPosition[][]) => rings.reduce((total, ring) => total + ring.length, 0);
  if (geometry.type === "Polygon") return countRings(geometry.coordinates);
  return geometry.coordinates.reduce((total, polygon) => total + countRings(polygon), 0);
}

export function geometryBounds(geometry: GeoJsonAreaGeometry): [number, number, number, number] {
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  const visit = (ring: GeoJsonPosition[]) => {
    for (const [lon, lat] of ring) {
      if (lon < minLon) minLon = lon;
      if (lon > maxLon) maxLon = lon;
      if (lat < minLat) minLat = lat;
      if (lat > maxLat) maxLat = lat;
    }
  };
  if (geometry.type === "Polygon") geometry.coordinates.forEach(visit);
  else geometry.coordinates.forEach((polygon) => polygon.forEach(visit));
  return [minLon, minLat, maxLon, maxLat];
}

/** Client-side guard mirroring the engine's limits, so an upload fails before a round trip. */
export function assertSelectionWithinLimits(geometry: GeoJsonAreaGeometry): void {
  const vertices = geometryVertexCount(geometry);
  if (vertices > MAX_SELECTION_VERTICES) {
    throw new GeoJsonSelectionError(
      `This boundary has ${vertices} vertices; the engine accepts up to ${MAX_SELECTION_VERTICES} per request.`,
    );
  }
  const [minLon, minLat, maxLon, maxLat] = geometryBounds(geometry);
  const overlapsIndia =
    !(maxLat < INDIA_BBOX[1] || minLat > INDIA_BBOX[3] || maxLon < INDIA_BBOX[0] || minLon > INDIA_BBOX[2]);
  if (!overlapsIndia) {
    throw new GeoJsonSelectionError("The selection lies outside India, so the engine will not assess it.");
  }
}

function ringsOfFeature(feature: GeoFeature): GeoJsonPosition[][][] {
  const geometry = feature.geometry as { type?: string; coordinates?: unknown } | null;
  if (!geometry) return [];
  const coords = geometry.coordinates as unknown;
  if (geometry.type === "Polygon") {
    return Array.isArray(coords) ? [coords as GeoJsonPosition[][]] : [];
  }
  if (geometry.type === "MultiPolygon") {
    return Array.isArray(coords) ? (coords as GeoJsonPosition[][][]) : [];
  }
  return [];
}

/** Combine picked district/state polygons into one Polygon or MultiPolygon selection. */
export function mergeFeatureGeometries(features: GeoFeature[]): GeoJsonAreaGeometry {
  const polygons = features.flatMap(ringsOfFeature);
  if (polygons.length === 0) {
    throw new GeoJsonSelectionError("None of the picked boundaries carries a polygon geometry.");
  }
  const normalized = polygons.map((polygon) => polygon.map((ring, index) => normalizeRing(ring, index)));
  const geometry: GeoJsonAreaGeometry =
    normalized.length === 1
      ? { type: "Polygon", coordinates: normalized[0] }
      : { type: "MultiPolygon", coordinates: normalized };
  assertSelectionWithinLimits(geometry);
  return geometry;
}

export interface SelectionExport {
  filename: string;
  collection: GeoJsonAreaFeatureCollection;
}

/** The export is the assessed boundary plus, when a run exists, the record it belongs to. */
export function buildSelectionExport(geometry: GeoJsonAreaGeometry, analysis?: Analysis | null): SelectionExport {
  const shortId = analysis ? analysis.analysis_uuid.slice(0, 8) : new Date().toISOString().slice(0, 10);
  const collection: GeoJsonAreaFeatureCollection = {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: {
          analysis_uuid: analysis?.analysis_uuid ?? null,
          generated_at: new Date().toISOString(),
          selection_source: "indigeo",
          geometry_kind: "polygon",
          area_km2: analysis?.selection?.area_km2 ?? null,
          overall_band: analysis?.overall.band ?? null,
          highest_hazard: analysis?.overall.highest ?? null,
          highest_score: analysis?.overall.highest_score ?? null,
          mean_confidence: analysis?.overall.mean_confidence ?? null,
          engine: analysis?.model_versions.engine ?? null,
          engine_version: analysis?.model_versions.engine_version ?? null,
          note: analysis
            ? "Boundary as submitted for this analysis and stored with it; no geometry was derived."
            : "Selection boundary only; it has not been analysed yet, so no score is attached.",
        },
        geometry,
      },
    ],
  };
  return { filename: `indigeo-selection-${shortId}.geojson`, collection };
}

export function downloadJson(filename: string, payload: unknown): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/geo+json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking immediately can cancel the download in some browsers; one tick is enough.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function formatBounds(bounds: [number, number, number, number]): string {
  const [minLon, minLat, maxLon, maxLat] = bounds;
  return `${minLat.toFixed(3)}, ${minLon.toFixed(3)} → ${maxLat.toFixed(3)}, ${maxLon.toFixed(3)}`;
}

/** Centre, half-span and bounding box for zooming the map onto a selection. */
export function selectionView(geometry: GeoJsonAreaGeometry): {
  lat: number;
  lon: number;
  radiusM: number;
  bounds: [number, number, number, number];
} {
  const bounds = geometryBounds(geometry);
  const lat = (bounds[1] + bounds[3]) / 2;
  const lon = (bounds[0] + bounds[2]) / 2;
  const latSpanM = Math.abs(bounds[3] - bounds[1]) * 111_320;
  const lonSpanM = Math.abs(bounds[2] - bounds[0]) * 111_320 * Math.cos((lat * Math.PI) / 180);
  return { lat, lon, radiusM: Math.max(latSpanM, lonSpanM) / 2, bounds };
}
