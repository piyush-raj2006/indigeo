from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..core.config import get_settings
from ..core.errors import ProviderAuthError, ProviderRateLimitError, ProviderUnavailableError
from ..core.logging import get_logger

log = get_logger("integrations.base")


@dataclass
class ProviderResult:
    """Every successful fetch carries its provenance with it."""

    provider: str
    endpoint: str
    fetched_at: datetime
    payload: Any
    http_status: int
    elapsed_ms: float
    from_cache: bool = False
    license: str | None = None
    attribution: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "fetched_at": self.fetched_at.isoformat(),
            "http_status": self.http_status,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "from_cache": self.from_cache,
            "license": self.license,
            "attribution": self.attribution,
            **({"notes": self.notes} if self.notes else {}),
        }


@dataclass
class ProviderStatus:
    provider: str
    state: str  # healthy|degraded|unavailable|unconfigured|unverified
    detail: str
    requires_auth: bool = False
    endpoint: str | None = None
    verified_at: str | None = None
    latency_ms: float | None = None
    http_status: int | None = None


class Provider:
    """HTTP adapter with timeout, retry, rate limiting, caching, validation and health state."""

    key: str = "provider"
    base_url: str = ""
    license: str | None = None
    attribution: str | None = None
    requires_auth: bool = False
    min_interval_seconds: float = 0.0
    cacheable: bool = True

    def __init__(self) -> None:
        self._last_call = 0.0
        self._settings = get_settings()
        self._cache_dir = Path(self._settings.provider_cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # Kept because providers are shared between request threads and, for Overpass,
        # between concurrent layer workers.
        self._throttle_lock = threading.Lock()
        self.last_status: ProviderStatus = ProviderStatus(self.key, "unverified", "Not called yet.")

    # -- auth -------------------------------------------------------------
    def credentials(self) -> dict[str, str]:
        return {}

    def auth_problem(self) -> str | None:
        return None

    def _check_auth(self) -> None:
        problem = self.auth_problem()
        if problem:
            raise ProviderAuthError(self.key, problem, missing_env=self.credential_env())

    def credential_env(self) -> str | None:
        return None

    # -- http -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._settings.nominatim_user_agent, "Accept": "application/json, */*"}
        headers.update(self.credentials())
        return headers

    def _cache_path(self, url: str, params: dict[str, Any] | None, suffix: str = ".json") -> Path:
        digest = hashlib.sha256(f"{url}|{json.dumps(params or {}, sort_keys=True, default=str)}".encode()).hexdigest()
        return self._cache_dir / f"{self.key}_{digest[:32]}{suffix}"

    def _throttle(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        with self._throttle_lock:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(min(wait, 15.0))
            # Reserve the slot before releasing, so parallel callers queue behind each other.
            self._last_call = time.monotonic()

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        validator: Callable[[Any], None] | None = None,
        cache_ttl: int | None = None,
        method: str = "GET",
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> ProviderResult:
        """Fetch JSON with retry/throttle/caching.

        ``timeout`` and ``attempts`` let a caller that is working against a wall-clock
        deadline cap a single request instead of inheriting the global retry budget.
        """
        self._check_auth()
        ttl = self._settings.provider_cache_ttl_seconds if cache_ttl is None else cache_ttl
        request_timeout = timeout if timeout is not None else self._settings.http_timeout_seconds
        max_attempts = max(1, attempts if attempts is not None else self._settings.http_max_retries)
        cache_file = self._cache_path(url, params)
        if self.cacheable and ttl > 0 and cache_file.exists() and content is None:
            age = time.time() - cache_file.stat().st_mtime
            if age < ttl:
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    return ProviderResult(
                        provider=self.key,
                        endpoint=url,
                        fetched_at=datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc),
                        payload=cached,
                        http_status=200,
                        elapsed_ms=0.0,
                        from_cache=True,
                        license=self.license,
                        attribution=self.attribution,
                        notes={"cache_age_seconds": round(age)},
                    )
                except (json.JSONDecodeError, OSError):
                    cache_file.unlink(missing_ok=True)

        request_headers = headers or self._headers()

        @retry(
            reraise=True,
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, _TransientStatus)),
        )
        def _attempt() -> tuple[Any, int, float]:
            self._throttle()
            started = time.monotonic()
            try:
                with httpx.Client(timeout=request_timeout, follow_redirects=True) as client:
                    response = client.request(
                        method, url, params=params, content=content, headers=request_headers
                    )
            except httpx.HTTPError as exc:
                self.last_status = ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", self.requires_auth, url)
                raise
            elapsed = (time.monotonic() - started) * 1000.0
            self._last_call = time.monotonic()
            if response.status_code == 429:
                raise _TransientStatus("rate limited")
            if response.status_code in {401, 403}:
                raise ProviderAuthError(
                    self.key,
                    f"{self.key} rejected the credentials (HTTP {response.status_code}). "
                    f"{response.text[:180]}".strip(),
                    missing_env=self.credential_env(),
                )
            if response.status_code >= 500:
                raise _TransientStatus(f"upstream HTTP {response.status_code}")
            if response.status_code >= 400:
                raise ProviderUnavailableError(
                    self.key,
                    f"{self.key} returned HTTP {response.status_code} for {url}.",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise ProviderUnavailableError(
                    self.key, f"{self.key} returned a non-JSON body ({len(response.content)} bytes)."
                ) from exc
            if validator:
                try:
                    validator(payload)
                except Exception as exc:
                    raise ProviderUnavailableError(self.key, f"Response from {self.key} failed validation: {exc}") from exc
            if self.cacheable and ttl > 0 and content is None and response.status_code == 200:
                try:
                    cache_file.write_text(json.dumps(payload, default=str), encoding="utf-8")
                except OSError:
                    pass
            self.last_status = ProviderStatus(
                self.key, "healthy", f"HTTP {response.status_code}", self.requires_auth, url,
                datetime.now(timezone.utc).isoformat(), elapsed, response.status_code,
            )
            return payload, response.status_code, elapsed

        try:
            payload, status_code, elapsed = _attempt()
        except _TransientStatus as inner:
            # reraise=True hands back the last transient failure directly.
            if "rate limited" in str(inner):
                raise ProviderRateLimitError(self.key, f"{self.key} rate-limited this client; retry later.") from inner
            raise ProviderUnavailableError(self.key, f"{self.key} kept failing: {inner}") from inner
        except RetryError as exc:
            inner = exc.last_attempt.exception()
            if isinstance(inner, _TransientStatus):
                if "rate limited" in str(inner):
                    raise ProviderRateLimitError(self.key, f"{self.key} rate-limited this client; retry later.") from exc
                raise ProviderUnavailableError(self.key, f"{self.key} kept failing: {inner}") from exc
            raise
        except ProviderAuthError:
            self.last_status = ProviderStatus(self.key, "unavailable", "Authentication rejected.", True, url)
            raise
        except ProviderUnavailableError as exc:
            self.last_status = ProviderStatus(self.key, "unavailable", exc.message, self.requires_auth, url)
            raise

        return ProviderResult(
            provider=self.key,
            endpoint=url,
            fetched_at=datetime.now(timezone.utc),
            payload=payload,
            http_status=status_code,
            elapsed_ms=elapsed,
            license=self.license,
            attribution=self.attribution,
        )

    def post_form(self, url: str, data: dict[str, str], *, cache_ttl: int = 0) -> ProviderResult:
        content = "&".join(f"{k}={v}" for k, v in data.items()).encode("utf-8")
        return self.get_json(
            url,
            content=content,
            method="POST",
            cache_ttl=cache_ttl,
            headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"},
        )

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cache_ttl: int | None = None,
        timeout: float | None = None,
    ) -> tuple[str, ProviderResult]:
        """Non-JSON fetch (RSS/CAP/CSV feeds) with the same timeout/retry/health treatment.

        ``timeout`` lets a feed connector keep its own wall-clock ceiling instead of
        inheriting the global provider timeout.
        """
        self._check_auth()
        ttl = self._settings.provider_cache_ttl_seconds if cache_ttl is None else cache_ttl
        cache_file = self._cache_path(url, params, suffix=".txt")
        if self.cacheable and ttl > 0 and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < ttl:
                try:
                    text = cache_file.read_text(encoding="utf-8")
                    return text, ProviderResult(
                        provider=self.key,
                        endpoint=url,
                        fetched_at=datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc),
                        payload=None,
                        http_status=200,
                        elapsed_ms=0.0,
                        from_cache=True,
                        license=self.license,
                        attribution=self.attribution,
                        notes={"cache_age_seconds": round(age)},
                    )
                except OSError:
                    cache_file.unlink(missing_ok=True)

        started = time.monotonic()
        self._throttle()
        try:
            with httpx.Client(
                timeout=timeout if timeout is not None else self._settings.http_timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            self.last_status = ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", self.requires_auth, url)
            raise ProviderUnavailableError(self.key, f"{self.key} unreachable: {exc}") from exc
        self._last_call = time.monotonic()
        elapsed = (time.monotonic() - started) * 1000.0
        if response.status_code == 401 or response.status_code == 403:
            self.last_status = ProviderStatus(self.key, "unavailable", "Authentication required.", True, url)
            raise ProviderAuthError(self.key, f"{self.key} requires an API key.", missing_env=self.credential_env())
        if response.status_code == 429:
            self.last_status = ProviderStatus(self.key, "degraded", "Rate limited.", self.requires_auth, url)
            raise ProviderRateLimitError(self.key, f"{self.key} rate limit reached.")
        if response.status_code >= 400:
            self.last_status = ProviderStatus(self.key, "unavailable", f"HTTP {response.status_code}", self.requires_auth, url, None, elapsed, response.status_code)
            raise ProviderUnavailableError(self.key, f"{self.key} returned HTTP {response.status_code}.", status_code=response.status_code)
        self.last_status = ProviderStatus(
            self.key, "healthy", f"HTTP {response.status_code}", self.requires_auth, url,
            datetime.now(timezone.utc).isoformat(), elapsed, response.status_code,
        )
        if self.cacheable and ttl > 0:
            try:
                cache_file.write_text(response.text, encoding="utf-8")
            except OSError:
                pass
        result = ProviderResult(
            provider=self.key,
            endpoint=url,
            fetched_at=datetime.now(timezone.utc),
            payload=None,
            http_status=response.status_code,
            elapsed_ms=elapsed,
            license=self.license,
            attribution=self.attribution,
        )
        return response.text, result

    # -- health -----------------------------------------------------------
    def healthcheck(self) -> ProviderStatus:
        """Default health probe: a tiny documented request. Adapters override when cheaper options exist."""
        if not self.base_url:
            return ProviderStatus(self.key, "unverified", "No probe configured.", self.requires_auth)
        try:
            self.get_json(self.base_url, cache_ttl=0)
            return self.last_status
        except ProviderAuthError as exc:
            return ProviderStatus(self.key, "unconfigured", exc.message, True, self.base_url)
        except Exception as exc:
            return ProviderStatus(self.key, "unavailable", f"{type(exc).__name__}: {exc}", self.requires_auth, self.base_url)


class _TransientStatus(Exception):
    pass


_registry: dict[str, Provider] = {}


def register_provider(provider: Provider) -> Provider:
    _registry[provider.key] = provider
    return provider


def get_provider(key: str) -> Provider:
    if key not in _registry:
        initialise_providers()
    return _registry[key]


def all_providers() -> list[Provider]:
    initialise_providers()
    return list(_registry.values())


def initialise_providers() -> None:
    if _registry:
        return
    from .earthdata import EarthdataProvider
    from .firms import FirmsProvider
    from .india_alerts import BhuvanHazardsProvider, CwcGaugesProvider, ImdAlertsProvider
    from .local_osm import LocalOSMProvider
    from .nominatim import NominatimProvider
    from .open_meteo import OpenMeteoProvider
    from .osm_infra import OverpassProvider
    from .usgs import UsgsEarthquakeProvider

    for cls in (
        NominatimProvider,
        UsgsEarthquakeProvider,
        FirmsProvider,
        OpenMeteoProvider,
        EarthdataProvider,
        OverpassProvider,
        LocalOSMProvider,
        ImdAlertsProvider,
        CwcGaugesProvider,
        BhuvanHazardsProvider,
    ):
        register_provider(cls())


__all__ = ["Provider", "ProviderResult", "ProviderStatus", "get_provider", "all_providers", "initialise_providers"]
