"""Tools the model can call. Each wraps a service function (the same code the REST API
uses), returns a compact, pre-formatted view for the model, and registers a citable
source in the ledger. The model never computes a metric itself: growth, margins and
multiples arrive pre-computed and pre-formatted."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.config import load_metrics
from app.db.connection import DbConn
from app.models import BadRequest, Citation, CitationKind, NotFound
from app.services import filings, fundamentals, insiders, market
from app.services.metrics import fmt_money

MAX_RESULT_CHARS = 14000


# --------------------------------------------------------------------------- source ledger


@dataclass
class SourceLedger:
    """Assigns citation ids (F1, S3, ...) and keeps everything the model was shown,
    so the answer's citations and numbers can be checked afterwards."""

    citations: dict[str, Citation] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)  # exact serialized tool payloads shown to the model
    _counters: dict[str, int] = field(default_factory=dict)

    PREFIX: ClassVar[dict[str, str]] = {
        "financials": "F",
        "comparison": "C",
        "valuation": "V",
        "prices": "P",
        "filing_text": "S",
        "risk_diff": "R",
        "insiders": "I",
    }

    def add(
        self,
        kind: CitationKind,
        description: str,
        ticker: str | None = None,
        url: str | None = None,
        excerpt: str | None = None,
    ) -> str:
        prefix = self.PREFIX[kind]
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        cid = f"{prefix}{self._counters[prefix]}"
        self.citations[cid] = Citation(
            id=cid, kind=kind, ticker=ticker, description=description, url=url, excerpt=excerpt
        )
        return cid


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[DbConn, dict[str, Any], SourceLedger], Any]
    group: str  # "numbers" | "narrative"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _growth(v: float | None) -> str | None:
    return None if v is None else f"{v * 100:+.1f}%"


def _fmt_metric(mv) -> str:
    s = mv.display or "n/a"
    if mv.yoy_growth is not None:
        s += f" (YoY {_growth(mv.yoy_growth)})"
    elif mv.yoy_change_pp is not None:
        s += f" (YoY {mv.yoy_change_pp:+.1f} pp)"
    return s


# --------------------------------------------------------------------------- numbers tools


def _get_financials(conn, args, ledger: SourceLedger):
    out = []
    for ticker in args["tickers"]:
        f = fundamentals.get_fundamentals(
            conn, ticker, args.get("metrics"), args.get("fiscal_years"), args.get("last_n", 3)
        )
        accessions = sorted({p.accession for p in f.periods if p.accession})
        sid = ledger.add(
            "financials",
            f"{f.ticker} annual financials FY{min(p.fiscal_year for p in f.periods)}-"
            f"FY{max(p.fiscal_year for p in f.periods)} from SEC XBRL company facts "
            f"(10-K accessions {', '.join(accessions)})",
            ticker=f.ticker,
            url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={f.ticker}&type=10-K",
        )
        derived = sorted({f"{m}: {mv.source}" for p in f.periods for m, mv in p.metrics.items() if mv.derived})
        out.append(
            {
                "source_id": sid,
                "ticker": f.ticker,
                "company": f.name,
                "rows": [
                    {
                        "fiscal_year": p.fiscal_year,
                        "period_end": p.period_end.isoformat(),
                        **{m: _fmt_metric(mv) for m, mv in p.metrics.items()},
                    }
                    for p in f.periods
                ],
                **({"derived_not_reported": derived} if derived else {}),
            }
        )
    return out


def _compare(conn, args, ledger: SourceLedger):
    c = fundamentals.compare_metric(conn, args["metric"], args.get("fiscal_year"), args.get("tickers"))
    sid = ledger.add("comparison", f"{c.label} ranking, {c.basis} (SEC XBRL)")
    return {
        "source_id": sid,
        "metric": c.label,
        "basis": c.basis,
        "ranking": [
            {
                "rank": r.rank,
                "ticker": r.ticker,
                "fiscal_year": r.fiscal_year,
                "period_end": r.period_end.isoformat(),
                "value": r.display,
            }
            for r in c.rows
        ],
        "missing": c.missing,
        "alignment_note": c.alignment_note,
    }


