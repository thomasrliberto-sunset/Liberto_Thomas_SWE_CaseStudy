"""Daily prices and splits. yfinance is the primary provider; Stooq's CSV endpoint is
a fallback because Yahoo rate-limits unauthenticated clients unpredictably.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceBar:
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    adj_close: float | None
    volume: int | None


@dataclass(frozen=True)
class Split:
    date: date
    ratio: float


@dataclass(frozen=True)
class PriceHistory:
    source: str
    bars: list[PriceBar]
    splits: list[Split]


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v  # NaN -> None


def fetch_yfinance(ticker: str, start: date) -> PriceHistory:
    import yfinance as yf

    df = yf.Ticker(ticker).history(start=start.isoformat(), auto_adjust=False, actions=True)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no rows for {ticker}")
    bars, splits = [], []
    for ts, row in df.iterrows():
        d = ts.date()
        close = _num(row.get("Close"))
        if close is None:
            continue
        vol = _num(row.get("Volume"))
        bars.append(
            PriceBar(
                date=d,
                open=_num(row.get("Open")),
                high=_num(row.get("High")),
                low=_num(row.get("Low")),
                close=close,
                adj_close=_num(row.get("Adj Close")),
                volume=int(vol) if vol is not None else None,
            )
        )
        ratio = _num(row.get("Stock Splits"))
        if ratio and ratio > 0:
            splits.append(Split(date=d, ratio=ratio))
    return PriceHistory(source="yahoo", bars=bars, splits=splits)


def fetch_stooq(ticker: str, start: date) -> PriceHistory:
    """Stooq daily CSV (split-adjusted closes, no split events)."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d&d1={start:%Y%m%d}"
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    if not rows or "Close" not in rows[0]:
        raise RuntimeError(f"stooq returned no usable data for {ticker}")
    bars = [
        PriceBar(
            date=date.fromisoformat(r["Date"]),
            open=_num(r.get("Open")),
            high=_num(r.get("High")),
            low=_num(r.get("Low")),
            close=float(r["Close"]),
            adj_close=None,
            volume=int(float(r["Volume"])) if r.get("Volume") else None,
        )
        for r in rows
        if _num(r.get("Close")) is not None
    ]
    return PriceHistory(source="stooq", bars=bars, splits=[])


def fetch_prices(ticker: str, years: int) -> PriceHistory:
    start = date.today() - timedelta(days=365 * years + 30)
    try:
        return fetch_yfinance(ticker, start)
    except Exception as exc:  # provider failures are expected; fall back
        log.warning("yfinance failed for %s (%s); falling back to stooq", ticker, exc)
        return fetch_stooq(ticker, start)
