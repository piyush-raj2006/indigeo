"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { LeafletMouseEvent } from "leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import {
  Circle,
  CircleMarker,
  MapContainer,
  Polygon,
  Polyline,
  TileLayer,
  Tooltip,
  useMap,
  useMapEvents,
} from "react-leaflet";

import { AnalysisOverlays, SelectionShape } from "@/components/MapOverlays";
import {
  DEFAULT_LAYER_VISIBILITY,
  MapLayerControl,
  MapLegend,
  type LayerVisibility,
} from "@/components/MapLayerControl";
import { ApiError, api } from "@/lib/api";
import { featureKey, geometryBounds, geometryVertexCount } from "@/lib/geojson";
import type {
  Analysis,
  GeoFeature,
  GeoJsonAreaGeometry,
  GeoJsonPosition,
  MapOverlays,
} from "@/lib/types";

export interface MapStatus {
  layer: string | null;
  features: number;
  truncated: boolean;
  zoom: number;
  busy: boolean;
  error: string | null;
}

export interface MapFocus {
  lat: number;
  lon: number;
  radiusM: number;
  /** Bumped by the caller so choosing the same place twice still recentres. */
  token: number;
  /** When set, the view fits this [min_lon, min_lat, max_lon, max_lat] box instead of a radius. */
  bounds?: [number, number, number, number];
}

export type DrawMode = "off" | "polygon" | "district";

const INDIA_WINDOW: [number, number, number, number] = [68.1, 6.75, 97.4, 37.1];
const DISTRICT_ZOOM = 7;
const DRAFT_VERTEX_LIMIT = 5000;

/** Rings as Leaflet wants them, from the GeoJSON the API actually sends. */
function ringsOf(geometry: GeoFeature["geometry"]): L.LatLngExpression[][] {
  if (!geometry) return [];
  const coords = geometry.coordinates as unknown;
  const toRing = (ring: unknown[]) =>
    (ring as [number, number][]).map(([lon, lat]) => [lat, lon] as L.LatLngExpression);
  if (geometry.type === "Polygon") return (coords as unknown[][]).map(toRing);
  if (geometry.type === "MultiPolygon") return (coords as unknown[][][]).flat().map(toRing);
  return [];
}

function BoundaryLayer({
  onStatus,
  districtMode,
  selectedFids,
  onToggleDistrict,
}: {
  onStatus: (status: MapStatus) => void;
  districtMode: boolean;
  selectedFids: Set<string>;
  onToggleDistrict: (feature: GeoFeature) => void;
}) {
  const map = useMap();
  const request = useRef(0);
  const [features, setFeatures] = useState<GeoFeature[]>([]);

  const load = useCallback(async () => {
    const bounds = map.getBounds();
    const zoom = map.getZoom();
    const table = zoom >= DISTRICT_ZOOM ? "admin_districts" : "admin_states";
    const id = ++request.current;
    onStatus({ layer: table, features: 0, truncated: false, zoom, busy: true, error: null });

    const bbox: [number, number, number, number] = [
      Math.max(bounds.getWest(), INDIA_WINDOW[0]),
      Math.max(bounds.getSouth(), INDIA_WINDOW[1]),
      Math.min(bounds.getEast(), INDIA_WINDOW[2]),
      Math.min(bounds.getNorth(), INDIA_WINDOW[3]),
    ];
    if (bbox[0] >= bbox[2] || bbox[1] >= bbox[3]) {
      // The viewport misses the loaded boundaries. Say so rather than drawing an empty map silently.
      setFeatures([]);
      onStatus({ layer: table, features: 0, truncated: false, zoom, busy: false, error: null });
      return;
    }
    try {
      const collection = await api.layerGeoJson(table, bbox, { limit: 4000 });
      if (id !== request.current) return;
      setFeatures(collection.features);
      onStatus({
        layer: collection.layer,
        features: collection.returned,
        truncated: collection.truncated,
        zoom: map.getZoom(),
        busy: false,
        error: null,
      });
    } catch (error) {
      if (id !== request.current) return;
      setFeatures([]);
      onStatus({
        layer: table,
        features: 0,
        truncated: false,
        zoom,
        busy: false,
        error: error instanceof ApiError ? error.message : String(error),
      });
    }
  }, [map, onStatus]);

  useEffect(() => {
    // Deferred off the effect body (the status push is parent state), but with a microtask and not
    // requestAnimationFrame: a backgrounded tab never fires animation frames, and the boundaries
    // would silently never load there.
    queueMicrotask(() => void load());
  }, [load]);
  useMapEvents({ moveend: () => void load(), zoomend: () => void load() });

  return (
    <>
      {features.map((feature) => {
        const shape = ringsOf(feature.geometry);
        if (shape.length === 0) return null;
        const district = feature.properties.district as string | undefined;
        const state = feature.properties.state as string | undefined;
        const stateName = feature.properties.state_name as string | undefined;
        const vintage = feature.properties.vintage as string | undefined;
        const fid = featureKey(feature);
        const selected = selectedFids.has(fid);
        return (
          <Polygon
            key={fid}
            positions={shape}
            pathOptions={
              selected
                ? { color: "#fbbf24", weight: 2, fillColor: "#f59e0b", fillOpacity: 0.2 }
                : { color: "#38bdf8", weight: 0.8, fillColor: "#0ea5e9", fillOpacity: 0.06 }
            }
            eventHandlers={{
              click: (event) => {
                if (!districtMode) return; // let the click reach the map for point/draw modes
                L.DomEvent.stopPropagation(event.originalEvent);
                onToggleDistrict(feature);
              },
            }}
          >
            <Tooltip sticky direction="top">
              <strong>{district ?? state ?? `fid ${feature.properties.fid}`}</strong>
              {stateName ? `, ${stateName}` : ""}
              <br />
              {districtMode
                ? selected
                  ? "picked — click to remove it from the selection"
                  : "click to add this boundary to the selection"
                : "stored boundary — geometry only, no hazard score"}
              {vintage ? ` (vintage ${vintage})` : ""}
            </Tooltip>
          </Polygon>
        );
      })}
    </>
  );
}

