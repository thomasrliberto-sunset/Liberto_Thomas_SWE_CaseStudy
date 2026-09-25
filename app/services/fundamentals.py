"""Read-side fundamentals: annual table with margins and YoY, and cross-company comparison.

Derived metrics are computed here at read time (not stored), from the canonical
reported values, so there's exactly one definition of each ratio.
"""

from __future__ import annotations

from collections import defaultdict

import psycopg

from app.config import MetricCatalog, load_metrics
from app.models import BadRequest, Comparison, ComparisonRow, FiscalPeriod, Fundamentals, MetricValue, NotFound
from app.services.companies import get_company
from app.services.metrics import consecutive_years, fmt_value, safe_ratio, yoy_growth

DEFAULT_METRICS = [
    "revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
    "gross_margin", "operating_margin", "net_margin", "free_cash_flow",
]


def _validate_metrics(metrics: list[str] | None, catalog: MetricCatalog) -> list[str]:
    if not metrics:
        return DEFAULT_METRICS
    unknown = [m for m in metrics if m not in catalog.all_names]
    if unknown:
        raise BadRequest(f"unknown metrics {unknown}; available: {catalog.all_names}")
    return metrics


def _load_rows(conn: psycopg.Connection, company_id: int) -> dict[int, dict[str, dict]]:
    rows = conn.execute(
        """
        SELECT metric, fiscal_year, period_start, period_end, value::float8 AS value, unit,
               source_concept, derivation, accession
        FROM annual_financial WHERE company_id = %s
        """,
        (company_id,),
    ).fetchall()
    by_year: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        by_year[r["fiscal_year"]][r["metric"]] = r
    return by_year


def _metric_value(
    name: str, year: int, by_year: dict[int, dict[str, dict]], catalog: MetricCatalog
) -> MetricValue | None:
    cur = by_year.get(year, {})
    prev = by_year.get(year - 1, {})

    if name in catalog.reported:
        r = cur.get(name)
        if r is None:
            return None
        p = prev.get(name)
        growth = (
            yoy_growth(r["value"], p["value"])
            if p and consecutive_years(p["period_end"], r["period_end"])
            else None
        )
        return MetricValue(
            value=r["value"],
            display=fmt_value(r["value"], r["unit"]),
            unit=r["unit"],
            yoy_growth=growth,
            source=r["source_concept"] or r["derivation"],
            derived=r["derivation"] is not None,
        )

    d = catalog.derived[name]

    def ratio(rows: dict[str, dict]) -> float | None:
        num, den = rows.get(d.numerator), rows.get(d.denominator)
        return safe_ratio(num and num["value"], den and den["value"])

    value = ratio(cur)
    if value is None:
        return None
    prior = ratio(prev) if prev.get(d.denominator) else None
    aligned = prev.get(d.denominator) and consecutive_years(
        prev[d.denominator]["period_end"], cur[d.denominator]["period_end"]
    )
    num_row = cur.get(d.numerator)
    return MetricValue(
        value=value,
        display=fmt_value(value, "ratio"),
        unit="ratio",
        yoy_change_pp=(value - prior) * 100 if prior is not None and aligned else None,
        source=f"{d.numerator} / {d.denominator}",
        derived=bool(num_row and num_row["derivation"]),
    )


def get_fundamentals(
    conn: psycopg.Connection,
    ticker: str,
    metrics: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    last_n: int = 5,
) -> Fundamentals:
    catalog = load_metrics()
    metrics = _validate_metrics(metrics, catalog)
    company = get_company(conn, ticker)
    by_year = _load_rows(conn, company["id"])
    if not by_year:
        raise NotFound(f"no fundamentals ingested for {ticker}")

    years = sorted((y for y in by_year if "revenue" in by_year[y] or len(by_year[y]) > 3), reverse=True)
    if fiscal_years:
        missing = sorted(set(fiscal_years) - set(years))
        years = [y for y in years if y in set(fiscal_years)]
        if not years:
            raise NotFound(f"{ticker}: no data for fiscal years {missing}; available {sorted(by_year)[-8:]}")
    else:
        years = years[: max(1, min(last_n, 20))]

    periods = []
    for y in years:
        anchor = by_year[y].get("revenue") or next(iter(by_year[y].values()))
        values = {m: v for m in metrics if (v := _metric_value(m, y, by_year, catalog)) is not None}
        periods.append(
            FiscalPeriod(
                fiscal_year=y,
                period_start=anchor["period_start"],
                period_end=anchor["period_end"],
                accession=anchor["accession"],
                metrics=values,
            )
        )
    return Fundamentals(
        ticker=company["ticker"], name=company["name"], fiscal_year_end=company["fiscal_year_end"], periods=periods
    )


def compare_metric(
    conn: psycopg.Connection,
    metric: str,
    fiscal_year: int | None = None,
    tickers: list[str] | None = None,
) -> Comparison:
    """Rank companies on one metric.

    Default basis is each company's *latest reported fiscal year*, which is what
    "last year" means to an analyst; fiscal years end in different months across
    this universe, so the response always states the period ends it compared.
    """
    catalog = load_metrics()
    if metric not in catalog.all_names:
        raise BadRequest(f"unknown metric {metric!r}; available: {catalog.all_names}")
    rows = conn.execute(
        "SELECT id, ticker FROM company WHERE %s::text[] IS NULL OR ticker = ANY(%s) ORDER BY ticker",
        (tickers, tickers),
    ).fetchall()
    if not rows:
        raise NotFound("no matching companies")

    results, missing = [], []
    for c in rows:
        by_year = _load_rows(conn, c["id"])
        if fiscal_year is not None:
            year = fiscal_year
        else:
            complete = [y for y in by_year if "revenue" in by_year[y]]
            year = max(complete) if complete else None
        mv = _metric_value(metric, year, by_year, catalog) if year is not None else None
        if mv is None or mv.value is None:
            missing.append(c["ticker"])
            continue
        anchor = by_year[year].get("revenue") or next(iter(by_year[year].values()))
        results.append((c["ticker"], year, anchor["period_end"], mv))

    results.sort(key=lambda r: r[3].value, reverse=True)
    ends = {r[2] for r in results}
    note = None
    if len(ends) > 1:
        spread = (max(ends) - min(ends)).days
        note = (
            f"Fiscal years are not calendar-aligned (period ends span {spread} days: "
            + ", ".join(f"{t} FY{y} ended {e.isoformat()}" for t, y, e, _ in results)
            + ")."
        )
    return Comparison(
        metric=metric,
        label=catalog.label(metric),
        basis=f"fiscal year {fiscal_year}" if fiscal_year else "each company's latest reported fiscal year",
        rows=[
            ComparisonRow(rank=i, ticker=t, fiscal_year=y, period_end=e, value=mv.value, display=mv.display)
            for i, (t, y, e, mv) in enumerate(results, start=1)
        ],
        missing=missing,
        alignment_note=note,
    )
