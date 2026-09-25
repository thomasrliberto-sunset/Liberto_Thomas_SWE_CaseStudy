"""Data-quality report: explicit, per-ticker checks on what was ingested.

The brief expects messy data. Rather than hide the workarounds (derived metrics, section
fallbacks, split adjustments), this surfaces them next to consistency checks a reviewer
or analyst would otherwise have to run by hand.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.config import load_metrics
from app.db.connection import DbConn
from app.services.companies import get_company

Status = Literal["ok", "info", "warn"]


class Check(BaseModel):
    check: str
    status: Status
    detail: str


class QualityReport(BaseModel):
    ticker: str
    status: Status
    checks: list[Check]


def _worst(checks: list[Check]) -> Status:
    order: dict[Status, int] = {"ok": 0, "info": 1, "warn": 2}
    worst: Status = "ok"
    for c in checks:
        if order[c.status] > order[worst]:
            worst = c.status
    return worst


def _rel_diff(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-9)


def quality_report(conn: DbConn, ticker: str, as_of: date | None = None) -> QualityReport:
    company = get_company(conn, ticker)
    cid = company["id"]
    checks: list[Check] = []

    rows = conn.execute(
        """
        SELECT metric, fiscal_year, period_end, value::float8 AS value, derivation
        FROM annual_financial WHERE company_id = %s
        """,
        (cid,),
    ).fetchall()
    by_year: dict[int, dict[str, dict]] = {}
    for r in rows:
        by_year.setdefault(r["fiscal_year"], {})[r["metric"]] = r
    years = sorted((y for y in by_year if "revenue" in by_year[y]), reverse=True)
    if not years:
        return QualityReport(
            ticker=company["ticker"], status="warn", checks=[Check(check="fundamentals", status="warn", detail="none")]
        )
    latest = years[0]
    cur = by_year[latest]

    # 1. coverage of the metrics the API and agent rely on
    core = ["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted", "free_cash_flow"]
    missing = [m for m in core if m not in cur]
    checks.append(
        Check(
            check="latest_fiscal_year_coverage",
            status="warn" if missing else "ok",
            detail=f"FY{latest}: " + (f"missing {missing}" if missing else f"all {len(core)} core metrics present"),
        )
    )

    # 2. derived (not directly reported) values, disclosed rather than hidden
    catalog = load_metrics()
    formula_only = {m for m, spec in catalog.reported.items() if not spec.concepts}  # e.g. FCF: derived by definition
    derived = sorted(
        {
            f"{r['metric']} = {r['derivation']}"
            for y in years[:3]
            for r in by_year[y].values()
            if r["derivation"] and r["metric"] not in formula_only
        }
    )
    checks.append(
        Check(
            check="derived_metrics",
            status="info" if derived else "ok",
            detail="; ".join(derived) + " (no XBRL tag reported)" if derived else "all core metrics directly reported",
        )
    )

    # 3. accounting identities and bounds on the last three fiscal years
    problems = []
    for y in years[:3]:
        v = {m: r["value"] for m, r in by_year[y].items()}
        rev = v.get("revenue")
        reported_gp = "gross_profit" in v and not by_year[y]["gross_profit"]["derivation"]
        if (
            rev
            and reported_gp
            and "cost_of_revenue" in v
            and _rel_diff(v["gross_profit"], rev - v["cost_of_revenue"]) > 0.01
        ):
            problems.append(f"FY{y} gross profit != revenue - cost of revenue")
        if rev and "gross_profit" in v and not (-1 <= v["gross_profit"] / rev <= 1):
            problems.append(f"FY{y} gross margin out of range")
        if "operating_income" in v and "gross_profit" in v and v["operating_income"] > v["gross_profit"] * 1.001:
            problems.append(f"FY{y} operating income > gross profit")
        if "net_income" in v and "diluted_shares" in v and "eps_diluted" in v and v["diluted_shares"]:
            implied = v["net_income"] / v["diluted_shares"]
            if _rel_diff(implied, v["eps_diluted"]) > 0.05:
                problems.append(
                    f"FY{y} EPS {v['eps_diluted']:.2f} vs net income / diluted shares {implied:.2f} (basis mismatch?)"
                )
    checks.append(
        Check(
            check="accounting_identities",
            status="warn" if problems else "ok",
            detail="; ".join(problems) or "gross profit, margins, operating income and EPS consistent for last 3 FYs",
        )
    )

    # 4. filing text extraction
    sections = conn.execute(
        """
        SELECT f.fiscal_year, s.item, s.char_count, s.extraction_method,
               (SELECT count(*) FROM risk_factor r WHERE r.filing_id = f.id) AS risks
        FROM filing f JOIN filing_section s ON s.filing_id = f.id
        WHERE f.company_id = %s AND f.form = '10-K' ORDER BY f.filing_date DESC, s.item
        """,
        (cid,),
    ).fetchall()
    text_warn, text_info = [], []
    seen = {(s["fiscal_year"], s["item"]) for s in sections}
    for fy in sorted({s["fiscal_year"] for s in sections}, reverse=True):
        for item in ("1A", "7"):
            if (fy, item) not in seen:
                text_warn.append(f"FY{fy} Item {item} not extracted")
    for s in sections:
        if s["char_count"] < 10_000:
            text_warn.append(f"FY{s['fiscal_year']} Item {s['item']} only {s['char_count']:,} chars")
        if s["extraction_method"] != "item-heading":
            text_info.append(f"FY{s['fiscal_year']} Item {s['item']} located via {s['extraction_method']} fallback")
        if s["item"] == "1A" and s["risks"] < 5:
            text_warn.append(f"FY{s['fiscal_year']} only {s['risks']} risk factors split out")
    checks.append(
        Check(
            check="filing_text",
            status="warn" if text_warn else ("info" if text_info else "ok"),
            detail="; ".join(text_warn + text_info) or f"{len(sections)} sections extracted by Item heading",
        )
    )

    # 5. market data freshness and gaps
    px = conn.execute(
        """
        SELECT max(date) AS last, count(*) AS n,
               max(gap) AS max_gap
        FROM (SELECT date, date - lag(date) OVER (ORDER BY date) AS gap
              FROM price_daily WHERE company_id = %s AND date > now()::date - 365) t
        """,
        (cid,),
    ).fetchone()
    anchor = as_of or date.today()
    if not px or px["last"] is None:
        checks.append(Check(check="prices", status="warn", detail="no prices in the last year"))
    else:
        stale = (anchor - px["last"]).days
        gap = px["max_gap"] or 0
        status: Status = "warn" if gap > 5 else ("info" if stale > 4 else "ok")
        checks.append(
            Check(
                check="prices",
                status=status,
                detail=f"last close {px['last']} ({stale} days before {anchor}); "
                f"{px['n']} trading days in the last year; longest gap {gap} days",
            )
        )

    # 6. latest ingestion status per source
    runs = conn.execute(
        """
        SELECT DISTINCT ON (source) source, status, finished_at, detail
        FROM ingestion_run WHERE ticker = %s ORDER BY source, id DESC
        """,
        (company["ticker"],),
    ).fetchall()
    failed = [f"{r['source']}: {r['detail']}" for r in runs if r["status"] != "ok"]
    checks.append(
        Check(
            check="ingestion",
            status="warn" if failed or not runs else "ok",
            detail="; ".join(failed)
            or (
                ", ".join(f"{r['source']} ok {r['finished_at']:%Y-%m-%d}" for r in runs)
                if runs
                else "no ingestion runs recorded"
            ),
        )
    )

    return QualityReport(ticker=company["ticker"], status=_worst(checks), checks=checks)