def _valuation(conn, args, ledger: SourceLedger):
    out = []
    for ticker in args["tickers"]:
        v = market.get_valuation(conn, ticker)
        sid = ledger.add(
            "valuation",
            f"{v.ticker} trailing P/E & P/S: {v.price_source} close {v.price_date} x FY{v.eps_fiscal_year} "
            "10-K" + (f" and TTM through {v.ttm.period_end}" if v.ttm else "") + " (SEC XBRL)",
            ticker=v.ticker,
        )
        out.append(
            {
                "source_id": sid,
                "ticker": v.ticker,
                "price": f"${v.price:,.2f} (close {v.price_date.isoformat()})",
                "eps_diluted": f"${v.eps_diluted_adjusted:,.2f} (FY{v.eps_fiscal_year}, period ended "
                f"{v.eps_period_end.isoformat()})",
                "trailing_pe": f"{v.trailing_pe:.1f}x" if v.trailing_pe else "not meaningful",
                "price_to_sales": f"{v.price_to_sales:.1f}x" if v.price_to_sales else None,
                "market_cap_approx": fmt_money(v.market_cap_approx),
                "ttm": (
                    {
                        "period": f"{v.ttm.period_start} to {v.ttm.period_end}",
                        "eps_diluted": f"${v.ttm.eps_diluted:,.2f}" if v.ttm.eps_diluted is not None else None,
                        "trailing_pe": f"{v.ttm.pe:.1f}x" if v.ttm.pe else "not meaningful",
                        "price_to_sales": f"{v.ttm.price_to_sales:.1f}x" if v.ttm.price_to_sales else None,
                        "method": v.ttm.method,
                    }
                    if v.ttm
                    else "not available"
                ),
                "pe_at_fiscal_year_ends": [
                    {"fiscal_year": h.fiscal_year, "pe": f"{h.pe:.1f}x" if h.pe else None} for h in v.history
                ],
                "notes": v.notes,
            }
        )
    return out


def _prices(conn, args, ledger: SourceLedger):
    p = market.get_prices(conn, args["ticker"], args.get("start_date"), args.get("end_date"), include_bars=False)
    sid = ledger.add("prices", f"{p.ticker} daily prices {p.start}..{p.end} ({p.source})", ticker=p.ticker)
    return {
        "source_id": sid,
        "ticker": p.ticker,
        "start": str(p.start),
        "end": str(p.end),
        "first_close": f"${p.first_close:,.2f}",
        "last_close": f"${p.last_close:,.2f}",
        "change": f"{p.change_pct:+.1f}%" if p.change_pct is not None else None,
        "high": f"${p.high:,.2f}",
        "low": f"${p.low:,.2f}",
    }


def _insiders(conn, args, ledger: SourceLedger):
    s = insiders.insider_summary(conn, args["ticker"], args.get("days", 365))
    sid = ledger.add("insiders", f"{s.ticker} SEC Form 4 filings {s.window_start}..{s.window_end}", ticker=s.ticker)
    return {
        "source_id": sid,
        "ticker": s.ticker,
        "window": f"{s.window_start} to {s.window_end}",
        "window_days": (s.window_end - s.window_start).days,
        "open_market_buys": f"{s.open_market_buys} ({fmt_money(s.open_market_buy_value)})",
        "open_market_sales": f"{s.open_market_sales} ({fmt_money(s.open_market_sale_value)})",
        "net_open_market": fmt_money(s.net_open_market_value),
        "sales_under_10b5_1_plans": f"{s.sale_value_under_10b5_1_pct:.0f}%"
        if s.sale_value_under_10b5_1_pct is not None
        else None,
        "top_sellers": [f"{t.insider} ({t.relationship}): {fmt_money(t.value)}" for t in s.top_sellers],
        "other_activity_by_code": s.other_activity,
        "notes": s.notes,
    }


# --------------------------------------------------------------------------- narrative tools


