from __future__ import annotations

"""In-process TTL + LRU cache.

Scope is one process: two workers keep two caches, and the disk provider cache in
``integrations/base.py`` remains the layer that survives a restart and is shared between
processes. This exists for the reads that are cheap to recompute but expensive to repeat -
geocoding (Nominatim allows ~1 request/second) and point resolution (a DEM window sample).

A miss is always a miss: nothing is invented to fill a gap, and the stats are counted so an
operator can see whether the cache is earning its memory.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Hashable

_MISS = object()
# Public alias so callers can tell an expired/absent entry (``is MISS``) from a stored None.
MISS = _MISS


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class TTLCache:
    """Thread-safe bounded cache with a per-entry time to live."""

    def __init__(self, *, name: str, max_entries: int = 512, ttl_seconds: float = 300.0) -> None:
        self.name = name
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = float(ttl_seconds)
        self._entries: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._stats = CacheStats()

    def get(self, key: Hashable, *, now: float | None = None) -> Any:
        moment = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._stats.misses += 1
                return _MISS
            expires_at, value = entry
            if expires_at <= moment:
                del self._entries[key]
                self._stats.expirations += 1
                self._stats.misses += 1
                return _MISS
            self._entries.move_to_end(key)
            self._stats.hits += 1
            return value

    def set(self, key: Hashable, value: Any, *, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (time.time() + ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self._stats.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"name": self.name, "entries": len(self._entries), **self._stats.as_dict()}


_registry: dict[str, TTLCache] = {}
_registry_lock = threading.Lock()


def cache_for(name: str, *, max_entries: int = 512, ttl_seconds: float = 300.0) -> TTLCache:
    with _registry_lock:
        cache = _registry.get(name)
        if cache is None:
            cache = TTLCache(name=name, max_entries=max_entries, ttl_seconds=ttl_seconds)
            _registry[name] = cache
        return cache


def cache_stats() -> dict[str, dict[str, Any]]:
    with _registry_lock:
        caches = list(_registry.values())
    return {cache.name: cache.stats() for cache in caches}


def clear_caches() -> None:
    with _registry_lock:
        caches = list(_registry.values())
    for cache in caches:
        cache.clear()


def cached(
    cache: TTLCache,
    key: Hashable,
    producer: Callable[[], Any],
    *,
    enabled: bool = True,
    ttl_seconds: float | None = None,
) -> Any:
    """Return the cached value, else call ``producer`` once and store its result."""
    if not enabled:
        return producer()
    hit = cache.get(key)
    if hit is not _MISS:
        return hit
    value = producer()
    cache.set(key, value, ttl_seconds=ttl_seconds)
    return value
