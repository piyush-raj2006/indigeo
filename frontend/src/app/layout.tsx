import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "IndiGeo | Geospatial Risk Analytics",
  description:
    "India-wide multi-hazard analysis over live public sources, with provenance and limitations on every number.",
};

const NAV = [
  { href: "/", label: "Command center" },
  { href: "/history", label: "History" },
  { href: "/sources", label: "Sources" },
];

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col bg-slate-950 font-sans text-slate-200">
        <header className="sticky top-0 z-40 border-b border-slate-800/80 bg-slate-950/80 shadow-[0_18px_40px_-30px_rgba(0,0,0,0.95)] backdrop-blur-xl">
          <div className="mx-auto flex w-full max-w-[1680px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5">
            <Link href="/" className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg border border-sky-500/30 bg-sky-500/10 font-mono text-[11px] font-semibold text-sky-300">
                IG
              </span>
              <span className="text-[15px] font-semibold tracking-tight text-slate-50">
                INDI<span className="text-sky-400">GEO</span>
              </span>
            </Link>
            <nav className="flex items-center gap-1 rounded-full border border-slate-800/80 bg-slate-900/50 p-0.5">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="rounded-full px-3 py-1 text-[12.5px] text-slate-400 transition-colors hover:bg-slate-800/70 hover:text-slate-50"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <p className="ml-auto max-w-[52ch] text-[10.5px] leading-tight text-slate-500">
              A rule engine over live public sources. A source that does not answer lowers confidence and is reported
              as a gap — it is never replaced with a substitute value. No machine-learning model contributes to these
              scores yet.
            </p>
          </div>
        </header>
        {children}
        <footer className="mt-auto border-t border-slate-800/70 bg-slate-950/60 px-4 py-3">
          <p className="mx-auto w-full max-w-[1680px] text-[11px] leading-relaxed text-slate-600">
            Guidance only — not a substitute for the instructions of the local disaster authority. Hazard scores are
            proxies built from the data this instance could actually reach; each run lists its own limitations. Map
            basemap © OpenStreetMap contributors, used under their tile usage policy for local testing.
          </p>
        </footer>
      </body>
    </html>
  );
}