def _search(conn, args, ledger: SourceLedger):
    which = {"both": "all"}.get(args.get("filing", "latest"), args.get("filing", "latest"))
    hits = filings.search_filings(
        conn, args["ticker"], args["query"], args.get("section"), which, None, min(args.get("limit", 5), 8)
    )
    out = []
    for h in hits:
        section = "Item 1A Risk Factors" if h.item == "1A" else "Item 7 MD&A"
        sid = ledger.add(
            "filing_text",
            f"{h.ticker} FY{h.fiscal_year} 10-K (filed {h.filing_date}), {section}"
            + (f" - {h.heading[:120]}" if h.heading else ""),
            ticker=h.ticker,
            url=h.url,
            excerpt=h.text[:400],
        )
        out.append(
            {
                "source_id": sid,
                "fiscal_year": h.fiscal_year,
                "section": section,
                "heading": h.heading,
                "text": h.text[:1500],
            }
        )
    return out or {"result": "no matching passages; try different keywords or section='any'"}


def _risk_diff(conn, args, ledger: SourceLedger):
    d = filings.diff_risk_factors(conn, args["ticker"])
    sid = ledger.add(
        "risk_diff",
        f"{d.ticker} Item 1A risk factors: FY{d.latest.fiscal_year} 10-K vs FY{d.prior.fiscal_year} 10-K",
        ticker=d.ticker,
        url=d.latest.url,
    )
    return {
        "source_id": sid,
        "latest_10k": f"FY{d.latest.fiscal_year} (period ended {d.latest.period_end}, filed {d.latest.filing_date})",
        "prior_10k": f"FY{d.prior.fiscal_year} (period ended {d.prior.period_end}, filed {d.prior.filing_date})",
        "counts": d.counts,
        "new_risk_factors": [{"heading": n.heading, "category": n.category, "excerpt": n.excerpt} for n in d.new],
        "reworded_or_substantially_revised": [
            {"heading": m.heading, "previously": m.matched_prior_heading, "excerpt": (m.excerpt or "")[:300]}
            for m in d.modified
        ],
        "removed": [r.heading for r in d.removed],
        "method": d.method,
    }


# --------------------------------------------------------------------------- registry


