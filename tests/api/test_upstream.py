"""Tests for the per-IP and global budgets on PSX fetches."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from psxdata import PSXClient

from api.cache.historical import HistoricalService
from api.cache.store import MemoryLRU, RedisStore, TieredStore
from api.main import app
from api.proxy import ProxyPassthrough
from api.upstream import (
    GLOBAL_ENV,
    PER_IP_ENV,
    UpstreamBudget,
    UpstreamBudgetExceeded,
)


def _history() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2024-01-02", "2024-01-01"]),
        "open": [2.0, 1.0], "high": [2.5, 1.5], "low": [1.5, 0.5],
        "close": [2.2, 1.2], "volume": [200, 100], "is_anomaly": [False, False],
    })


def _client(ip: str) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, client=(ip, 50000))


def _use(budget: UpstreamBudget) -> UpstreamBudget:
    app.state.upstream_budget = budget
    return budget


# ── UpstreamBudget ─────────────────────────────────────────────────


def test_per_ip_limit_is_per_client() -> None:
    budget = UpstreamBudget(per_ip_per_minute=2, global_per_minute=0)
    budget.charge("1.1.1.1")
    budget.charge("1.1.1.1")
    with pytest.raises(UpstreamBudgetExceeded) as exc:
        budget.charge("1.1.1.1")
    assert exc.value.status_code == 429
    assert 1 <= exc.value.retry_after <= 60
    budget.charge("2.2.2.2")  # another client still has its own budget


def test_global_limit_covers_all_clients() -> None:
    budget = UpstreamBudget(per_ip_per_minute=0, global_per_minute=2)
    budget.charge("1.1.1.1")
    budget.charge("2.2.2.2")
    with pytest.raises(UpstreamBudgetExceeded) as exc:
        budget.charge("3.3.3.3")
    assert exc.value.status_code == 503
    assert 1 <= exc.value.retry_after <= 60


def test_full_server_does_not_spend_client_budget() -> None:
    budget = UpstreamBudget(per_ip_per_minute=1, global_per_minute=1)
    budget.charge("1.1.1.1")
    with pytest.raises(UpstreamBudgetExceeded) as exc:
        budget.charge("2.2.2.2")
    assert exc.value.status_code == 503
    # 2.2.2.2 was refused by the global budget, so its own fetch is still unspent
    assert budget._limiter.test(budget._per_ip, "psx-fetch", "2.2.2.2")


def test_zero_disables_a_limit() -> None:
    budget = UpstreamBudget(per_ip_per_minute=0, global_per_minute=0)
    for _ in range(500):
        budget.charge("1.1.1.1")


@pytest.mark.parametrize(
    ("env", "per_ip", "global_"),
    [
        ({}, 20, 60),
        ({PER_IP_ENV: "5", GLOBAL_ENV: "40"}, 5, 40),
        ({PER_IP_ENV: "0"}, 0, 60),
        ({PER_IP_ENV: "lots", GLOBAL_ENV: "-1"}, 20, 60),
    ],
)
def test_from_env(env: dict[str, str], per_ip: int, global_: int) -> None:
    budget = UpstreamBudget.from_env(env)
    assert (budget._per_ip.amount if budget._per_ip else 0) == per_ip
    assert (budget._global.amount if budget._global else 0) == global_


# ── Through the API ────────────────────────────────────────────────


def test_historical_misses_past_client_budget_return_429() -> None:
    _use(UpstreamBudget(per_ip_per_minute=3, global_per_minute=0))
    client = _client("203.0.113.1")
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        codes = [client.get(f"/stocks/SYM{i}/historical").status_code for i in range(4)]
        other = _client("203.0.113.2").get("/stocks/OTHER/historical")
    assert codes == [200, 200, 200, 429]
    assert mock_stocks.call_count == 4  # three from the first client, one from the other
    assert other.status_code == 200


def test_429_envelope_and_retry_after() -> None:
    _use(UpstreamBudget(per_ip_per_minute=1, global_per_minute=0))
    client = _client("203.0.113.3")
    with patch("psxdata.stocks", return_value=_history()):
        client.get("/stocks/A/historical")
        resp = client.get("/stocks/B/historical")
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert resp.json()["error"]["code"] == "rate_limited"


def test_cache_hits_do_not_spend_budget() -> None:
    _use(UpstreamBudget(per_ip_per_minute=1, global_per_minute=0))
    client = _client("203.0.113.4")
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        codes = [client.get("/stocks/SYS/historical").status_code for _ in range(5)]
    assert codes == [200] * 5
    assert mock_stocks.call_count == 1


def test_sdk_disk_cache_hits_do_not_spend_budget(
    proxy_passthrough_disabled: ProxyPassthrough, tmp_path
) -> None:
    _use(UpstreamBudget(per_ip_per_minute=1, global_per_minute=0))
    screener = pd.DataFrame({"symbol": ["ENGRO"], "price": [481.99]})
    PSXClient(cache_dir=str(tmp_path / "psxdata-cache"))._cache.set("screener_all", screener)
    client = _client("203.0.113.5")
    with patch("psxdata.quote", side_effect=AssertionError("PSX must not be called")):
        codes = [client.get("/stocks/ENGRO/quote").status_code for _ in range(3)]
    assert codes == [200, 200, 200]


def test_global_budget_returns_503_without_psx_cooldown() -> None:
    _use(UpstreamBudget(per_ip_per_minute=0, global_per_minute=1))
    service = HistoricalService(TieredStore(MemoryLRU(), RedisStore(None)))
    app.state.historical_service = service
    with patch("psxdata.stocks", return_value=_history()):
        assert _client("203.0.113.6").get("/stocks/A/historical").status_code == 200
        resp = _client("203.0.113.7").get("/stocks/B/historical")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "psx_unavailable"
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert not service._in_cooldown()  # only a real 429 from PSX pauses PSX for everyone


def test_budget_exceeded_serves_stale_historical_copy() -> None:
    # Tuesday 10:00 PKT, market open, so an entry goes stale after the 30-minute market TTL
    now = [datetime(2024, 1, 2, 5, 0, tzinfo=timezone.utc)]
    service = HistoricalService(TieredStore(MemoryLRU(), RedisStore(None)), now=lambda: now[0])
    app.state.historical_service = service
    _use(UpstreamBudget(per_ip_per_minute=1, global_per_minute=0))
    client = _client("203.0.113.8")
    with patch("psxdata.stocks", return_value=_history()) as mock_stocks:
        assert client.get("/stocks/SYS/historical").headers["X-Cache"] == "MISS"
        now[0] += timedelta(minutes=31)
        resp = client.get("/stocks/SYS/historical")
    assert resp.status_code == 200
    assert resp.headers["X-Cache"] == "STALE"
    assert mock_stocks.call_count == 1


def test_budget_exceeded_is_recorded_on_span(otel) -> None:
    _use(UpstreamBudget(per_ip_per_minute=1, global_per_minute=0))
    client = _client("203.0.113.9")
    with patch("psxdata.stocks", return_value=_history()):
        client.get("/stocks/A/historical")
        otel.spans.clear()
        client.get("/stocks/B/historical")
    span = otel.span("psx.fetch")
    assert span.attributes["psxdata.budget_exceeded"] == 429
    assert span.status.status_code.name == "ERROR"
