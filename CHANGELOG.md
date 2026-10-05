# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

- `/stocks/{symbol}/quote` no longer returns 404 while a stock has a pending corporate action. PSX lists such stocks under a suffixed ticker (`LUCK` as `LUCKXD` once it goes ex-dividend; `XB` ex-bonus, `XR` ex-rights), so the quote now falls back to that ticker, and a suffixed request falls back to the plain one once the suffix is dropped. The response's `symbol` is the ticker PSX currently lists. The fallback reads the already-cached screener and makes no extra PSX request.

---

## [0.5.0] — 2026-10-02

### Added

- OpenTelemetry telemetry using FastAPI's native support. Spans are written to stdout as one JSON object per line, so they are written to stdout for the existing log export.
- Custom spans: `cache.historical` (HIT, MISS or STALE, memory/redis/none tier, cooldown) and `psx.fetch` (SDK function, proxied, cache-only hit).
- Request spans carry the client address and service version. `/health` is excluded and no request headers are recorded.
- `PSX_TELEMETRY=off` disables telemetry.

### Changed

- Requires `fastapi[standard]>=0.142.0,<0.143` and adds `opentelemetry-sdk>=1.45,<1.46`. Both are capped because the code uses `fastapi.telemetry` (new in 0.142) and the private `opentelemetry.sdk._logs` module.
- Every request is traced regardless of an incoming `traceparent` sampling flag.
- The reported `service.version` now comes from `api.__version__`, the single source of the package version.

---

## [0.4.0] — 2026-09-30

### Added

- Per-request proxy passthrough. An optional `X-PSX-Proxy` header (`http://`, `socks5://` or `socks5h://`, with an explicit port and optional `user:pass@`) makes the API fetch that request's PSX data, including the request token, through the caller's proxy, using psxdata 1.2.0's proxy support. Off by default. Enable it with `PSX_PROXY_PASSTHROUGH=true`.
- Hardening against server-side request forgery (SSRF) for the proxy:
  - the host must resolve only to public addresses, and the connection is pinned to the checked IP (defeats DNS rebinding);
  - `https://` proxies and ports below 1024 (other than 80 and 443) are refused;
  - a 5-second TCP check runs first (`502 proxy_unreachable` on failure);
  - limits of 10 proxied PSX fetches per minute per IP and 4 concurrent server-wide;
  - a bounded pool of proxied clients.
- The proxy is only used on a cache miss: cached data is served without resolving or contacting the caller's proxy, and cache hits don't count toward the proxy rate limit. A malformed header, or one sent while the feature is off, is still rejected on every request.
- Proxied requests use the same caches as all other requests. The proxy only changes the route to PSX: cached data is served without contacting PSX, and data fetched through a proxy is cached for everyone. PSX is HTTPS-only with certificate verification, so a proxy cannot alter what it relays.
- Proxy credentials are never logged or echoed in error messages.

### Changed

- Requires `psxdata[socks]==1.2.0` (was `psxdata==1.1.1`).

---

## [0.3.0] — 2026-09-30

### Added

- `GET /stocks/{symbol}/historical` is now cached. Each symbol's full history is fetched from PSX once and reused for every `start`/`end` range. It stays fresh for 30 minutes during PSX trading hours (Mon–Fri 09:00–17:00 PKT) and until the next trading-day open otherwise. Responses carry `X-Cache: HIT | MISS | STALE`, an `Age` header on cached responses, and `meta.cached: true` when served from cache.
- When PSX rate-limits the API or is unreachable, `/historical` serves the last cached copy (`X-Cache: STALE`) instead of failing.
- Optional `REDIS_URL` environment variable: any Redis-compatible server (e.g. Aiven for Valkey) that keeps the cache across restarts and instances. Without it the cache is in-memory only. `HISTORICAL_CACHE_MARKET_TTL` (seconds, default `1800`) tunes trading-hours freshness.

### Fixed

- PSX rate-limiting (HTTP 429 from PSX) surfaced as `500 internal_error` on every endpoint (324 such responses on `/historical` on 2026-09-29). It now returns `503 psx_unavailable` with `Retry-After: 60`, and `/historical` stops calling PSX for 60 seconds after a 429.
- Invalid `start`/`end` dates, or `start` after `end`, on `/historical` returned `500`; they now return `422 bad_request`.

### Changed

- `start`/`end` on `/historical` must be ISO dates (`YYYY-MM-DD`).
- The OpenAPI spec is pushed to the docs site on release tags (`v*`) instead of every push to `main`, so public docs change only when a release is made.

---

## [0.2.1] — 2026-09-26

### Fixed

