"use client";

import { useState } from "react";

import { bandColor, bandLabel } from "@/lib/bands";
import type { MapOverlays, OverlayCatalogueEntry } from "@/lib/types";

export type LayerVisibility = Record<string, boolean>;

/** Everything the engine can draw is on by default except the elevation readings, which
 *  duplicate the sample positions. Fault lines default off because the layer does not exist. */
export const DEFAULT_LAYER_VISIBILITY: LayerVisibility = {
  selection: true,
  hazard_samples: true,
  seismic_events: true,
  firms_hotspots: true,
  elevation_samples: false,
  osm_infrastructure: true,
  live_alerts: true,
  seismic_faults: false,
};

const LAYER_SYMBOL: Record<string, "ring" | "circle" | "triangle" | "square" | "diamond" | "dash"> = {
  selection: "ring",
  hazard_samples: "circle",
  seismic_events: "ring",
  firms_hotspots: "triangle",
  elevation_samples: "square",
  osm_infrastructure: "diamond",
  live_alerts: "circle",
  seismic_faults: "dash",
};

const SYMBOL_COLOR: Record<string, string> = {
  selection: "#fbbf24",
  hazard_samples: "#f43f5e",
  seismic_events: "#a78bfa",
  firms_hotspots: "#fb923c",
  elevation_samples: "#94a3b8",
  osm_infrastructure: "#22d3ee",
  live_alerts: "#f87171",
  seismic_faults: "#e2e8f0",
};

const NO_RUN_LAYERS: { id: string; label: string; reason: string }[] = [
  { id: "selection", label: "Selection boundary", reason: "No selection has been run yet." },
  { id: "hazard_samples", label: "Hazard sample points", reason: "No run yet; samples appear after an analysis." },
  { id: "seismic_events", label: "Historical earthquakes (USGS)", reason: "No run yet; epicentres appear after an analysis." },
  { id: "firms_hotspots", label: "NASA FIRMS hotspots", reason: "No run yet; hotspots appear after an analysis." },
  { id: "elevation_samples", label: "Elevation samples (DEM)", reason: "No run yet; DEM readings appear after an analysis." },
  { id: "osm_infrastructure", label: "OSM lifeline infrastructure", reason: "No run yet; lifelines appear after a queued run." },
  { id: "live_alerts", label: "Live IMD / CWC alerts", reason: "No run yet; live-feed matches appear after an analysis." },
  {
    id: "seismic_faults",
    label: "Seismic fault lines",
    reason: "No fault-line dataset is ingested in this build.",
  },
];

export function LayerSymbol({ kind, color }: { kind: string; color: string }) {
  const shape = LAYER_SYMBOL[kind] ?? "circle";
  return (
    <svg aria-hidden viewBox="0 0 14 14" className="h-3.5 w-3.5 shrink-0">
      {shape === "circle" && <circle cx="7" cy="7" r="4.5" fill={color} fillOpacity={0.85} />}
      {shape === "ring" && (
        <>
          <circle cx="7" cy="7" r="5" fill="none" stroke={color} strokeWidth="1.6" />
          <circle cx="7" cy="7" r="1.5" fill={color} />
        </>
      )}
      {shape === "triangle" && <path d="M7 2 L12.2 11.5 L1.8 11.5 Z" fill={color} fillOpacity={0.85} />}
      {shape === "square" && <rect x="2.5" y="2.5" width="9" height="9" fill={color} fillOpacity={0.85} />}
      {shape === "diamond" && <path d="M7 1.8 L12.2 7 L7 12.2 L1.8 7 Z" fill={color} fillOpacity={0.85} />}
      {shape === "dash" && <path d="M1 10 L5 6 L8 8 L13 3" stroke={color} strokeWidth="1.6" strokeDasharray="2.5 2" fill="none" />}
    </svg>
  );
}

function LayerRow({
  entry,
  checked,
  onToggle,
}: {
  entry: Pick<OverlayCatalogueEntry, "id" | "label" | "available" | "feature_count" | "reason">;
  checked: boolean;
  onToggle: () => void;
}) {
  const disabled = !entry.available;
  return (
    <label
      title={entry.reason ?? undefined}
      className={`flex cursor-pointer items-start gap-2 rounded-md px-1.5 py-1 text-[11px] transition ${
        disabled ? "cursor-not-allowed opacity-55" : "hover:bg-slate-800/50"
      }`}
    >
      <input
        type="checkbox"
        checked={!disabled && checked}
        disabled={disabled}
        onChange={onToggle}
        className="mt-0.5 accent-sky-500"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <LayerSymbol kind={entry.id} color={disabled ? "#64748b" : SYMBOL_COLOR[entry.id] ?? "#94a3b8"} />
          <span className={`truncate ${disabled ? "text-slate-500" : "text-slate-200"}`}>{entry.label}</span>
          {entry.available && (
            <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-500">{entry.feature_count}</span>
          )}
        </span>
        {disabled && entry.reason && (
          <span className="mt-0.5 block text-[10px] leading-snug text-slate-500">{entry.reason}</span>
        )}
      </span>
    </label>
  );
}

/** Floating layer control. It is built from the run's own catalogue, so it can never offer
 *  a layer the engine did not measure. */
