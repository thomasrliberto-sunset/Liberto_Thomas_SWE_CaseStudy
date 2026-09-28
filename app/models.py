"""Response models: the API contract, also what the agent's tools return."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class NotFound(Exception):
    """Raised by services when a ticker/filing/metric doesn't exist; mapped to 404."""


class BadRequest(Exception):
    """Raised by services for invalid parameters; mapped to 400."""


# ------------------------------------------------------------------ companies


class CompanySummary(BaseModel):
    ticker: str
    name: str
    cik: int
    fiscal_year_end: str | None = Field(None, description="MMDD")
    fiscal_years_available: list[int]
    tenk_fiscal_years: list[int] = Field(description="Fiscal years with 10-K narrative text ingested")
    latest_price_date: date | None
    insider_transactions: int


# ------------------------------------------------------------------ fundamentals


class MetricValue(BaseModel):
    value: float | None
    display: str | None
    unit: str
    yoy_growth: float | None = Field(None, description="Fractional YoY growth (amount metrics)")
    yoy_change_pp: float | None = Field(None, description="YoY change in percentage points (ratio metrics)")
    source: str | None = Field(None, description="XBRL concept, derivation formula, or ratio definition")
    derived: bool = False


class FiscalPeriod(BaseModel):
    fiscal_year: int
    period_start: date | None
    period_end: date
    accession: str | None = Field(None, description="10-K the values were taken from (most recent filer view)")
    metrics: dict[str, MetricValue]


class Fundamentals(BaseModel):
    ticker: str
    name: str
    fiscal_year_end: str | None
    periods: list[FiscalPeriod] = Field(description="Most recent fiscal year first")


class ComparisonRow(BaseModel):
    rank: int
    ticker: str
    fiscal_year: int
    period_end: date
    value: float
    display: str | None


class Comparison(BaseModel):
    metric: str
    label: str
    basis: str = Field(description="How periods were chosen")
    rows: list[ComparisonRow]
    missing: list[str] = Field(default_factory=list, description="Tickers without a value for the period")
    alignment_note: str | None = None


# ------------------------------------------------------------------ valuation


class ValuationPoint(BaseModel):
    fiscal_year: int
    period_end: date
    price: float | None
    price_date: date | None
    eps_diluted: float | None
    pe: float | None


class TrailingTwelveMonths(BaseModel):
    period_start: date
    period_end: date
    eps_diluted: float | None
    revenue: float | None
    pe: float | None = Field(description="None when TTM EPS <= 0")
    price_to_sales: float | None
    through_filing: str | None = Field(description="10-Q whose year-to-date figures roll the fiscal year forward")
    method: str


class Valuation(BaseModel):
    ticker: str
    price: float
    price_date: date
    price_source: str
    eps_fiscal_year: int
    eps_period_end: date
    eps_diluted_reported: float
    split_adjustment: float = Field(description="EPS divided by this to match today's share basis")
    eps_diluted_adjusted: float
    trailing_pe: float | None = Field(description="None when EPS <= 0")
    revenue: float | None
    diluted_shares_adjusted: float | None
    market_cap_approx: float | None = Field(description="price x FY weighted-average diluted shares")
    price_to_sales: float | None
    ttm: TrailingTwelveMonths | None = Field(
        None, description="Same multiples on trailing-twelve-month figures (latest FY + 10-Q YTD - prior YTD)"
    )
    notes: list[str]
    history: list[ValuationPoint] = Field(description="P/E at each fiscal year end")


# ------------------------------------------------------------------ prices


class PriceBar(BaseModel):
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    adj_close: float | None
    volume: int | None


class PriceSeries(BaseModel):
    ticker: str
    source: str | None
    start: date | None
    end: date | None
    first_close: float | None
    last_close: float | None
    change_pct: float | None
    high: float | None
    low: float | None
    bars: list[PriceBar]


# ------------------------------------------------------------------ filings


class FilingSectionInfo(BaseModel):
    item: str
    title: str
    char_count: int
    chunks: int


class FilingInfo(BaseModel):
    accession: str
    form: str
    fiscal_year: int | None
    filing_date: date
    report_date: date | None
    url: str
    sections: list[FilingSectionInfo]
    risk_factors: int


class SearchHit(BaseModel):
    chunk_id: int
    ticker: str
    fiscal_year: int | None
    filing_date: date
    item: str
    section: str
    heading: str | None
    text: str
    rank: float
    url: str


class RiskFactorItem(BaseModel):
    heading: str
    category: str | None
    excerpt: str | None = None
    matched_prior_heading: str | None = None
    similarity: float | None = None


class RiskFactorDiff(BaseModel):
    ticker: str
    latest: FilingRefOut
    prior: FilingRefOut
    counts: dict[str, int]
    new: list[RiskFactorItem]
    modified: list[RiskFactorItem]
    removed: list[RiskFactorItem]
    method: str


class FilingRefOut(BaseModel):
    accession: str
    fiscal_year: int | None
    period_end: date | None
    filing_date: date
    url: str


RiskFactorDiff.model_rebuild()


# ------------------------------------------------------------------ insiders


class InsiderSeller(BaseModel):
    insider: str
    relationship: str | None
    shares: float
    value: float
    under_10b5_1_pct: float | None


class InsiderSummary(BaseModel):
    ticker: str
    window_start: date
    window_end: date
    open_market_buys: int
    open_market_buy_value: float
    open_market_sales: int
    open_market_sale_value: float
    net_open_market_value: float
    sale_value_under_10b5_1_pct: float | None
    distinct_insiders: int
    other_activity: dict[str, int] = Field(description="Counts by Form 4 transaction code (A, M, F, G...)")
    top_sellers: list[InsiderSeller]
    notes: list[str]


# ------------------------------------------------------------------ ops


class IngestionRun(BaseModel):
    id: int
    source: str
    ticker: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    rows_written: int | None
    detail: str | None


# ------------------------------------------------------------------ ask


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class RouteDecision(BaseModel):
    route: Literal["numbers", "narrative", "both", "out_of_scope"]
    tickers: list[str] = Field(default_factory=list)
    rationale: str = ""
    out_of_scope_reason: str | None = None


class ToolCallTrace(BaseModel):
    name: str
    arguments: dict
    ok: bool
    latency_ms: int
    sources: list[str] = Field(default_factory=list)
    error: str | None = None


CitationKind = Literal["financials", "comparison", "valuation", "prices", "filing_text", "risk_diff", "insiders"]


class Citation(BaseModel):
    id: str
    kind: CitationKind
    ticker: str | None = None
    description: str
    url: str | None = None
    excerpt: str | None = None


class GroundingReport(BaseModel):
    numbers_checked: int
    citations_checked: int = 0
    unverified_numbers: list[str]
    unknown_citations: list[str]
    issues: list[str] = Field(default_factory=list)
    grounded: bool


class LLMUsage(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int


class AskResponse(BaseModel):
    question: str
    answer: str
    route: RouteDecision
    citations: list[Citation]
    tool_calls: list[ToolCallTrace]
    grounding: GroundingReport | None
    model: str
    latency_ms: int
    usage: LLMUsage | None = Field(None, description="LLM calls and tokens spent on this question")
