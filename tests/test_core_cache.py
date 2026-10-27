from __future__ import annotations

"""The in-process cache: bounds, expiry, stats and the disabled path.

No network and no spatial store; these tests own the object directly so the behaviour is
checked without the rest of the application's fixtures.
"""

import pytest

from app.core.cache import MISS, TTLCache, cached


def test_a_stored_value_comes_back_and_a_missing_key_is_a_miss():
    cache = TTLCache(name="unit", max_entries=4, ttl_seconds=60)
    assert cache.get("absent") is MISS

    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries"] == 1


def test_entries_expire_at_their_ttl_and_count_as_expirations():
    cache = TTLCache(name="unit", max_entries=4, ttl_seconds=10)
    cache.set("k", "value")
    assert cache.get("k", now=1_000.0) == "value"
    # set() stored at time.time(); this is far past the ttl either way.
    assert cache.get("k", now=10_000_000_000.0) is MISS
    assert cache.stats()["expirations"] == 1
    assert cache.stats()["entries"] == 0


def test_the_least_recently_used_entry_is_evicted_at_capacity():
    cache = TTLCache(name="unit", max_entries=2, ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # "a" is now the most recently used
    cache.set("c", 3)

    assert cache.get("a") == 1
    assert cache.get("b") is MISS
    assert cache.get("c") == 3
    assert cache.stats()["evictions"] == 1


def test_a_non_positive_ttl_stores_nothing():
    cache = TTLCache(name="unit", max_entries=4, ttl_seconds=0)
    cache.set("k", "value")
    assert cache.get("k") is MISS
    assert cache.stats()["entries"] == 0


@pytest.mark.parametrize("enabled", [True, False])
def test_cached_calls_the_producer_once_when_enabled(enabled):
    cache = TTLCache(name="unit", max_entries=4, ttl_seconds=60)
    calls: list[int] = []

    def producer() -> str:
        calls.append(1)
        return "made"

    first = cached(cache, "k", producer, enabled=enabled)
    second = cached(cache, "k", producer, enabled=enabled)

    assert first == second == "made"
    assert len(calls) == (1 if enabled else 2)


def test_stats_are_per_cache_name_and_clear_drops_entries():
    from app.core.cache import cache_for, cache_stats, clear_caches

    one = cache_for("unit-one", max_entries=4, ttl_seconds=60)
    two = cache_for("unit-two", max_entries=4, ttl_seconds=60)
    one.set("k", 1)
    two.set("k", 2)

    names = cache_stats()
    assert names["unit-one"]["entries"] == 1
    assert names["unit-two"]["entries"] == 1

    clear_caches()
    assert one.get("k") is MISS
    assert two.get("k") is MISS
