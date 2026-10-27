"""Run the hazard engine end-to-end against live sources for a few real Indian points.

    python scripts/probe_hazard_engine.py            # fast path, no Overpass
    python scripts/probe_hazard_engine.py --with-infrastructure
    python scripts/probe_hazard_engine.py --persist

Prints one block per point: what each source actually answered, the eight hazard signals
with their factors and observations, the first recommendations, and the wall-clock cost.
Nothing here writes to the database unless --persist is given, and nothing is synthesised:
a source that did not answer is reported as it is.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.analysis import run_point_analysis  # noqa: E402

POINTS = {
    "nagpur (inland, Deccan plateau)": (21.1458, 79.0882),
    "chennai (coastal, Coromandel)": (13.0878, 80.2785),
    "port blair (island, seismic zone VI)": (11.6234, 92.5715),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-infrastructure", action="store_true",
                        help="query the four OSM lifeline layers too (measured 500-570 s per point)")
    parser.add_argument("--persist", action="store_true", help="store the analyses")
    parser.add_argument("--json", metavar="PATH", help="also dump the raw payloads to a file")
    args = parser.parse_args()

    dumps: dict[str, object] = {}
    for label, (lat, lon) in POINTS.items():
        started = time.time()
        try:
            payload = run_point_analysis(
                lat, lon, requested_by="probe-hazard-engine",
                persist=args.persist, include_infrastructure=args.with_infrastructure,
            )
        except Exception as exc:
            print(f"\n=== {label} -> FAILED {type(exc).__name__}: {exc}")
            continue
        elapsed = time.time() - started
        completeness = payload["data_completeness"]
        print(f"\n=== {label}  {elapsed:.1f}s  overall={payload['overall']['band']}  "
              f"recommendations={len(payload['recommendations'])}  stored={args.persist}")
        print("  sources:", json.dumps(completeness["sources"]))
        for signal in payload["hazards"]:
            print(f"  {signal['hazard']:<14} {signal['band']:<11} score={signal['score']:<5} "
                  f"confidence={signal['confidence']}")
            for factor in signal["factors"]:
                if not factor["available"]:
                    print(f"      - {factor['name']:<32} {factor['source']:<16} UNAVAILABLE "
                          f"{(factor.get('note') or '')[:70]}")
                else:
                    print(f"      - {factor['name']:<32} {factor['source']:<16} "
                          f"sev={factor['severity']:<6} obs={json.dumps(factor.get('observed'))[:80]}")
        for limitation in payload["limitations"]:
            print("  note:", limitation[:150])
        dumps[lat_lon_key(label)] = payload
    if args.json:
        Path(args.json).write_text(json.dumps(dumps, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


def lat_lon_key(label: str) -> str:
    return label.split(" ")[0].lower()


if __name__ == "__main__":
    raise SystemExit(main())
