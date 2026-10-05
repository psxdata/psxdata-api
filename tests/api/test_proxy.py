"""Tests for X-PSX-Proxy passthrough and its SSRF hardening — no network.

PSX is faked at the scraper level: ``fake_psx`` replaces a scraper's
``fetch`` with one that first calls ``self._request`` (which raises CacheMiss
on the cache-only client, exactly like a real network call would) and then
records which proxy the fetching scraper was configured with.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psxdata import BaseScraper, PSXClient
from psxdata.exceptions import PSXConnectionError
from psxdata.proxy import normalize_proxy
from psxdata.scrapers import token as token_module
from psxdata.scrapers.screener import ScreenerScraper

from api.main import app
from api.proxy import PROXY_HEADER, ProxyPassthrough, pin_proxy

PUBLIC_IP = "93.184.216.34"
PROXY = "http://alice:s3cret@proxy.example.com:8080"
PINNED = f"http://alice:s3cret@{PUBLIC_IP}:8080"
PINNED_PROXIES = {"http": PINNED, "https": PINNED}


def resolver_for(*addrs: str):
    return lambda host, port: list(addrs)


def no_connect(host: str, port: int) -> None:
    return None


class Counter:
    """Connector that records every proxy contact."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> None:
        self.calls.append((host, port))


def refuse_network(*args: Any) -> Any:
    raise AssertionError("the proxy must not be contacted")


@contextmanager
def fake_psx(
    scraper_cls: type, result: Any = None, error: Exception | None = None
) -> Iterator[list]:
    """Fake PSX for *scraper_cls*; yields the proxies of every scraper that reached PSX."""
    reached: list[dict | None] = []

    def fetch(self: BaseScraper, *args: Any, **kwargs: Any) -> Any:
        self._request("GET", "https://dps.psx.com.pk/")  # CacheMiss on the cache-only client
        reached.append(self._proxies)
        if error is not None:
            raise error
        return result

    with (
        patch.object(scraper_cls, "fetch", fetch),
        patch.object(BaseScraper, "_request", return_value=None),
    ):
        yield reached


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def make_passthrough(tmp_path):
    """Build an enabled passthrough over an isolated SDK disk cache and install it."""
    def make(**kwargs: Any) -> ProxyPassthrough:
        kwargs.setdefault("resolver", resolver_for(PUBLIC_IP))
        kwargs.setdefault("connector", no_connect)
        passthrough = ProxyPassthrough(True, cache_dir=str(tmp_path / "cache"), **kwargs)
        app.state.proxy_passthrough = passthrough
        return passthrough
    return make


@pytest.fixture
def enabled(make_passthrough) -> ProxyPassthrough:
    return make_passthrough()


def _history_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": [pd.Timestamp("2024-01-05")],
        "open": [1.0], "high": [2.0], "low": [0.5],
        "close": [1.5], "volume": [10], "is_anomaly": [False],
    })


def _screener_df() -> pd.DataFrame:
    return pd.DataFrame([{"symbol": "ENGRO", "sector": 7.0, "price": 300.0}])


def _proxied(client: TestClient, path: str, proxy: str = PROXY):
    return client.get(path, headers={PROXY_HEADER: proxy})


# ---------------------------------------------------------------------------
# pin_proxy — validation and SSRF checks
# ---------------------------------------------------------------------------

