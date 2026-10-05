"""Stocks router — /stocks and /stocks/{symbol}/* endpoints."""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api.cache.historical import CacheStatus
from api.dependencies import get_cache, limiter
from api.proxy import PsxSource, psx_source
from api.schemas import (
    ErrorEnvelope,
    FundamentalsResponse,
    FundamentalsRow,
    HistoricalResponse,
    MetaList,
    MetaSingle,
    OHLCVRow,
    QuoteData,
    QuoteResponse,
    StringListResponse,
)

router = APIRouter(tags=["stocks"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to JSON-safe records: Timestamps to ISO strings, NaN to None."""
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].apply(lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else None)
    df = df.where(pd.notna(df), other=None)
    return df.to_dict("records")


_HISTORICAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            "X-Cache": {
                "description": "HIT (fresh cache), MISS (fetched from PSX), or STALE "
                "(PSX refused or was unreachable; last cached copy served)",
                "schema": {"type": "string", "enum": ["HIT", "MISS", "STALE"]},
            },
            "Age": {
                "description": "Seconds since the data was fetched from PSX (HIT and STALE only)",
                "schema": {"type": "integer"},
            },
        },
    },
    503: {
        "model": ErrorEnvelope,
        "description": "PSX is unavailable or rate-limiting, and no cached copy exists",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying (sent when PSX is rate-limiting)",
                "schema": {"type": "integer"},
            },
        },
    },
}


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_date(name: str, value: str | None) -> date | None:
    if value is None:
        return None
    # fromisoformat also accepts 20240102 and 2024-W01-2; the documented contract is YYYY-MM-DD
    if _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise HTTPException(status_code=422, detail=f"{name} must be an ISO date (YYYY-MM-DD)")


def _full_history_fetcher(psx: PsxSource) -> Callable[[str], list[dict]]:
    """Fetch a symbol's full history from PSX, through the caller's proxy if one was given."""
    def fetch(symbol: str) -> list[dict]:
        return _df_to_records(psx.fetch("stocks", symbol, cache=False))
    return fetch


@router.get("/stocks", response_model=StringListResponse)
@limiter.limit("60/minute")
def list_stocks(
    request: Request, index: str | None = None, psx: PsxSource = Depends(psx_source)
) -> StringListResponse:
    tickers = psx.fetch("tickers", index=index)
    return StringListResponse(
        data=tickers,
        meta=MetaList(timestamp=_now_iso(), cached=False, count=len(tickers)),
    )


@router.get(
    "/stocks/{symbol}/historical",
    response_model=HistoricalResponse,
    responses=_HISTORICAL_RESPONSES,
)
@limiter.limit("60/minute")
def get_historical(
    request: Request,
    response: Response,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    psx: PsxSource = Depends(psx_source),
) -> HistoricalResponse:
    start_date = _parse_date("start", start)
    end_date = _parse_date("end", end)
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start must not be after end")

    result = get_cache(request).get(symbol.upper(), _full_history_fetcher(psx))

    lo = start_date.isoformat() if start_date else None
    hi = end_date.isoformat() if end_date else None
    rows = [
        OHLCVRow.model_validate(r)
        for r in result.rows
        if (lo is None or r["date"] >= lo) and (hi is None or r["date"] <= hi)
    ]

    cached = result.status is not CacheStatus.MISS
    response.headers["X-Cache"] = result.status.value
    if cached:
        age = (datetime.now(timezone.utc) - result.fetched_at).total_seconds()
        response.headers["Age"] = str(max(0, int(age)))
    return HistoricalResponse(
        data=rows,
        meta=MetaList(timestamp=_now_iso(), cached=cached, count=len(rows)),
    )


# While a corporate action is pending, PSX lists a stock under a suffixed ticker (LUCK trades as
# LUCKXD once it goes ex-dividend) and drops the suffix again afterwards. XD = ex-dividend,
# XB = ex-bonus, XR = ex-rights.
_CORPORATE_ACTION_SUFFIXES = ("XD", "XB", "XR")


def _quote_aliases(symbol: str) -> list[str]:
    """Other tickers the same stock may be listed under, in lookup order."""
    base = symbol
    if len(symbol) > 2 and symbol.endswith(_CORPORATE_ACTION_SUFFIXES):
        base = symbol[:-2]
    candidates = [base + suffix for suffix in _CORPORATE_ACTION_SUFFIXES] + [base]
    return [c for c in candidates if c != symbol]


def _find_quote(psx: PsxSource, symbol: str) -> pd.DataFrame:
    df = psx.fetch("quote", symbol)
    if not df.empty:
        return df
    # quote() just loaded the screener into psxdata's cache, so this does not call PSX again
    screener = psx.fetch("screener")
    if screener.empty or "symbol" not in screener.columns:
        return df
    for alias in _quote_aliases(symbol):
        match = screener[screener["symbol"] == alias]
        if not match.empty:
            return match.reset_index(drop=True)
    return df


@router.get("/stocks/{symbol}/quote", response_model=QuoteResponse)
@limiter.limit("60/minute")
def get_quote(request: Request, symbol: str, psx: PsxSource = Depends(psx_source)) -> QuoteResponse:
    df = _find_quote(psx, symbol.upper())
    if df.empty:
        raise HTTPException(status_code=404, detail=f"{symbol.upper()} not found")
    row = _df_to_records(df)[0]
    data = QuoteData.model_validate(row)
    return QuoteResponse(
        data=data,
        meta=MetaSingle(timestamp=_now_iso(), cached=False),
    )


@router.get("/stocks/{symbol}/fundamentals", response_model=FundamentalsResponse)
@limiter.limit("60/minute")
def get_fundamentals(
    request: Request, symbol: str, psx: PsxSource = Depends(psx_source)
) -> FundamentalsResponse:
    df = psx.fetch("fundamentals", symbol=symbol.upper())
    rows: list[FundamentalsRow] = []
    if not df.empty:
        rows = [FundamentalsRow.model_validate(r) for r in _df_to_records(df)]
    return FundamentalsResponse(
        data=rows,
        meta=MetaList(timestamp=_now_iso(), cached=False, count=len(rows)),
    )
