"""Trailing-twelve-month (TTM) figures from 10-Q year-to-date facts.

    TTM = latest fiscal year + current YTD - prior-year YTD (same fiscal period)

The latest annual EPS can be up to ~15 months old (Apple's FY ends in September), so a PM
reading "trailing P/E" usually wants TTM. This is pure: it takes the annual value and the
10-Q facts for the same XBRL concept, so it is testable without a database.

EPS is not strictly additive (share counts move between quarters); summing YTD EPS is the
standard approximation and is labelled as such. Every component is put on today's share
basis with the split factor for splits after that component's filing date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.services.metrics import split_factor

_TOLERANCE = timedelta(days=7)  # 52/53-week calendars shift period boundaries by a few days


@dataclass(frozen=True)
class YtdFact:
    start: date
    end: date
    value: float
    filed: date
    accession: str


@dataclass(frozen=True)
class TTM:
    value: float
    period_start: date
    period_end: date
    fy_value: float
    ytd_current: float | None
    ytd_prior: float | None
    ytd_months: int
    source_accession: str | None

    @property
    def is_annual_only(self) -> bool:
        return self.ytd_current is None


def _close(a: date, b: date) -> bool:
    return abs(a - b) <= _TOLERANCE


def _latest_filed(facts: list[YtdFact]) -> YtdFact | None:
    return max(facts, key=lambda f: f.filed, default=None)


def trailing_twelve_months(
    fy_start: date,
    fy_end: date,
    fy_value: float,
    fy_filed: date,
    ytd_facts: list[YtdFact],
    splits: list[tuple[date, float]] | None = None,
) -> TTM:
    """Roll the latest fiscal year forward with the most recent 10-Q year-to-date figure."""
    splits = splits or []

    def adj(value: float, filed: date) -> float:
        return value / split_factor(splits, filed)

    fy_adj = adj(fy_value, fy_filed)
    # current-year YTD: starts right after the fiscal year ended, the longest one wins
    current = [f for f in ytd_facts if _close(f.start, fy_end + timedelta(days=1)) and f.end > fy_end]
    if not current:
        return TTM(fy_adj, fy_start, fy_end, fy_adj, None, None, 0, None)
    latest_end = max(f.end for f in current)
    cur = _latest_filed([f for f in current if f.end == latest_end])
    if cur is None:
        return TTM(fy_adj, fy_start, fy_end, fy_adj, None, None, 0, None)
    # prior-year comparative: same fiscal period one year earlier (restated values preferred)
    prior = _latest_filed(
        [f for f in ytd_facts if _close(f.start, fy_start) and _close(f.end, cur.end - timedelta(days=365))]
    )
    if prior is None:
        return TTM(fy_adj, fy_start, fy_end, fy_adj, None, None, 0, None)
    cur_v, prior_v = adj(cur.value, cur.filed), adj(prior.value, prior.filed)
    months = round((cur.end - cur.start).days / 30.4)
    return TTM(
        value=fy_adj + cur_v - prior_v,
        period_start=cur.end - timedelta(days=364),
        period_end=cur.end,
        fy_value=fy_adj,
        ytd_current=cur_v,
        ytd_prior=prior_v,
        ytd_months=months,
        source_accession=cur.accession,
    )
