"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Card, ErrorNote, Prose } from "@/components/ui";
import { API_BASE, ApiError, api } from "@/lib/api";
import { bandLabel, bandTone, formatDuration, formatTime } from "@/lib/bands";
import type { AnalysisSummary } from "@/lib/types";

export default function HistoryPage() {
  const [rows, setRows] = useState<AnalysisSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(50);

  useEffect(() => {
    void api
      .analyses(limit)
      .then((body) => setRows(body.analyses))
      .catch((reason: unknown) => setError(reason instanceof ApiError ? reason.message : String(reason)));
  }, [limit]);

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <h1 className="text-[17px] font-semibold text-slate-50">Stored analyses</h1>
        <span className="text-[11.5px] text-slate-500">
          every run the API persisted on this machine · <span className="font-mono">{API_BASE}</span>
        </span>
      </div>

      {error && <ErrorNote title="Could not read the archive" message={error} />}
      {rows === null && !error && <Prose>Reading the archive…</Prose>}
      {rows && rows.length === 0 && (
        <Prose>
          Nothing has been analysed on this instance yet. Run one from the{" "}
          <Link className="text-sky-400 underline" href="/">
            command center
          </Link>
          .
        </Prose>
      )}

      {rows && rows.length > 0 && (
        <Card title={`${rows.length} most recent`} aside="band and warning count as recorded at run time">
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-[12.5px]">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-1.5 pr-3 font-medium">when</th>
                  <th className="py-1.5 pr-3 font-medium">point</th>
                  <th className="py-1.5 pr-3 font-medium">window / area</th>
                  <th className="py-1.5 pr-3 font-medium">band</th>
                  <th className="py-1.5 pr-3 font-medium">status</th>
                  <th className="py-1.5 pr-3 font-medium">warnings</th>
                  <th className="py-1.5 pr-3 font-medium">took</th>
                  <th className="py-1.5 font-medium">record</th>
                </tr>
              </thead>
              <tbody className="align-top">
                {rows.map((row) => (
                  <tr key={row.analysis_uuid} className="border-t border-slate-800">
                    <td className="py-1.5 pr-3 text-slate-400">{formatTime(row.created_at)}</td>
                    <td className="py-1.5 pr-3 font-mono text-[11.5px] text-slate-300">
                      {row.lat.toFixed(4)}, {row.lon.toFixed(4)}
                    </td>
                    <td className="py-1.5 pr-3">
                      {row.geometry_kind === "polygon"
                        ? `${row.area_km2 === null || row.area_km2 === undefined ? "—" : row.area_km2.toFixed(1)} km² area`
                        : `${(row.radius_m / 1000).toFixed(1)} km`}
                    </td>
                    <td className="py-1.5 pr-3">
                      <span
                        className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${bandTone(row.overall_band).chip}`}
                      >
                        {bandLabel(row.overall_band)}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 text-slate-400">{row.status}</td>
                    <td className="py-1.5 pr-3">{row.warnings}</td>
                    <td className="py-1.5 pr-3">{formatDuration(row.duration_ms)}</td>
                    <td className="py-1.5">
                      <Link
                        href={`/analysis/${row.analysis_uuid}`}
                        className="text-sky-400 underline font-mono text-[11.5px] break-all"
                      >
                        {row.analysis_uuid.slice(0, 8)}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex items-center gap-2 text-[12px] text-slate-500">
            showing
            <select
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-slate-200"
            >
              {[20, 50, 100, 200].map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <span>the API caps the list at 200.</span>
          </div>
        </Card>
      )}
    </div>
  );
}
