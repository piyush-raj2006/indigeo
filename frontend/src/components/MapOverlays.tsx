"use client";

import { CircleMarker, Polygon, Tooltip } from "react-leaflet";

import { bandColor, formatTime } from "@/lib/bands";
import type { GeoJsonAreaGeometry, GeoJsonPosition, MapOverlays } from "@/lib/types";
import type { LayerVisibility } from "@/components/MapLayerControl";

type LatLngRing = [number, number][];

function ringsOf(geometry: GeoJsonAreaGeometry | null | undefined): LatLngRing[] {
  if (!geometry) return [];
  const toRing = (ring: GeoJsonPosition[]): LatLngRing => ring.map(([lon, lat]) => [lat, lon]);
  if (geometry.type === "Polygon") return geometry.coordinates.map(toRing);
  return geometry.coordinates.flatMap((polygon) => polygon.map(toRing));
}

/** The submitted selection, drawn exactly as it was assessed. */
export function SelectionShape({
  geometry,
  label,
}: {
  geometry: GeoJsonAreaGeometry | null | undefined;
  label?: string;
}) {
  const rings = ringsOf(geometry);
  if (rings.length === 0) return null;
  return (
    <Polygon
      positions={rings}
      pathOptions={{ color: "#fbbf24", weight: 2, fillColor: "#f59e0b", fillOpacity: 0.08, dashArray: "6 4" }}
    >
      <Tooltip sticky direction="top">
        {label ?? "Drawn selection"}
      </Tooltip>
    </Polygon>
  );
}

/** Every measured overlay the run returned, filtered by the layer toggles. Nothing here is
 *  synthesised: an empty layer simply renders no markers. */
