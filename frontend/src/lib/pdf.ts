/**
 * Client-side PDF export for a stored analysis.
 *
 * html2pdf rasterises the DOM, and html2canvas cannot parse Tailwind v4's `oklch()` colours.
 * The report is therefore built off-screen with inline hex colours only, never by cloning the
 * page. It renders the stored payload as-is: no value is recomputed for the document.
 */

import { bandLabel, formatConfidence, formatDuration, formatScore, formatTime } from "./bands";
import type { Analysis, LiveFeedEntry, Recommendation } from "./types";

const INK = "#0f172a";
const MUTED = "#475569";
const LINE = "#cbd5e1";
const BRAND = "#0c4a6e";
const BRAND_SOFT = "#e0f2fe";
const WARN = "#92400e";

const SANS = "Helvetica, Arial, sans-serif";
const MONO = "Consolas, 'Courier New', monospace";

interface PdfDocument {
  internal: { getNumberOfPages(): number };
  setPage(page: number): void;
  setFontSize(size: number): void;
  setTextColor(red: number, green: number, blue: number): void;
  text(text: string, x: number, y: number, options?: { align?: string }): void;
  save(name: string): void;
}

function node<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  style: Partial<CSSStyleDeclaration>,
  text?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  Object.assign(element.style, style);
  if (text !== undefined) element.textContent = text;
  return element;
}

function section(title: string, subtitle?: string): HTMLElement {
  const wrap = node("section", { marginTop: "16px", pageBreakInside: "avoid" });
  wrap.appendChild(
    node(
      "h2",
      {
        margin: "0 0 2px",
        fontSize: "12px",
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color: BRAND,
        borderBottom: `1px solid ${LINE}`,
        paddingBottom: "3px",
      },
      title,
    ),
  );
  if (subtitle) {
    wrap.appendChild(node("p", { margin: "3px 0 6px", fontSize: "9px", color: MUTED, lineHeight: "1.4" }, subtitle));
  }
  return wrap;
}

function table(headers: string[], rows: (string | number | null)[][], columnWidths?: string[]): HTMLTableElement {
  const table = node("table", {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: "8.5px",
    lineHeight: "1.35",
    marginTop: "4px",
  });
  const head = table.insertRow();
  headers.forEach((label, index) => {
    const cell = node("th", {
      textAlign: "left",
      padding: "3px 4px",
      background: BRAND_SOFT,
      color: BRAND,
      borderBottom: `1px solid ${LINE}`,
      fontSize: "8px",
      textTransform: "uppercase",
      letterSpacing: "0.04em",
      width: columnWidths?.[index] ?? "auto",
    });
    cell.textContent = label;
    head.appendChild(cell);
  });
  for (const row of rows) {
    const tr = table.insertRow();
    // Keep a row from being split across a page boundary.
    tr.style.pageBreakInside = "avoid";
    row.forEach((value, index) => {
      const td = node("td", {
        padding: "3px 4px",
        borderBottom: `1px solid ${LINE}`,
        color: INK,
        verticalAlign: "top",
        wordBreak: "break-word",
      });
      td.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
      tr.appendChild(td);
      if (columnWidths?.[index]) td.style.width = columnWidths[index];
    });
  }
  return table;
}

function bulletList(lines: string[]): HTMLElement {
  const list = node("ul", { margin: "4px 0 0 0", paddingLeft: "15px", fontSize: "9px", lineHeight: "1.45", color: INK });
  for (const line of lines) {
    const item = node("li", { marginBottom: "2px" });
    item.textContent = line;
    list.appendChild(item);
  }
  return list;
}

function feedRow(key: string, entry: LiveFeedEntry | undefined, labels: Record<string, string>): (string | number | null)[] {
  if (!entry) return [labels[key] ?? key, "not gathered", "—", "—", "—"];
  const matches =
    key === "imd"
      ? `${entry.matched_count ?? "—"} of ${entry.alerts_total ?? "—"} warning(s)`
      : key === "cwc"
        ? `${entry.gauges_within_radius ?? "—"} gauge(s) in range`
        : `${entry.matched_features ?? "—"} of ${entry.features_in_feed ?? "—"} feature(s)`;
  return [
    entry.label ?? labels[key] ?? key,
    entry.status ?? "not stated",
    matches,
    entry.reason ?? entry.provider ?? "—",
    entry.env_var ?? "—",
  ];
}

