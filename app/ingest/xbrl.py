"""XBRL company-facts: flatten the raw payload, then normalize it into one canonical
annual value per (metric, fiscal year).

The hard parts, all handled here and covered by tests:

* Fiscal-year labels. The `fy` field on a fact is the fiscal year *of the filing that
  reported it*, so a FY2025 10-K tags its FY2023 comparative with fy=2025. We label a
  period by the fy of the annual filing whose *current* period ends on that date.
  This also gets NVIDIA right (FY2025 ends in January 2025) without special-casing.
* Annual vs. quarterly. Only ~12-month duration facts from annual forms qualify
  (350-380 days covers 52/53-week years).
* Restatements and splits. When several filings report the same period, the most
  recently filed value wins, so history is on the latest basis (e.g. NVIDIA's EPS
  before its 2024 10-for-1 split comes back split-adjusted).
* Tag drift. Concepts are tried in priority order per period, and simple fallback
  formulas fill gaps (Alphabet reports no GrossProfit tag).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from app.config import MetricCatalog

ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
TAXONOMIES = ("us-gaap", "ifrs-full", "dei")


@dataclass(frozen=True)
class Fact:
    taxonomy: str
    concept: str
    unit: str
    start: date | None
    end: date
    value: float
    fy: int | None
    fp: str | None
    form: str | None
    accession: str
    filed: date
    frame: str | None

    @property
    def duration_days(self) -> int | None:
        return (self.end - self.start).days if self.start else None

    @property
    def is_annual(self) -> bool:
        d = self.duration_days
        return self.form in ANNUAL_FORMS and d is not None and 350 <= d <= 380


@dataclass(frozen=True)
class AnnualValue:
    metric: str
    fiscal_year: int
    start: date | None
    end: date
    value: float
    unit: str
    source_concept: str | None
    derivation: str | None
    accession: str | None
    filed: date | None


def flatten_companyfacts(doc: dict, taxonomies: tuple[str, ...] = TAXONOMIES) -> list[Fact]:
    facts: list[Fact] = []
    for taxonomy in taxonomies:
        for concept, body in (doc.get("facts", {}).get(taxonomy) or {}).items():
            for unit, rows in body.get("units", {}).items():
                for r in rows:
                    facts.append(
                        Fact(
                            taxonomy=taxonomy,
                            concept=concept,
                            unit=unit,
                            start=date.fromisoformat(r["start"]) if r.get("start") else None,
                            end=date.fromisoformat(r["end"]),
                            value=float(r["val"]),
                            fy=r.get("fy"),
                            fp=r.get("fp"),
                            form=r.get("form"),
                            accession=r["accn"],
                            filed=date.fromisoformat(r["filed"]),
                            frame=r.get("frame"),
                        )
                    )
    return facts


def fiscal_year_labels(facts: list[Fact]) -> tuple[dict[date, int], int]:
    """Return ({period_end: fiscal_year}, year_offset).

    year_offset (label - calendar year of period end, usually 0) is used to label
    periods that were only ever reported as comparatives.
    """
    by_accession: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        if f.is_annual and f.fy:
            by_accession[f.accession].append(f)

    labels: dict[date, int] = {}
    for fs in sorted(by_accession.values(), key=lambda fs: min(f.filed for f in fs)):
        current_end = max(f.end for f in fs)
        fy = Counter(f.fy for f in fs if f.end == current_end).most_common(1)[0][0]
        if abs(fy - current_end.year) <= 1:  # guard against mis-tagged DocumentFiscalYearFocus
            labels.setdefault(current_end, fy)

    offsets = Counter(fy - end.year for end, fy in labels.items())
    offset = offsets.most_common(1)[0][0] if offsets else 0
    return labels, offset


def _annual_series(facts: list[Fact]) -> dict[date, Fact]:
    """Best annual fact per period end: latest filed wins, then duration closest to a year."""
    best: dict[date, Fact] = {}
    for f in facts:
        if not f.is_annual:
            continue
        cur = best.get(f.end)
        if cur is None or (f.filed, -abs((f.duration_days or 0) - 365)) > (
            cur.filed,
            -abs((cur.duration_days or 0) - 365),
        ):
            best[f.end] = f
    return best


_TOKEN = re.compile(r"\s*([+-])?\s*([a-z_][a-z0-9_]*)")


def parse_formula(expr: str) -> list[tuple[int, str]]:
    """'a - b + c' -> [(1, 'a'), (-1, 'b'), (1, 'c')]. Only + and - are supported."""
    terms, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"bad formula: {expr!r}")
        if terms and not m.group(1):
            raise ValueError(f"missing operator in formula: {expr!r}")
        terms.append((-1 if m.group(1) == "-" else 1, m.group(2)))
        pos = m.end()
    return terms


def build_annual_financials(facts: list[Fact], catalog: MetricCatalog) -> list[AnnualValue]:
    labels, offset = fiscal_year_labels(facts)

    def label_for(end: date) -> int:
        return labels.get(end, end.year + offset)

    index: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for f in facts:
        if f.taxonomy in ("us-gaap", "ifrs-full"):
            index[(f.concept, f.unit)].append(f)

    resolved: dict[str, dict[int, AnnualValue]] = {}
    for metric in catalog.reported.values():
        by_year: dict[int, AnnualValue] = {}
        for concept in metric.concepts:  # priority order, decided per period
            filled_by_higher_priority = set(by_year)
            series = _annual_series(index.get((concept, metric.unit), []))
            for end, fact in sorted(series.items()):  # ascending: a later end wins a label clash
                fy = label_for(end)
                if fy not in filled_by_higher_priority:
                    by_year[fy] = AnnualValue(
                        metric=metric.name,
                        fiscal_year=fy,
                        start=fact.start,
                        end=end,
                        value=fact.value,
                        unit=metric.unit,
                        source_concept=f"{fact.taxonomy}:{concept}",
                        derivation=None,
                        accession=fact.accession,
                        filed=fact.filed,
                    )

        if metric.fallback:
            terms = parse_formula(metric.fallback)
            unknown = [name for _, name in terms if name not in resolved]
            if unknown:
                raise ValueError(f"{metric.name}: fallback uses {unknown} before they are defined")
            years = set.intersection(*(set(resolved[name]) for _, name in terms))
            for fy in sorted(years - set(by_year)):
                operands = [resolved[name][fy] for _, name in terms]
                if len({o.end for o in operands}) != 1:
                    continue  # operands must describe the same period
                value = sum(sign * o.value for (sign, _), o in zip(terms, operands))
                latest = max(operands, key=lambda o: o.filed or date.min)
                by_year[fy] = AnnualValue(
                    metric=metric.name,
                    fiscal_year=fy,
                    start=operands[0].start,
                    end=operands[0].end,
                    value=value,
                    unit=metric.unit,
                    source_concept=None,
                    derivation=metric.fallback,
                    accession=latest.accession,
                    filed=latest.filed,
                )
        resolved[metric.name] = by_year

    return [v for by_year in resolved.values() for v in by_year.values()]


def fiscal_year_for_accession(facts: list[Fact], accession: str) -> int | None:
    """DocumentFiscalYearFocus of a filing, as carried on its facts."""
    fys = Counter(f.fy for f in facts if f.accession == accession and f.fy)
    return fys.most_common(1)[0][0] if fys else None
