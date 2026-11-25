"""Cache-first access to full symbol histories.

PSX always returns a symbol's whole history, so one entry per symbol serves every date range.
Concurrent misses for one symbol share a single PSX fetch via a fixed pool of striped locks.
When PSX refuses (429) or is unavailable, the last cached copy is served as STALE. After a 429,
PSX is not called again for ``cooldown_seconds``. With a Redis client the cooldown is shared by
every worker; if Redis fails, each process falls back to its own timer.
"""
from __future__ import annotations

import logging
import math
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import redis
from opentelemetry.trace import Span
from psxdata.exceptions import PSXRateLimitError, PSXUnavailableError

from api.cache.codec import CacheEntry, decode, encode
from api.cache.freshness import DEFAULT_MARKET_TTL, fresh_until
from api.cache.store import TieredStore
from api.telemetry import get_tracer

logger = logging.getLogger(__name__)

LOCK_STRIPES = 256
PSX_COOLDOWN_SECONDS = 60
COOLDOWN_KEY = "psxdata:psx_cooldown"
COOLDOWN_MESSAGE = "PSX is rate-limiting requests; retry later"

Row = dict[str, Any]
Fetcher = Callable[[str], list[Row]]


class CacheStatus(str, Enum):
    HIT = "HIT"
    MISS = "MISS"
    STALE = "STALE"


@dataclass(frozen=True)
class CacheResult:
    rows: list[Row]
    status: CacheStatus
    fetched_at: datetime


def cache_key(symbol: str) -> str:
    return f"psxdata-api:v1:historical:{symbol.upper()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _traced(span: Span, result: CacheResult, tier: str) -> CacheResult:
    span.set_attribute("cache.status", result.status.value)
    span.set_attribute("cache.tier", tier)
    return result


class HistoricalService:
    def __init__(
        self,
        store: TieredStore,
        *,
        market_ttl: timedelta = DEFAULT_MARKET_TTL,
        cooldown_seconds: float = PSX_COOLDOWN_SECONDS,
        cooldown_redis: redis.Redis | None = None,
        now: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._market_ttl = market_ttl
        self._cooldown = cooldown_seconds
        self._cooldown_redis = cooldown_redis
        self._now = now
        self._monotonic = monotonic
        self._blocked_until = 0.0
        self._state_lock = threading.Lock()
        self._locks = [threading.Lock() for _ in range(LOCK_STRIPES)]

    def get(self, symbol: str, fetch: Fetcher) -> CacheResult:
        symbol = symbol.upper()
        key = cache_key(symbol)
        with get_tracer().start_as_current_span(
            "cache.historical", attributes={"psxdata.symbol": symbol}
        ) as span:
            entry, tier = self._load(key)
            if entry is not None and self._is_fresh(entry):
                hit = CacheResult(entry.rows, CacheStatus.HIT, entry.fetched_at)
                return _traced(span, hit, tier)

            with self._lock_for(key):
                entry, tier = self._load(key)
                if entry is not None and self._is_fresh(entry):
                    hit = CacheResult(entry.rows, CacheStatus.HIT, entry.fetched_at)
                    return _traced(span, hit, tier)
                if self._in_cooldown():
                    span.set_attribute("psxdata.cooldown", True)
                    stale = self._fallback(entry, PSXRateLimitError(COOLDOWN_MESSAGE))
                    return _traced(span, stale, tier)
                try:
                    rows = fetch(symbol)
                except PSXRateLimitError as exc:
                    self._start_cooldown()
                    span.set_attribute("psxdata.cooldown", True)
                    return _traced(span, self._fallback(entry, exc), tier)
                except PSXUnavailableError as exc:
                    return _traced(span, self._fallback(entry, exc), tier)

                rows = sorted(rows, key=lambda row: row.get("date") or "", reverse=True)
                fetched_at = self._now()
                fresh = CacheEntry(
                    symbol, fetched_at, fresh_until(fetched_at, self._market_ttl), rows
                )
                self.store.put(key, encode(fresh))
                return _traced(span, CacheResult(rows, CacheStatus.MISS, fetched_at), "none")

    def close(self) -> None:
        self.store.close()

    def _load(self, key: str) -> tuple[CacheEntry | None, str]:
        blob, tier = self.store.get_with_tier(key)
        entry = decode(blob) if blob is not None else None
        return entry, tier if entry is not None else "none"

    def _is_fresh(self, entry: CacheEntry) -> bool:
        return self._now() < entry.fresh_until

    def _lock_for(self, key: str) -> threading.Lock:
        return self._locks[zlib.crc32(key.encode()) % LOCK_STRIPES]

    def _in_cooldown(self) -> bool:
        with self._state_lock:
            if self._monotonic() < self._blocked_until:
                return True
        if self._cooldown_redis is None:
            return False
        try:
            return bool(self._cooldown_redis.exists(COOLDOWN_KEY))
        except (redis.RedisError, OSError) as exc:
            logger.warning(
                "Shared PSX cooldown unreadable (%s); using local timer", type(exc).__name__
            )
            return False

    def _start_cooldown(self) -> None:
        with self._state_lock:
            self._blocked_until = self._monotonic() + self._cooldown
        if self._cooldown_redis is None:
            return
        try:
            self._cooldown_redis.set(COOLDOWN_KEY, "1", ex=max(1, math.ceil(self._cooldown)))
        except (redis.RedisError, OSError) as exc:
            logger.warning(
                "Shared PSX cooldown not saved (%s); using local timer", type(exc).__name__
            )

    @staticmethod
    def _fallback(entry: CacheEntry | None, exc: Exception) -> CacheResult:
        if entry is None:
            raise exc
        return CacheResult(entry.rows, CacheStatus.STALE, entry.fetched_at)
