"""Shared Pydantic models for psxdata API response envelopes."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MetaSingle(BaseModel):
    """Metadata block for single-item responses."""

    timestamp: str
    cached: bool


class MetaList(BaseModel):
    """Metadata block for list responses. Always includes count."""

    timestamp: str
    cached: bool
    count: int


class ErrorDetail(BaseModel):
    """Structured error payload."""

    status: int
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Top-level error response wrapper."""

    error: ErrorDetail


class HealthData(BaseModel):
    """Data payload for GET /health."""

    status: str


class HealthResponse(BaseModel):
    """Typed response model for GET /health."""

    data: HealthData
    meta: MetaSingle


class StringListResponse(BaseModel):
    data: list[str]
    meta: MetaList


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------

class OHLCVRow(BaseModel):
    date: str  # ISO 8601 date string
    open: float
    high: float
    low: float
    close: float
    volume: int
    is_anomaly: bool


class HistoricalResponse(BaseModel):
    data: list[OHLCVRow]
    meta: MetaList


class QuoteData(BaseModel):
    symbol: str
    price: float
    market_cap: float | None = None
    pe_ratio: float | None = None
    dividend_yield: float | None = None
    free_float: float | None = None
    volume_avg_30d: float | None = None
    change_1y_pct: float | None = None
    ldcp: float | None = None
    change: float | None = None
    change_pct: float | None = None
    traded_today: bool | None = Field(
        default=None,
        description=(
            "True if the stock has traded on PSX's regular-market board so far this session, "
            "false if not, null if unknown. PSX keeps delisted and non-trading stocks in its "
            "screener with their last trading day's price and change_pct."
        ),
    )
    sector_code: str | None = Field(
        default=None,
        description="PSX sector code as PSX writes it, e.g. \"0809\"; null if PSX gives none.",
    )
    sector_name: str | None = Field(
        default=None,
        description="Sector name for sector_code, e.g. \"FERTILIZER\"; null if unknown.",
    )


class QuoteResponse(BaseModel):
    data: QuoteData
    meta: MetaSingle


class FundamentalsRow(BaseModel):
    symbol: str
    year: str
    type: str
    period_ended: str
    posting_date: str
    document: str


class FundamentalsResponse(BaseModel):
    data: list[FundamentalsRow]
    meta: MetaList


# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------

class IndexConstituentRow(BaseModel):
    symbol: str
    current_index: float
    idx_weight: float
    idx_point: float
    market_cap_m: float
    freefloat_m: float | None = None
    shares_m: float | None = None


class IndexConstituentResponse(BaseModel):
    data: list[IndexConstituentRow]
    meta: MetaList


# ---------------------------------------------------------------------------
# Sectors
# ---------------------------------------------------------------------------

class SectorRow(BaseModel):
    sector_code: str
    sector_name: str
    advance: int
    decline: int
    unchanged: int
    turnover: int
    market_cap_b: float


class SectorsResponse(BaseModel):
    data: list[SectorRow]
    meta: MetaList


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

class ScreenerRow(BaseModel):
    symbol: str
    sector: float | None = None
    listed_in: str | None = None
    market_cap: float | None = None
    price: float | None = None
    pe_ratio: float | None = None
    dividend_yield: float | None = None
    free_float: float | None = None
    volume_avg_30d: float | None = None
    change_pct: float | None = None
    change_1y_pct: float | None = None
    traded_today: bool | None = Field(
        default=None,
        description=(
            "True if the stock has traded on PSX's regular-market board so far this session, "
            "false if not, null if unknown. Filter on it before ranking change_pct: PSX keeps "
            "delisted and non-trading stocks in its screener with their last trading day's move."
        ),
    )
    sector_code: str | None = Field(
        default=None,
        description="PSX sector code as PSX writes it, e.g. \"0809\"; null if PSX gives none.",
    )
    sector_name: str | None = Field(
        default=None,
        description="Sector name for sector_code, e.g. \"FERTILIZER\"; null if unknown.",
    )


class ScreenerResponse(BaseModel):
    data: list[ScreenerRow]
    meta: MetaList


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

class MarketTablesResponse(BaseModel):
    data: dict[str, list[dict[str, Any]]]
    meta: MetaSingle
