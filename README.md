# psxdata-api — REST API for Pakistan Stock Exchange (PSX) Data

[![CI](https://github.com/psxdata/psxdata-api/actions/workflows/ci.yml/badge.svg)](https://github.com/psxdata/psxdata-api/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/badge/docs-mintlify-blue)](https://psxdata.mintlify.app/rest-api)
[![API](https://img.shields.io/badge/api-live-brightgreen)](https://psxdata-api.fastapicloud.dev)
[![Docker Hub](https://img.shields.io/docker/v/mtauha/psxdata-api?label=Docker+Hub)](https://hub.docker.com/r/mtauha/psxdata-api)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.md)

**psxdata-api** is a FastAPI REST service that exposes Pakistan Stock Exchange data over HTTP. It wraps the [psxdata](https://pypi.org/project/psxdata/) Python library and ships its own Docker image, CI/CD pipeline, and auto-deploy config.

**Base URL:** `https://psxdata-api.fastapicloud.dev`  
**Documentation:** [https://psxdata.mintlify.app/rest-api](https://psxdata.mintlify.app/rest-api)

---

## Quick Start

```bash
# Run with Docker
docker run -p 8000:8000 mtauha/psxdata-api

# Try it
curl http://localhost:8000/health
curl http://localhost:8000/stocks
curl "http://localhost:8000/stocks/ENGRO/historical?start=2024-01-01&end=2024-12-31"
curl http://localhost:8000/screener
```

Interactive docs available at `http://localhost:8000/docs` (Swagger UI) and `/redoc`.

---

## Endpoints

### Health

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/health` | API liveness check |

### Stocks

| Method | Path | Query params | Description |
| ------ | ---- | ------------ | ----------- |
| `GET` | `/stocks` | `index` (optional) | All listed tickers, optionally filtered by index name |
| `GET` | `/stocks/{symbol}/historical` | `start`, `end` (ISO dates, optional) | OHLCV history for a ticker |
| `GET` | `/stocks/{symbol}/quote` | — | Live quote for a ticker |
| `GET` | `/stocks/{symbol}/fundamentals` | — | Financial report links for a ticker |

### Indices

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/indices` | All 18 PSX index names |
| `GET` | `/indices/{name}` | Constituents of a named index (e.g. `KSE100`) |

### Sectors

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/sectors` | All 37 sector summaries |
| `GET` | `/sectors/{name}/stocks` | Tickers in a named sector |

### Screener

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/screener` | Full, unfiltered PSX screener table (~729 symbols, all columns) |

---

### Market Instruments

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET` | `/debt-market` | Debt market instruments (TFCs, Sukuks, etc.) |
| `GET` | `/eligible-scrips` | Margin trading eligible stocks |

---

## Response Envelope

Every response wraps its payload in a consistent envelope.

**Single-item response**
```json
{
  "data": { "status": "ok" },
  "meta": { "timestamp": "2024-01-15T10:30:00+00:00", "cached": false }
}
```

**List response**
```json
{
  "data": [{"symbol": "ENGRO", ...}, ...],
  "meta": { "timestamp": "2024-01-15T10:30:00+00:00", "cached": false, "count": 42 }
}
```

`meta.cached` is `true` when the response was served from the API's cache (currently `/stocks/{symbol}/historical`; see [Caching](#caching)).

**Error response**
```json
{
  "error": { "status": 404, "code": "not_found", "message": "ENGRO not found" }
}
```

### Error codes

| HTTP status | `code` | Meaning |
| ----------- | ------ | ------- |
| 400, 422 | `bad_request` | Invalid input or query parameters |
| 404 | `not_found` | Symbol or index does not exist |
| 429 | `rate_limited` | Exceeded 60 requests/minute per IP, or 20 uncached requests/minute per IP (sent with a `Retry-After` header) |
| 502 | `upstream_data_error` | Upstream PSX data failed validation |
| 502 | `proxy_unreachable` | The `X-PSX-Proxy` you sent did not accept a connection |
| 503 | `psx_unavailable` | PSX website unreachable, PSX is rate-limiting the API, or the server's own PSX budget is used up (sent with a `Retry-After` header) |
| 500 | `internal_error` | Unexpected server error |

---

## Rate Limiting

60 requests per minute per IP address. Exceeding the limit returns `429 rate_limited`.

Requests that need fresh data from PSX have tighter limits, because PSX rate-limits the server as a whole:

- **20 per minute per IP.** Beyond that: `429 rate_limited`.
- **60 per minute across all clients.** Beyond that: `503 psx_unavailable`.

Both responses carry a `Retry-After` header. Requests answered from the cache don't count, and `/historical` serves its last cached copy (`X-Cache: STALE`) instead of an error when it has one. Each server instance keeps its own counts.

---

## Caching

`GET /stocks/{symbol}/historical` is served from a cache. PSX always returns a symbol's full history, so the API fetches it once and slices it to your `start`/`end` range.

| When the data was fetched | Stays fresh until |
| ------------------------- | ----------------- |
| Mon–Fri 09:00–17:00 PKT (trading hours) | 30 minutes later, but no later than 17:00 |
| Any other time | The next weekday 09:00 PKT |

Every `/historical` response says where it came from:

| Header | Values |
| ------ | ------ |
| `X-Cache` | `HIT` — fresh cached copy · `MISS` — fetched from PSX just now · `STALE` — PSX refused or was unreachable, so the last cached copy was served |
| `Age` | Seconds since the data was fetched from PSX (on `HIT` and `STALE`) |

`meta.cached` is `true` for `HIT` and `STALE`. If PSX is rate-limiting and no cached copy exists, the API returns `503 psx_unavailable` with `Retry-After: 60`.

---

## Proxy Passthrough

Send an `X-PSX-Proxy` header to have the API fetch that request's PSX data through your own proxy, using psxdata's [proxy support](https://psxdata.mintlify.app/sdk/guides/proxy). The request token is fetched through the same proxy.

```bash
curl -H "X-PSX-Proxy: http://user:pass@proxy.example.com:8080"   https://psxdata-api.fastapicloud.dev/stocks/ENGRO/quote
```

The proxy is only used when PSX has to be contacted: if the data is already in the API's cache, it's served straight from there and your proxy is never touched. When PSX must be fetched, the server connects to an address you choose, so the proxy is checked first:

- **Only when enabled.** The server operator has to turn it on (`PSX_PROXY_PASSTHROUGH`). Otherwise the header is rejected with `400`.
- **Schemes:** `http://`, `socks5://` or `socks5h://`, with an explicit port (80, 443, or 1024–65535) and optional `user:pass@`. `https://` proxies are not accepted.
- **Public addresses only.** The proxy host must resolve only to public internet addresses. Loopback, private, link-local (including cloud metadata), CGNAT and multicast addresses are rejected. The connection is pinned to the checked IP, so the name cannot be re-pointed afterwards.
- **Quick reachability check.** On a cache miss, a proxy that doesn't accept a TCP connection within 5 seconds returns `502 proxy_unreachable`.
- **Same cache as everyone else.** The proxy only changes the route to PSX, not the data. Proxied requests are served from the cache when it's fresh, and what they fetch is cached for all callers. PSX is HTTPS-only and certificates are verified, so a proxy tunnels encrypted traffic and cannot alter it.
- **Stricter limits on PSX fetches.** Requests that go through your proxy to PSX are limited to 10 per minute per IP, with at most 4 in progress server-wide (`429` beyond that). Requests answered from the cache don't count toward this, only toward the normal limit.
- **Credentials are never logged or echoed back.** Send the proxy only in the header, never in the URL.

---

## Configuration

All settings are optional environment variables.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `REDIS_URL` | unset | Redis-compatible server for the `/historical` cache, e.g. an [Aiven for Valkey](https://aiven.io/valkey) service URI (`rediss://default:<password>@<host>:<port>`). Keeps the cache across restarts and instances. When unset, or when the server is unreachable, the API falls back to an in-memory cache and keeps working. |
| `HISTORICAL_CACHE_MARKET_TTL` | `1800` | How long, in seconds, `/historical` data stays fresh during PSX trading hours. |
| `PSX_FETCH_LIMIT_PER_IP` | `20` | PSX fetches per minute one client IP may cause (see [Rate Limiting](#rate-limiting)). `0` turns the limit off. |
| `PSX_FETCH_LIMIT_GLOBAL` | `60` | PSX fetches per minute across all clients. `0` turns the limit off. |
| `PSX_PROXY_PASSTHROUGH` | off | Set to `true` to honour the per-request `X-PSX-Proxy` header (see [Proxy Passthrough](#proxy-passthrough)). |

```bash
docker run -p 8000:8000 -e REDIS_URL="rediss://default:<password>@<host>:<port>" mtauha/psxdata-api
```

On FastAPI Cloud, set it as an app environment variable (`fastapi cloud env`) rather than committing it.

---

## Docker

```bash
# Pull and run
docker run -p 8000:8000 mtauha/psxdata-api

# Custom port
docker run -p 9000:9000 -e PORT=9000 mtauha/psxdata-api

# Build from source
docker build -t psxdata-api .
docker run -p 8000:8000 psxdata-api
```

The image runs as a non-root user (`psxuser`) and includes a `HEALTHCHECK` against `/health`.

---

## Local Development

```bash
cd api
pip install -e ".[dev]"
uvicorn api.main:app --reload
```

Run tests:
```bash
pytest
```

Lint and type-check:
```bash
ruff check .
mypy api/
```

Requires Python 3.11+.

---

## Related

- **[psxdata](https://github.com/psxdata/psxdata)** — Python library this service wraps
- **[psxdata on PyPI](https://pypi.org/project/psxdata/)** — installable package
- **[mtauha/psxdata-api on Docker Hub](https://hub.docker.com/r/mtauha/psxdata-api)** — Docker image
