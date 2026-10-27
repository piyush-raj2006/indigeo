from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description="IndiGeo data acquisition runner")
    parser.add_argument("task", choices=["boundaries"], help="Which acquisition pipeline to run")
    parser.add_argument("--force-download", action="store_true", help="Re-download upstream files instead of reusing the cache")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable result summary")
    args = parser.parse_args()

    if args.task == "boundaries":
        from app.services.ingest_boundaries import run

        result = run(force_download=args.force_download)
        print(json.dumps(result, indent=2, default=str) if args.json else _human_boundaries(result))
        return 0
    return 2


def _human_boundaries(result: dict) -> str:
    counts = result.get("admin_counts", {})
    lines = [
        f"District polygons loaded : {result['districts']}",
        f"States/UT polygons       : {result['states_and_uts']}",
        f"States / UTs in register  : {counts.get('states')} / {counts.get('union_territories')}",
        f"National outline features: {result['outline_features']}",
        f"Extent (lon/lat)         : {result['district_extent']}",
        f"Spatial index            : {result['rtree']}",
        f"Source file digest       : {result['digest']}",
        "",
        "Coverage of hazard/observation layers (see docs/DATA_COVERAGE.md for the full table):",
    ]
    for dataset in result.get("coverage", {}).get("datasets", []):
        lines.append(
            f"  - {dataset['code']:<22} {dataset['category']:<14} {dataset['features']:>8} features  "
            f"status={dataset['status']}  source={dataset['source']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