class TestPinProxy:
    def test_pins_hostname_to_resolved_public_ip(self):
        assert pin_proxy(PROXY, resolver_for(PUBLIC_IP)) == PINNED

    def test_socks5h_without_credentials(self):
        assert pin_proxy("socks5h://p.example.com:1080", resolver_for(PUBLIC_IP)) == (
            f"socks5h://{PUBLIC_IP}:1080"
        )

    def test_prefers_ipv4_and_brackets_ipv6(self):
        v6 = "2606:4700:4700::1111"
        assert pin_proxy("http://p:8080", resolver_for(v6, PUBLIC_IP)).endswith(f"{PUBLIC_IP}:8080")
        assert pin_proxy("http://p:8080", resolver_for(v6)) == f"http://[{v6}]:8080"

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",          # loopback
            "10.0.0.5",           # private
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",    # cloud metadata / link-local
            "100.64.0.1",         # carrier-grade NAT
            "0.0.0.0",
            "224.0.0.1",          # multicast
            "::1",
            "fd00::1",            # unique local
            "fe80::1%eth0",       # link-local with scope id
            "::ffff:10.0.0.1",    # IPv4-mapped private
            "2002:0a00:0001::1",  # 6to4 wrapping 10.0.0.1
        ],
    )
    def test_rejects_non_public_addresses(self, address):
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy("http://p.example.com:8080", resolver_for(address))
        assert excinfo.value.status_code == 400

    def test_rejects_when_any_resolved_address_is_private(self):
        with pytest.raises(HTTPException):
            pin_proxy("http://p.example.com:8080", resolver_for(PUBLIC_IP, "10.0.0.1"))

    def test_rejects_ip_literal_private_host(self):
        def resolver(host, port):
            return [host]
        with pytest.raises(HTTPException):
            pin_proxy("http://169.254.169.254:8080", resolver)

    @pytest.mark.parametrize(
        "url",
        [
            "https://p.example.com:8443",      # https proxies cannot be IP-pinned
            "ftp://p.example.com:2121",
            "p.example.com:8080",
            "http://p.example.com",            # no explicit port
            "http://p.example.com:22",         # low port
            "http://p.example.com:6379x",
            "http://p.example.com:8080/path",
            "http://p.example.com:8080?x=1",
            "http://:8080",
            "",
            "http://p.example.com:8080" + "a" * 2048,
        ],
    )
    def test_rejects_malformed_or_disallowed_urls(self, url):
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy(url, resolver_for(PUBLIC_IP))
        assert excinfo.value.status_code == 400

    def test_unresolvable_host(self):
        def resolver(host, port):
            raise OSError("no such host")
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy(PROXY, resolver)
        assert "resolved" in excinfo.value.detail

    def test_rejection_never_echoes_credentials(self):
        with pytest.raises(HTTPException) as excinfo:
            pin_proxy("http://alice:s3cret@p.example.com:8080", resolver_for("10.0.0.1"))
        assert "s3cret" not in excinfo.value.detail
        assert "alice" not in excinfo.value.detail


# ---------------------------------------------------------------------------
# Endpoints — upfront checks
# ---------------------------------------------------------------------------

class TestUpfrontChecks:
    def test_no_header_uses_shared_default(self, client):
        with patch("psxdata.screener", return_value=_screener_df()) as default_screener:
            resp = client.get("/screener")
        assert resp.status_code == 200
        default_screener.assert_called_once_with()

    def test_header_rejected_when_disabled(self, client):
        with patch("psxdata.screener") as default_screener:
            resp = _proxied(client, "/screener")
        assert resp.status_code == 400
        assert "not enabled" in resp.json()["error"]["message"]
        default_screener.assert_not_called()

    def test_malformed_proxy_rejected_even_when_cached(self, client, enabled):
        with fake_psx(ScreenerScraper, _screener_df()):
            _proxied(client, "/screener")  # fill the cache
            resp = _proxied(client, "/screener", proxy="ftp://alice:s3cret@p.example.com:2121")
        assert resp.status_code == 400
        assert "s3cret" not in resp.text


# ---------------------------------------------------------------------------
# Endpoints — cache first, proxy only on a miss
# ---------------------------------------------------------------------------