function PickHandler({ onPick, active }: { onPick: (lat: number, lon: number) => void; active: boolean }) {
  useMapEvents({
    click: (event: LeafletMouseEvent) => {
      if (active) onPick(event.latlng.lat, event.latlng.lng);
    },
  });
  return null;
}

function DraftClickHandler({ active, onAdd }: { active: boolean; onAdd: (lat: number, lon: number) => void }) {
  useMapEvents({
    click: (event: LeafletMouseEvent) => {
      if (active) onAdd(event.latlng.lat, event.latlng.lng);
    },
  });
  return null;
}

/** Zoom that roughly fits the sample radius on screen, clamped to the tile limit. */
function zoomForRadius(radiusM: number): number {
  if (!Number.isFinite(radiusM) || radiusM <= 5000) return 12;
  if (radiusM <= 10000) return 11;
  return 10;
}

const FLY_SECONDS = 0.8;

function Recenter({ focus }: { focus: MapFocus | null }) {
  const map = useMap();
  useEffect(() => {
    if (!focus) return;
    const visible = typeof document === "undefined" || document.visibilityState === "visible";
    if (focus.bounds) {
      const [[minLon, minLat], [maxLon, maxLat]] = [
        [focus.bounds[0], focus.bounds[1]],
        [focus.bounds[2], focus.bounds[3]],
      ];
      // fitBounds animates on requestAnimationFrame, which a backgrounded tab never fires.
      map.fitBounds(
        L.latLngBounds([minLat, minLon], [maxLat, maxLon]),
        { padding: [24, 24], maxZoom: 12, animate: visible },
      );
      return;
    }
    const zoom = zoomForRadius(focus.radiusM);
    // flyTo animates on requestAnimationFrame, which a backgrounded tab never fires; there
    // the view would never arrive, so land it immediately instead.
    if (visible) {
      map.flyTo([focus.lat, focus.lon], zoom, { duration: FLY_SECONDS });
    } else {
      map.setView([focus.lat, focus.lon], zoom, { animate: false });
    }
  }, [map, focus]);
  return null;
}

export interface MapCanvasProps {
  point: { lat: number; lon: number } | null;
  radiusM: number;
  focus?: MapFocus | null;
  onPick: (lat: number, lon: number) => void;
  onStatus: (status: MapStatus) => void;
  analysis: Analysis | null;
  selectionGeometry: GeoJsonAreaGeometry | null;
  selectionOrigin: "draw" | "district" | "upload" | null;
  /** True only in point mode: map clicks place the sample point. */
  pointPicking: boolean;
  layerVisibility?: LayerVisibility;
  onToggleLayer?: (id: string, next: boolean) => void;
  drawMode: DrawMode;
  onPolygonDrawn: (geometry: GeoJsonAreaGeometry) => void;
  onDistrictToggle: (feature: GeoFeature) => void;
  selectedDistrictFids: Set<string>;
  bandThresholds?: [number, string][];
  bandLabels?: Record<string, string>;
}

