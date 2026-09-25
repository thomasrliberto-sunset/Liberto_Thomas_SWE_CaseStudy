from datetime import date

import pytest

from app.config import load_metrics
from app.ingest.xbrl import build_annual_financials, fiscal_year_labels, flatten_companyfacts, parse_formula


def fact(start, end, val, fy, accn, filed, form="10-K", fp="FY"):
    return {"start": start, "end": end, "val": val, "fy": fy, "fp": fp, "form": form, "accn": accn, "filed": filed}


def companyfacts(concepts: dict) -> dict:
    return {"facts": {"us-gaap": {c: {"units": units} for c, units in concepts.items()}}}


# NVIDIA-style fiscal years ending in late January. The FY2025 10-K also reports FY2024
# as a comparative, tagged fy=2025 - the classic trap.
NVDA_LIKE = companyfacts({
    "Revenues": {"USD": [
        fact("2023-01-30", "2024-01-28", 60_922, 2024, "A-24", "2024-02-21"),
        fact("2024-01-29", "2025-01-26", 130_497, 2025, "A-25", "2025-02-26"),
        fact("2023-01-30", "2024-01-28", 60_922, 2025, "A-25", "2025-02-26"),  # comparative
        fact("2024-10-28", "2025-01-26", 39_331, 2025, "A-25", "2025-02-26", fp="Q4"),  # quarter: excluded
    ]},
    "EarningsPerShareDiluted": {"USD/shares": [
        fact("2023-01-30", "2024-01-28", 11.93, 2024, "A-24", "2024-02-21"),  # pre-split
        fact("2023-01-30", "2024-01-28", 1.19, 2025, "A-25", "2025-02-26"),  # restated post-split
        fact("2024-01-29", "2025-01-26", 2.94, 2025, "A-25", "2025-02-26"),
    ]},
})


def by_key(values):
    return {(v.metric, v.fiscal_year): v for v in values}


def test_fiscal_year_label_comes_from_the_filing_whose_current_period_it_is():
    labels, offset = fiscal_year_labels(flatten_companyfacts(NVDA_LIKE))
    assert labels == {date(2024, 1, 28): 2024, date(2025, 1, 26): 2025}
    assert offset == 0


def test_annual_only_and_restated_value_wins():
    vals = by_key(build_annual_financials(flatten_companyfacts(NVDA_LIKE), load_metrics()))
    assert vals[("revenue", 2025)].value == 130_497  # the Q4-only fact was ignored
    assert vals[("revenue", 2024)].value == 60_922
    # FY2024 EPS: the later (split-adjusted) filing wins over the original
    assert vals[("eps_diluted", 2024)].value == pytest.approx(1.19)
    assert vals[("eps_diluted", 2024)].accession == "A-25"
    assert vals[("revenue", 2025)].source_concept == "us-gaap:Revenues"


def test_concept_priority_is_decided_per_period_and_fallbacks_fill_gaps():
    doc = companyfacts({
        # tag drift: old periods use SalesRevenueNet, new ones the ASC 606 concept
        "SalesRevenueNet": {"USD": [fact("2016-01-01", "2016-12-31", 90, 2016, "K16", "2017-02-01")]},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
            fact("2017-01-01", "2017-12-31", 100, 2017, "K17", "2018-02-01"),
        ]},
        "CostOfRevenue": {"USD": [
            fact("2016-01-01", "2016-12-31", 50, 2016, "K16", "2017-02-01"),
            fact("2017-01-01", "2017-12-31", 55, 2017, "K17", "2018-02-01"),
        ]},
    })
    vals = by_key(build_annual_financials(flatten_companyfacts(doc), load_metrics()))
    assert vals[("revenue", 2016)].source_concept == "us-gaap:SalesRevenueNet"
    assert vals[("revenue", 2017)].source_concept.endswith("RevenueFromContractWithCustomerExcludingAssessedTax")
    gp = vals[("gross_profit", 2017)]
    assert gp.value == 45 and gp.derivation == "revenue - cost_of_revenue" and gp.source_concept is None


def test_parse_formula():
    assert parse_formula("a - b + c") == [(1, "a"), (-1, "b"), (1, "c")]
    with pytest.raises(ValueError):
        parse_formula("a b")
