"use client";

import { useMemo, useState } from "react";

import {
  bandLabel,
  bandTone,
  describeObserved,
  formatConfidence,
  formatDuration,
  formatScore,
  formatTime,
  sourceTone,
} from "@/lib/bands";
import type {
  Analysis,
  AreaBlock,
  AreaDistrict,
  HazardResult,
  LiveFeedEntry,
  LiveFeeds,
  Provenance,
  Recommendation,
  ScoringProxy,
} from "@/lib/types";
import { exportAnalysisPdf } from "@/lib/pdf";
import {
  ActionButton,
  Badge,
  Card,
  KeyValue,
  Prose,
  ProvenanceTable,
  SectionHeading,
  Sheet,
  SourceBadge,
  StatTile,
} from "./ui";

const ML_TONE = "bg-sky-500/15 text-sky-300 ring-sky-500/30";

/** Human names for the provider keys the engine records. Unknown keys fall back to the key. */
const SOURCE_LABELS: Record<string, string> = {
  dem: "AWS terrain tiles (Terrarium DEM)",
  dem_local: "local SRTM/Bhuvan DEM raster",
  coastline: "stored India coastline outline",
  open_meteo: "Open-Meteo reanalysis + forecast",
  open_meteo_recent: "Open-Meteo ERA5 reanalysis",
  open_meteo_forecast: "Open-Meteo forecast",
  usgs_earthquakes: "USGS ComCat",
  osm_overpass: "OpenStreetMap via Overpass",
  osm_local: "local OpenStreetMap extract",
  firms: "NASA FIRMS",
  imd_alerts: "IMD weather warnings",
  cwc_gauges: "CWC river gauges",
  bhuvan_hazards: "Bhuvan/NDMA hazard maps",
};

/** Only used for ordering and the Moderate-and-above filter; the words come from /hazards. */
const BAND_RANK: Record<string, number> = {
  unassessed: -1,
  low: 0,
  moderate: 1,
  high: 2,
  very_high: 3,
  extreme: 4,
};
const TARGET_RANK = BAND_RANK.moderate;

const HORIZONS = [
  { key: "immediate", label: "P1", title: "Immediate emergency & response" },
  { key: "short_term", label: "P2", title: "Mid-term mitigation & monitoring" },
  { key: "long_term", label: "P3", title: "Structural & long-term planning" },
] as const;

type SourceFilter = "all" | "answered" | "failed";
type ProvenanceTab = "overview" | "sources" | "feeds" | "gaps" | "limits";

const SOURCE_FILTERS: { key: SourceFilter; label: string }[] = [
  { key: "all", label: "all" },
  { key: "answered", label: "answered" },
  { key: "failed", label: "failed" },
];

interface HorizonGroup {
  key: string;
  label: string;
  title: string;
  items: Recommendation[];
}

interface RecommendationSummary {
  targeted: Recommendation[];
  withheld: Recommendation[];
  groups: HorizonGroup[];
}

function sourceLabel(key: string): string {
  return SOURCE_LABELS[key] ?? key.replace(/_/g, " ");
}

/** The per-hazard proxy list. Older stored records predate `scoring`, so rebuild it from factors. */
function proxiesOf(analysis: Analysis): Record<string, ScoringProxy[]> {
  if (analysis.scoring?.heuristic_proxies) return analysis.scoring.heuristic_proxies;
  return Object.fromEntries(
    analysis.hazards.map((hazard) => [
      hazard.hazard,
      hazard.factors.map((factor) => ({ proxy: factor.name, source: factor.source, used: factor.available })),
    ]),
  );
}