export function MapLayerControl({
  overlays,
  visible,
  onToggle,
}: {
  overlays: MapOverlays | null;
  visible: LayerVisibility;
  onToggle: (id: string, next: boolean) => void;
}) {
  const [open, setOpen] = useState(true);
  const entries: Pick<OverlayCatalogueEntry, "id" | "label" | "available" | "feature_count" | "reason">[] =
    overlays?.layers ?? NO_RUN_LAYERS.map((entry) => ({ ...entry, available: false, feature_count: 0 }));
  const availableCount = entries.filter((entry) => entry.available).length;

  return (
    <div className="pointer-events-auto w-[236px] overflow-hidden rounded-xl border border-slate-700/60 bg-slate-950/85 text-slate-200 shadow-xl backdrop-blur">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition hover:bg-slate-900/70"
      >
        <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">Layers</span>
        <span className="rounded-full bg-slate-800/80 px-1.5 text-[10px] text-slate-400">
          {availableCount}/{entries.length}
        </span>
        <span className="ml-auto text-[11px] text-slate-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="max-h-[42vh] space-y-0.5 overflow-y-auto border-t border-slate-800/80 px-1 py-1.5">
          {entries.map((entry) => (
            <LayerRow
              key={entry.id}
              entry={entry}
              checked={visible[entry.id] ?? false}
              onToggle={() => onToggle(entry.id, !(visible[entry.id] ?? false))}
            />
          ))}
          <p className="px-1.5 pb-0.5 pt-1 text-[10px] leading-snug text-slate-500">
            Only layers this run actually measured or found are enabled. No layer is drawn to fill the menu.
          </p>
        </div>
      )}
    </div>
  );
}

const DEFAULT_THRESHOLDS: [number, string][] = [
  [25, "low"],
  [50, "moderate"],
  [70, "high"],
  [85, "very_high"],
];

function bandRanges(bandThresholds?: [number, string][]) {
  const cuts = bandThresholds && bandThresholds.length > 0 ? bandThresholds : DEFAULT_THRESHOLDS;
  const rows: { band: string; range: string }[] = [];
  let previous = 0;
  for (const [upper, band] of cuts) {
    rows.push({ band, range: `${previous}–${upper}` });
    previous = upper;
  }
  rows.push({ band: "extreme", range: `≥ ${previous}` });
  rows.push({ band: "unassessed", range: "no score" });
  return rows;
}

/** Bottom-right legend: hazard band colours plus a symbol for every visible map layer. */
export function MapLegend({
  overlays,
  visible,
  bandThresholds,
  bandLabels,
}: {
  overlays: MapOverlays | null;
  visible: LayerVisibility;
  bandThresholds?: [number, string][];
  bandLabels?: Record<string, string>;
}) {
  const [open, setOpen] = useState(true);
  const shown = (overlays?.layers ?? []).filter((entry) => entry.available && visible[entry.id]);
  const thresholds = bandRanges(bandThresholds);
  const scopeLabel = overlays?.scope === "area" ? "drawn area" : overlays?.scope === "point" ? "point + radius" : null;

  return (
    <div className="pointer-events-auto w-[212px] overflow-hidden rounded-xl border border-slate-700/60 bg-slate-950/85 text-slate-200 shadow-xl backdrop-blur">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition hover:bg-slate-900/70"
      >
        <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">Legend</span>
        {scopeLabel && <span className="text-[10px] text-slate-500">{scopeLabel}</span>}
        <span className="ml-auto text-[11px] text-slate-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-slate-800/80 px-2.5 py-2">
          <div>
            <p className="text-[9.5px] font-semibold uppercase tracking-[0.14em] text-slate-500">Hazard band</p>
            <ul className="mt-1 space-y-0.5">
              {thresholds.map(({ band, range }) => (
                <li key={band} className="flex items-center gap-1.5 text-[10.5px] text-slate-300">
                  <span
                    aria-hidden
                    className="h-2 w-2 shrink-0 rounded-sm"
                    style={{ backgroundColor: bandColor(band) }}
                  />
                  <span>{bandLabel(band, bandLabels)}</span>
                  <span className="ml-auto font-mono text-[10px] text-slate-500">{range}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <p className="text-[9.5px] font-semibold uppercase tracking-[0.14em] text-slate-500">Visible layers</p>
            {shown.length === 0 ? (
              <p className="mt-1 text-[10.5px] leading-snug text-slate-500">
                {overlays
                  ? "No measured layer is switched on."
                  : "Run an analysis to populate the layer list."}
              </p>
            ) : (
              <ul className="mt-1 space-y-0.5">
                {shown.map((entry) => (
                  <li key={entry.id} className="flex items-center gap-1.5 text-[10.5px] text-slate-300">
                    <LayerSymbol kind={entry.id} color={SYMBOL_COLOR[entry.id] ?? "#94a3b8"} />
                    <span className="min-w-0 flex-1 truncate">{entry.label}</span>
                    <span className="font-mono text-[10px] text-slate-500">{entry.feature_count}</span>
                  </li>
                ))}
                {overlays?.live_alerts.imd && overlays.live_alerts.imd.length > 0 && visible.live_alerts && (
                  <li className="flex items-center gap-1.5 pl-5 text-[10px] text-slate-400">
                    <span aria-hidden className="h-2 w-2 border border-red-400 bg-red-500/25" />
                    IMD alert polygon
                  </li>
                )}
                {(overlays?.live_alerts.cwc ?? []).length > 0 && visible.live_alerts && (
                  <li className="flex items-center gap-1.5 pl-5 text-[10px] text-slate-400">
                    <span aria-hidden className="h-2 w-2 rounded-full bg-sky-400" />
                    CWC river gauge
                  </li>
                )}
              </ul>
            )}
          </div>
          <p className="border-t border-slate-800/70 pt-1.5 text-[9.5px] leading-snug text-slate-500">
            Sample points are discrete readings. No interpolated heat surface or elevation gradient is drawn.
          </p>
        </div>
      )}
    </div>
  );
}
