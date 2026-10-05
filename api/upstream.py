"""Budgets for the requests this server makes to PSX.

The route limits (60 requests per minute per IP) count every request, including cache hits that
cost PSX nothing. These budgets count only fetches that actually go to PSX, which is what PSX
rate-limits, and it rate-limits this server's IP as a whole: on 2026-09-29 one client sweeping
about 120 uncached symbols a minute made PSX refuse every client, while 60 to 86 a minute went
through.

- Per client IP: one client cannot spend the server's PSX allowance (``429 rate_limited``).
- Global: all clients together stay under PSX's limit (``503 psx_unavailable``).

Either way ``Retry-After`` says when a fetch would be allowed, and /historical serves its stale
copy instead when it has one. Proxied fetches don't count: they leave through the caller's
proxy and have their own limit. Counters are in memory, so each replica has its own budget.
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Mapping

from fastapi import Request
from limits import RateLimitItem, RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from psxdata.exceptions import PSXUnavailableError

logger = logging.getLogger(__name__)

PER_IP_ENV = "PSX_FETCH_LIMIT_PER_IP"
GLOBAL_ENV = "PSX_FETCH_LIMIT_GLOBAL"
DEFAULT_PER_IP_PER_MINUTE = 20
DEFAULT_GLOBAL_PER_MINUTE = 60

CLIENT_MESSAGE = "Too many requests that need fresh data from PSX; retry later"
GLOBAL_MESSAGE = "The server's PSX request budget is used up; retry later"


class UpstreamBudgetExceeded(PSXUnavailableError):
    """A PSX fetch refused by this server's own budget, before PSX was contacted.

    A PSXUnavailableError, so the /historical cache answers it with its stale copy when it has
    one, and without starting the PSX cooldown that a real 429 from PSX starts.
    """

    def __init__(self, status_code: int, retry_after: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _per_minute(limit: int) -> RateLimitItem | None:
    return RateLimitItemPerMinute(limit) if limit > 0 else None


def _limit_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    return value


class UpstreamBudget:
    """Per-IP and global per-minute limits on PSX fetches. A limit of 0 turns that one off."""

    def __init__(
        self,
        per_ip_per_minute: int = DEFAULT_PER_IP_PER_MINUTE,
        global_per_minute: int = DEFAULT_GLOBAL_PER_MINUTE,
    ) -> None:
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        self._per_ip = _per_minute(per_ip_per_minute)
        self._global = _per_minute(global_per_minute)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> UpstreamBudget:
        return cls(
            _limit_from_env(env, PER_IP_ENV, DEFAULT_PER_IP_PER_MINUTE),
            _limit_from_env(env, GLOBAL_ENV, DEFAULT_GLOBAL_PER_MINUTE),
        )

    def charge(self, client: str) -> None:
        """Count one PSX fetch for *client*, or raise UpstreamBudgetExceeded without counting."""
        # Check the global budget before spending the client's, so a full server doesn't use it up
        if self._global is not None and not self._limiter.test(self._global, "psx-fetch"):
            raise UpstreamBudgetExceeded(503, self._retry_after(self._global), GLOBAL_MESSAGE)
        if self._per_ip is not None and not self._limiter.hit(self._per_ip, "psx-fetch", client):
            raise UpstreamBudgetExceeded(
                429, self._retry_after(self._per_ip, client), CLIENT_MESSAGE
            )
        if self._global is not None and not self._limiter.hit(self._global, "psx-fetch"):
            raise UpstreamBudgetExceeded(503, self._retry_after(self._global), GLOBAL_MESSAGE)

    def _retry_after(self, item: RateLimitItem, *identifiers: str) -> int:
        reset_at = self._limiter.get_window_stats(item, "psx-fetch", *identifiers).reset_time
        return max(1, math.ceil(reset_at - time.time()))


def get_upstream_budget(request: Request) -> UpstreamBudget:
    """Return the app's UpstreamBudget; build one from the environment if lifespan did not run."""
    budget: UpstreamBudget | None = getattr(request.app.state, "upstream_budget", None)
    if budget is None:
        budget = UpstreamBudget.from_env(os.environ)
        request.app.state.upstream_budget = budget
    return budget
