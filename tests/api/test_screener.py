"""Unit tests for screener router."""
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from psxdata.exceptions import PSXUnavailableError

from api.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_list_screener_returns_200(client: TestClient) -> None:
    df = pd.DataFrame({
        "symbol": ["ENGRO", "LUCK"],
        "sector": [8.0, 6.0],
        "listed_in": ["REGULAR", "REGULAR"],
        "market_cap": [1.2e9, 5.6e8],
        "price": [250.5, 900.1],
        "pe_ratio": [8.2, 12.4],
        "dividend_yield": [0.045, 0.02],
        "free_float": [35.0, 40.0],
        "volume_avg_30d": [1500000.0, 800000.0],
        "change_1y_pct": [12.3, -4.5],
    })
    with patch("psxdata.screener", return_value=df):
        resp = client.get("/screener")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["data"][0]["symbol"] == "ENGRO"
    assert body["meta"]["count"] == 2


def test_list_screener_empty_returns_200(client: TestClient) -> None:
    with patch("psxdata.screener", return_value=pd.DataFrame()):
        resp = client.get("/screener")
    assert resp.status_code == 200
    assert resp.json()["data"] == []
    assert resp.json()["meta"]["count"] == 0


def test_screener_psx_unavailable_returns_503(client: TestClient) -> None:
    with patch("psxdata.screener", side_effect=PSXUnavailableError("PSX down")):
        resp = client.get("/screener")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "psx_unavailable"


def test_list_screener_includes_change_pct_and_traded_today(client: TestClient) -> None:
    """traded_today (nullable boolean from psxdata >= 1.3.0) and change_pct pass through;
    <NA> becomes null."""
    df = pd.DataFrame({
        "symbol": ["ENGRO", "FFBL", "LUCK"],
        "price": [250.5, 88.94, 900.1],
        "change_pct": [1.2, 8.07, -0.5],
        "traded_today": pd.array([True, False, pd.NA], dtype="boolean"),
    })
    with patch("psxdata.screener", return_value=df):
        resp = client.get("/screener")
    assert resp.status_code == 200
    rows = {r["symbol"]: r for r in resp.json()["data"]}
    assert rows["ENGRO"]["traded_today"] is True
    assert rows["FFBL"]["traded_today"] is False
    assert rows["FFBL"]["change_pct"] == 8.07
    assert rows["LUCK"]["traded_today"] is None


def test_list_screener_without_traded_today_column(client: TestClient) -> None:
    """Older psxdata (no traded_today column) still works: field is null."""
    with patch("psxdata.screener", return_value=pd.DataFrame({"symbol": ["ENGRO"], "price": [1.0]})):
        resp = client.get("/screener")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["traded_today"] is None


def test_list_screener_includes_sector_code_and_name(client: TestClient) -> None:
    """sector_code/sector_name (StringDtype from psxdata >= 1.4.0) pass through; <NA> becomes null."""
    df = pd.DataFrame({
        "symbol": ["FFC", "PIAA"],
        "sector": [809.0, float("nan")],
        "price": [400.0, 22.17],
        "sector_code": pd.array(["0809", pd.NA], dtype="string"),
        "sector_name": pd.array(["FERTILIZER", pd.NA], dtype="string"),
    })
    with patch("psxdata.screener", return_value=df):
        resp = client.get("/screener")
    assert resp.status_code == 200
    rows = {r["symbol"]: r for r in resp.json()["data"]}
    assert (rows["FFC"]["sector_code"], rows["FFC"]["sector_name"]) == ("0809", "FERTILIZER")
    assert rows["PIAA"]["sector_code"] is None and rows["PIAA"]["sector_name"] is None
