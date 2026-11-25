"""Shared FastAPI dependencies for the API layer."""
import os
from typing import Any

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.cache.factory import build_historical_service
from api.cache.historical import HistoricalService

RATE_LIMIT_REDIS_TIMEOUT_SECONDS = 0.5


def build_limiter(redis_url: str | None) -> Limiter:
    """Per-IP limiter whose counters live in Redis, so every worker shares one count.

    If Redis is unreachable, requests are still served and counted per process.
    """
    options: dict[str, Any] = {}
    if redis_url:
        options = {
            "socket_connect_timeout": RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
            "socket_timeout": RATE_LIMIT_REDIS_TIMEOUT_SECONDS,
        }
    return Limiter(
        key_func=get_remote_address,
        default_limits=["60/minute"],
        storage_uri=redis_url or "memory://",
        storage_options=options,
        strategy="moving-window",
        in_memory_fallback_enabled=True,
        swallow_errors=True,
    )


limiter = build_limiter(os.environ.get("REDIS_URL"))


def get_rate_limiter() -> Limiter:
    return limiter


def get_cache(request: Request) -> HistoricalService:
    """Return the app's /historical cache; build a memory-only one if lifespan did not run."""
    service: HistoricalService | None = getattr(request.app.state, "historical_service", None)
    if service is None:
        service = build_historical_service({})
        request.app.state.historical_service = service
    return service