const FEED_NAMES: Record<string, string> = {
  imd: "IMD warnings",
  cwc: "CWC river gauges",
  bhuvan: "Bhuvan/NDMA hazard maps",
};

function recommendationRows(recommendations: Recommendation[]): (string | number | null)[][] {
  return recommendations.slice(0, 24).map((item) => [
    item.horizon,
    item.hazard.replace(/_/g, " "),
    item.action,
    item.authority ?? "not attributed",
    item.requires_professional_assessment ? "professional assessment" : "guidance",
  ]);
}

function buildReportElement(analysis: Analysis, labels?: Record<string, string>): HTMLElement {
  const root = node("div", {
    width: "760px",
    padding: "26px 30px 18px",
    background: "#ffffff",
    color: INK,
    fontFamily: SANS,
    fontSize: "10px",
    lineHeight: "1.45",
  });

  // Branded masthead
  const masthead = node("header", {
    borderTop: `5px solid ${BRAND}`,
    paddingTop: "10px",
    marginBottom: "12px",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: "16px",
  });
  const brand = node("div", {});
  brand.appendChild(
    node("p", { margin: "0", fontSize: "9px", letterSpacing: "0.22em", color: BRAND, fontWeight: "bold" }, "INDIGEO"),
  );
  brand.appendChild(node("h1", { margin: "3px 0 2px", fontSize: "20px", lineHeight: "1.15", color: INK }, "Hazard Assessment Report"));
  brand.appendChild(
    node(
      "p",
      { margin: "0", fontSize: "9.5px", color: MUTED },
      `${analysis.location.admin.label} · ${analysis.location.lat.toFixed(4)}, ${analysis.location.lon.toFixed(4)}`,
    ),
  );
  const meta = node("div", { textAlign: "right", fontSize: "8.5px", color: MUTED, minWidth: "200px" });
  meta.appendChild(node("p", { margin: "0", fontFamily: MONO }, analysis.analysis_uuid));
  meta.appendChild(node("p", { margin: "2px 0 0" }, `Created ${formatTime(analysis.created_at)}`));
  meta.appendChild(node("p", { margin: "2px 0 0" }, `Engine ${analysis.model_versions.engine} ${analysis.model_versions.engine_version}`));
  masthead.appendChild(brand);
  masthead.appendChild(meta);
  root.appendChild(masthead);

  // Executive summary
  const exec = section(
    "1 · Executive summary",
    "Every figure below is read from the stored payload. Missing observations are reported as gaps, never filled in.",
  );
  exec.appendChild(
    table(
      ["field", "value"],
      [
        ["Overall band", bandLabel(analysis.overall.band, labels)],
        [
          "Highest hazard",
          analysis.overall.highest
            ? `${analysis.overall.highest.replace(/_/g, " ")} · ${formatScore(analysis.overall.highest_score)}`
            : "no hazard scored",
        ],
        ["Hazards assessed", `${analysis.overall.hazards_assessed.length} of ${analysis.hazards.length}`],
        ["Mean confidence", formatConfidence(analysis.overall.mean_confidence)],
        [
          "Selection",
          analysis.selection
            ? `${analysis.selection.area_km2.toFixed(1)} km² polygon · ${analysis.selection.vertices} vertices`
            : `point + ${(analysis.location.radius_m / 1000).toFixed(1)} km radius`,
        ],
        ["Sources answered", `${analysis.data_completeness.sources_available.length} of ${Object.keys(analysis.data_completeness.sources).length}`],
        ["Run time", formatDuration(analysis.duration_ms)],
        ["Synthetic data", analysis.is_synthetic ? "yes — not a real observation" : "no (live sources)"],
      ],
      ["28%", "72%"],
    ),
  );
  root.appendChild(exec);

  // Area block
  if (analysis.area) {
    const area = analysis.area;
    const areaSection = section(
      "2 · Drawn area",
      area.aggregation_note,
    );
    areaSection.appendChild(
      table(
        ["metric", "value"],
        [
          ["Area", `${area.area_km2.toFixed(1)} km²`],
          ["Perimeter", `${area.perimeter_km.toFixed(1)} km`],
          ["Bounds", area.bounds.map((value) => value.toFixed(3)).join(", ")],
          ["Interior samples", `${area.samples_total} points, ${area.sample_radius_m.toFixed(0)} m window each`],
          ["Districts touched", area.districts.map((row) => row.district ?? "unnamed").join(", ") || "none matched"],
          ["States touched", area.states.map((row) => row.state ?? "unnamed").join(", ") || "none matched"],
          ["Aggregation", "worst sampled point per hazard (not an interior average)"],
        ],
        ["28%", "72%"],
      ),
    );
    areaSection.appendChild(
      table(
        ["hazard", "min", "mean", "max", "band at max", "samples scored"],
        area.hazard_summary.map((row) => [
          row.hazard.replace(/_/g, " "),
          row.min_score === null ? "—" : row.min_score.toFixed(1),
          row.mean_score === null ? "—" : row.mean_score.toFixed(1),
          row.max_score === null ? "—" : row.max_score.toFixed(1),
          bandLabel(row.band_at_max, labels),
          `${row.samples_assessed} of ${row.samples_total}`,
        ]),
      ),
    );
    root.appendChild(areaSection);
  }

  // Hazard scores
  const hazardSection = section(
    `${analysis.area ? 3 : 2} · Hazard scores`,
    "Score = 100 × Σ(weight × severity) ÷ Σ(weight over the factors that answered). A factor whose source failed is dropped and lowers confidence.",
  );
  hazardSection.appendChild(
    table(
      ["hazard", "score", "band", "confidence", "factors", "method", "warnings"],
      analysis.hazards.map((hazard) => [
        hazard.hazard.replace(/_/g, " "),
        hazard.score === null ? "—" : formatScore(hazard.score),
        bandLabel(hazard.band, labels),
        formatConfidence(hazard.confidence),
        `${hazard.data_completeness.factors_available} of ${hazard.data_completeness.factors_total}`,
        hazard.method === "rule_engine" ? "heuristic proxies" : hazard.method,
        hazard.warnings.length,
      ]),
    ),
  );
  root.appendChild(hazardSection);

  // Live feeds
  const feeds = analysis.live_feeds;
  const feedSection = section(
    `${analysis.area ? 4 : 3} · Live Indian feeds`,
    feeds?.note ?? "No live-feed block was attached to this run.",
  );
  if (feeds && Object.keys(feeds.feeds).length > 0) {
    feedSection.appendChild(
      table(
        ["feed", "status", "matches", "reason / provider", "env var"],
        (["imd", "cwc", "bhuvan"] as const).map((key) => feedRow(key, feeds.feeds[key], FEED_NAMES)),
        ["19%", "11%", "19%", "36%", "15%"],
      ),
    );
    if (Object.keys(feeds.blended_into).length > 0) {
      feedSection.appendChild(
        bulletList(
          Object.entries(feeds.blended_into).map(
            ([hazard, sources]) => `${hazard.replace(/_/g, " ")} read ${sources.join(", ")} as a live factor.`,
          ),
        ),
      );
    }
  } else {
    feedSection.appendChild(
      node("p", { margin: "4px 0 0", fontSize: "9px", color: WARN }, feeds?.note ?? "No live-feed block was attached to this run."),
    );
  }
  root.appendChild(feedSection);

  // Recommendations
  const targeted = analysis.recommendations.filter((item) => {
    const hazard = analysis.hazards.find((row) => row.hazard === item.hazard);
    return hazard ? ["moderate", "high", "very_high", "extreme"].includes(hazard.band) : false;
  });
  const recSection = section(
    `${analysis.area ? 5 : 4} · Recommended actions`,
    `${targeted.length} action(s) attached to Moderate-or-higher hazards, strongest first. Guidance only — not a substitute for the local disaster authority.`,
  );
  if (targeted.length > 0) {
    recSection.appendChild(
      table(
        ["horizon", "hazard", "action", "authority", "flag"],
        recommendationRows(targeted),
        ["11%", "13%", "48%", "17%", "11%"],
      ),
    );
    if (targeted.length > 24) {
      recSection.appendChild(
        node("p", { margin: "4px 0 0", fontSize: "8.5px", color: MUTED }, `Showing 24 of ${targeted.length} actions; the full list is on the stored record.`),
      );
    }
  } else {
    recSection.appendChild(node("p", { margin: "4px 0 0", fontSize: "9px", color: MUTED }, "No action was issued for a Moderate-or-higher hazard in this run."));
  }
  root.appendChild(recSection);

  // Provenance
  const provenance = analysis.data_completeness.provenance;
  const provenanceSection = section(
    `${analysis.area ? 6 : 5} · Provenance`,
    `${provenance.length} request(s) recorded at fetch time: endpoint, HTTP status, cache state, retrieval time and licence, so every number above is traceable.`,
  );
  if (provenance.length > 0) {
    provenanceSection.appendChild(
      table(
        ["provider", "endpoint", "HTTP", "elapsed", "cache", "retrieved", "licence / attribution"],
        provenance.map((row) => [
          row.provider,
          row.endpoint,
          row.http_status === null ? "no answer" : row.http_status,
          row.elapsed_ms === null ? "—" : `${Math.round(row.elapsed_ms)} ms`,
          row.from_cache ? "cached" : "live",
          row.fetched_at ?? "—",
          [row.license, row.attribution].filter(Boolean).join(" — ") || "not stated",
        ]),
        ["14%", "35%", "7%", "9%", "7%", "12%", "16%"],
      ),
    );
  } else {
    provenanceSection.appendChild(
      node("p", { margin: "4px 0 0", fontSize: "9px", color: WARN }, "No provenance record was attached; treat these numbers as unverified."),
    );
  }
  root.appendChild(provenanceSection);

  // Limitations and gaps
  const limits = section(
    `${analysis.area ? 7 : 6} · Limitations and data gaps`,
    "Stated by the engine for this run, in its own order.",
  );
  if (analysis.limitations.length > 0) limits.appendChild(bulletList(analysis.limitations));
  const missing = analysis.hazards.flatMap((hazard) =>
    hazard.factors
      .filter((factor) => !factor.available)
      .map((factor) => `${hazard.hazard.replace(/_/g, " ")} · ${factor.name.replace(/_/g, " ")} (${factor.source}): ${factor.note ?? "no note supplied"}`),
  );
  if (missing.length > 0) {
    limits.appendChild(
      node("p", { margin: "8px 0 2px", fontSize: "9px", fontWeight: "bold", color: WARN }, `${missing.length} factor(s) did not answer:`),
    );
    limits.appendChild(bulletList(missing));
  }
  root.appendChild(limits);

  // Footer
  root.appendChild(
    node(
      "p",
      { marginTop: "16px", paddingTop: "6px", borderTop: `1px solid ${LINE}`, fontSize: "7.5px", color: MUTED, lineHeight: "1.4" },
      `Generated ${new Date().toLocaleString()} from the stored analysis payload (no values recomputed). ` +
        "Hazard scores are spatial heuristics over public evidence, not a forecast or a substitute for a licensed assessment. " +
        "Live feeds reflect the API host's configuration and the request time recorded above.",
    ),
  );

  return root;
}

