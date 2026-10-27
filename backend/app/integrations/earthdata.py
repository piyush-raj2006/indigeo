from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..core.config import get_settings
from ..core.errors import ProviderAuthError, ProviderUnavailableError
from .base import Provider, ProviderResult, ProviderStatus

CMR_SEARCH = "https://cmr.earthdata.nasa.gov/search/collections.umm-json"


class EarthdataProvider(Provider):
    """NASA Earthdata. CMR metadata is public; data access needs a personal access token.

    A successful public metadata search is *not* proof that the token works, so
    verification is a separate, explicitly labelled step.
    """

    key = "nasa_earthdata"
    base_url = CMR_SEARCH
    license = "NASA open data; per-dataset terms apply"
    attribution = "NASA Earthdata / LP DAAC / GES DAAC"
    requires_auth = True

    def credential_env(self) -> str | None:
        return "EARTHDATA_TOKEN"

    def auth_problem(self) -> str | None:
        return None  # metadata searches are public; protected access checks the token per call

    def credentials(self) -> dict[str, str]:
        token = get_settings().earthdata_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def token_present(self) -> bool:
        return bool((get_settings().earthdata_token or "").strip())

    def search_collections(
        self,
        *,
        short_name: str | None = None,
        concept_id: str | None = None,
        page_size: int = 10,
        **filters: Any,
    ) -> ProviderResult:
        """Public CMR metadata search. Never report this as authenticated data access.

        Filters are passed as query parameters, never spliced into the URL string.
        """
        params: dict[str, Any] = {"page_size": max(1, min(int(page_size), 2000))}
        if short_name:
            params["short_name"] = short_name
        if concept_id:
            params["concept_id"] = concept_id
        params.update({k: v for k, v in filters.items() if v is not None})
        return self.get_json(
            CMR_SEARCH,
            params=params,
            validator=_validate_cmr,
            cache_ttl=3600,
        )

    def protected_probe(self) -> ProviderResult:
        """Fetch a protected URL so the token is exercised for real.

        EARTHDATA_PROBE_URL must name a resource that only returns 200 with
        Earthdata Login auth. Without it we deliberately report "unverified" rather
        than treat a public metadata search as proof of authentication.
        """
        if not self.token_present():
            raise ProviderAuthError(
                self.key,
                "EARTHDATA_TOKEN is not set, so protected data access cannot be verified.",
                missing_env="EARTHDATA_TOKEN",
            )
        probe = (get_settings().earthdata_probe_url or "").strip()
        if not probe:
            raise ProviderAuthError(
                self.key,
                "No protected probe URL configured, so token validity cannot be confirmed. "
                "Set EARTHDATA_PROBE_URL to a dataset URL that requires Earthdata Login.",
                missing_env="EARTHDATA_PROBE_URL",
            )
        import httpx

        started = time.monotonic()
        self._throttle()
        try:
            with httpx.Client(timeout=self._settings.http_timeout_seconds, follow_redirects=True) as client:
                response = client.get(probe, headers={"Authorization": f"Bearer {get_settings().earthdata_token}"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(self.key, f"Protected probe unreachable: {exc}") from exc
        elapsed = (time.monotonic() - started) * 1000.0
        if response.status_code in {401, 403}:
            raise ProviderAuthError(self.key, f"Token rejected by the protected resource (HTTP {response.status_code}).")
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                self.key, f"Protected probe returned HTTP {response.status_code}.", status_code=response.status_code
            )
        return ProviderResult(
            provider=self.key,
            endpoint=probe,
            fetched_at=datetime.now(timezone.utc),
            payload=None,
            http_status=response.status_code,
            elapsed_ms=elapsed,
            license=self.license,
            attribution=self.attribution,
        )

    def verify_token(self) -> ProviderStatus:
        if not self.token_present():
            return ProviderStatus(
                self.key,
                "unconfigured",
                "EARTHDATA_TOKEN is not set. Public CMR metadata still works; protected data downloads stay unavailable.",
                True,
                CMR_SEARCH,
            )
        probe = (get_settings().earthdata_probe_url or "").strip()
        if not probe:
            return ProviderStatus(
                self.key,
                "unverified",
                "A token is configured but no protected resource was named, so its validity is "
                "unverified. Public metadata succeeding proves nothing about authentication. "
                "Set EARTHDATA_PROBE_URL to confirm.",
                True,
                CMR_SEARCH,
            )
        try:
            result = self.protected_probe()
            return ProviderStatus(
                self.key,
                "healthy",
                f"Protected resource returned HTTP {result.http_status} with the configured token.",
                True,
                result.endpoint,
                verified_at=result.fetched_at.isoformat(),
                latency_ms=result.elapsed_ms,
                http_status=result.http_status,
            )
        except ProviderAuthError as exc:
            return ProviderStatus(self.key, "unverified", exc.message, True, probe)
        except ProviderUnavailableError as exc:
            return ProviderStatus(self.key, "degraded", f"Verification inconclusive: {exc.message}", True, probe)
        except Exception as exc:
            return ProviderStatus(self.key, "degraded", f"Verification inconclusive: {type(exc).__name__}: {exc}", True, probe)

    def healthcheck(self) -> ProviderStatus:
        try:
            result = self.search_collections(keyword="IMERG", page_size=1)
            hits = result.payload.get("hits", 0) if isinstance(result.payload, dict) else 0
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"CMR metadata search failed: {exc}", True, CMR_SEARCH)
        token_state = self.verify_token()
        state = token_state.state if token_state.state in {"healthy", "unconfigured", "unverified"} else "degraded"
        return ProviderStatus(
            self.key,
            state,
            f"CMR metadata OK ({hits} hits, public); protected access: {token_state.detail}",
            True,
            CMR_SEARCH,
            verified_at=token_state.verified_at,
            latency_ms=result.elapsed_ms,
            http_status=result.http_status,
        )


def _validate_cmr(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("CMR returned a non-object body")
    if payload.get("errors") or payload.get("error"):
        raise ValueError(f"CMR error: {payload.get('errors') or payload.get('error')}")
    if "hits" not in payload:
        raise ValueError("CMR response has no 'hits' field")


__all__ = ["EarthdataProvider", "CMR_SEARCH"]
