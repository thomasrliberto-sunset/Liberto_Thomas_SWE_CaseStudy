"""Bonus: Form 4 insider activity summary."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg

from app.models import InsiderSeller, InsiderSummary, NotFound
from app.services.companies import get_company


def insider_summary(conn: psycopg.Connection, ticker: str, days: int = 365) -> InsiderSummary:
    company = get_company(conn, ticker)
    # Anchor the window on the last successful ingest, not the wall clock, so a stored
    # snapshot keeps answering "the last 12 months" consistently.
    anchor = conn.execute(
        """
        SELECT max(finished_at)::date AS d FROM ingestion_run
        WHERE ticker = %s AND source = 'insiders' AND status = 'ok'
        """,
        (company["ticker"],),
    ).fetchone()["d"] or date.today()
    start = anchor - timedelta(days=days)
    rows = conn.execute(
        """
        SELECT insider_name, relationship, code, shares::float8 AS shares, price::float8 AS price, is_10b5_1
        FROM insider_transaction
        WHERE company_id = %s AND transaction_date > %s AND transaction_date <= %s
        """,
        (company["id"], start, anchor),
    ).fetchall()
    if not rows:
        raise NotFound(f"no Form 4 transactions for {company['ticker']} in the {days} days to {anchor}")

    def value(r: dict) -> float:
        return (r["shares"] or 0) * (r["price"] or 0)

    buys = [r for r in rows if r["code"] == "P"]
    sales = [r for r in rows if r["code"] == "S"]
    buy_value = sum(value(r) for r in buys)
    sale_value = sum(value(r) for r in sales)
    plan_value = sum(value(r) for r in sales if r["is_10b5_1"])

    sellers: dict[str, dict] = defaultdict(lambda: {"shares": 0.0, "value": 0.0, "plan": 0.0, "rel": None})
    for r in sales:
        s = sellers[r["insider_name"]]
        s["shares"] += r["shares"] or 0
        s["value"] += value(r)
        s["plan"] += value(r) if r["is_10b5_1"] else 0
        s["rel"] = r["relationship"]
    top = sorted(sellers.items(), key=lambda kv: kv[1]["value"], reverse=True)[:5]

    return InsiderSummary(
        ticker=company["ticker"],
        window_start=start,
        window_end=anchor,
        open_market_buys=len(buys),
        open_market_buy_value=buy_value,
        open_market_sales=len(sales),
        open_market_sale_value=sale_value,
        net_open_market_value=buy_value - sale_value,
        sale_value_under_10b5_1_pct=(plan_value / sale_value * 100) if sale_value else None,
        distinct_insiders=len({r["insider_name"] for r in rows}),
        other_activity=dict(Counter(r["code"] for r in rows if r["code"] not in {"P", "S"})),
        top_sellers=[
            InsiderSeller(
                insider=name, relationship=s["rel"], shares=s["shares"], value=s["value"],
                under_10b5_1_pct=(s["plan"] / s["value"] * 100) if s["value"] else None,
            )
            for name, s in top
        ],
        notes=[
            "Open-market activity only counts Form 4 codes P (purchase) and S (sale); grants (A), option "
            "exercises (M), tax withholding (F) and gifts (G) are listed under other_activity.",
            "10b5-1 flag comes from the filing's Rule 10b5-1 checkbox or a footnote citing a 10b5-1 plan.",
        ],
    )