class TestCacheFirst:
    def test_miss_fetches_through_pinned_proxy(self, client, make_passthrough):
        connector = Counter()
        make_passthrough(connector=connector)
        with fake_psx(ScreenerScraper, _screener_df()) as reached:
            resp = _proxied(client, "/screener")
        assert resp.status_code == 200
        assert reached == [PINNED_PROXIES]
        assert connector.calls == [(PUBLIC_IP, 8080)]

    def test_hit_is_served_without_contacting_proxy(self, client, make_passthrough):
        connector = Counter()
        make_passthrough(connector=connector)
        with fake_psx(ScreenerScraper, _screener_df()) as reached:
            first = _proxied(client, "/screener")
            second = _proxied(client, "/screener")
        assert first.json()["data"] == second.json()["data"]
        assert len(reached) == 1          # PSX fetched once
        assert len(connector.calls) == 1  # proxy contacted once

    def test_hit_on_data_cached_by_another_caller(self, client, make_passthrough, tmp_path):
        make_passthrough(resolver=refuse_network, connector=refuse_network)
        PSXClient(cache_dir=str(tmp_path / "cache"))._cache.set("screener_all", _screener_df())
        with fake_psx(ScreenerScraper, _screener_df()) as reached:
            resp = _proxied(client, "/stocks/ENGRO/quote")
        assert resp.status_code == 200
        assert resp.json()["data"]["symbol"] == "ENGRO"
        assert reached == []

    def test_hits_do_not_count_against_proxy_rate_limit(self, client, make_passthrough):
        make_passthrough(per_ip_per_minute=1)
        with fake_psx(ScreenerScraper, _screener_df()):
            codes = [_proxied(client, "/screener").status_code for _ in range(5)]
        assert codes == [200] * 5

    def test_proxied_and_default_clients_share_disk_cache(self, enabled):
        proxied = enabled._client_for(PINNED)
        cache_only = enabled.cache_only_client()
        assert proxied._cache._cache.directory == cache_only._cache._cache.directory

    def test_proxied_historical_miss_fills_shared_cache(self, client, enabled):
        with (
            patch.object(PSXClient, "stocks", return_value=_history_df()) as proxied_stocks,
            patch("psxdata.stocks") as default_stocks,
        ):
            resp = _proxied(client, "/stocks/ENGRO/historical")
            assert resp.status_code == 200
            assert resp.headers["X-Cache"] == "MISS"
            proxied_stocks.assert_called_once_with("ENGRO", cache=False)

            # Data fetched through a proxy serves everyone: an unproxied call is a HIT
            resp = client.get("/stocks/ENGRO/historical")
            assert resp.headers["X-Cache"] == "HIT"
            assert resp.json()["data"][0]["close"] == 1.5
            default_stocks.assert_not_called()

    def test_proxied_historical_hit_never_contacts_proxy(self, client, make_passthrough):
        make_passthrough(resolver=refuse_network, connector=refuse_network)
        with patch("psxdata.stocks", return_value=_history_df()):
            client.get("/stocks/ENGRO/historical")
        with patch.object(PSXClient, "stocks") as proxied_stocks:
            resp = _proxied(client, "/stocks/ENGRO/historical")
        assert resp.headers["X-Cache"] == "HIT"
        proxied_stocks.assert_not_called()


# ---------------------------------------------------------------------------
# Endpoints — network checks on a miss
# ---------------------------------------------------------------------------

class TestMissChecks:
    # An empty screener is never cached, so every request below is a miss.

    def test_private_proxy_rejected_with_400(self, client, make_passthrough):
        make_passthrough(resolver=resolver_for("169.254.169.254"))
        with fake_psx(ScreenerScraper, pd.DataFrame()) as reached:
            resp = _proxied(client, "/screener")
        assert resp.status_code == 400
        assert "s3cret" not in resp.text
        assert reached == []

    def test_unreachable_proxy_returns_502(self, client, make_passthrough):
        def refuse(host, port):
            raise ConnectionRefusedError
        make_passthrough(connector=refuse)
        with fake_psx(ScreenerScraper, pd.DataFrame()) as reached:
            resp = _proxied(client, "/screener")
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "proxy_unreachable"
        assert "s3cret" not in resp.text
        assert reached == []

    def test_per_ip_rate_limit(self, client, make_passthrough):
        make_passthrough(per_ip_per_minute=2)
        with fake_psx(ScreenerScraper, pd.DataFrame()):
            codes = [_proxied(client, "/screener").status_code for _ in range(3)]
        assert codes == [200, 200, 429]

    def test_concurrency_cap(self, client, make_passthrough):
        passthrough = make_passthrough(max_concurrent=1)
        passthrough._slots.acquire()  # simulate one proxied fetch in flight
        try:
            with fake_psx(ScreenerScraper, pd.DataFrame()):
                resp = _proxied(client, "/screener")
        finally:
            passthrough._slots.release()
        assert resp.status_code == 429

    def test_slot_released_after_upstream_error(self, client, make_passthrough):
        make_passthrough(max_concurrent=1)
        with fake_psx(ScreenerScraper, error=PSXConnectionError("down")):
            assert _proxied(client, "/screener").status_code == 503
        with fake_psx(ScreenerScraper, pd.DataFrame()):
            assert _proxied(client, "/screener").status_code == 200