export default function MapCanvas({
  point,
  radiusM,
  focus,
  onPick,
  onStatus,
  analysis,
  selectionGeometry,
  selectionOrigin,
  pointPicking,
  layerVisibility,
  onToggleLayer,
  drawMode,
  onPolygonDrawn,
  onDistrictToggle,
  selectedDistrictFids,
  bandThresholds,
  bandLabels,
}: MapCanvasProps) {
  // The draft is tagged with the mode that collected it, so any mode change (in either
  // direction) starts from an empty draft without an effect that would cascade renders.
  const [draft, setDraft] = useState<{ mode: DrawMode; points: [number, number][] }>({ mode: drawMode, points: [] });
  if (draft.mode !== drawMode) setDraft({ mode: drawMode, points: [] });
  const draftPoints = draft.mode === drawMode ? draft.points : [];
  const overlays: MapOverlays | null = analysis?.map_overlays ?? null;
  const visible = layerVisibility ?? DEFAULT_LAYER_VISIBILITY;

  const addVertex = (lat: number, lon: number) => {
    setDraft((current) => {
      const points = current.mode === drawMode ? current.points : [];
      return points.length >= DRAFT_VERTEX_LIMIT
        ? { mode: drawMode, points }
        : { mode: drawMode, points: [...points, [lat, lon]] };
    });
  };

  const finishPolygon = () => {
    if (draftPoints.length < 3) return;
    const ring: GeoJsonPosition[] = draftPoints.map(([lat, lon]) => [lon, lat]);
    const [firstLon, firstLat] = ring[0];
    ring.push([firstLon, firstLat]);
    onPolygonDrawn({ type: "Polygon", coordinates: [ring] });
    setDraft({ mode: drawMode, points: [] });
  };

  const undoVertex = () =>
    setDraft((current) => ({
      mode: drawMode,
      points: (current.mode === drawMode ? current.points : []).slice(0, -1),
    }));
  const areaSelection = drawMode !== "off";

  return (
    <div className="relative h-full w-full">
      <MapContainer
        center={[22.9, 79.5]}
        zoom={5}
        minZoom={4}
        maxZoom={12}
        preferCanvas
        className="h-full w-full"
        style={{ background: "#020617" }}
      >
        <TileLayer
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          maxZoom={12}
        />
        <BoundaryLayer
          onStatus={onStatus}
          districtMode={drawMode === "district"}
          selectedFids={selectedDistrictFids}
          onToggleDistrict={onDistrictToggle}
        />
        <PickHandler onPick={onPick} active={pointPicking && drawMode === "off"} />
        <DraftClickHandler active={drawMode === "polygon"} onAdd={addVertex} />
        <Recenter focus={focus ?? null} />
        {point && pointPicking && !areaSelection && !selectionGeometry && (
          <>
            <Circle
              center={[point.lat, point.lon]}
              radius={radiusM}
              pathOptions={{ color: "#fbbf24", weight: 1, fillColor: "#f59e0b", fillOpacity: 0.08, dashArray: "4 3" }}
            >
              <Tooltip direction="bottom">
                sample radius {(radiusM / 1000).toFixed(1)} km (the DEM window the engine measured)
              </Tooltip>
            </Circle>
            <CircleMarker
              center={[point.lat, point.lon]}
              radius={6}
              pathOptions={{ color: "#f8fafc", weight: 1.5, fillColor: "#ef4444", fillOpacity: 1 }}
            >
              <Tooltip direction="right">
                {point.lat.toFixed(4)}, {point.lon.toFixed(4)}
              </Tooltip>
            </CircleMarker>
          </>
        )}

        {selectionGeometry && <SelectionShape geometry={selectionGeometry} />}

        {draftPoints.length > 0 && (
          <>
            {draftPoints.length >= 3 ? (
              <Polygon
                positions={draftPoints}
                pathOptions={{ color: "#fbbf24", weight: 1.5, dashArray: "5 4", fillColor: "#f59e0b", fillOpacity: 0.1 }}
              />
            ) : (
              <Polyline positions={draftPoints} pathOptions={{ color: "#fbbf24", weight: 1.5 }} />
            )}
            {draftPoints.map(([lat, lon], index) => (
              <CircleMarker
                key={`draft-${index}`}
                center={[lat, lon]}
                radius={4}
                pathOptions={{ color: "#f8fafc", weight: 1.2, fillColor: "#fbbf24", fillOpacity: 1 }}
              >
                <Tooltip direction="top">
                  vertex {index + 1} · {lat.toFixed(4)}, {lon.toFixed(4)}
                </Tooltip>
              </CircleMarker>
            ))}
          </>
        )}

        <AnalysisOverlays
          overlays={overlays}
          visible={visible}
          selectionGeometry={selectionGeometry}
        />
      </MapContainer>

      <div className="pointer-events-none absolute left-3 top-3 z-20 flex w-[236px] flex-col gap-2">
        <MapLayerControl
          overlays={overlays}
          visible={visible}
          onToggle={(id, next) => onToggleLayer?.(id, next)}
        />

        {drawMode === "polygon" && (
          <div className="pointer-events-auto rounded-xl border border-amber-500/40 bg-slate-950/90 px-2.5 py-2 text-[11px] text-slate-200 shadow-xl backdrop-blur">
            <p className="font-semibold text-amber-200">Draw a bounding polygon</p>
            <p className="mt-0.5 leading-snug text-slate-400">
              {draftPoints.length === 0
                ? "Click the map to place the first corner."
                : `${draftPoints.length} corner${draftPoints.length === 1 ? "" : "s"} placed. Add at least three, then finish.`}
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1">
              <button
                type="button"
                onClick={finishPolygon}
                disabled={draftPoints.length < 3}
                className="rounded-md bg-amber-500 px-2 py-0.5 font-semibold text-slate-950 transition hover:bg-amber-400 disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500"
              >
                finish ({draftPoints.length})
              </button>
              <button
                type="button"
                onClick={undoVertex}
                disabled={draftPoints.length === 0}
                className="rounded-md border border-slate-700 px-2 py-0.5 text-slate-300 transition hover:border-slate-500 disabled:opacity-40"
              >
                undo
              </button>
              <button
                type="button"
                onClick={() => setDraft({ mode: drawMode, points: [] })}
                className="rounded-md border border-slate-700 px-2 py-0.5 text-slate-300 transition hover:border-slate-500"
              >
                clear
              </button>
            </div>
          </div>
        )}

        {drawMode === "district" && (
          <div className="pointer-events-auto rounded-xl border border-sky-500/40 bg-slate-950/90 px-2.5 py-2 text-[11px] text-slate-200 shadow-xl backdrop-blur">
            <p className="font-semibold text-sky-200">Pick administrative boundaries</p>
            <p className="mt-0.5 leading-snug text-slate-400">
              {selectedDistrictFids.size === 0
                ? "Click a district outline to add it; click again to remove it. Zoom to 7+ for districts."
                : `${selectedDistrictFids.size} boundar${selectedDistrictFids.size === 1 ? "y" : "ies"} picked; they are merged into one MultiPolygon.`}
            </p>
          </div>
        )}

        {selectionGeometry && (
          <div className="pointer-events-auto rounded-xl border border-slate-700/60 bg-slate-950/85 px-2.5 py-1.5 text-[10.5px] text-slate-300 shadow-xl backdrop-blur">
            <p className="font-semibold uppercase tracking-[0.14em] text-slate-500">Selection</p>
            <p className="mt-0.5">
              {selectionOrigin === "district"
                ? `merged from ${selectedDistrictFids.size || "picked"} boundaries`
                : selectionOrigin === "upload"
                  ? "uploaded GeoJSON"
                  : "drawn polygon"}{" "}
              · {geometryVertexCount(selectionGeometry)} vertices
            </p>
            <p className="font-mono text-[10px] text-slate-500">
              {geometryBounds(selectionGeometry)
                .map((value) => value.toFixed(2))
                .join(", ")}
            </p>
          </div>
        )}
      </div>

      <div className="pointer-events-none absolute bottom-3 right-3 z-20">
        <MapLegend
          overlays={overlays}
          visible={visible}
          bandThresholds={bandThresholds}
          bandLabels={bandLabels}
        />
      </div>
    </div>
  );
}