- Every data endpoint (`/stocks`, `/indices`, `/sectors`, `/sectors/{name}/stocks`, `/screener`) was returning `502 upstream_data_error` since 2026-09-24, because PSX started requiring an `X-Req-Id` request token on its data endpoints and the pinned `psxdata` version had no way to send one. Bumped the `psxdata` pin to `1.1.1`, which fetches and sends the token automatically ([mtauha/psxdata#161](https://github.com/psxdata/psxdata/issues/161)). No API changes on our side.

---

## [0.2.0] — 2026-09-02

### Added

- `GET /screener` endpoint — returns the full, unfiltered PSX screener table (~729 symbols, all columns), following the same shape as `GET /sectors`. Backed by the new `psxdata.screener()` SDK function.

### Changed

- Bumped the `psxdata` pin to `1.1.0`, which ships `screener()`.

---

## [0.1.3] — 2026-07-03

### Fixed

- `GET /stocks/{symbol}/quote` no longer fails with a `502 upstream_data_error` for every symbol. Root cause was in the `psxdata` SDK: the screener scraper never coerced `change_pct` to a float, leaving it as a percent-suffixed string (e.g. `"1.24%"`) that failed `QuoteData` validation. Bumped the `psxdata` pin to `0.1.0a5`, which fixes the coercion at the source.

---

## [0.1.2] — 2026-07-03

### Fixed

- `GET /sectors/{name}/stocks` no longer returns a bare `500 Internal Server Error`. It called `psxdata.symbols()`, which existed in the SDK's git source but had never been published to PyPI. Bumped the `psxdata` pin to `0.1.0a4`, which ships it (fixes [#1](https://github.com/psxdata/psxdata-api/issues/1)).

### Changed

- `psxdata` dependency pin bumped from `0.1.0a3` to `0.1.0a4`.

---

## [0.1.1] — 2026-07-03

### Fixed

- `GET /stocks?index=<invalid>` no longer returns a bare `500 Internal Server Error`. `PSXParseError` (raised when PSX rejects an unknown index name) is now caught and returns `400 bad_request` with a descriptive message (fixes [#2](https://github.com/psxdata/psxdata-api/issues/2)).
- Pydantic validation failures when building response models from upstream PSX data (e.g. a missing/renamed field) now return `502 upstream_data_error` instead of an opaque `500`, and are logged server-side via `logger.exception` for diagnosis.
- The generic unhandled-exception handler now logs the exception instead of silently discarding it.

---

## [0.1.0] — 2026-06-25

### Added

- Initial release — REST API service extracted from [mtauha/psxdata](https://github.com/psxdata/psxdata) with full git history preserved via `git filter-repo`.
- `GET /health` — liveness check returning `{"data": {"status": "ok"}, "meta": {"timestamp": ..., "cached": false}}`.
- `GET /stocks` — real-time trading panel data across all 15 board combinations.
- `GET /indices` — all 18 PSX index values.
- `GET /sectors` — 37 sector summaries.
- `GET /sectors/{name}/stocks` — symbol lookup filtered by sector.
- Standardised response envelope: `{"data": ..., "meta": {"count": N}}` for list endpoints; `{"error": {"status", "code", "message"}}` for errors.
- Six Pydantic v2 response models in `api/schemas.py`: `MetaSingle`, `MetaList`, `ErrorDetail`, `ErrorEnvelope`, `HealthData`, `HealthResponse`.
- Slowapi rate-limiting middleware (60 req/min per IP by default).
- Multi-stage `Dockerfile`: `builder` stage installs deps into a venv; `runtime` stage copies only the venv and `api/` source. Runs as non-root `psxuser`. Supports `PORT` env var. Includes `HEALTHCHECK`.
- `.dockerignore` stripping all non-essential paths from the build context.
- CI workflow: `ruff` + `mypy` lint, `pytest` test matrix (Python 3.11/3.12), docs smoke-test (uvicorn boot + curl `/health`, `/docs`, `/redoc`).
- Docker Hub publish workflow: builds and pushes `mtauha/psxdata-api:latest` and `:<version>` on `v*` tag push.
- FastAPI Cloud auto-deploy configured via `[tool.fastapi] entrypoint = "api.main:app"` in `pyproject.toml`.

### Known Issues

- `GET /sectors/{name}/stocks` returns an empty list — `psxdata.symbols()` is not yet part of the public `psxdata` API (tracked in [#1](https://github.com/psxdata/psxdata-api/issues/1)).

---

[0.2.1]: https://github.com/psxdata/psxdata-api/releases/tag/v0.2.1
[0.2.0]: https://github.com/psxdata/psxdata-api/releases/tag/v0.2.0
[0.1.3]: https://github.com/psxdata/psxdata-api/releases/tag/v0.1.3
[0.1.2]: https://github.com/psxdata/psxdata-api/releases/tag/v0.1.2
[0.1.1]: https://github.com/psxdata/psxdata-api/releases/tag/v0.1.1
[0.1.0]: https://github.com/psxdata/psxdata-api/releases/tag/v0.1.0
