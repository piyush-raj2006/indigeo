"use client";

import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

import { sourceTone } from "@/lib/bands";
import type { Provenance } from "@/lib/types";

export function Badge({ children, tone }: { children: ReactNode; tone?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${
        tone ?? "bg-slate-500/15 text-slate-300 ring-slate-500/30"
      }`}
    >
      {children}
    </span>
  );
}

export function SourceBadge({ state }: { state: string }) {
  return <Badge tone={sourceTone(state)}>{state}</Badge>;
}

export function Card({
  title,
  aside,
  children,
  className = "",
  accent,
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Optional colour dot in the header, e.g. a band bar class. */
  accent?: string;
}) {
  return (
    <section className={`glass-card rounded-xl ${className}`}>
      {(title || aside) && (
        <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-slate-800/70 px-4 py-2.5">
          <h2 className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            {accent && <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${accent}`} />}
            {title}
          </h2>
          {aside && <div className="text-[11px] text-slate-500">{aside}</div>}
        </header>
      )}
      <div className="px-4 py-3.5">{children}</div>
    </section>
  );
}

export function ActionButton({
  children,
  onClick,
  variant = "subtle",
  title,
  disabled,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "subtle";
  title?: string;
  disabled?: boolean;
  className?: string;
}) {
  const skin =
    variant === "primary"
      ? "bg-sky-500 text-slate-950 ring-sky-400/50 hover:bg-sky-400 shadow-[0_12px_30px_-16px_rgba(14,165,233,0.9)]"
      : "bg-slate-900/60 text-slate-200 ring-slate-700/80 hover:bg-slate-800/70 hover:text-white";
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      disabled={disabled}
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-semibold ring-1 ring-inset transition disabled:cursor-not-allowed disabled:opacity-50 ${skin} ${className}`}
    >
      {children}
    </button>
  );
}

export function StatTile({
  label,
  value,
  hint,
  tone = "text-slate-50",
  accent = "bg-slate-600",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: string;
  accent?: string;
}) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-slate-800/70 bg-slate-900/35 px-3.5 pb-2.5 pt-3">
      <span aria-hidden className={`absolute inset-x-0 top-0 h-0.5 ${accent}`} />
      <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">{label}</p>
      <p className={`mt-1 truncate text-[18px] font-semibold leading-tight ${tone}`}>{value}</p>
      {hint && <p className="mt-0.5 truncate text-[11px] text-slate-500">{hint}</p>}
    </div>
  );
}

export function SectionHeading({
  children,
  hint,
  action,
}: {
  children: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-2">
      <div>
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">{children}</h3>
        {hint && <p className="mt-0.5 text-[11px] text-slate-600">{hint}</p>}
      </div>
      {action}
    </div>
  );
}

export function Prose({ children, tone = "text-slate-400" }: { children: ReactNode; tone?: string }) {
  return <p className={`text-[12.5px] leading-relaxed ${tone}`}>{children}</p>;
}

export function KeyValue({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12.5px]">
      {rows.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-slate-500">{key}</dt>
          <dd className="text-slate-300">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ProvenanceTable({ rows }: { rows: Provenance[] }) {
  if (rows.length === 0) {
    return (
      <Prose tone="text-amber-300/90">
        No provenance record was attached to this block. Treat the numbers in it as unverified: the engine
        reports where each observation came from, and there is nothing to report here.
      </Prose>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800/70">
      <table className="w-full border-collapse text-left text-[12px]">
        <thead className="bg-slate-900/60 text-slate-500">
          <tr>
            <th className="py-1.5 pl-3 pr-3 font-medium">provider</th>
            <th className="py-1.5 pr-3 font-medium">endpoint</th>
            <th className="py-1.5 pr-3 font-medium">HTTP</th>
            <th className="py-1.5 pr-3 font-medium">elapsed</th>
            <th className="py-1.5 pr-3 font-medium">cache</th>
            <th className="py-1.5 pr-3 font-medium">retrieved</th>
            <th className="py-1.5 pr-3 font-medium">licence / attribution</th>
          </tr>
        </thead>
        <tbody className="align-top">
          {rows.map((row, index) => (
            <tr key={`${row.provider}-${index}`} className="border-t border-slate-800/70 transition hover:bg-slate-900/40">
              <td className="py-1.5 pl-3 pr-3 font-medium text-slate-200">{row.provider}</td>
              <td className="py-1.5 pr-3 font-mono text-[11px] break-all text-slate-400">{row.endpoint}</td>
              <td className="py-1.5 pr-3">
                {row.http_status === null ? (
                  <span className="text-red-300">no answer</span>
                ) : (
                  <span className={row.http_status >= 400 ? "text-red-300" : "text-emerald-300"}>{row.http_status}</span>
                )}
              </td>
              <td className="py-1.5 pr-3 text-slate-300">{row.elapsed_ms === null ? "—" : `${Math.round(row.elapsed_ms)} ms`}</td>
              <td className="py-1.5 pr-3 text-slate-400">{row.from_cache ? "cached" : "live"}</td>
              <td className="py-1.5 pr-3 text-[11px] text-slate-400">{row.fetched_at ?? "—"}</td>
              <td className="py-1.5 pr-3 text-[11px] text-slate-400">
                {[row.license, row.attribution].filter(Boolean).join(" — ") || "not stated"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ErrorNote({ title, message, hint }: { title: string; message: string; hint?: string }) {
  return (
    <div role="alert" className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-3">
      <p className="text-[13px] font-semibold text-red-200">{title}</p>
      <p className="mt-1 text-[12.5px] leading-relaxed text-red-100/90">{message}</p>
      {hint && <p className="mt-1 text-[12px] leading-relaxed text-red-100/70">{hint}</p>}
    </div>
  );
}

/**
 * Slide-over drawer (default) or centred modal (`size="modal"`), rendered through a portal so
 * no ancestor `backdrop-filter` can trap the fixed layer. Escape and the backdrop close it, and
 * body scroll is locked while it is open.
 */
export function Sheet({
  open,
  onClose,
  eyebrow,
  title,
  subtitle,
  children,
  footer,
  size = "drawer",
}: {
  open: boolean;
  onClose: () => void;
  eyebrow?: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: "drawer" | "wide" | "modal";
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = requestAnimationFrame(() => panelRef.current?.focus());
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
      cancelAnimationFrame(frame);
    };
  }, [open]);

  if (!open || typeof document === "undefined") return null;

  const centered = size === "modal";

  return createPortal(
    <div
      className={`fixed inset-0 z-[1200] flex ${centered ? "items-center justify-center p-3 sm:p-6" : "justify-end"}`}
    >
      <div
        className="overlay-enter absolute inset-0 bg-slate-950/75 backdrop-blur-[3px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`relative flex flex-col overflow-hidden border border-slate-700/70 bg-slate-950/95 text-slate-200 outline-none ${
          centered
            ? "modal-enter max-h-[90vh] w-full max-w-3xl rounded-2xl shadow-[0_40px_90px_-30px_rgba(0,0,0,0.9)]"
            : `sheet-enter h-full w-full border-l shadow-[-30px_0_80px_-40px_rgba(0,0,0,0.95)] ${
                size === "wide" ? "max-w-[1120px]" : "max-w-3xl"
              }`
        }`}
      >
        <header className="flex items-start gap-3 border-b border-slate-800/80 bg-slate-950/85 px-5 py-3.5">
          <div className="min-w-0">
            {eyebrow && (
              <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-sky-300/80">{eyebrow}</p>
            )}
            <h2 id={titleId} className="mt-0.5 text-[16px] font-semibold tracking-tight text-slate-50">
              {title}
            </h2>
            {subtitle && <p className="mt-0.5 text-[11.5px] leading-relaxed text-slate-500">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close panel"
            className="ml-auto rounded-lg px-2 py-0.5 text-[18px] leading-none text-slate-400 transition hover:bg-slate-800/70 hover:text-slate-100"
          >
            ×
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <footer className="border-t border-slate-800/80 bg-slate-950/85 px-5 py-3">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
