"use client";

import { useEffect, useState } from "react";

import { Badge, Card, ErrorNote, KeyValue, Prose, SourceBadge } from "@/components/ui";
import { API_BASE, ApiError, api } from "@/lib/api";
import { bandTone, formatTime } from "@/lib/bands";
import type { HazardMeta, Health, LayerSummary, PlaceSearch, ProviderReport } from "@/lib/types";

export default function SourcesPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [providers, setProviders] = useState<ProviderReport | null>(null);
  const [layers, setLayers] = useState<LayerSummary | null>(null);
  const [meta, setMeta] = useState<HazardMeta | null>(null);
  const [registerHit, setRegisterHit] = useState<PlaceSearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  useEffect(() => {
    const fail = (key: string) => (reason: unknown) =>
      setError(`${key}: ${reason instanceof ApiError ? reason.message : String(reason)}`);
    void api.health().then(setHealth).catch(fail("health"));
    void api.providers().then(setProviders).catch(fail("providers"));
    void api.layers().then(setLayers).catch(fail("layers"));
    void api.hazardMeta().then(setMeta).catch(fail("hazards"));
    void api.searchPlaces("Nagpur", 1).then(setRegisterHit).catch(fail("register"));
  }, []);

  const refreshLive = async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      setProviders(await api.providers(true));
    } catch (reason) {
      setRefreshError(reason instanceof ApiError ? reason.message : String(reason));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h1 className="text-[17px] font-semibold text-slate-50">What this instance can and cannot reach</h1>
        <span className="font-mono text-[11px] text-slate-600">{API_BASE}</span>
      </div>
      {error && <Prose tone="text-amber-300/90">One or more reads failed: {error}</Prose>}

      <Card
        title="Data providers"
        aside={
          providers ? `${providers.mode} · recorded ${formatTime(providers.observed_at)}` : "not loaded"
        }
      >
        {providers && (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <button
                onClick={() => void refreshLive()}
                disabled={refreshing}
                className="rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-[12.5px] text-slate-100 hover:border-sky-500 disabled:opacity-60"
              >
                {refreshing ? "probing every source…" : "Re-probe now"}
              </button>
              <span className="text-[11.5px] text-slate-500">
                measured 51 s for the full sweep; the Overpass mirror is the slow part.
              </span>
            </div>
            {refreshError && <ErrorNote title="The live sweep failed" message={refreshError} />}
            <p className="mb-3 text-[11.5px] leading-relaxed text-slate-500">{providers.note}</p>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-left text-[12px]">
                <thead className="text-slate-500">
                  <tr>
                    <th className="py-1.5 pr-3 font-medium">provider</th>
                    <th className="py-1.5 pr-3 font-medium">state</th>
                    <th className="py-1.5 pr-3 font-medium">auth</th>
                    <th className="py-1.5 pr-3 font-medium">HTTP</th>
                    <th className="py-1.5 pr-3 font-medium">latency</th>
                    <th className="py-1.5 font-medium">what it said</th>
                  </tr>
                </thead>
                <tbody className="align-top">
                  {providers.providers.map((row) => (
                    <tr key={row.provider} className="border-t border-slate-800">
                      <td className="py-1.5 pr-3 font-medium text-slate-200">{row.provider}</td>
                      <td className="py-1.5 pr-3">
                        <SourceBadge state={row.state} />
                      </td>
                      <td className="py-1.5 pr-3 text-slate-400">{row.requires_auth ? "key needed" : "public"}</td>
                      <td className="py-1.5 pr-3">{row.http_status ?? "—"}</td>
                      <td className="py-1.5 pr-3">{row.latency_ms === null ? "—" : `${row.latency_ms} ms`}</td>
                      <td className="py-1.5 text-[11.5px] leading-relaxed text-slate-400">
                        {row.detail}
                        <span className="mt-0.5 block font-mono text-[10.5px] break-all text-slate-600">{row.endpoint}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {providers.providers.some((row) => row.state === "unconfigured") && (
              <Prose tone="text-sky-300/90">
                A provider marked <em>unconfigured</em> needs a credential this machine does not have. The engine does
                not guess the missing values: the affected factors are reported unavailable and confidence is lowered.
              </Prose>
            )}
          </>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Engine" aside={health ? `v${health.version} · ${health.environment}` : ""}>
          {health ? (
            <KeyValue
              rows={[
                ["name", health.app],
                ["engine", `${health.engine.name} ${health.engine.version}`],
                [
                  "ML models",
                  health.engine.ml_models_used.length === 0
                    ? "none — no model output is presented as a score"
                    : health.engine.ml_models_used.join(", "),
                ],
                ["database", `${health.database.dialect} (probe ${health.database.probe})`],
                ["spatial backend", health.spatial.backend],
                ["hazards", health.hazards_assessed.join(", ")],
              ]}
            />
          ) : (
            <Prose>The health endpoint did not answer, so nothing about the engine is claimed here.</Prose>
          )}
          {health && <p className="mt-2 text-[11.5px] leading-relaxed text-slate-500">{health.note}</p>}
        </Card>

        <Card title="Spatial layers loaded" aside={layers ? `${layers.backend}` : ""}>
          {layers ? (
            <ul className="space-y-2 text-[12.5px]">
              {layers.layers.map((layer) => (
                <li key={layer.table}>
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-slate-200">{layer.table}</span>
                    <Badge tone={layer.features > 0 ? bandTone("low").chip : bandTone("unassessed").chip}>
                      {layer.features} features
                    </Badge>
                  </div>
                  <span className="text-[11px] text-slate-500">
                    extent {layer.extent ? layer.extent.map((value) => value.toFixed(3)).join(", ") : "unknown"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Prose>No layer list was returned.</Prose>
          )}
          {layers && (
            <p className="mt-2 text-[11px] leading-relaxed text-slate-600">
              stored at {layers.path} — boundaries only; elevation is sampled per request from the terrarium DEM and
              coastline distance from the loaded outline.
            </p>
          )}
        </Card>
      </div>

      <Card title="Administrative boundary provenance" aside="read back from the register through /locations/search">
        {registerHit && registerHit.results.length > 0 ? (
          <>
            <KeyValue
              rows={[
                ["dataset", registerHit.results[0].provenance.dataset ?? "not stated"],
                ["vintage", registerHit.results[0].provenance.vintage ?? "not stated"],
                ["retrieved", formatTime(registerHit.results[0].provenance.retrieved_at)],
                [
                  "source",
                  registerHit.results[0].provenance.url ? (
                    <a className="text-sky-400 underline break-all" href={registerHit.results[0].provenance.url} target="_blank" rel="noreferrer noopener">
                      {registerHit.results[0].provenance.url}
                    </a>
                  ) : (
                    "not stated"
                  ),
                ],
              ]}
            />
            <p className="mt-2 text-[11.5px] leading-relaxed text-slate-500">
              Shown for the register hit “{registerHit.results[0].label}” ({registerHit.results[0].source}). The same
              block travels with every search result and every analysis. Vintage 2011_c means the district geometry
              follows the 2011 census arrangement; several states have since re-districted, which is why boundary
              caveats appear in each run’s limitations.
            </p>
          </>
        ) : (
          <Prose tone="text-amber-300/90">
            The register returned no hit, so its provenance block could not be shown here.
          </Prose>
        )}
      </Card>

      <Card title="What is not loaded" aside={meta ? `${meta.data_gaps.length} named gaps` : ""}>
        {meta ? (
          <>
            <ul className="space-y-1 text-[12.5px] leading-relaxed text-slate-300">
              {meta.data_gaps.map((gap) => (
                <li key={gap} className="flex gap-2">
                  <span className="text-amber-400">×</span>
                  <span>{gap}</span>
                </li>
              ))}
            </ul>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-[11.5px] text-slate-500">
              band thresholds:
              {meta.band_thresholds.map(([value, label]) => (
                <Badge key={label} tone={bandTone(label).chip}>
                  ≤ {value} {label}
                </Badge>
              ))}
              <Badge tone={bandTone("unassessed").chip}>no measured driver → unassessed, not low</Badge>
            </div>
            <div className="mt-3">
              <p className="text-[11.5px] text-slate-500">authorities cited by the advice layer</p>
              <ul className="mt-1 space-y-0.5 text-[12px]">
                {Object.entries(meta.authorities).map(([key, [name, url]]) => (
                  <li key={key} className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[11px] text-slate-400">{key}</span>
                    <span className="text-slate-300">{name}</span>
                    <a className="text-sky-400/80 underline" href={url} target="_blank" rel="noreferrer noopener">
                      {url.replace("https://", "")}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          </>
        ) : (
          <Prose>The hazard metadata endpoint did not answer, so its gap list is not shown.</Prose>
        )}
      </Card>
    </div>
  );
}