export function AnalysisOverlays({
  overlays,
  visible,
  selectionGeometry,
}: {
  overlays: MapOverlays | null;
  visible: LayerVisibility;
  selectionGeometry?: GeoJsonAreaGeometry | null;
}) {
  if (!overlays) return null;

  const boundary = selectionGeometry ?? overlays.geometry;

  return (
    <>
      {visible.selection && <SelectionShape geometry={boundary} />}

      {visible.hazard_samples &&
        overlays.samples.map((sample, index) => (
          <CircleMarker
            key={`sample-${index}-${sample.lat}-${sample.lon}`}
            center={[sample.lat, sample.lon]}
            radius={7}
            pathOptions={{
              color: bandColor(sample.band),
              weight: 2,
              fillColor: bandColor(sample.band),
              fillOpacity: 0.55,
            }}
          >
            <Tooltip direction="top">
              sample {index + 1}/{overlays.samples.length} · {sample.band.replace(/_/g, " ")} ·{" "}
              {sample.score === null ? "no hazard scored" : `peak ${sample.score.toFixed(1)}`} ·{" "}
              {sample.hazards_assessed} hazard(s) assessed
              {sample.elevation_at_point_m === null ? "" : ` · ${sample.elevation_at_point_m} m`}
            </Tooltip>
          </CircleMarker>
        ))}

      {visible.seismic_events &&
        overlays.seismic_events.map((event, index) => (
          <CircleMarker
            key={`quake-${index}-${event.lat}-${event.lon}-${event.time ?? ""}`}
            center={[event.lat, event.lon]}
            radius={Math.min(12, 3 + event.magnitude * 1.1)}
            pathOptions={{ color: "#a78bfa", weight: 1.2, fillColor: "#8b5cf6", fillOpacity: 0.28 }}
          >
            <Tooltip direction="top">
              M{event.magnitude.toFixed(1)} · {event.distance_km.toFixed(1)} km away · {event.place ?? "place not stated"}
              {event.time === null ? "" : ` · ${formatTime(new Date(event.time).toISOString())}`}
            </Tooltip>
          </CircleMarker>
        ))}

      {visible.firms_hotspots &&
        overlays.fire_hotspots.map((hotspot, index) => (
          <CircleMarker
            key={`fire-${index}-${hotspot.lat}-${hotspot.lon}`}
            center={[hotspot.lat, hotspot.lon]}
            radius={3.5}
            pathOptions={{ color: "#fb923c", weight: 1, fillColor: "#f97316", fillOpacity: 0.85 }}
          >
            <Tooltip direction="top">
              FIRMS detection · {hotspot.acq_date ?? "date not stated"}
              {hotspot.acq_time ? ` ${hotspot.acq_time}` : ""} · confidence {hotspot.confidence ?? "not stated"}
              {hotspot.brightness === null ? "" : ` · brightness ${hotspot.brightness}`}
            </Tooltip>
          </CircleMarker>
        ))}

      {visible.elevation_samples &&
        overlays.samples
          .filter((sample) => sample.terrain_available && sample.elevation_at_point_m !== null)
          .map((sample) => (
            <CircleMarker
              key={`elev-${sample.lat}-${sample.lon}`}
              center={[sample.lat, sample.lon]}
              radius={2.5}
              pathOptions={{ color: "#cbd5e1", weight: 1, fillColor: "#94a3b8", fillOpacity: 0.9 }}
            >
              <Tooltip direction="bottom">
                DEM {sample.elevation_at_point_m} m · relief {sample.relief_m ?? "not stated"} m ·{" "}
                {sample.terrain_source ?? "source not stated"}
              </Tooltip>
            </CircleMarker>
          ))}

      {visible.osm_infrastructure &&
        overlays.osm_infrastructure.features.map((feature, index) => (
          <CircleMarker
            key={`osm-${feature.layer}-${feature.osm_id ?? index}`}
            center={[feature.lat, feature.lon]}
            radius={3}
            pathOptions={{ color: "#22d3ee", weight: 1, fillColor: "#06b6d4", fillOpacity: 0.85 }}
          >
            <Tooltip direction="top">
              {feature.name ?? feature.kind ?? "unnamed feature"} · {feature.layer.replace(/_/g, " ")}
            </Tooltip>
          </CircleMarker>
        ))}

      {visible.live_alerts &&
        (overlays.live_alerts.imd ?? []).map((alert, index) => {
          const rings = ringsOf(
            alert.geometry && (alert.geometry.type === "Polygon" || alert.geometry.type === "MultiPolygon")
              ? (alert.geometry as GeoJsonAreaGeometry)
              : null,
          );
          if (rings.length > 0) {
            return (
              <Polygon
                key={`imd-${alert.id ?? index}`}
                positions={rings}
                pathOptions={{ color: "#f87171", weight: 1.5, fillColor: "#ef4444", fillOpacity: 0.1 }}
              >
                <Tooltip sticky direction="top">
                  IMD · {alert.title ?? alert.event ?? "warning"} · severity {alert.severity ?? "not stated"}
                  {alert.expires ? ` · expires ${alert.expires}` : ""}
                </Tooltip>
              </Polygon>
            );
          }
          return null;
        })}

      {visible.live_alerts &&
        (overlays.live_alerts.cwc ?? [])
          .filter((gauge) => gauge.lat !== null && gauge.lon !== null)
          .map((gauge, index) => (
            <CircleMarker
              key={`cwc-${gauge.station ?? index}`}
              center={[gauge.lat as number, gauge.lon as number]}
              radius={5}
              pathOptions={{ color: "#38bdf8", weight: 2, fillColor: "#0ea5e9", fillOpacity: 0.5 }}
            >
              <Tooltip direction="top">
                CWC gauge · {gauge.station ?? "station not stated"}
                {gauge.river ? ` on ${gauge.river}` : ""} · level {gauge.level_m ?? "not stated"} m ·{" "}
                {gauge.distance_km === null ? "distance not stated" : `${gauge.distance_km} km away`}
              </Tooltip>
            </CircleMarker>
          ))}
    </>
  );
}
