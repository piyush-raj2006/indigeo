"""Probe every data provider once and print the status the platform would report.

python -m scripts.check_providers
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.integrations.base import all_providers  # noqa: E402


def main() -> int:
    rows = []
    for provider in all_providers():
        try:
            status = provider.healthcheck()
        except Exception as exc:  # a probe must never crash the sweep
            status = type("S", (), {})()
            status.state = "unavailable"
            status.detail = f"probe crashed: {type(exc).__name__}: {exc}"
            status.endpoint = provider.base_url
            status.latency_ms = None
            status.http_status = None
        rows.append(
            {
                "provider": provider.key,
                "state": status.state,
                "requires_auth": status.requires_auth,
                "endpoint": status.endpoint,
                "http_status": status.http_status,
                "latency_ms": round(status.latency_ms) if status.latency_ms else None,
                "detail": status.detail,
            }
        )
    print(json.dumps(rows, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