class TestPooling:
    def test_client_pool_bounded_and_token_providers_pruned(self, monkeypatch, tmp_path):
        monkeypatch.setattr(token_module, "_proxy_providers", {})
        passthrough = ProxyPassthrough(True, max_clients=1, cache_dir=str(tmp_path))
        first = f"http://{PUBLIC_IP}:8080"
        second = f"http://{PUBLIC_IP}:8081"

        passthrough._client_for(first)
        token_module.get_default_provider(normalize_proxy(first))
        assert len(token_module._proxy_providers) == 1

        passthrough._client_for(second)
        assert list(passthrough._clients) == [second]
        assert token_module._proxy_providers == {}

    def test_same_proxy_reuses_client(self, tmp_path):
        passthrough = ProxyPassthrough(True, cache_dir=str(tmp_path))
        url = f"http://{PUBLIC_IP}:8080"
        assert passthrough._client_for(url) is passthrough._client_for(url)


class TestFromEnv:
    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("ON", True), ("", False), ("0", False), ("no", False),
    ])
    def test_flag(self, value, expected):
        assert ProxyPassthrough.from_env({"PSX_PROXY_PASSTHROUGH": value}).enabled is expected

    def test_default_off(self):
        assert ProxyPassthrough.from_env({}).enabled is False


# ---------------------------------------------------------------------------
# Telemetry: psx.fetch span and credential hygiene
# ---------------------------------------------------------------------------

class TestPsxFetchSpan:
    def test_direct_fetch_span(self, client, otel):
        with patch("psxdata.tickers", return_value=["HBL"]):
            assert client.get("/stocks").status_code == 200
        attrs = dict(otel.span("psx.fetch").attributes)
        assert attrs == {
            "psxdata.function": "tickers", "psxdata.proxied": False,
            "psxdata.cache_only_hit": False,
        }
        assert otel.span("psx.fetch").parent.span_id == otel.server_spans()[0].context.span_id

    def test_proxied_miss_then_cache_only_hit(self, client, enabled, otel):
        with fake_psx(ScreenerScraper, result=_screener_df()) as reached:
            assert _proxied(client, "/screener").status_code == 200
            first = dict(otel.span("psx.fetch").attributes)
            otel.spans.clear()
            assert _proxied(client, "/screener").status_code == 200
            second = dict(otel.span("psx.fetch").attributes)
        assert len(reached) == 1  # second request was served from the shared cache
        assert first == {
            "psxdata.function": "screener", "psxdata.proxied": True,
            "psxdata.cache_only_hit": False,
        }
        assert second == {
            "psxdata.function": "screener", "psxdata.proxied": True,
            "psxdata.cache_only_hit": True,
        }

    def test_fetch_error_is_recorded_and_reraised(self, client, otel):
        from psxdata.exceptions import PSXUnavailableError

        with patch("psxdata.tickers", side_effect=PSXUnavailableError("down")):
            assert client.get("/stocks").status_code == 503
        assert otel.span("psx.fetch").status.status_code.name == "ERROR"

    def test_proxy_credentials_never_emitted(self, make_passthrough, otel):
        def refuse(host: str, port: int) -> None:
            raise OSError("refused")

        make_passthrough(connector=refuse)
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/screener", headers={PROXY_HEADER: PROXY}
        )
        assert resp.status_code == 502
        assert "s3cret" not in resp.text
        lines = otel.lines()
        assert lines, "expected telemetry for the request"
        assert not [line for line in lines if "s3cret" in line or "alice" in line]