def build_tools(tickers: list[str]) -> dict[str, ToolSpec]:
    catalog = load_metrics()
    metric_enum = catalog.all_names
    ticker_list = {"type": "array", "items": {"type": "string", "enum": tickers}, "minItems": 1}
    one_ticker = {"type": "string", "enum": tickers}
    specs = [
        ToolSpec(
            "get_financials",
            "Annual reported financials from SEC XBRL for one or more companies, with YoY growth and margins "
            "pre-computed. Use for revenue, profit, EPS, cash flow, margins and growth over fiscal years.",
            {
                "type": "object",
                "properties": {
                    "tickers": ticker_list,
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string", "enum": metric_enum},
                        "description": "Default: revenue, profits, EPS, margins, FCF",
                    },
                    "fiscal_years": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Specific fiscal years (company's own labels)",
                    },
                    "last_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Most recent N fiscal years (default 3)",
                    },
                },
                "required": ["tickers"],
            },
            _get_financials,
            "numbers",
        ),
        ToolSpec(
            "compare_companies",
            "Rank companies on one metric for their latest fiscal year (default) or a given fiscal year. "
            "Use for 'which company had the highest/lowest ...' questions.",
            {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": metric_enum},
                    "fiscal_year": {"type": "integer", "description": "Omit for each company's latest fiscal year"},
                    "tickers": {**ticker_list, "description": "Omit for all tracked companies"},
                },
                "required": ["metric"],
            },
            _compare,
            "numbers",
        ),
        ToolSpec(
            "get_valuation",
            "Trailing P/E and P/S: latest stored closing price joined with the latest annual diluted EPS and "
            "revenue from the 10-K (split-adjusted), the same multiples on trailing-twelve-month figures "
            "rolled forward with 10-Q data, and P/E at past fiscal year ends.",
            {"type": "object", "properties": {"tickers": ticker_list}, "required": ["tickers"]},
            _valuation,
            "numbers",
        ),
        ToolSpec(
            "get_price_performance",
            "Stock price performance over a date range (first/last close, % change, high, low).",
            {
                "type": "object",
                "properties": {
                    "ticker": one_ticker,
                    "start_date": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
                },
                "required": ["ticker"],
            },
            _prices,
            "numbers",
        ),
        ToolSpec(
            "get_insider_activity",
            "Insider open-market buying and selling from SEC Form 4 filings, incl. share of sales under "
            "Rule 10b5-1 plans and top sellers.",
            {
                "type": "object",
                "properties": {
                    "ticker": one_ticker,
                    "days": {"type": "integer", "minimum": 7, "maximum": 730, "description": "Lookback (default 365)"},
                },
                "required": ["ticker"],
            },
            _insiders,
            "numbers",
        ),
        ToolSpec(
            "search_filings",
            "Full-text search over the company's 10-K narrative: Item 1A Risk Factors and Item 7 MD&A "
            "(management's discussion of results, drivers, outlook commentary, liquidity). Returns passages "
            "with source ids. Use specific keywords (e.g. 'revenue increased driven by Azure').",
            {
                "type": "object",
                "properties": {
                    "ticker": one_ticker,
                    "query": {"type": "string", "description": "Keywords to search for"},
                    "section": {
                        "type": "string",
                        "enum": ["risk_factors", "mdna", "any"],
                        "description": "Default any",
                    },
                    "filing": {"type": "string", "enum": ["latest", "prior", "both"], "description": "Default latest"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["ticker", "query"],
            },
            _search,
            "narrative",
        ),
        ToolSpec(
            "diff_risk_factors",
            "Compare Item 1A risk factors between the latest and prior 10-K: new, reworded/expanded, and "
            "removed risk factors (deterministic heading + text matching).",
            {"type": "object", "properties": {"ticker": one_ticker}, "required": ["ticker"]},
            _risk_diff,
            "narrative",
        ),
    ]
    return {s.name: s for s in specs}


def tools_for_route(all_tools: dict[str, ToolSpec], route: str) -> dict[str, ToolSpec]:
    groups = {"numbers": {"numbers"}, "narrative": {"narrative"}, "both": {"numbers", "narrative"}}[route]
    return {n: t for n, t in all_tools.items() if t.group in groups}


def run_tool(spec: ToolSpec, conn: DbConn, args: dict[str, Any], ledger: SourceLedger) -> tuple[str, bool, str | None]:
    """-> (content for the model, ok, error). Errors go back to the model so it can adapt."""
    if "__invalid_json__" in args:
        return json.dumps({"error": "arguments were not valid JSON"}), False, "invalid JSON arguments"
    citations_before = set(ledger.citations)
    counters_before = ledger._counters.copy()
    evidence_before = len(ledger.evidence)

    def rollback_sources() -> None:
        for cid in set(ledger.citations) - citations_before:
            del ledger.citations[cid]
        ledger._counters = counters_before
        del ledger.evidence[evidence_before:]

    try:
        result = spec.handler(conn, args, ledger)
    except (NotFound, BadRequest) as exc:
        rollback_sources()
        return json.dumps({"error": str(exc)}), False, str(exc)
    except (KeyError, TypeError, ValueError) as exc:
        rollback_sources()
        return json.dumps({"error": f"bad arguments: {exc}"}), False, f"bad arguments: {exc}"
    # Handlers may not broaden the evidence set with raw service objects. The
    # only admissible evidence is the final payload returned to the model.
    del ledger.evidence[evidence_before:]
    text = json.dumps(result, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + '..."[truncated]'
    # Only sources and figures present in the exact payload sent to the model may
    # be used to validate its answer. A large result can be truncated mid-list.
    for cid in set(ledger.citations) - citations_before:
        if f'"source_id": {json.dumps(cid)}' not in text:
            del ledger.citations[cid]
    ledger.evidence.append(text)
    return text, True, None
