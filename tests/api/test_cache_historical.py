"""Tests for HistoricalService: hit/miss, expiry, stale fallback, cooldown, single-flight."""
import gzip
import json
import redis
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from psxdata.exceptions import (
    InvalidSymbolError,
    PSXRateLimitError,
    PSXServerError,
    PSXUnavailableError,
)

from api.cache.historical import LOCK_STRIPES, CacheStatus, HistoricalService, cache_key
from api.cache.store import MemoryLRU, RedisStore, TieredStore

ASCENDING_ROWS = [
    {"date": "2026-09-28", "open": 9.0, "high": 10.0, "low": 8.5, "close": 9.5,
     "volume": 900, "is_anomaly": False},
    {"date": "2026-09-29", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
     "volume": 1000, "is_anomaly": False},
]


class Clock:
    """Tuesday 2026-09-29 10:00 PKT (inside the trading window, 30 min TTL)."""

    def __init__(self) -> None:
        self.t = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


class FakeFetch:
    def __init__(self, rows=None) -> None:
        self.rows = ASCENDING_ROWS if rows is None else rows
        self.error: Exception | None = None
        self.calls: list[str] = []

    def __call__(self, symbol: str):
        self.calls.append(symbol)
        if self.error is not None:
            raise self.error
        return [dict(r) for r in self.rows]


