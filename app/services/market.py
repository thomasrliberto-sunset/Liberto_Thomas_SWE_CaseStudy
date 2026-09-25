"""Prices and the cross-source valuation join (EDGAR fundamentals x market data)."""

from __future__ import annotations

from datetime import date

import psycopg

from app.models import NotFound, PriceBar, PriceSeries, Valuation, ValuationPoint
from app.services.companies import get_company
from app.services.metrics import pe_ratio, safe_ratio, split_factor


def get_prices(
    conn: psycopg.Connection, ticker: str, start: date | None = None, end: date | None = None,
    include_bars: bool = True,
) -> PriceSeries:
    company = get_company(conn, ticker)
    rows = conn.execute(
        """
        SELECT date, open::float8, high::float8, low::float8, close::float8, adj_close::float8, volume, source
        FROM price_daily
        WHERE company_id = %s AND (%s::date IS NULL OR date >= %s) AND (%s::date IS NULL OR date <= %s)
        ORDER BY date
        """,
        (company["id"], start, start, end, end),
    ).fetchall()
    if not rows:
        raise NotFound(f"no prices for {company['ticker']} in the requested window")
    first, last = rows[0], rows[-1]
    return PriceSeries(
        ticker=company["ticker"],
        source=last["source"],
        start=first["date"],
        end=last["date"],
        first_close=first["close"],
        last_close=last["close"],
        change_pct=(last["close"] / first["close"] - 1) * 100 if first["close"] else None,
        high=max(r["high"] or r["close"] for r in rows),
        low=min(r["low"] or r["close"] for r in rows),
        bars=[PriceBar(**{k: r[k] for k in PriceBar.model_fields}) for r in rows] if include_bars else [],
    )


def _annual(conn: psycopg.Connection, company_id: int, metric: str) -> list[dict]:
    return conn.execute(
        """
        SELECT fiscal_year, period_end, value::float8 AS value, filed, accession
        FROM annual_financial WHERE company_id = %s AND metric = %s ORDER BY fiscal_year DESC
        """,
        (company_id, metric),
    ).fetchall()


def get_valuation(conn: psycopg.Connection, ticker: str, history_years: int = 5) -> Valuation:
    """Trailing P/E and P/S from the latest close and the latest *annual* 10-K figures.

    The join has two alignment problems, both handled explicitly:
      * share basis: EPS is reported on the share count at filing time, Yahoo closes are
        split-adjusted to today -> divide EPS by splits that happened after the filing.
      * staleness: the latest annual EPS can be up to ~15 months old; we report the
        fiscal year and period end so the reader can judge.
    """
    company = get_company(conn, ticker)
    cid = company["id"]
    px = conn.execute(
        "SELECT date, close::float8 AS close, source FROM price_daily WHERE company_id = %s ORDER BY date DESC LIMIT 1",
        (cid,),
    ).fetchone()
    if px is None:
        raise NotFound(f"no prices ingested for {company['ticker']}")
    eps_rows = _annual(conn, cid, "eps_diluted")
    if not eps_rows:
        raise NotFound(f"no diluted EPS ingested for {company['ticker']}")
    splits = [
        (r["date"], float(r["ratio"]))
        for r in conn.execute("SELECT date, ratio FROM stock_split WHERE company_id = %s", (cid,)).fetchall()
    ]

    eps = eps_rows[0]
    factor = split_factor(splits, eps["filed"])
    eps_adj = eps["value"] / factor

    revenue = next((r for r in _annual(conn, cid, "revenue") if r["fiscal_year"] == eps["fiscal_year"]), None)
    shares = next((r for r in _annual(conn, cid, "diluted_shares") if r["fiscal_year"] == eps["fiscal_year"]), None)
    shares_adj = shares["value"] * split_factor(splits, shares["filed"]) if shares else None
    market_cap = px["close"] * shares_adj if shares_adj else None

    notes = [
        f"Price is the {px['source']} close on {px['date'].isoformat()} (latest in the database).",
        f"EPS is diluted EPS for FY{eps['fiscal_year']} (period ended {eps['period_end'].isoformat()}), "
        "the latest annual figure; trailing-twelve-month EPS from 10-Qs is not used.",
    ]
    if factor != 1.0:
        notes.append(f"EPS divided by {factor:g} for stock splits after the 10-K was filed.")
    if eps_adj <= 0:
        notes.append("P/E is not meaningful because EPS is not positive.")
    if market_cap is not None:
        notes.append("Market cap and P/S use FY weighted-average diluted shares, not current shares outstanding.")

    history = []
    for r in eps_rows[:history_years]:
        p = conn.execute(
            """
            SELECT date, close::float8 AS close FROM price_daily
            WHERE company_id = %s AND date <= %s ORDER BY date DESC LIMIT 1
            """,
            (cid, r["period_end"]),
        ).fetchone()
        adj = r["value"] / split_factor(splits, r["filed"])
        history.append(
            ValuationPoint(
                fiscal_year=r["fiscal_year"],
                period_end=r["period_end"],
                price=p["close"] if p else None,
                price_date=p["date"] if p else None,
                eps_diluted=adj,
                pe=pe_ratio(p["close"], adj) if p else None,
            )
        )

    return Valuation(
        ticker=company["ticker"],
        price=px["close"],
        price_date=px["date"],
        price_source=px["source"],
        eps_fiscal_year=eps["fiscal_year"],
        eps_period_end=eps["period_end"],
        eps_diluted_reported=eps["value"],
        split_adjustment=factor,
        eps_diluted_adjusted=eps_adj,
        trailing_pe=pe_ratio(px["close"], eps_adj),
        revenue=revenue["value"] if revenue else None,
        diluted_shares_adjusted=shares_adj,
        market_cap_approx=market_cap,
        price_to_sales=safe_ratio(market_cap, revenue["value"] if revenue else None),
        notes=notes,
        history=history,
    )
