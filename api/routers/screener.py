"""Screener router — /screener endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, Depends, Request

from api.dependencies import limiter
from api.proxy import PsxSource, psx_source
from api.schemas import MetaList, ScreenerResponse, ScreenerRow

router = APIRouter(tags=["screener"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/screener", response_model=ScreenerResponse)
@limiter.limit("60/minute")
def list_screener(request: Request, psx: PsxSource = Depends(psx_source)) -> ScreenerResponse:
    df = psx.fetch("screener")
    rows: list[ScreenerRow] = []
    if not df.empty:
        df = df.astype(object).where(pd.notna(df), other=None)
        rows = [
            ScreenerRow(**{k: r.get(k) for k in ScreenerRow.model_fields})
            for r in df.to_dict("records")
        ]
    return ScreenerResponse(
        data=rows,
        meta=MetaList(timestamp=_now_iso(), cached=False, count=len(rows)),
    )