export async function exportAnalysisPdf(analysis: Analysis, labels?: Record<string, string>): Promise<void> {
  const { default: html2pdf } = await import("html2pdf.js");
  const element = buildReportElement(analysis, labels);
  const host = document.createElement("div");
  host.style.position = "fixed";
  host.style.left = "-10000px";
  host.style.top = "0";
  host.style.zIndex = "-1";
  host.appendChild(element);
  document.body.appendChild(host);

  const filename = `indigeo-report-${analysis.analysis_uuid.slice(0, 8)}.pdf`;
  try {
    const worker = html2pdf().set({
      margin: [10, 10, 12, 10],
      filename,
      image: { type: "jpeg", quality: 0.96 },
      html2canvas: { scale: 2, useCORS: true, backgroundColor: "#ffffff", logging: false },
      jsPDF: { unit: "mm", format: "a4", orientation: "portrait" },
      enableLinks: false,
    });
    const pdf = (await worker.from(element).toPdf().get("pdf")) as PdfDocument;
    const pages = pdf.internal.getNumberOfPages();
    for (let page = 1; page <= pages; page += 1) {
      pdf.setPage(page);
      pdf.setFontSize(7);
      pdf.setTextColor(71, 85, 105);
      pdf.text(`INDIGEO · ${analysis.analysis_uuid} · page ${page} of ${pages}`, 105, 291, { align: "center" });
    }
    pdf.save(filename);
  } finally {
    host.remove();
  }
}
