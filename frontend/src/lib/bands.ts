import type { Band } from "./types";

/** Presentation only. The words and thresholds come from /hazards; this maps them to colour. */
const BAND_TONE: Record<Band, { chip: string; bar: string; text: string }> = {
  low: { chip: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30", bar: "bg-emerald-500", text: "text-emerald-300" },
  moderate: { chip: "bg-amber-500/15 text-amber-300 ring-amber-500/30", bar: "bg-amber-500", text: "text-amber-300" },
  high: { chip: "bg-rose-600/15 text-rose-300 ring-rose-600/30", bar: "bg-rose-600", text: "text-rose-300" },
  very_high: { chip: "bg-red-500/15 text-red-300 ring-red-500/30", bar: "bg-red-500", text: "text-red-300" },
  extreme: { chip: "bg-fuchsia-500/15 text-fuchsia-300 ring-fuchsia-500/30", bar: "bg-fuchsia-500", text: "text-fuchsia-300" },
  unassessed: { chip: "bg-slate-500/15 text-slate-300 ring-slate-500/30", bar: "bg-slate-600", text: "text-slate-300" },
};

export const UNKNOWN_BAND_TONE = BAND_TONE.unassessed;

export function bandTone(band: string | null | undefined) {
  return BAND_TONE[(band ?? "unassessed") as Band] ?? UNKNOWN_BAND_TONE;
}

/** Leaflet takes literal colours, not Tailwind classes; these mirror BAND_TONE. */
const BAND_HEX: Record<Band, string> = {
  low: "#10b981",
  moderate: "#f59e0b",
  high: "#e11d48",
  very_high: "#ef4444",
  extreme: "#d946ef",
  unassessed: "#64748b",
};

export function bandColor(band: string | null | undefined): string {
  return BAND_HEX[(band ?? "unassessed") as Band] ?? BAND_HEX.unassessed;
}

export const BAND_ORDER: Band[] = ["low", "moderate", "high", "very_high", "extreme", "unassessed"];

export function bandLabel(
  band: Band | string | null | undefined,
  labels?: Record<string, string>,
): string {
  if (!band || band === "unassessed") return "Not assessed";
  return labels?.[band] ?? band.replace(/_/g, " ");
}

const SOURCE_TONE: Record<string, string> = {
  available: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  healthy: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  ok: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  degraded: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  partial: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  unconfigured: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  unavailable: "bg-red-500/15 text-red-300 ring-red-500/30",
  error: "bg-red-500/15 text-red-300 ring-red-500/30",
  unverified: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
};

export function sourceTone(state: string): string {
  return SOURCE_TONE[state] ?? "bg-slate-500/15 text-slate-300 ring-slate-500/30";
}

export function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? "—" : score.toFixed(1);
}

export function formatConfidence(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "no timestamp";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export function describeObserved(observed: Record<string, number | string | null> | undefined): string {
  if (!observed || Object.keys(observed).length === 0) return "no observed values";
  return Object.entries(observed)
    .map(([key, value]) => `${key.replace(/_/g, " ")}: ${value === null ? "not measured" : value}`)
    .join(" · ");
}
