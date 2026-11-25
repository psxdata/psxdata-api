"""Tests for building the /historical cache from environment variables."""
import logging
from datetime import timedelta

import redis
from fastapi.testclient import TestClient

from api.cache.factory import build_historical_service, build_redis_client, market_ttl_from_env
from api.main import app


def test_market_ttl_defaults_to_30_minutes() -> None:
    assert market_ttl_from_env({}) == timedelta(minutes=30)


def test_market_ttl_from_env_seconds() -> None:
    assert market_ttl_from_env({"HISTORICAL_CACHE_MARKET_TTL": "900"}) == timedelta(minutes=15)


def test_invalid_market_ttl_falls_back_with_warning(caplog) -> None:
    caplog.set_level(logging.WARNING)
    assert market_ttl_from_env({"HISTORICAL_CACHE_MARKET_TTL": "abc"}) == timedelta(minutes=30)
    assert market_ttl_from_env({"HISTORICAL_CACHE_MARKET_TTL": "0"}) == timedelta(minutes=30)
    assert "HISTORICAL_CACHE_MARKET_TTL" in caplog.text


def test_no_redis_url_means_memory_only() -> None:
    assert build_redis_client(None) is None
    assert build_redis_client("") is None
    service = build_historical_service({})
    assert service.store.l2.enabled is False


def test_redis_client_uses_short_timeouts_and_does_not_connect_eagerly() -> None:
    client = build_redis_client("rediss://default:pw@cache.example.invalid:12345")
    assert isinstance(client, redis.Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 0.5
    assert kwargs["socket_connect_timeout"] == 0.5


def test_invalid_redis_url_falls_back_without_leaking_secret(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    service = build_historical_service({"REDIS_URL": "http://default:s3cr3t@cache.example:1"})
    assert service.store.l2.enabled is False
    assert "REDIS_URL" in caplog.text
    assert "s3cr3t" not in caplog.text


def test_unreachable_redis_at_startup_logs_host_only(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    service = build_historical_service(
        {"REDIS_URL": "redis://default:s3cr3t@127.0.0.1:1"}  # nothing listens on port 1
    )
    assert service.store.l2.enabled is True                   # stays configured; fails open
    assert "127.0.0.1" in caplog.text
    assert "s3cr3t" not in caplog.text
    service.close()


def test_lifespan_installs_service_without_redis(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.historical_service.store.l2.enabled is False


def test_factory_shares_one_redis_client_with_the_cooldown() -> None:
    service = build_historical_service({"REDIS_URL": "redis://127.0.0.1:1/0"})
    assert service._cooldown_redis is not None
    assert service._cooldown_redis is service.store.l2._client
    assert build_historical_service({})._cooldown_redis is None
