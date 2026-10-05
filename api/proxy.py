"""Per-request PSX proxy passthrough (``X-PSX-Proxy`` header) with SSRF hardening.

A caller may send ``X-PSX-Proxy: <url>`` to have this server fetch PSX data
through the caller's proxy. The server then opens a connection to an address
the caller chose, so every proxy is checked before use:

- The feature is off unless ``PSX_PROXY_PASSTHROUGH`` is set to a true value.
- The proxy is only contacted when PSX must be: a request is first answered
  from the shared caches, and the network checks below (rate limit, DNS
  pinning, concurrency slot, TCP check) run only on a cache miss.
- Only ``http``, ``socks5`` and ``socks5h`` proxies. ``https`` proxies are
  refused: TLS to the proxy verifies its hostname, which rules out IP pinning.
- An explicit port is required: 80, 443, or 1024-65535.
- Every address the host resolves to must be public (globally routable), and
  the connection is pinned to the checked IP, so DNS rebinding cannot point it
  somewhere else between the check and the request.
- A short TCP connect check rejects dead proxies before any PSX request.
- Per-IP rate limit, a global concurrency cap, and a bounded pool of clients.

Proxied requests share the same caches as all other requests: the proxy only
changes the egress to PSX, not the data. PSX is HTTPS-only and certificates are
verified, so a proxy tunnels TLS end to end and cannot alter what it relays.

Credentials in the proxy URL are never logged or echoed back.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import psxdata
from fastapi import Header, HTTPException, Request
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from opentelemetry.trace import Span
from psxdata import BaseScraper, PSXClient
from psxdata.constants import CACHE_DIR
from psxdata.proxy import normalize_proxy
from psxdata.scrapers import token as token_module
from slowapi.util import get_remote_address

from api.telemetry import get_tracer
from api.upstream import UpstreamBudget, UpstreamBudgetExceeded, get_upstream_budget

PROXY_HEADER = "X-PSX-Proxy"
ENABLE_ENV = "PSX_PROXY_PASSTHROUGH"
ALLOWED_SCHEMES = frozenset({"http", "socks5", "socks5h"})
MAX_PROXY_URL_LENGTH = 2048
CONNECT_TIMEOUT = 5.0
MAX_CLIENTS = 32
MAX_CONCURRENT = 4
PER_IP_PER_MINUTE = 10

Resolver = Callable[[str, int], list[str]]
Connector = Callable[[str, int], None]


class ProxyUnreachableError(Exception):
    """The caller's proxy passed validation but did not accept a TCP connection."""


class CacheMiss(Exception):
    """Raised by a cache-only client when answering would need a PSX request."""


@dataclass(frozen=True)
class ParsedProxy:
    """A syntactically valid caller proxy, not yet resolved or contacted."""

    scheme: str
    userinfo: str  # "user:pass@" or ""
    host: str
    port: int


