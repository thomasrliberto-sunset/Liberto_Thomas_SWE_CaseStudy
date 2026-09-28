"""Price ingestion without the network: parsing Yahoo's frame (bars and split events)
and falling back to Stooq's CSV when Yahoo fails or returns nothing."""

import sys
from datetime import date
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from app.ingest import prices

STOOQ_CSV = """Date,Open,High,Low,Close,Volume
2026-09-23,250.1,252.0,249.5,251.3,41000000
2026-09-24,251.0,,,,
2026-09-25,252.2,255.9,251.8,255.0,38500000.0
"""


def fake_yfinance(frame: pd.DataFrame) -> SimpleNamespace:
    return SimpleNamespace(Ticker=lambda ticker: SimpleNamespace(history=lambda **kwargs: frame))


def stooq_returning(text: str, calls: list[str]):
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, text=text, request=httpx.Request("GET", url))

    return get


def test_yahoo_rows_become_bars_and_split_events(monkeypatch):
    idx = pd.DatetimeIndex(["2024-06-07", "2024-06-10", "2024-06-11"], tz="America/New_York")
    frame = pd.DataFrame(
        {
            "Open": [1197.0, 120.4, 121.8],
            "High": [1216.9, 121.0, 122.5],
            "Low": [1180.2, 117.0, 118.9],
            "Close": [1208.9, 121.8, float("nan")],
            "Adj Close": [120.9, 121.8, float("nan")],
            "Volume": [41e6, 3.1e8, 2.0e8],
            "Stock Splits": [0.0, 10.0, 0.0],
        },
        index=idx,
    )
    monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(frame))
    history = prices.fetch_yfinance("NVDA", date(2024, 6, 1))

    assert history.source == "yahoo"
    assert [b.date for b in history.bars] == [date(2024, 6, 7), date(2024, 6, 10)]  # NaN close dropped
    assert history.bars[1].volume == 310_000_000 and history.bars[0].adj_close == 120.9
    assert history.splits == [prices.Split(date=date(2024, 6, 10), ratio=10.0)]  # needed by the P/E join


@pytest.mark.parametrize("yahoo_failure", ["raises", "empty", "no-valid-closes"])
def test_falls_back_to_stooq_when_yahoo_fails(monkeypatch, yahoo_failure):
    if yahoo_failure == "raises":

        def rate_limited(ticker, start):
            raise RuntimeError("Too Many Requests")

        monkeypatch.setattr(prices, "fetch_yfinance", rate_limited)
    elif yahoo_failure == "empty":
        monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(pd.DataFrame()))
    else:
        invalid = pd.DataFrame(
            {"Close": [float("nan")], "Stock Splits": [0.0]},
            index=pd.DatetimeIndex(["2026-09-25"]),
        )
        monkeypatch.setitem(sys.modules, "yfinance", fake_yfinance(invalid))
    calls: list[str] = []
    monkeypatch.setattr(prices.httpx, "get", stooq_returning(STOOQ_CSV, calls))

    history = prices.fetch_prices("NVDA", years=1)

    assert history.source == "stooq" and history.splits == []
    assert [b.date for b in history.bars] == [date(2026, 9, 23), date(2026, 9, 25)]  # row without a close skipped
    assert history.bars[0].close == 251.3 and history.bars[0].adj_close is None
    assert [b.volume for b in history.bars] == [41_000_000, 38_500_000]
    assert "s=nvda.us" in calls[0] and "d1=" in calls[0]


def test_yahoo_success_never_calls_stooq(monkeypatch):
    bar = prices.PriceBar(date(2026, 9, 25), 252.2, 255.9, 251.8, 255.0, 255.0, 38_500_000)
    monkeypatch.setattr(prices, "fetch_yfinance", lambda ticker, start: prices.PriceHistory("yahoo", [bar], []))

    def stooq_must_not_run(ticker, start):
        raise AssertionError("stooq should not be called when Yahoo works")

    monkeypatch.setattr(prices, "fetch_stooq", stooq_must_not_run)
    assert prices.fetch_prices("NVDA", years=1).source == "yahoo"


def test_stooq_error_page_is_not_mistaken_for_data(monkeypatch):
    monkeypatch.setattr(prices.httpx, "get", stooq_returning("No data", []))
    with pytest.raises(RuntimeError, match="no usable data"):
        prices.fetch_stooq("NVDA", date(2026, 1, 1))


def test_stooq_rows_without_a_valid_close_are_rejected(monkeypatch):
    invalid = "Date,Open,High,Low,Close,Volume\n2026-09-25,252.2,255.9,251.8,,38500000\n"
    monkeypatch.setattr(prices.httpx, "get", stooq_returning(invalid, []))
    with pytest.raises(RuntimeError, match="no usable data"):
        prices.fetch_stooq("NVDA", date(2026, 1, 1))