function gapRows(analysis: Analysis) {
  const seen = new Set<string>();
  const rows: { hazard: string; proxy: string; source: string; note: string }[] = [];
  for (const hazard of analysis.hazards) {
    for (const factor of hazard.factors) {
      if (factor.available) continue;
      const key = `${factor.name}|${factor.note ?? ""}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({
        hazard: hazard.hazard,
        proxy: factor.name.replace(/_/g, " "),
        source: factor.source,
        note: factor.note ?? "no note supplied with this gap",
      });
    }
  }
  return rows;
}

function bandForHazard(analysis: Analysis, hazard: string): string {
  return analysis.hazards.find((row) => row.hazard === hazard)?.band ?? "unassessed";
}

/** Moderate-and-above only, strongest hazards first, grouped by the horizon the engine recorded. */
function summariseRecommendations(analysis: Analysis): RecommendationSummary {
  const rankOf = (item: Recommendation) => BAND_RANK[bandForHazard(analysis, item.hazard)] ?? TARGET_RANK;
  const targeted = analysis.recommendations.filter((item) => rankOf(item) >= TARGET_RANK);
  const withheld = analysis.recommendations.filter((item) => rankOf(item) < TARGET_RANK);
  const order = (a: Recommendation, b: Recommendation) =>
    rankOf(b) - rankOf(a) || (b.score ?? 0) - (a.score ?? 0);

  const groups: HorizonGroup[] = HORIZONS.map((horizon) => ({
    key: horizon.key,
    label: horizon.label,
    title: horizon.title,
    items: targeted.filter((item) => item.horizon === horizon.key).sort(order),
  })).filter((group) => group.items.length > 0);

  const known = new Set<string>(HORIZONS.map((horizon) => horizon.key));
  const other = targeted.filter((item) => !known.has(item.horizon)).sort(order);
  if (other.length > 0) {
    groups.push({ key: "other", label: "P4", title: "Other issued actions", items: other });
  }

  return { targeted, withheld, groups };
}

function paragraphOf(group: HorizonGroup, analysis: Analysis) {
  const hazards = [...new Set(group.items.map((item) => item.hazard))].map((hazard) => ({
    hazard,
    band: bandForHazard(analysis, hazard),
  }));
  const actions: string[] = [];
  for (const item of group.items) {
    const action = item.action.trim();
    if (!actions.includes(action)) actions.push(action);
  }
  return { hazards, actions };
}

function authoritiesOf(items: Recommendation[]) {
  const rows = new Map<string, string | null>();
  for (const item of items) {
    if (!item.authority) continue;
    if (!rows.has(item.authority)) rows.set(item.authority, item.guidance_url);
  }
  return [...rows.entries()].map(([authority, url]) => ({ authority, url }));
}

function rowAnswered(row: Provenance): boolean {
  return row.http_status !== null && row.http_status < 400;
}

function SeverityBar({ severity }: { severity: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-slate-800">
        <div className="h-full rounded-full bg-sky-400" style={{ width: `${Math.round(severity * 100)}%` }} />
      </div>
      <span className="font-mono text-[11px] text-slate-400">{severity.toFixed(3)}</span>
    </div>
  );
}

function HazardCard({
  hazard,
  labels,
  onOpenAudit,
}: {
  hazard: HazardResult;
  labels?: Record<string, string>;
  onOpenAudit: () => void;
}) {
  const tone = bandTone(hazard.band);
  const unassessed = hazard.band === "unassessed";
  const barWidth = unassessed || hazard.score === null ? 0 : Math.max(2, Math.min(100, hazard.score));
  return (
    <details className="group relative overflow-hidden rounded-xl border border-slate-800/80 bg-slate-900/35 transition hover:border-slate-700/90 open:border-slate-700 open:bg-slate-900/50 open:shadow-[0_18px_40px_-32px_rgba(2,6,23,0.95)]">
      <span aria-hidden className={`absolute inset-y-0 left-0 w-[3px] ${tone.bar}`} />
      <summary className="flex cursor-pointer list-none flex-col gap-1.5 py-3 pl-4 pr-3.5">
        <div className="flex items-center gap-2">
          <span className="truncate text-[13px] font-semibold text-slate-100">
            {hazard.hazard.replace(/_/g, " ")}
          </span>
          <Badge tone={tone.chip}>{bandLabel(hazard.band, labels)}</Badge>
          <span className={`ml-auto shrink-0 font-mono text-[15px] font-semibold ${tone.text}`}>
            {unassessed ? "—" : formatScore(hazard.score)}
          </span>
        </div>
        <div className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
          <span>
            confidence {formatConfidence(hazard.confidence)} · {hazard.data_completeness.factors_available}/
            {hazard.data_completeness.factors_total} factors measured
          </span>
          <span className="shrink-0 text-slate-600 transition-transform group-open:rotate-90">›</span>
        </div>
        <div className="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-slate-800">
          <div className={`h-full rounded-full ${tone.bar}`} style={{ width: `${barWidth}%` }} />
        </div>
      </summary>

      <div className="space-y-3 border-t border-slate-800/70 py-3 pl-4 pr-3.5">
        <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-slate-500">
          {hazard.method === "rule_engine" ? (
            <Badge>scored by heuristic proxies</Badge>
          ) : (
            <Badge tone={ML_TONE}>scored by ML inference · {hazard.method}</Badge>
          )}
          <span>
            {hazard.evidence_kind === "model"
              ? "modelled / reanalysis drivers — not a measurement at this site"
              : `evidence kind: ${hazard.evidence_kind}`}
          </span>
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-800/70">
          <table className="w-full min-w-[560px] border-collapse text-left text-[12px]">
            <thead className="bg-slate-900/60 text-slate-500">
              <tr>
                <th className="py-1.5 pl-3 pr-3 font-medium">factor</th>
                <th className="py-1.5 pr-3 font-medium">source</th>
                <th className="py-1.5 pr-3 font-medium">weight</th>
                <th className="py-1.5 pr-3 font-medium">severity</th>
                <th className="py-1.5 pr-3 font-medium">what was seen</th>
              </tr>
            </thead>
            <tbody className="align-top">
              {hazard.factors.map((factor) => (
                <tr key={factor.name} className="border-t border-slate-800/70">
                  <td className="py-1.5 pl-3 pr-3 font-medium text-slate-200">{factor.name.replace(/_/g, " ")}</td>
                  <td className="py-1.5 pr-3">
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${
                        factor.available ? sourceTone("available") : sourceTone("unavailable")
                      }`}
                    >
                      {factor.source}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-slate-400">{factor.weight}</td>
                  <td className="py-1.5 pr-3">
                    {factor.available && factor.severity !== undefined ? (
                      <SeverityBar severity={factor.severity} />
                    ) : (
                      <span className="text-[11px] text-amber-300/90">not measured</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-[11.5px] text-slate-400">
                    {factor.available ? (
                      describeObserved(factor.observed)
                    ) : (
                      <span className="text-slate-400">{factor.note ?? "no note supplied with this gap"}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {hazard.data_completeness.unavailable_sources.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11.5px] text-slate-500">sources missing from this score:</span>
            {hazard.data_completeness.unavailable_sources.map((source) => (
              <Badge key={source} tone={bandTone("unassessed").chip}>
                {source}
              </Badge>
            ))}
            <span className="text-[11px] text-slate-500">
              — their weight was removed and confidence lowered; nothing was filled in for them.
            </span>
          </div>
        )}

        {hazard.warnings.map((warning) => (
          <p
            key={warning}
            className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[12px] leading-relaxed text-amber-200"
          >
            {warning}
          </p>
        ))}

        {Object.keys(hazard.details).length > 0 && (
          <details className="text-[11px]">
            <summary className="cursor-pointer text-slate-500">extra detail block</summary>
            <pre className="mt-1 overflow-x-auto rounded bg-slate-950 p-2 text-[11px] leading-relaxed text-slate-400">
              {JSON.stringify(hazard.details, null, 2)}
            </pre>
          </details>
        )}

        {hazard.provenance.length === 0 ? (
          <p className="text-[11px] text-amber-300/90">
            No provenance record was attached to this hazard; treat its factors as unverified.
          </p>
        ) : (
          <p className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            <span>
              {hazard.provenance.length} provenance record{hazard.provenance.length === 1 ? "" : "s"} logged for this
              hazard.
            </span>
            <button
              type="button"
              onClick={onOpenAudit}
              className="text-sky-400 underline decoration-sky-500/40 underline-offset-2 transition hover:text-sky-300"
            >
              open data audit
            </button>
          </p>
        )}
      </div>
    </details>
  );
}

function MethodProvenance({ analysis }: { analysis: Analysis }) {
  const mlModels = analysis.scoring?.ml_models_used ?? analysis.model_versions.ml_models_used;
  const heuristicCount = analysis.hazards.filter((hazard) => hazard.method === "rule_engine").length;
  const mlCount = analysis.hazards.length - heuristicCount;
  const proxies = proxiesOf(analysis);
  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={mlModels.length > 0 ? ML_TONE : undefined}>
          {mlModels.length > 0
            ? `ML inference · ${mlModels.join(", ")}`
            : "spatial heuristic proxies · no ML inference"}
        </Badge>
        <Badge>
          engine {analysis.scoring?.engine ?? analysis.model_versions.engine}{" "}
          {analysis.scoring?.engine_version ?? analysis.model_versions.engine_version}
        </Badge>
        <Badge>
          {heuristicCount} heuristic{heuristicCount === 1 ? "" : "s"}
          {mlCount > 0 ? ` · ${mlCount} with ML` : ""}
        </Badge>
        <Badge>mean confidence {formatConfidence(analysis.overall.mean_confidence)}</Badge>
      </div>
      <Prose>
        {analysis.scoring?.note ??
          "Every score is a weighted read of measured spatial, terrain and weather evidence; no ML model is trained on real labels in this build. A factor whose source did not answer is dropped and lowers confidence, never imputed."}
      </Prose>
      {mlModels.length > 0 && (
        <Prose tone="text-sky-300/90">
          Models applied to this run: {mlModels.join(", ")}. The confidence on each hazard row above is the share of
          that hazard&apos;s factor weight that actually answered; the report does not attribute a per-model score
          where the engine does not record one.
        </Prose>
      )}
      <details className="text-[11.5px]">
        <summary className="cursor-pointer text-slate-500">
          proxy list per hazard — amber entries never answered
        </summary>
        <ul className="mt-1.5 grid gap-x-4 gap-y-1 sm:grid-cols-2">
          {analysis.hazards.map((hazard) => (
            <li key={hazard.hazard} className="rounded border border-slate-800/70 px-2 py-1">
              <span className="text-slate-300">{hazard.hazard.replace(/_/g, " ")}</span>
              <span className="text-slate-500">
                {" "}
                · {hazard.factors.length} proxies · confidence {formatConfidence(hazard.confidence)}
              </span>
              <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5">
                {(proxies[hazard.hazard] ?? []).map((proxy) => (
                  <span
                    key={proxy.proxy}
                    className={proxy.used ? "text-slate-400" : "text-amber-300/90"}
                    title={proxy.source}
                  >
                    {proxy.proxy.replace(/_/g, " ")}
                    {proxy.used ? "" : " (missing)"}
                  </span>
                ))}
              </div>
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function SourceInventory({
  analysis,
  query,
  onQuery,
  filter,
  onFilter,
}: {
  analysis: Analysis;
  query: string;
  onQuery: (value: string) => void;
  filter: SourceFilter;
  onFilter: (value: SourceFilter) => void;
}) {
  const sources = Object.entries(analysis.data_completeness.sources);
  const rows = analysis.data_completeness.provenance;
  const needle = query.trim().toLowerCase();
  const visible = rows.filter((row) => {
    if (filter === "answered" && !rowAnswered(row)) return false;
    if (filter === "failed" && rowAnswered(row)) return false;
    if (!needle) return true;
    return `${row.provider} ${row.endpoint} ${row.license ?? ""} ${row.attribution ?? ""}`
      .toLowerCase()
      .includes(needle);
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5">
        {sources.map(([source, state]) => (
          <span
            key={source}
            className="inline-flex items-center gap-1.5 rounded-full border border-slate-800/70 bg-slate-800/40 px-2 py-0.5 text-[11px] text-slate-300"
          >
            {sourceLabel(source)}
            <SourceBadge state={state} />
          </span>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="filter by provider, endpoint or licence"
          className="min-w-[220px] flex-1 rounded-lg border border-slate-700/80 bg-slate-950/80 px-2.5 py-1.5 text-[12px] text-slate-100 outline-none placeholder:text-slate-600 focus:border-sky-500"
        />
        <div className="flex gap-1 rounded-lg border border-slate-800/70 bg-slate-900/40 p-0.5">
          {SOURCE_FILTERS.map((option) => (
            <button
              key={option.key}
              type="button"
              onClick={() => onFilter(option.key)}
              className={`rounded-md px-2 py-1 text-[11px] font-medium transition ${
                filter === option.key
                  ? "bg-slate-800 text-slate-100 shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-slate-500">
          {visible.length} of {rows.length} rows
        </span>
      </div>

      {visible.length > 0 ? (
        <ProvenanceTable rows={visible} />
      ) : rows.length === 0 ? (
        <ProvenanceTable rows={[]} />
      ) : (
        <p className="rounded-lg border border-slate-800/70 bg-slate-900/30 px-3 py-2 text-[11.5px] text-slate-400">
          No provenance row matches this filter. Clear it to see all {rows.length} recorded requests.
        </p>
      )}

      <Prose>
        Every observation behind a factor names its provider; the table records the exact endpoint, HTTP status, cache
        state, retrieval time and licence, so a number can be traced to the request that produced it.
      </Prose>
    </div>
  );
}

function DataGaps({ analysis }: { analysis: Analysis }) {
  const rows = gapRows(analysis);
  const infra = analysis.infrastructure_summary;
  const firmsState = analysis.data_completeness.sources.firms;
  const infraLayers = infra?.queried ? Object.entries(infra.layers) : [];
  const origins = infra?.origins ?? {};
  const localLayers = origins.local ?? 0;
  const remoteLayers = origins.overpass ?? 0;
  const liveFeeds = [
    { key: "imd_alerts", label: "IMD warnings" },
    { key: "cwc_gauges", label: "CWC river gauges" },
    { key: "bhuvan_hazards", label: "Bhuvan/NDMA hazard maps" },
  ]
    .map((feed) => ({ ...feed, state: analysis.data_completeness.sources[feed.key] }))
    .filter((feed) => feed.state !== undefined);
  const liveUnconfigured = liveFeeds.filter((feed) => feed.state === "unconfigured");
  const liveUnavailable = liveFeeds.filter((feed) => feed.state !== "available" && feed.state !== "unconfigured");
  return (
    <div className="space-y-3">
      {liveFeeds.length > 0 && (liveUnconfigured.length > 0 || liveUnavailable.length > 0) && (
        <p className="rounded border border-sky-500/30 bg-sky-500/10 px-2.5 py-1.5 text-[12px] leading-relaxed text-sky-200">
          Live Indian hazard feeds:{" "}
          {[
            ...liveUnconfigured.map((feed) => `${feed.label} (not configured)`),
            ...liveUnavailable.map((feed) => `${feed.label} (${feed.state})`),
          ].join(", ")}
          . No live alert was blended from those feeds — an unconfigured or failed feed is a data gap, not a finding
          that no warning exists.
        </p>
      )}
      {firmsState && firmsState !== "available" && (
        <p className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[12px] leading-relaxed text-amber-200">
          Active fires:{" "}
          {firmsState === "unconfigured"
            ? "no FIRMS API key is configured, so no hotspot detections were queried."
            : `FIRMS reported ${firmsState}.`}{" "}
          Wildfire is scored on its dryness proxy alone — a data gap, not a finding of zero fires.
        </p>
      )}

      {rows.length === 0 ? (
        <Prose tone="text-slate-400">Every proxy in this run answered; no factor was dropped.</Prose>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-800/70">
          <table className="w-full border-collapse text-left text-[12px]">
            <thead className="bg-slate-900/60 text-slate-500">
              <tr>
                <th className="py-1.5 pl-3 pr-3 font-medium">hazard</th>
                <th className="py-1.5 pr-3 font-medium">proxy</th>
                <th className="py-1.5 pr-3 font-medium">source</th>
                <th className="py-1.5 pr-3 font-medium">why it is missing</th>
              </tr>
            </thead>
            <tbody className="align-top">
              {rows.map((row) => (
                <tr key={`${row.hazard}-${row.proxy}`} className="border-t border-slate-800/70">
                  <td className="py-1.5 pl-3 pr-3 text-slate-300">{row.hazard.replace(/_/g, " ")}</td>
                  <td className="py-1.5 pr-3 text-slate-200">{row.proxy}</td>
                  <td className="py-1.5 pr-3 font-mono text-[11px] text-slate-400">{row.source}</td>
                  <td className="py-1.5 pr-3 text-[11.5px] text-amber-200/90">{row.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {infra?.queried && (
        <div className="rounded border border-slate-800 bg-slate-950/40 px-2.5 py-2 text-[11.5px] leading-relaxed text-slate-400">
          <p className="text-slate-300">
            Lifelines (
            {localLayers > 0
              ? `local OSM extract${localLayers === 1 ? "" : "s"}${remoteLayers > 0 ? ` + ${remoteLayers} layer(s) via Overpass` : ", no Overpass request"}`
              : "OpenStreetMap Overpass"}
            , {infra.deadline_seconds ?? "—"} s budget, elapsed {formatDuration(infra.elapsed_ms)}): {infra.answered} of{" "}
            {infraLayers.length} layers answered.
          </p>
          {infra.local?.status === "used" && (infra.local.extracts?.length ?? 0) > 0 && (
            <p className="mt-1">
              Local extract:{" "}
              {infra.local.extracts
                ?.map((extract) => `${extract.name}${extract.vintage ? ` (${extract.vintage})` : ""}`)
                .join(", ")}
              .
            </p>
          )}
          {infraLayers.some(([, layer]) => layer.status !== "ok") && (
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {infraLayers.map(([name, layer]) =>
                layer.status === "ok" ? null : (
                  <li key={name}>
                    {name.replace(/_/g, " ")} ({layer.origin ?? "overpass"}): {layer.status}
                    {layer.reason ? ` — ${layer.reason}` : ""}. Reported unavailable, not as zero features.
                  </li>
                ),
              )}
            </ul>
          )}
          {infra.warnings.map((warning) => (
            <p key={warning} className="mt-1 text-amber-300/90">
              {warning}
            </p>
          ))}
        </div>
      )}
      {infra && infra.queried === false && (
        <Prose>
          OSM lifelines were not queried for this run (the lookup is opt-in). Their absence is a deliberate omission,
          not a finding that no hospitals, bridges or emergency services exist here.
        </Prose>
      )}

      {analysis.data_completeness.evidence_warnings.length > 0 && (
        <div>
          <p className="text-[11.5px] text-slate-500">engine-reported warnings for this run</p>
          <ul className="mt-1 space-y-1.5">
            {analysis.data_completeness.evidence_warnings.map((warning) => (
              <li
                key={warning}
                className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[12px] leading-relaxed text-amber-200"
              >
                {warning}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function RecommendationsBody({
  analysis,
  summary,
  showWithheld,
  onToggleWithheld,
}: {
  analysis: Analysis;
  summary: RecommendationSummary;
  showWithheld: boolean;
  onToggleWithheld: () => void;
}) {
  if (analysis.recommendations.length === 0) {
    return (
      <Prose tone="text-slate-400">
        No band-gated advice was issued for this run. That is a property of the scores, not a failure to produce a
        number: advice is only attached to hazards that crossed their threshold.
      </Prose>
    );
  }

  const visible = showWithheld ? [...summary.targeted, ...summary.withheld] : summary.targeted;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-sky-500/25 bg-sky-500/5 px-3 py-2.5">
        <Badge tone={bandTone("moderate").chip}>{summary.targeted.length} moderate or higher</Badge>
        <Badge tone={bandTone("unassessed").chip}>{summary.withheld.length} low or unassessed</Badge>
        <span className="text-[11.5px] text-slate-400">of {analysis.recommendations.length} actions issued</span>
        {summary.withheld.length > 0 && (
          <button
            type="button"
            onClick={onToggleWithheld}
            className="ml-auto rounded-md border border-slate-700/80 px-2 py-0.5 text-[11px] text-slate-300 transition hover:border-sky-500 hover:text-slate-100"
          >
            {showWithheld
              ? "hide low-risk actions"
              : `show ${summary.withheld.length} low-risk action${summary.withheld.length === 1 ? "" : "s"}`}
          </button>
        )}
      </div>

      {summary.targeted.length === 0 ? (
        <Prose tone="text-slate-400">
          No action was issued for a Moderate-or-higher hazard in this run. Low-risk actions stay recorded below;
          nothing is promoted to fill the summary.
        </Prose>
      ) : (
        <div className="space-y-3">
          {summary.groups.map((group) => {
            const { hazards, actions } = paragraphOf(group, analysis);
            const authorities = authoritiesOf(group.items);
            return (
              <div key={group.key} className="rounded-xl border border-slate-800/70 bg-slate-900/35 p-3.5">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded-md bg-sky-500/15 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-sky-300 ring-1 ring-inset ring-sky-500/30">
                    {group.label}
                  </span>
                  <h3 className="text-[13px] font-semibold text-slate-100">{group.title}</h3>
                  <span className="ml-auto text-[11px] text-slate-500">
                    {group.items.length} action{group.items.length === 1 ? "" : "s"}
                  </span>
                </div>
                <p className="mt-2 text-[13px] leading-relaxed text-slate-300">
                  <span className="text-slate-400">
                    {hazards.length === 1
                      ? `Applying to ${hazards[0].hazard.replace(/_/g, " ")} (${bandLabel(hazards[0].band)}). `
                      : `Applying to ${hazards
                          .slice(0, -1)
                          .map(({ hazard, band }) => `${hazard.replace(/_/g, " ")} (${bandLabel(band)})`)
                          .join(", ")} and ${hazards[hazards.length - 1].hazard.replace(/_/g, " ")} (${bandLabel(
                          hazards[hazards.length - 1].band,
                        )}). `}
                  </span>
                  {actions.join(" ")}
                </p>
                {authorities.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {authorities.map(({ authority, url }) =>
                      url ? (
                        <a
                          key={authority}
                          href={url}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="inline-flex items-center gap-1 rounded-full bg-slate-800/60 px-2 py-0.5 text-[10.5px] text-sky-300 ring-1 ring-inset ring-slate-700/70 transition hover:text-sky-200 hover:ring-sky-500/40"
                        >
                          {authority}
                        </a>
                      ) : (
                        <span
                          key={authority}
                          className="inline-flex items-center gap-1 rounded-full bg-slate-800/60 px-2 py-0.5 text-[10.5px] text-slate-300 ring-1 ring-inset ring-slate-700/70"
                        >
                          {authority}
                        </span>
                      ),
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <details className="group rounded-xl border border-slate-800/70 bg-slate-950/40">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 text-[12px] text-slate-300">
          <span>Every issued action in this view</span>
          <span className="text-slate-500">({visible.length})</span>
          <span className="ml-auto text-[11px] text-slate-500 group-open:hidden">expand</span>
        </summary>
        <div className="overflow-x-auto border-t border-slate-800/70">
          <table className="w-full border-collapse text-left text-[12px]">
            <thead className="bg-slate-900/60 text-slate-500">
              <tr>
                <th className="py-1.5 pl-3 pr-3 font-medium">hazard</th>
                <th className="py-1.5 pr-3 font-medium">band</th>
                <th className="py-1.5 pr-3 font-medium">horizon</th>
                <th className="py-1.5 pr-3 font-medium">category</th>
                <th className="py-1.5 pr-3 font-medium">action</th>
                <th className="py-1.5 pr-3 font-medium">authority</th>
                <th className="py-1.5 pr-3 font-medium">flag</th>
              </tr>
            </thead>
            <tbody className="align-top">
              {visible.map((item, index) => {
                const band = bandForHazard(analysis, item.hazard);
                const tone = bandTone(band);
                return (
                  <tr key={`${item.hazard}-${item.horizon}-${index}`} className="border-t border-slate-800/70">
                    <td className="py-1.5 pl-3 pr-3 text-slate-200">{item.hazard.replace(/_/g, " ")}</td>
                    <td className="py-1.5 pr-3">
                      <Badge tone={tone.chip}>{bandLabel(band)}</Badge>
                    </td>
                    <td className="py-1.5 pr-3 text-slate-400">{item.horizon.replace(/_/g, " ")}</td>
                    <td className="py-1.5 pr-3 text-slate-400">{item.category.replace(/_/g, " ")}</td>
                    <td className="py-1.5 pr-3 text-[11.5px] leading-relaxed text-slate-300">{item.action}</td>
                    <td className="py-1.5 pr-3 text-[11px] text-slate-400">
                      {item.authority ? (
                        item.guidance_url ? (
                          <a
                            className="text-sky-400 underline"
                            href={item.guidance_url}
                            target="_blank"
                            rel="noreferrer noopener"
                          >
                            {item.authority}
                          </a>
                        ) : (
                          item.authority
                        )
                      ) : (
                        "none cited"
                      )}
                    </td>
                    <td className="py-1.5 pr-3">
                      {item.requires_professional_assessment ? (
                        <Badge tone={bandTone("high").chip}>professional assessment</Badge>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="border-t border-slate-800/70 px-3.5 py-2 text-[11px] leading-relaxed text-slate-500">
          Rows marked <span className="text-orange-300">professional assessment</span> are the engine&apos;s own flag
          that structural, hydrological or geotechnical work needs a licensed professional. Each action&apos;s
          rationale, evidence and limitation stay on the stored record.
        </div>
      </details>
    </div>
  );
}

function ProvenancePanel({
  analysis,
  tab,
  onTab,
  gaps,
  sourceQuery,
  onSourceQuery,
  sourceFilter,
  onSourceFilter,
}: {
  analysis: Analysis;
  tab: ProvenanceTab;
  onTab: (tab: ProvenanceTab) => void;
  gaps: ReturnType<typeof gapRows>;
  sourceQuery: string;
  onSourceQuery: (value: string) => void;
  sourceFilter: SourceFilter;
  onSourceFilter: (value: SourceFilter) => void;
}) {
  const mlModels = analysis.scoring?.ml_models_used ?? analysis.model_versions.ml_models_used;
  const engineName = analysis.scoring?.engine ?? analysis.model_versions.engine;
  const engineVersion = analysis.scoring?.engine_version ?? analysis.model_versions.engine_version;
  const hazardWarnings = analysis.hazards.flatMap((hazard) =>
    hazard.warnings.map((warning) => ({ hazard: hazard.hazard, warning })),
  );
  const flagged = analysis.recommendations.filter((item) => item.requires_professional_assessment).length;
  const feedCount = Object.keys(analysis.live_feeds?.feeds ?? {}).length;
  const tabs: { key: ProvenanceTab; label: string; count: number }[] = [
    { key: "overview", label: "Method", count: analysis.hazards.length },
    { key: "sources", label: "Data sources", count: analysis.data_completeness.provenance.length },
    ...(analysis.live_feeds
      ? [{ key: "feeds" as const, label: "Live feeds", count: feedCount }]
      : []),
    {
      key: "gaps",
      label: "Gaps & warnings",
      count: gaps.length + analysis.data_completeness.evidence_warnings.length + hazardWarnings.length,
    },
    { key: "limits", label: "Limitations", count: analysis.limitations.length },
  ];

  return (
    <div>
      <div className="sticky top-0 z-10 -mx-1 mb-4 flex flex-wrap gap-1 rounded-xl border border-slate-800/70 bg-slate-950/90 p-1 backdrop-blur">
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => onTab(item.key)}
            className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11.5px] font-medium transition ${
              tab === item.key
                ? "bg-slate-800 text-slate-100 shadow-sm ring-1 ring-inset ring-slate-700"
                : "text-slate-400 hover:bg-slate-900/60 hover:text-slate-200"
            }`}
          >
            {item.label}
            <span className="rounded-full bg-slate-950/60 px-1.5 text-[10px] text-slate-400">{item.count}</span>
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2.5 xl:grid-cols-4">
            <StatTile
              label="Engine"
              value={engineName.replace(/_/g, " ")}
              hint={`${engineName} · version ${engineVersion}`}
              accent="bg-sky-500"
            />
            <StatTile
              label="Scoring mode"
              value={mlModels.length > 0 ? "ML inference" : "Heuristic proxies"}
              hint={mlModels.length > 0 ? mlModels.join(", ") : "no ML model was called"}
              accent={mlModels.length > 0 ? "bg-sky-500" : "bg-violet-500"}
            />
            <StatTile
              label="Mean confidence"
              value={formatConfidence(analysis.overall.mean_confidence)}
              hint="answered weight ÷ total weight"
              accent="bg-amber-500"
            />
            <StatTile
              label="Provenance rows"
              value={String(analysis.data_completeness.provenance.length)}
              hint="requests recorded for this run"
              accent="bg-emerald-500"
            />
          </div>
          <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3.5">
            <SectionHeading hint="Every score is a weighted read of measured evidence; nothing is imputed.">
              How the scores were produced
            </SectionHeading>
            <p className="mt-2 rounded-lg border border-slate-800/70 bg-slate-950/70 px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-sky-200/90">
              score = 100 × Σ(weight × severity) ÷ Σ(weight over the factors that answered)
            </p>
            <div className="mt-3">
              <MethodProvenance analysis={analysis} />
            </div>
          </div>
        </div>
      )}

      {tab === "sources" && (
        <div className="space-y-3">
          <SourceInventory
            analysis={analysis}
            query={sourceQuery}
            onQuery={onSourceQuery}
            filter={sourceFilter}
            onFilter={onSourceFilter}
          />
          {analysis.data_completeness.unavailable_layers.length > 0 && (
            <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3.5">
              <SectionHeading hint="Their factors are absent from the scores above.">
                Layers not loaded in this instance
              </SectionHeading>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {analysis.data_completeness.unavailable_layers.map((layer) => (
                  <Badge key={layer} tone={bandTone("unassessed").chip}>
                    {layer.replace(/_/g, " ")}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "feeds" && <LiveFeedsPanel feeds={analysis.live_feeds} />}

      {tab === "gaps" && (
        <div className="space-y-3">
          <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3.5">
            <SectionHeading hint="A missing factor is dropped and lowers confidence; no stand-in value is generated.">
              Dropped factors and source states
            </SectionHeading>
            <div className="mt-2">
              <DataGaps analysis={analysis} />
            </div>
          </div>
          <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3.5">
            <SectionHeading hint={`${hazardWarnings.length} warning(s) attached to individual hazard rows`}>
              Per-hazard warnings
            </SectionHeading>
            {hazardWarnings.length === 0 ? (
              <div className="mt-2">
                <Prose>No hazard row carried a warning in this run.</Prose>
              </div>
            ) : (
              <ul className="mt-2 space-y-1.5">
                {hazardWarnings.map(({ hazard, warning }) => (
                  <li
                    key={`${hazard}-${warning}`}
                    className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[12px] leading-relaxed text-amber-200"
                  >
                    <span className="font-medium">{hazard.replace(/_/g, " ")}</span> · {warning}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {tab === "limits" && (
        <div className="space-y-3">
          <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3.5">
            <SectionHeading hint="As recorded by the engine for this run, in its own order.">
              Stated limitations
            </SectionHeading>
            {analysis.limitations.length === 0 ? (
              <div className="mt-2">
                <Prose tone="text-slate-400">The engine attached no limitations to this run.</Prose>
              </div>
            ) : (
              <ol className="mt-2 list-decimal space-y-1.5 pl-5 text-[12.5px] leading-relaxed text-slate-300">
                {analysis.limitations.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ol>
            )}
          </div>
          <div className="rounded-xl border border-orange-500/25 bg-orange-500/5 p-3.5">
            <SectionHeading hint="The engine flags these itself; it does not substitute for a licensed engineer.">
              Professional-assessment flags
            </SectionHeading>
            <p className="mt-2 text-[12.5px] leading-relaxed text-orange-200/90">
              {flagged === 0
                ? "No issued action in this run carries the professional-assessment flag."
                : `${flagged} of ${analysis.recommendations.length} issued actions carry the professional-assessment flag. They stay visible in the recommendations drawer with that marking.`}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

function LiveFeedCard({ feedKey, entry }: { feedKey: string; entry: LiveFeedEntry }) {
  return (
    <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-[12.5px] font-semibold text-slate-100">{entry.label ?? feedKey}</p>
          <p className="font-mono text-[10.5px] text-slate-500">
            {entry.provider ?? "provider not stated"} · {entry.env_var ?? "env var not stated"}
          </p>
        </div>
        <SourceBadge state={entry.status ?? "unverified"} />
      </div>
      {entry.reason && entry.status !== "available" && (
        <p className="mt-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11.5px] leading-relaxed text-amber-200">
          {entry.reason}
        </p>
      )}
      {feedKey === "imd" && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[11.5px] text-slate-400">
            {entry.matched_count ?? "—"} of {entry.alerts_total ?? "—"} warning(s) intersect this selection.
          </p>
          {(entry.items ?? []).length === 0 ? (
            <p className="text-[11px] text-slate-500">No IMD warning matched this selection.</p>
          ) : (
            <ul className="space-y-1">
              {(entry.items ?? []).map((alert, index) => (
                <li key={alert.id ?? index} className="rounded border border-slate-800/70 px-2 py-1 text-[11.5px]">
                  <p className="font-medium text-slate-200">{alert.title ?? alert.event ?? "untitled warning"}</p>
                  <p className="text-[11px] text-slate-500">
                    severity {alert.severity ?? "not stated"}
                    {alert.onset ? ` · onset ${alert.onset}` : ""}
                    {alert.expires ? ` · expires ${alert.expires}` : ""}
                    {alert.matched_by ? ` · matched by ${alert.matched_by}` : ""}
                  </p>
                  {alert.description && <p className="mt-0.5 leading-relaxed text-slate-400">{alert.description}</p>}
                  {alert.url && (
                    <a href={alert.url} target="_blank" rel="noreferrer" className="text-sky-400 underline">
                      source bulletin
                    </a>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {feedKey === "cwc" && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[11.5px] text-slate-400">
            {entry.gauges_within_radius ?? "—"} gauge(s) within{" "}
            {entry.search_radius_m === null || entry.search_radius_m === undefined
              ? "an unstated radius"
              : `${(entry.search_radius_m / 1000).toFixed(0)} km`}
            {entry.max_severity !== null && entry.max_severity !== undefined
              ? ` · max severity ${Number(entry.max_severity).toFixed(2)}`
              : ""}
            {entry.unrated ? ` · ${entry.unrated} unrated` : ""}
          </p>
          {(entry.gauges ?? []).length === 0 ? (
            <p className="text-[11px] text-slate-500">No CWC gauge matched this selection.</p>
          ) : (
            <ul className="space-y-1">
              {(entry.gauges ?? []).map((gauge, index) => (
                <li key={`${gauge.station ?? "gauge"}-${index}`} className="rounded border border-slate-800/70 px-2 py-1 text-[11.5px]">
                  <p className="font-medium text-slate-200">
                    {gauge.station ?? "station not stated"}
                    {gauge.river ? ` · ${gauge.river}` : ""}
                  </p>
                  <p className="text-[11px] text-slate-500">
                    level {gauge.level_m ?? "—"} m · warning {gauge.warning_level_m ?? "—"} m · danger{" "}
                    {gauge.danger_level_m ?? "—"} m · trend {gauge.trend ?? "not stated"} ·{" "}
                    {gauge.distance_km === null ? "distance not stated" : `${gauge.distance_km} km away`}
                    {gauge.observed_at ? ` · observed ${gauge.observed_at}` : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {feedKey === "bhuvan" && (
        <div className="mt-2 space-y-1.5">
          <p className="text-[11.5px] text-slate-400">
            {entry.matched_features ?? "—"} of {entry.features_in_feed ?? "—"} hazard-map feature(s) intersect this
            selection{entry.unclassified_features ? ` · ${entry.unclassified_features} unclassified` : ""}. Their
            polygons are not retained by the connector, so no geometry is drawn.
          </p>
          {Object.keys(entry.hazards ?? {}).length > 0 && (
            <ul className="flex flex-wrap gap-1.5">
              {Object.entries(entry.hazards ?? {}).map(([hazard, group]) => (
                <li key={hazard}>
                  <Badge>
                    {hazard.replace(/_/g, " ")} · {group.features} feature(s) · max{" "}
                    {Number(group.max_severity).toFixed(2)}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function LiveFeedsPanel({ feeds }: { feeds: LiveFeeds | null | undefined }) {
  if (!feeds) {
    return (
      <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3.5 py-3">
        <p className="text-[12.5px] leading-relaxed text-amber-200">
          This run carries no live-feed block, so no IMD, CWC or Bhuvan status can be reported. That is a gap in this
          run — not a finding that no warning exists.
        </p>
      </div>
    );
  }
  const order = ["imd", "cwc", "bhuvan"] as const;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={feeds.configured ? bandTone("low").chip : bandTone("unassessed").chip}>
          {feeds.configured ? "at least one feed configured" : "no live feed configured"}
        </Badge>
        {feeds.unconfigured.length > 0 && (
          <Badge tone={bandTone("moderate").chip}>{feeds.unconfigured.length} unconfigured</Badge>
        )}
        {feeds.unavailable.length > 0 && (
          <Badge tone={sourceTone("unavailable")}>{feeds.unavailable.length} did not answer</Badge>
        )}
        <span className="text-[11px] text-slate-500">
          matched records are listed per feed below and drawn on the map when geometry exists
        </span>
      </div>

      {order.map((key) => {
        const entry = feeds.feeds[key];
        return entry ? <LiveFeedCard key={key} feedKey={key} entry={entry} /> : null;
      })}

      {Object.keys(feeds.blended_into).length > 0 && (
        <div className="rounded-xl border border-slate-800/70 bg-slate-900/30 p-3">
          <SectionHeading hint="A source appears here only when its factor actually answered and was weighted.">
            Blended into the scores
          </SectionHeading>
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(feeds.blended_into).map(([hazard, sources]) => (
              <li key={hazard}>
                <Badge tone={bandTone("low").chip}>
                  {hazard.replace(/_/g, " ")} ← {sources.join(", ")}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
      )}

      {feeds.warnings.length > 0 && (
        <ul className="space-y-1">
          {feeds.warnings.map((warning) => (
            <li
              key={warning}
              className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[12px] leading-relaxed text-amber-200"
            >
              {warning}
            </li>
          ))}
        </ul>
      )}

      <Prose>{feeds.note}</Prose>
    </div>
  );
}

function districtName(row: AreaDistrict): string {
  return row.name_missing || !row.district ? "unnamed boundary" : row.district;
}

function AreaSummary({ area, labels }: { area: AreaBlock; labels?: Record<string, string> }) {
  return (
    <Card
      title="Drawn area"
      aside={`${area.samples_total} interior samples · worst-sample aggregation`}
      accent={bandTone("moderate").bar}
    >
      <div className="grid grid-cols-2 gap-2.5 xl:grid-cols-4">
        <StatTile label="Area" value={`${area.area_km2.toFixed(1)} km²`} accent="bg-amber-500" />
        <StatTile label="Perimeter" value={`${area.perimeter_km.toFixed(1)} km`} accent="bg-slate-600" />
        <StatTile
          label="Interior samples"
          value={area.samples_total}
          hint={`${Math.round(area.sample_radius_m)} m window each`}
          accent="bg-sky-500"
        />
        <StatTile
          label="Districts touched"
          value={area.districts.length}
          hint={area.states.map((row) => row.state).filter(Boolean).join(", ") || "no state matched"}
          accent="bg-emerald-500"
        />
      </div>
      <div className="mt-3">
        <Prose>{area.aggregation_note}</Prose>
      </div>
      <div className="mt-3 overflow-x-auto rounded-lg border border-slate-800/70">
        <table className="w-full min-w-[560px] border-collapse text-left text-[12px]">
          <thead className="bg-slate-900/60 text-slate-500">
            <tr>
              <th className="py-1.5 pl-3 pr-3 font-medium">hazard</th>
              <th className="py-1.5 pr-3 font-medium">min</th>
              <th className="py-1.5 pr-3 font-medium">mean</th>
              <th className="py-1.5 pr-3 font-medium">max (headline)</th>
              <th className="py-1.5 pr-3 font-medium">band at max</th>
              <th className="py-1.5 pr-3 font-medium">samples scored</th>
            </tr>
          </thead>
          <tbody className="align-top">
            {area.hazard_summary.map((row) => (
              <tr key={row.hazard} className="border-t border-slate-800/70">
                <td className="py-1.5 pl-3 pr-3 font-medium text-slate-200">{row.hazard.replace(/_/g, " ")}</td>
                <td className="py-1.5 pr-3 font-mono text-slate-400">{formatScore(row.min_score)}</td>
                <td className="py-1.5 pr-3 font-mono text-slate-400">{formatScore(row.mean_score)}</td>
                <td className={`py-1.5 pr-3 font-mono font-semibold ${bandTone(row.band_at_max).text}`}>
                  {formatScore(row.max_score)}
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={bandTone(row.band_at_max).chip}>{bandLabel(row.band_at_max, labels)}</Badge>
                </td>
                <td className="py-1.5 pr-3 text-slate-400">
                  {row.samples_assessed} of {row.samples_total}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="mt-3 text-[11.5px]">
        <summary className="cursor-pointer text-slate-500">
          interior sample readings — discrete points, no interpolated surface
        </summary>
        <div className="mt-1.5 overflow-x-auto rounded-lg border border-slate-800/70">
          <table className="w-full min-w-[520px] border-collapse text-left text-[11.5px]">
            <thead className="bg-slate-900/60 text-slate-500">
              <tr>
                <th className="py-1.5 pl-3 pr-3 font-medium">#</th>
                <th className="py-1.5 pr-3 font-medium">lat, lon</th>
                <th className="py-1.5 pr-3 font-medium">peak score</th>
                <th className="py-1.5 pr-3 font-medium">band</th>
                <th className="py-1.5 pr-3 font-medium">terrain</th>
              </tr>
            </thead>
            <tbody className="align-top">
              {area.samples.map((sample, index) => (
                <tr key={`${sample.lat}-${sample.lon}-${index}`} className="border-t border-slate-800/70">
                  <td className="py-1.5 pl-3 pr-3 text-slate-500">{index + 1}</td>
                  <td className="py-1.5 pr-3 font-mono text-slate-400">
                    {sample.lat.toFixed(4)}, {sample.lon.toFixed(4)}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-slate-300">{formatScore(sample.score)}</td>
                  <td className="py-1.5 pr-3">
                    <Badge tone={bandTone(sample.band).chip}>{bandLabel(sample.band, labels)}</Badge>
                  </td>
                  <td className="py-1.5 pr-3 text-slate-400">
                    {sample.terrain_available
                      ? `${sample.elevation_at_point_m ?? "—"} m · relief ${sample.relief_m ?? "—"} m`
                      : "DEM did not answer here"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <div className="mt-3">
        <KeyValue
          rows={[
            ["bounds", area.bounds.map((value) => value.toFixed(3)).join(", ")],
            [
              "representative point",
              `${area.representative_point.lat.toFixed(4)}, ${area.representative_point.lon.toFixed(4)}`,
            ],
            ["vertices", String(area.vertices)],
            [
              "districts",
              area.districts.length === 0
                ? "none matched"
                : area.districts
                    .map(
                      (row) =>
                        `${districtName(row)} (${row.overlap_km2.toFixed(1)} km²)`,
                    )
                    .join(", "),
            ],
          ]}
        />
        {area.districts.some((row) => row.name_missing || !row.district) && (
          <div className="mt-2">
            <Prose tone="text-amber-300/90">
              At least one overlapping polygon in the loaded district layer carries no name upstream. It is listed as
              an unnamed boundary and its overlap is counted separately — it is not merged into a named district or
              silently dropped.
            </Prose>
          </div>
        )}
      </div>
    </Card>
  );
}

export default function AnalysisReport({ analysis, labels }: { analysis: Analysis; labels?: Record<string, string> }) {
  const [isOpenProvenanceModal, setIsOpenProvenanceModal] = useState(false);
  const [isOpenRecommendationsModal, setIsOpenRecommendationsModal] = useState(false);
  const [provenanceTab, setProvenanceTab] = useState<ProvenanceTab>("overview");
  const [sourceQuery, setSourceQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const [showWithheld, setShowWithheld] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const downloadPdf = async () => {
    setExporting(true);
    setExportError(null);
    try {
      await exportAnalysisPdf(analysis, labels);
    } catch (error) {
      setExportError(
        `The PDF could not be produced: ${error instanceof Error ? error.message : String(error)}. ` +
          "Nothing was downloaded.",
      );
    } finally {
      setExporting(false);
    }
  };

  const overallTone = bandTone(analysis.overall.band);
  const mlModels = analysis.scoring?.ml_models_used ?? analysis.model_versions.ml_models_used;
  const engineName = analysis.scoring?.engine ?? analysis.model_versions.engine;
  const engineVersion = analysis.scoring?.engine_version ?? analysis.model_versions.engine_version;
  const heuristicCount = analysis.hazards.filter((hazard) => hazard.method === "rule_engine").length;
  const mlCount = analysis.hazards.length - heuristicCount;
  const gaps = useMemo(() => gapRows(analysis), [analysis]);
  const summary = useMemo(() => summariseRecommendations(analysis), [analysis]);

  const sources = Object.entries(analysis.data_completeness.sources);
  const answeredSources = sources.filter(([, state]) => state === "available").length;
  const highPlus = analysis.hazards.filter((hazard) => BAND_RANK[hazard.band] >= BAND_RANK.high).length;

  const openProvenance = (tab: ProvenanceTab) => {
    setProvenanceTab(tab);
    setIsOpenProvenanceModal(true);
  };

  return (
    <div className="space-y-4">
      <section className="glass-card overflow-hidden rounded-xl">
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-4">
          <div className="min-w-0">
            <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-sky-300/80">Analysis record</p>
            <h2 className="mt-1 text-[19px] font-semibold leading-tight tracking-tight text-slate-50">
              {analysis.location.admin.label}{" "}
              <span className="font-normal text-slate-500">
                · {analysis.location.lat.toFixed(4)}, {analysis.location.lon.toFixed(4)}
              </span>
            </h2>
            <p className="mt-1 font-mono text-[11px] text-slate-500">
              {analysis.analysis_uuid} · {analysis.area ? "drawn area" : `${(analysis.location.radius_m / 1000).toFixed(1)} km`} ·{" "}
              {formatTime(analysis.created_at)} · {formatDuration(analysis.duration_ms)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ActionButton variant="primary" onClick={() => void downloadPdf()} disabled={exporting}>
              {exporting ? "preparing PDF…" : "Download PDF Report"}
            </ActionButton>
            <ActionButton variant="primary" onClick={() => setIsOpenRecommendationsModal(true)}>
              View recommendations
              {summary.targeted.length > 0 && (
                <span className="rounded-full bg-slate-950/20 px-1.5 text-[10.5px] font-bold">
                  {summary.targeted.length}
                </span>
              )}
            </ActionButton>
            <ActionButton onClick={() => openProvenance("overview")}>Provenance &amp; data audit</ActionButton>
          </div>
        </div>
        {exportError && (
          <p role="alert" className="border-t border-red-500/30 bg-red-500/10 px-4 py-2 text-[12px] leading-relaxed text-red-200">
            {exportError}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-1.5 border-t border-slate-800/70 px-4 py-2.5">
          <Badge tone={overallTone.chip}>overall {bandLabel(analysis.overall.band, labels)}</Badge>
          <Badge>{analysis.status}</Badge>
          {analysis.is_synthetic ? (
            <Badge tone={bandTone("very_high").chip}>synthetic data — not a real observation</Badge>
          ) : (
            <Badge tone={bandTone("low").chip}>live sources, no synthetic data</Badge>
          )}
          {mlModels.length === 0 ? (
            <Badge>scored by spatial heuristics · ML models used: none</Badge>
          ) : (
            <Badge tone={ML_TONE}>ML models used: {mlModels.join(", ")}</Badge>
          )}
          <Badge tone={bandTone("unassessed").chip}>
            engine {engineName.replace(/_/g, " ")} {engineVersion}
          </Badge>
          {analysis.selection && <Badge tone={bandTone("moderate").chip}>drawn area · {analysis.selection.area_km2.toFixed(1)} km²</Badge>}
          {analysis.live_feeds && (
            <Badge tone={analysis.live_feeds.configured ? bandTone("low").chip : bandTone("unassessed").chip}>
              live feeds · {analysis.live_feeds.configured ? `${analysis.live_feeds.unavailable.length} failing` : "none configured"}
            </Badge>
          )}
        </div>
      </section>

      <div className="grid grid-cols-2 gap-2.5 xl:grid-cols-4">
        <StatTile
          label="Overall band"
          value={bandLabel(analysis.overall.band, labels)}
          tone={overallTone.text}
          accent={overallTone.bar}
          hint={
            analysis.overall.highest ? `highest: ${analysis.overall.highest.replace(/_/g, " ")}` : "no hazard scored"
          }
        />
        <StatTile
          label="High or above"
          value={`${highPlus} / ${analysis.hazards.length}`}
          tone={highPlus > 0 ? bandTone("high").text : "text-slate-50"}
          accent={highPlus > 0 ? bandTone("high").bar : "bg-slate-600"}
          hint={`${analysis.overall.hazards_assessed.length} assessed · ${analysis.overall.hazards_unassessed.length} unassessed`}
        />
        <StatTile
          label="Mean confidence"
          value={formatConfidence(analysis.overall.mean_confidence)}
          hint="answered weight ÷ total weight"
          accent="bg-amber-500"
        />
        <StatTile
          label="Sources answered"
          value={`${answeredSources} / ${sources.length}`}
          hint={
            gaps.length === 0
              ? "every proxy in this run answered"
              : `${gaps.length} dropped factor${gaps.length === 1 ? "" : "s"}`
          }
          accent="bg-emerald-500"
        />
      </div>

      {analysis.area && <AreaSummary area={analysis.area} labels={labels} />}

      <Card title="Run record" aside={`${analysis.status} · engine ${engineName.replace(/_/g, " ")} ${engineVersion}`}>
        <div className="grid gap-4 sm:grid-cols-2">
          <KeyValue
            rows={[
              ["location", `${analysis.location.lat.toFixed(4)}, ${analysis.location.lon.toFixed(4)}`],
              ["admin", analysis.location.admin.label],
              [
                analysis.area ? "sample window" : "radius",
                `${(analysis.location.radius_m / 1000).toFixed(1)} km`,
              ],
              ["created", formatTime(analysis.created_at)],
            ]}
          />
          <KeyValue
            rows={[
              [
                "highest",
                analysis.overall.highest
                  ? `${analysis.overall.highest.replace(/_/g, " ")} ${formatScore(analysis.overall.highest_score)}`
                  : "—",
              ],
              ["mean confidence", formatConfidence(analysis.overall.mean_confidence)],
              ["assessed", `${analysis.overall.hazards_assessed.length} of ${analysis.hazards.length}`],
              ["unassessed", analysis.overall.hazards_unassessed.join(", ") || "none"],
            ]}
          />
        </div>
        {analysis.location.admin.boundary_uncertain && (
          <div className="mt-3">
            <Prose tone="text-amber-300/90">
              The district boundary for this point is marked uncertain
              {analysis.location.admin.boundary_distance_m !== null
                ? ` (nearest boundary ${Math.round(analysis.location.admin.boundary_distance_m)} m away)`
                : ""}
              . Scores are still the measured ones; the administrative label is the part in question.
            </Prose>
          </div>
        )}
      </Card>

      <Card
        title="Hazard scores"
        aside={`${analysis.overall.hazards_assessed.length} of ${analysis.hazards.length} assessed · expand for factors`}
        accent={overallTone.bar}
      >
        <Prose tone="text-slate-400">
          {mlCount === 0
            ? `All ${analysis.hazards.length} hazards were scored by weighted heuristic proxies read from measured evidence; no ML inference was used.`
            : `${heuristicCount} of ${analysis.hazards.length} hazards were scored by heuristic proxies; ${mlCount} used ML inference (${mlModels.join(", ")}).`}{" "}
          Each factor carries a fixed weight and a measured severity (0–1). A factor whose source did not answer is
          dropped, and the confidence shown is the answered weight ÷ total weight.
          {mlModels.length === 0 &&
            " The ML models are untrained on real labels in this build, which is why the fallback is the rule engine rather than an inference."}
        </Prose>
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {analysis.hazards.map((hazard) => (
            <HazardCard
              key={hazard.hazard}
              hazard={hazard}
              labels={labels}
              onOpenAudit={() => openProvenance("sources")}
            />
          ))}
        </div>
      </Card>

      <section className="glass-card flex flex-wrap items-center gap-3 rounded-xl px-4 py-3.5">
        <div className="min-w-0 flex-1">
          <p className={`text-[12.5px] font-medium ${gaps.length > 0 ? "text-amber-200" : "text-slate-200"}`}>
            {gaps.length === 0
              ? "Every proxy in this run answered."
              : `${gaps.length} factor${gaps.length === 1 ? "" : "s"} did not answer — the weight was removed and confidence lowered, never imputed.`}
          </p>
          <p className="mt-1 text-[11.5px] leading-relaxed text-slate-500">
            {mlModels.length === 0
              ? `All ${analysis.hazards.length} hazard rows scored by heuristic proxies · no ML model contributed`
              : `${heuristicCount} heuristic · ${mlCount} with ML inference (${mlModels.join(", ")})`}{" "}
            · mean confidence {formatConfidence(analysis.overall.mean_confidence)} · every number is traceable in the
            data audit.
          </p>
        </div>
        <ActionButton onClick={() => openProvenance(gaps.length > 0 ? "gaps" : "overview")}>
          {gaps.length > 0 ? "Review data gaps" : "Open data audit"}
        </ActionButton>
      </section>

      <Sheet
        open={isOpenRecommendationsModal}
        onClose={() => setIsOpenRecommendationsModal(false)}
        eyebrow="Executive summary"
        title="Recommended actions"
        subtitle={`${summary.targeted.length} action${summary.targeted.length === 1 ? "" : "s"} for Moderate-or-higher hazard zones · ${analysis.recommendations.length} issued by the rule engine`}
        size="modal"
        footer={
          <div className="flex flex-wrap items-center gap-3">
            <p className="min-w-[200px] flex-1 text-[11px] leading-relaxed text-slate-500">
              Guidance only — not a substitute for the instructions of the local disaster authority. Low-risk actions
              are filtered out of the summary by default, never deleted.
            </p>
            <ActionButton onClick={() => setIsOpenRecommendationsModal(false)}>Close</ActionButton>
          </div>
        }
      >
        <RecommendationsBody
          analysis={analysis}
          summary={summary}
          showWithheld={showWithheld}
          onToggleWithheld={() => setShowWithheld((value) => !value)}
        />
      </Sheet>

      <Sheet
        open={isOpenProvenanceModal}
        onClose={() => setIsOpenProvenanceModal(false)}
        eyebrow="Data audit"
        title="Provenance & data audit"
        subtitle={`${analysis.analysis_uuid} · ${analysis.location.admin.label} · ${
          analysis.area ? `${analysis.area.area_km2.toFixed(1)} km² drawn area` : `${(analysis.location.radius_m / 1000).toFixed(1)} km`
        } · read from this run's stored payload — opening the drawer re-fetches nothing`}
        size="wide"
        footer={
          <div className="flex flex-wrap items-center gap-3">
            <p className="min-w-[200px] flex-1 text-[11px] leading-relaxed text-slate-500">
              Provider states were recorded at request time on the API host. A source that did not answer is a gap,
              not a zero.
            </p>
            <ActionButton onClick={() => setIsOpenProvenanceModal(false)}>Close</ActionButton>
          </div>
        }
      >
        <ProvenancePanel
          analysis={analysis}
          tab={provenanceTab}
          onTab={setProvenanceTab}
          gaps={gaps}
          sourceQuery={sourceQuery}
          onSourceQuery={setSourceQuery}
          sourceFilter={sourceFilter}
          onSourceFilter={setSourceFilter}
        />
      </Sheet>
    </div>
  );
}
