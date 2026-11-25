"""Build the /historical cache from environment configuration.

``REDIS_URL`` (optional) points at any Redis-compatible server, e.g. Aiven for Valkey. It is
never logged, only its hostname.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from urllib.parse import urlparse

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from api.cache.freshness import DEFAULT_MARKET_TTL
from api.cache.historical import HistoricalService
from api.cache.store import MemoryLRU, RedisStore, TieredStore

logger = logging.getLogger(__name__)

REDIS_TIMEOUT_SECONDS = 0.5


def market_ttl_from_env(env: Mapping[str, str]) -> timedelta:
    raw = env.get("HISTORICAL_CACHE_MARKET_TTL")
    if raw is None:
        return DEFAULT_MARKET_TTL
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 0
    if seconds <= 0:
        logger.warning(
            "Ignoring invalid HISTORICAL_CACHE_MARKET_TTL=%r; using %ds",
            raw, int(DEFAULT_MARKET_TTL.total_seconds()),
        )
        return DEFAULT_MARKET_TTL
    return timedelta(seconds=seconds)


def build_redis_client(url: str | None) -> redis.Redis | None:
    if not url:
        return None
    try:
        return redis.Redis.from_url(
            url,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            health_check_interval=30,
            retry=Retry(NoBackoff(), 0),
        )
    except ValueError as exc:
        logger.warning(
            "REDIS_URL could not be parsed (%s); using in-memory cache only", type(exc).__name__
        )
        return None


def build_historical_service(env: Mapping[str, str]) -> HistoricalService:
    url = env.get("REDIS_URL")
    client = build_redis_client(url)
    if client is None:
        logger.info("No cache backend configured; /historical uses in-memory cache only")
    else:
        host = urlparse(url or "").hostname or "unknown-host"
        try:
            client.ping()
            logger.info("Cache backend connected: %s", host)
        except (redis.RedisError, OSError) as exc:
            logger.warning(
                "Cache backend unreachable at startup: %s (%s)", host, type(exc).__name__
            )
    store = TieredStore(MemoryLRU(), RedisStore(client))
    return HistoricalService(store, market_ttl=market_ttl_from_env(env), cooldown_redis=client)
