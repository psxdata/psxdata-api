"""Per-IP rate limiting: 60 requests/minute, shared through Redis, in-process fallback."""
from unittest.mock import patch

import fakeredis
import pytest
from fastapi.testclient import TestClient
from limits import parse
from psxdata.exceptions import PSXUnavailableError

from api import dependencies
from api.dependencies import build_limiter
from api.main import app

LIMIT = 60

client = TestClient(app)


PSX_CALLS = (
    "tickers", "quote", "screener", "sectors", "symbols", "indices", "stocks",
    "fundamentals", "debt_market", "eligible_scrips",
)


@pytest.fixture(autouse=True)
def psx_unavailable():
    """Keep every request offline: PSX calls fail fast with a 503, which is not a 429."""
    patches = [
        patch(f"psxdata.{name}", side_effect=PSXUnavailableError("down")) for name in PSX_CALLS
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture(autouse=True)
def reset_limiter():
    dependencies.limiter.reset()
    yield
    dependencies.limiter.reset()


@pytest.mark.parametrize(
    "path",
    [
        "/stocks/OGDC/quote",
        "/stocks/OGDC/historical",
        "/stocks/OGDC/fundamentals",
        "/stocks",
        "/screener",
        "/sectors",
        "/sectors/BANKS/stocks",
        "/indices",
        "/indices/KSE100",
        "/debt-market",
        "/eligible-scrips",
    ],
)
def test_sixty_first_request_from_one_ip_is_429(path):
    codes = [client.get(path).status_code for _ in range(LIMIT + 1)]
    assert 429 not in codes[:LIMIT]
    assert codes[LIMIT] == 429


def test_health_is_not_rate_limited():
    codes = {client.get("/health").status_code for _ in range(LIMIT + 5)}
    assert codes == {200}


def test_limit_is_per_client_ip():
    for _ in range(LIMIT):
        client.get("/stocks")
    assert client.get("/stocks").status_code == 429
    other = TestClient(app, client=("203.0.113.7", 50000))
    assert other.get("/stocks").status_code != 429


def test_429_uses_error_envelope():
    for _ in range(LIMIT):
        client.get("/stocks")
    body = client.get("/stocks").json()
    assert body["error"]["code"] == "rate_limited"


def test_limiter_is_moving_window_with_fallback_and_swallow():
    lim = build_limiter(None)
    assert type(lim.limiter).__name__ == "MovingWindowRateLimiter"
    assert lim._in_memory_fallback_enabled is True
    assert lim._swallow_errors is True


def test_limiter_with_redis_url_uses_redis_storage():
    lim = build_limiter("redis://localhost:6379/0")
    assert type(lim._storage).__name__ == "RedisStorage"


def test_counters_are_shared_between_limiters_on_one_redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        "redis.Redis.from_url",
        lambda *a, **kw: fakeredis.FakeRedis(server=server),
    )
    first = build_limiter("redis://shared:6379/0")
    second = build_limiter("redis://shared:6379/0")
    first.reset()
    item = parse("60/minute")
    for _ in range(LIMIT):
        assert first._limiter.hit(item, "ip-1")
    assert not second._limiter.hit(item, "ip-1")
    first.reset()


def test_redis_down_falls_back_to_in_process_limit(monkeypatch):
    broken = build_limiter("redis://127.0.0.1:1/0")
    for attr in ("_storage", "_limiter", "_fallback_storage", "_fallback_limiter"):
        monkeypatch.setattr(dependencies.limiter, attr, getattr(broken, attr))
    for _ in range(LIMIT):
        assert client.get("/stocks").status_code != 429
    assert client.get("/stocks").status_code == 429