def _resolve(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _connect(host: str, port: int) -> None:
    socket.create_connection((host, port), timeout=CONNECT_TIMEOUT).close()


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 addresses embedded in IPv6 must be public too
        embedded = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        return all(_is_public(e) for e in embedded if e is not None)
    return True


def _reject(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"{PROXY_HEADER}: {message}")


def parse_proxy(raw: str) -> ParsedProxy:
    """Check a caller-supplied proxy URL without any network access.

    Raises:
        HTTPException: 400 for a malformed or disallowed URL. The message never
            includes the URL, so credentials are not echoed back.
    """
    raw = raw.strip()
    if not raw or len(raw) > MAX_PROXY_URL_LENGTH:
        raise _reject("must be a proxy URL of at most 2048 characters")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _reject("scheme must be http://, socks5:// or socks5h://")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise _reject("must not contain a path, query or fragment")
    try:
        port = parts.port
    except ValueError:
        raise _reject("invalid port") from None
    if port is None:
        raise _reject("must include an explicit port")
    if not (port in (80, 443) or 1024 <= port <= 65535):
        raise _reject("port must be 80, 443, or between 1024 and 65535")
    host = parts.hostname
    if not host:
        raise _reject("must include a host")
    userinfo = parts.netloc.rpartition("@")[0] + "@" if "@" in parts.netloc else ""
    return ParsedProxy(scheme, userinfo, host, port)


def resolve_proxy(proxy: ParsedProxy, resolver: Resolver = _resolve) -> str:
    """Resolve *proxy* and return its URL pinned to a checked public IP.

    Raises:
        HTTPException: 400 if the host cannot be resolved or any of its
            addresses is not public.
    """
    try:
        ips = {
            ipaddress.ip_address(addr.split("%", 1)[0])
            for addr in resolver(proxy.host, proxy.port)
        }
    except (OSError, UnicodeError, ValueError):
        raise _reject("host could not be resolved") from None
    if not ips or not all(_is_public(ip) for ip in ips):
        raise _reject("host must resolve only to public internet addresses")

    ip = min(ips, key=lambda a: (a.version, int(a)))  # prefer IPv4
    host_part = f"[{ip}]" if ip.version == 6 else str(ip)
    pinned = f"{proxy.scheme}://{proxy.userinfo}{host_part}:{proxy.port}"
    try:
        normalize_proxy(pinned)
    except (ValueError, TypeError):
        raise _reject("invalid proxy URL") from None
    except ImportError:
        raise _reject("SOCKS proxies are not available on this server") from None
    return pinned


def pin_proxy(raw: str, resolver: Resolver = _resolve) -> str:
    """Parse, resolve and pin *raw* in one step (see parse_proxy and resolve_proxy)."""
    return resolve_proxy(parse_proxy(raw), resolver)


def _proxy_key(pinned: str) -> frozenset[tuple[str, str]]:
    # Same key psxdata uses for its per-proxy token providers
    return frozenset((normalize_proxy(pinned) or {}).items())


def _cache_only(client: PSXClient) -> PSXClient:
    """Block every scraper of *client* from the network, so it can only answer from cache."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise CacheMiss

    for scraper in vars(client).values():
        if isinstance(scraper, BaseScraper):
            scraper._request = refuse  # type: ignore[method-assign]
    return client


class PsxSource:
    """Where a request's PSX data comes from.

    The shared caches first, and only on a miss a request to PSX: through the
    caller's proxy if one was given, otherwise directly, charged to the
    caller's upstream budget. Both read and write the same caches; only the
    route to PSX differs. Without a passthrough or request, the module-level
    psxdata functions are called as is.
    """

    def __init__(
        self,
        passthrough: ProxyPassthrough | None = None,
        request: Request | None = None,
        proxy: ParsedProxy | None = None,
        budget: UpstreamBudget | None = None,
    ) -> None:
        self._passthrough = passthrough
        self._request = request
        self._proxy = proxy
        self._budget = budget

    @property
    def proxied(self) -> bool:
        return self._proxy is not None

    def fetch(self, name: str, *args: Any, **kwargs: Any) -> Any:
        with get_tracer().start_as_current_span(
            "psx.fetch", attributes={"psxdata.function": name, "psxdata.proxied": self.proxied}
        ) as span:
            return self._fetch(span, name, *args, **kwargs)

    def _fetch(self, span: Span, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._passthrough is None or self._request is None:
            return getattr(psxdata, name)(*args, **kwargs)
        if kwargs.get("cache", True):
            try:
                result = getattr(self._passthrough.cache_only_client(), name)(*args, **kwargs)
            except CacheMiss:
                pass
            else:
                span.set_attribute("psxdata.cache_only_hit", True)
                return result
        span.set_attribute("psxdata.cache_only_hit", False)
        if self._proxy is None:
            if self._budget is not None:
                try:
                    self._budget.charge(get_remote_address(self._request))
                except UpstreamBudgetExceeded as exc:
                    span.set_attribute("psxdata.budget_exceeded", exc.status_code)
                    raise
            return getattr(psxdata, name)(*args, **kwargs)
        with self._passthrough.acquire(self._request, self._proxy) as client:
            return getattr(client, name)(*args, **kwargs)


class ProxyPassthrough:
    """Validates caller proxies and hands out bounded, pooled proxied clients."""

    def __init__(
        self,
        enabled: bool,
        *,
        resolver: Resolver = _resolve,
        connector: Connector = _connect,
        max_clients: int = MAX_CLIENTS,
        max_concurrent: int = MAX_CONCURRENT,
        per_ip_per_minute: int = PER_IP_PER_MINUTE,
        cache_dir: str = CACHE_DIR,
    ) -> None:
        self.enabled = enabled
        self._cache_dir = cache_dir
        self._cache_only_client: PSXClient | None = None
        self._resolver = resolver
        self._connector = connector
        self._max_clients = max_clients
        self._clients: OrderedDict[str, PSXClient] = OrderedDict()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._rate_limiter = MovingWindowRateLimiter(MemoryStorage())
        self._rate = RateLimitItemPerMinute(per_ip_per_minute)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ProxyPassthrough:
        enabled = env.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
        return cls(enabled)

    def check(self, raw: str) -> ParsedProxy:
        """Checks that need no network: the feature is enabled and *raw* is well formed."""
        if not self.enabled:
            raise HTTPException(
                status_code=400, detail=f"{PROXY_HEADER} is not enabled on this server"
            )
        return parse_proxy(raw)

    def cache_only_client(self) -> PSXClient:
        """A client over the shared SDK disk cache that raises CacheMiss instead of calling PSX."""
        with self._lock:
            if self._cache_only_client is None:
                self._cache_only_client = _cache_only(PSXClient(cache_dir=self._cache_dir))
            return self._cache_only_client

    @contextmanager
    def acquire(self, request: Request, proxy: ParsedProxy) -> Iterator[PSXClient]:
        """Resolve and pin *proxy*, reserve a concurrency slot, and yield a proxied client.

        Called only when PSX must actually be contacted.
        """
        # Rate-limit before DNS resolution so the check itself cannot be spammed
        if not self._rate_limiter.hit(self._rate, "psx-proxy", get_remote_address(request)):
            raise HTTPException(status_code=429, detail="Too many proxied requests")
        pinned = resolve_proxy(proxy, self._resolver)
        if not self._slots.acquire(blocking=False):
            raise HTTPException(
                status_code=429, detail="Too many proxied requests in progress; retry shortly"
            )
        try:
            parts = urlsplit(pinned)
            try:
                self._connector(parts.hostname or "", parts.port or 0)
            except OSError:
                raise ProxyUnreachableError(
                    f"{PROXY_HEADER}: proxy did not accept a connection"
                ) from None
            yield self._client_for(pinned)
        finally:
            self._slots.release()

    def _client_for(self, pinned: str) -> PSXClient:
        with self._lock:
            client = self._clients.get(pinned)
            if client is not None:
                self._clients.move_to_end(pinned)
                return client
            # Same on-disk cache as the module-level functions (CACHE_DIR by default)
            client = PSXClient(cache_dir=self._cache_dir, proxy=pinned)
            self._clients[pinned] = client
            if len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
                self._prune_token_providers()
            return client

    def _prune_token_providers(self) -> None:
        """Drop psxdata's per-proxy token providers for clients no longer pooled.

        psxdata keeps one provider per proxy for the life of the process; on a
        server fed arbitrary proxies that would grow without bound.
        """
        live = {_proxy_key(url) for url in self._clients}
        with token_module._default_lock:
            for key in [k for k in token_module._proxy_providers if k not in live]:
                del token_module._proxy_providers[key]


def get_proxy_passthrough(request: Request) -> ProxyPassthrough:
    """Return the app's ProxyPassthrough; build one from the environment if lifespan did not run."""
    passthrough: ProxyPassthrough | None = getattr(request.app.state, "proxy_passthrough", None)
    if passthrough is None:
        passthrough = ProxyPassthrough.from_env(os.environ)
        request.app.state.proxy_passthrough = passthrough
    return passthrough


def psx_source(
    request: Request,
    x_psx_proxy: str | None = Header(
        default=None,
        alias=PROXY_HEADER,
        description=(
            "Optional proxy for this request's PSX traffic: http://, socks5:// or socks5h://, "
            "with an explicit port and optional user:pass@. Must resolve to a public address. "
            "Cached data is served without contacting the proxy; it is used only when PSX "
            "must be fetched, with stricter rate limits. Only honoured when the server "
            "enables proxy passthrough."
        ),
    ),
) -> PsxSource:
    """FastAPI dependency: the PSX data source for this request."""
    passthrough = get_proxy_passthrough(request)
    if x_psx_proxy is None:
        return PsxSource(passthrough, request, budget=get_upstream_budget(request))
    return PsxSource(passthrough, request, passthrough.check(x_psx_proxy))