def make_service(clock: Clock, l2_client=None) -> HistoricalService:
    store = TieredStore(MemoryLRU(), RedisStore(l2_client, monotonic=clock.monotonic))
    return HistoricalService(store, now=clock.now, monotonic=clock.monotonic)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_miss_then_hit(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    first = service.get("SYS", fetch)
    second = service.get("SYS", fetch)
    assert first.status is CacheStatus.MISS
    assert second.status is CacheStatus.HIT
    assert fetch.calls == ["SYS"]
    assert [r["date"] for r in second.rows] == [r["date"] for r in first.rows]
    assert second.fetched_at == clock.t


def test_rows_are_newest_first(clock: Clock) -> None:
    service = make_service(clock)
    miss = service.get("SYS", FakeFetch())
    hit = service.get("SYS", FakeFetch())
    assert [r["date"] for r in miss.rows] == ["2026-09-29", "2026-09-28"]
    assert [r["date"] for r in hit.rows] == ["2026-09-29", "2026-09-28"]


def test_symbol_is_uppercased(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    service.get("sys", fetch)
    assert service.get("SYS", fetch).status is CacheStatus.HIT
    assert fetch.calls == ["SYS"]
    assert cache_key("sys") == "psxdata-api:v1:historical:SYS"


def test_expired_entry_is_refetched(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    service.get("SYS", fetch)
    clock.advance(31 * 60)
    assert service.get("SYS", fetch).status is CacheStatus.MISS
    assert len(fetch.calls) == 2


def test_rate_limit_serves_stale_and_starts_cooldown(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    primed = service.get("SYS", fetch)
    clock.advance(31 * 60)

    fetch.error = PSXRateLimitError("PSX rate limit exceeded (429)")
    stale = service.get("SYS", fetch)
    assert stale.status is CacheStatus.STALE
    assert stale.fetched_at == primed.fetched_at
    assert len(fetch.calls) == 2

    fetch.error = None                       # PSX would answer now, but we are cooling down
    clock.advance(30)
    assert service.get("SYS", fetch).status is CacheStatus.STALE
    assert len(fetch.calls) == 2

    clock.advance(31)                        # cooldown (60 s) over
    assert service.get("SYS", fetch).status is CacheStatus.MISS
    assert len(fetch.calls) == 3


def test_rate_limit_without_copy_raises_and_cooldown_blocks_other_symbols(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    fetch.error = PSXRateLimitError("PSX rate limit exceeded (429)")
    with pytest.raises(PSXRateLimitError):
        service.get("SYS", fetch)
    fetch.error = None
    with pytest.raises(PSXRateLimitError, match="rate-limiting"):
        service.get("HUBC", fetch)           # blocked by cooldown, PSX not called
    assert fetch.calls == ["SYS"]


def test_unavailable_serves_stale_without_cooldown(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    service.get("SYS", fetch)
    clock.advance(31 * 60)
    fetch.error = PSXServerError("PSX 502")
    assert service.get("SYS", fetch).status is CacheStatus.STALE
    fetch.error = None
    assert service.get("SYS", fetch).status is CacheStatus.MISS
    assert len(fetch.calls) == 3


def test_unavailable_without_copy_raises(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    fetch.error = PSXUnavailableError("PSX down")
    with pytest.raises(PSXUnavailableError):
        service.get("SYS", fetch)


def test_other_errors_propagate_and_are_not_cached(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch()
    fetch.error = InvalidSymbolError("nope")
    with pytest.raises(InvalidSymbolError):
        service.get("NOPE", fetch)
    assert service.store.get(cache_key("NOPE")) is None


def test_empty_history_is_cached(clock: Clock) -> None:
    service, fetch = make_service(clock), FakeFetch(rows=[])
    assert service.get("NOPE", fetch).rows == []
    hit = service.get("NOPE", fetch)
    assert hit.status is CacheStatus.HIT
    assert hit.rows == []
    assert len(fetch.calls) == 1


def test_corrupt_cached_bytes_are_a_miss_and_get_overwritten(clock: Clock) -> None:
    redis_client = fakeredis.FakeRedis()
    redis_client.set(cache_key("SYS"), b"written by something else")
    service, fetch = make_service(clock, redis_client), FakeFetch()
    assert service.get("SYS", fetch).status is CacheStatus.MISS
    restarted = make_service(clock, redis_client)            # fresh L1, same L2
    assert restarted.get("SYS", fetch).status is CacheStatus.HIT
    assert len(fetch.calls) == 1


def test_wrong_schema_envelope_is_refetched(clock: Clock) -> None:
    far_future = (clock.t + timedelta(days=365)).isoformat()
    bogus = gzip.compress(json.dumps({
        "v": 1, "symbol": "SYS", "fetched_at": clock.t.isoformat(), "fresh_until": far_future,
        "columns": ["other"], "rows": [[1]],
    }).encode())
    redis_client = fakeredis.FakeRedis()
    redis_client.set(cache_key("SYS"), bogus)
    service, fetch = make_service(clock, redis_client), FakeFetch()
    result = service.get("SYS", fetch)
    assert result.status is CacheStatus.MISS
    assert result.rows[0]["date"] == "2026-09-29"
    assert len(fetch.calls) == 1


def test_entry_survives_restart_via_l2(clock: Clock) -> None:
    redis_client = fakeredis.FakeRedis()
    fetch = FakeFetch()
    make_service(clock, redis_client).get("SYS", fetch)
    assert make_service(clock, redis_client).get("SYS", fetch).status is CacheStatus.HIT
    assert len(fetch.calls) == 1


def test_concurrent_requests_for_one_symbol_fetch_once(clock: Clock) -> None:
    service = make_service(clock)
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def slow_fetch(symbol: str):
        calls.append(symbol)
        started.set()
        release.wait(5)
        return [dict(r) for r in ASCENDING_ROWS]

    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(service.get, "SYS", slow_fetch)
        assert started.wait(5)
        others = [pool.submit(service.get, "SYS", slow_fetch) for _ in range(7)]
        release.set()
        results = [first.result(5)] + [f.result(5) for f in others]

    assert calls == ["SYS"]
    assert [r.status for r in results].count(CacheStatus.MISS) == 1


def test_different_symbols_do_not_block_each_other(clock: Clock) -> None:
    stripe = lambda s: zlib.crc32(cache_key(s).encode()) % LOCK_STRIPES  # noqa: E731
    assert stripe("AAA") != stripe("BBB")
    service = make_service(clock)
    started, release = threading.Event(), threading.Event()

    def blocking_fetch(symbol: str):
        started.set()
        release.wait(5)
        return []

    with ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(service.get, "AAA", blocking_fetch)
        assert started.wait(5)
        assert service.get("BBB", FakeFetch()).status is CacheStatus.MISS
        assert not blocked.done()
        release.set()
        blocked.result(5)


def _attrs(otel, name="cache.historical"):
    return dict(otel.span(name).attributes)


def test_span_on_miss_then_hit(clock: Clock, otel) -> None:
    service = make_service(clock)
    service.get("hbl", FakeFetch())
    assert _attrs(otel) == {"psxdata.symbol": "HBL", "cache.status": "MISS", "cache.tier": "none"}
    otel.spans.clear()
    service.get("HBL", FakeFetch())
    assert _attrs(otel) == {"psxdata.symbol": "HBL", "cache.status": "HIT", "cache.tier": "memory"}


def test_span_on_stale_after_rate_limit(clock: Clock, otel) -> None:
    service = make_service(clock)
    service.get("HBL", FakeFetch())
    clock.advance(10 * 24 * 3600)  # well past any freshness window
    otel.spans.clear()
    limited = FakeFetch()
    limited.error = PSXRateLimitError("429")
    assert service.get("HBL", limited).status is CacheStatus.STALE
    assert _attrs(otel) == {
        "psxdata.symbol": "HBL", "cache.status": "STALE", "cache.tier": "memory",
        "psxdata.cooldown": True,
    }


def test_span_records_error_when_nothing_cached(clock: Clock, otel) -> None:
    limited = FakeFetch()
    limited.error = PSXRateLimitError("429")
    with pytest.raises(PSXRateLimitError):
        make_service(clock).get("HBL", limited)
    span = otel.span("cache.historical")
    assert span.status.status_code.name == "ERROR"
    assert "cache.status" not in span.attributes


class _DownRedis:
    def get(self, key):
        raise redis.ConnectionError("down")

    def set(self, key, value, ex=None):
        raise redis.ConnectionError("down")

    def close(self) -> None:
        pass


def test_cache_tier_when_redis_down(clock: Clock, otel) -> None:
    service = make_service(clock, l2_client=_DownRedis())
    assert service.get("HBL", FakeFetch()).status is CacheStatus.MISS
    assert _attrs(otel)["cache.tier"] == "none"
    otel.spans.clear()
    assert service.get("HBL", FakeFetch()).status is CacheStatus.HIT
    assert _attrs(otel)["cache.tier"] == "memory"


def test_miss_nests_fetch_span_under_cache_span(clock: Clock, otel) -> None:
    from api.telemetry import get_tracer

    def fetch(symbol: str):
        with get_tracer().start_as_current_span("psx.fetch"):
            return FakeFetch()(symbol)

    make_service(clock).get("HBL", fetch)
    assert otel.span("psx.fetch").parent.span_id == otel.span("cache.historical").context.span_id


def test_cooldown_is_shared_between_services_through_redis(clock: Clock) -> None:
    server = fakeredis.FakeServer()
    first = make_service(clock)
    second = make_service(clock)
    first._cooldown_redis = fakeredis.FakeRedis(server=server)
    second._cooldown_redis = fakeredis.FakeRedis(server=server)

    first_fetch = FakeFetch()
    first_fetch.error = PSXRateLimitError("PSX rate limit exceeded (429)")
    with pytest.raises(PSXRateLimitError):
        first.get("SYS", first_fetch)

    other_fetch = FakeFetch()
    with pytest.raises(PSXRateLimitError, match="rate-limiting"):
        second.get("HUBC", other_fetch)      # a different worker never calls PSX
    assert other_fetch.calls == []

    ttl = fakeredis.FakeRedis(server=server).ttl("psxdata:psx_cooldown")
    assert 0 < ttl <= 60


def test_cooldown_key_expires_for_other_services(clock: Clock) -> None:
    client = fakeredis.FakeRedis()
    first, second = make_service(clock), make_service(clock)
    first._cooldown_redis = second._cooldown_redis = client
    fetch = FakeFetch()
    fetch.error = PSXRateLimitError("429")
    with pytest.raises(PSXRateLimitError):
        first.get("SYS", fetch)
    client.delete("psxdata:psx_cooldown")    # what the TTL does after 60 s
    assert second.get("HUBC", FakeFetch()).status is CacheStatus.MISS


class BrokenRedis:
    def exists(self, *args, **kwargs):
        raise redis.ConnectionError("down")

    def set(self, *args, **kwargs):
        raise redis.ConnectionError("down")


def test_cooldown_falls_back_to_local_timer_when_redis_errors(clock: Clock) -> None:
    service = make_service(clock)
    service._cooldown_redis = BrokenRedis()
    fetch = FakeFetch()
    assert service.get("SYS", fetch).status is CacheStatus.MISS   # read error: not in cooldown

    clock.advance(31 * 60)
    fetch.error = PSXRateLimitError("429")
    assert service.get("SYS", fetch).status is CacheStatus.STALE  # write error: still cools down

    fetch.error = None
    clock.advance(30)
    assert service.get("SYS", fetch).status is CacheStatus.STALE  # local timer holds
    assert len(fetch.calls) == 2
    clock.advance(31)
    assert service.get("SYS", fetch).status is CacheStatus.MISS
