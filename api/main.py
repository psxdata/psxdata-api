import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psxdata.exceptions import (
    InvalidSymbolError,
    PSXParseError,
    PSXRateLimitError,
    PSXUnavailableError,
)
from pydantic import ValidationError
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.cache.factory import build_historical_service
from api.cache.historical import COOLDOWN_MESSAGE, PSX_COOLDOWN_SECONDS
from api.dependencies import limiter
from api.proxy import ProxyPassthrough, ProxyUnreachableError
from api.routers import router_registry
from api.telemetry import TELEMETRY, ServerSpanTags
from api.upstream import UpstreamBudget, UpstreamBudgetExceeded

logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the /historical cache and PSX budgets on startup; close the cache on shutdown."""
    service = build_historical_service(os.environ)
    app.state.historical_service = service
    app.state.proxy_passthrough = ProxyPassthrough.from_env(os.environ)
    app.state.upstream_budget = UpstreamBudget.from_env(os.environ)
    try:
        yield
    finally:
        try:
            service.close()
        finally:
            TELEMETRY.flush()


app = FastAPI(
    title="psxdata",
    lifespan=lifespan,
    servers=[{"url": "https://psxdata-api.fastapicloud.dev"}],
    telemetry=TELEMETRY.config,
)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(ServerSpanTags)

app.add_middleware(
    CORSMiddleware,
    # TODO: replace wildcard origin with explicit origins before production.
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    404: "not_found",
    422: "bad_request",
    429: "rate_limited",
    502: "upstream_data_error",
    503: "psx_unavailable",
    500: "internal_error",
}


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": {"status": 429, "code": "rate_limited", "message": "Rate limit exceeded"}
        },
    )


@app.exception_handler(UpstreamBudgetExceeded)
async def upstream_budget_handler(request: Request, exc: UpstreamBudgetExceeded) -> JSONResponse:
    code = _ERROR_CODES[exc.status_code]
    return JSONResponse(
        status_code=exc.status_code,
        headers={"Retry-After": str(exc.retry_after)},
        content={"error": {"status": exc.status_code, "code": code, "message": str(exc)}},
    )


@app.exception_handler(PSXUnavailableError)
async def psx_unavailable_handler(request: Request, exc: PSXUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"status": 503, "code": _ERROR_CODES[503], "message": str(exc)}},
    )


@app.exception_handler(PSXRateLimitError)
async def psx_rate_limit_handler(request: Request, exc: PSXRateLimitError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(PSX_COOLDOWN_SECONDS)},
        content={"error": {"status": 503, "code": _ERROR_CODES[503], "message": COOLDOWN_MESSAGE}},
    )


@app.exception_handler(ProxyUnreachableError)
async def proxy_unreachable_handler(request: Request, exc: ProxyUnreachableError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"error": {"status": 502, "code": "proxy_unreachable", "message": str(exc)}},
    )


@app.exception_handler(InvalidSymbolError)
async def invalid_symbol_handler(request: Request, exc: InvalidSymbolError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": {"status": 404, "code": _ERROR_CODES[404], "message": str(exc)}},
    )


@app.exception_handler(PSXParseError)
async def psx_parse_error_handler(request: Request, exc: PSXParseError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"status": 400, "code": _ERROR_CODES[400], "message": str(exc)}},
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_error_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    logger.exception(
        "Response data failed pydantic validation on %s", request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "status": 502,
                "code": _ERROR_CODES[502],
                "message": "Upstream data did not match the expected format",
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _ERROR_CODES.get(exc.status_code, "internal_error")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"status": exc.status_code, "code": code, "message": str(exc.detail)}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    parts = []
    for e in exc.errors():
        loc = " -> ".join(str(part) for part in e.get("loc", []) if part != "body")
        msg = e.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    message = "; ".join(parts) if parts else "invalid input"
    return JSONResponse(
        status_code=422,
        content={"error": {"status": 422, "code": "bad_request", "message": message}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s", request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"status": 500, "code": "internal_error", "message": "Internal Server Error"}},  # noqa: E501
    )


for router in router_registry:
    app.include_router(router)
