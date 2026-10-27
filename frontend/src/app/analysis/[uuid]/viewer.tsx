"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import AnalysisReport from "@/components/AnalysisReport";
import { ErrorNote, Prose } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { Analysis, HazardMeta } from "@/lib/types";

export default function AnalysisViewer({ uuid }: { uuid: string }) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [meta, setMeta] = useState<HazardMeta | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);

  useEffect(() => {
    void api
      .analysis(uuid)
      .then(setAnalysis)
      .catch((reason: unknown) => setError(reason instanceof ApiError ? reason : new Error(String(reason))));
    void api.hazardMeta().then(setMeta).catch(() => setMeta(null));
  }, [uuid]);

  return (
    <div className="mx-auto w-full max-w-[1180px] flex-1 space-y-4 px-4 pb-10 pt-4">
      <div className="glass-card flex flex-wrap items-center gap-3 rounded-xl px-4 py-2.5">
        <div>
          <p className="text-[10.5px] uppercase tracking-[0.18em] text-slate-500">archive</p>
          <h1 className="text-[16px] font-semibold text-slate-50">Stored run</h1>
        </div>
        <span className="rounded-full border border-slate-800/80 bg-slate-900/60 px-2 py-0.5 font-mono text-[11px] text-slate-400">
          {uuid}
        </span>
        <Link
          href="/"
          className="ml-auto rounded-lg border border-slate-700/70 bg-slate-900/60 px-3 py-1.5 text-[12px] text-slate-200 transition-colors hover:border-sky-500/60 hover:bg-slate-800/70"
        >
          new run in the command center
        </Link>
      </div>

      {error && (
        <ErrorNote
          title={error instanceof ApiError ? `Read failed (${error.code}, HTTP ${error.status})` : "Read failed"}
          message={error.message}
          hint="This is the record the API persisted for this uuid. If it is missing, the run never completed on this instance — no substitute data is generated here."
        />
      )}
      {!analysis && !error && <Prose>Reading the stored record…</Prose>}
      {analysis && <AnalysisReport analysis={analysis} labels={meta?.band_labels} />}
    </div>
  );
}
