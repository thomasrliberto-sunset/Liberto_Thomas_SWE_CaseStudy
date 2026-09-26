from datetime import date

import pytest

from app.services.filings import compare_risk_factors, to_or_tsquery
from app.services.market import _ttm
from app.services.metrics import consecutive_years, fmt_value, pe_ratio, split_factor, yoy_growth
from app.services.ttm import YtdFact, trailing_twelve_months


def test_yoy_growth():
    assert yoy_growth(130.5, 60.9) == pytest.approx(1.1429, rel=1e-3)
    assert yoy_growth(-5, -10) == pytest.approx(0.5)  # smaller loss reads as improvement
    assert yoy_growth(10, 0) is None and yoy_growth(None, 5) is None


def test_consecutive_years_allows_53_week_years_but_not_gaps():
    assert consecutive_years(date(2024, 1, 28), date(2025, 1, 26))
    assert consecutive_years(date(2022, 9, 24), date(2023, 9, 30))  # 53-week year
    assert not consecutive_years(date(2022, 12, 31), date(2024, 12, 31))


def test_split_factor_only_counts_splits_after_the_filing():
    splits = [(date(2021, 7, 20), 4.0), (date(2024, 6, 10), 10.0)]
    assert split_factor(splits, date(2024, 2, 21)) == 10.0
    assert split_factor(splits, date(2021, 2, 26)) == 40.0
    assert split_factor(splits, date(2025, 2, 26)) == 1.0


def test_pe_ratio_not_meaningful_for_losses():
    assert pe_ratio(100, 4) == 25
    assert pe_ratio(100, -1) is None and pe_ratio(100, 0) is None


def test_formatting():
    assert fmt_value(130_497_000_000, "USD") == "$130.50B"
    assert fmt_value(0.7499, "ratio") == "75.0%"
    assert fmt_value(2.94, "USD/shares") == "$2.94"


def rf(heading, body, category="Cat"):
    return {"heading": heading, "body": body, "category": category}


def test_risk_factor_diff_classifies_new_modified_removed():
    shared_body = "demand for our products depends on datacenter spending by a small number of customers " * 5
    old = [
        rf("Competition could adversely impact our market share.", "competitors " + shared_body),
        rf("We depend on a limited number of suppliers.", "suppliers foundry capacity wafers lead times " * 10),
        rf("Pandemics could disrupt our operations.", "covid pandemic lockdown travel restrictions " * 10),
    ]
    cur = [
        rf("Competition could adversely impact our market share.", "competitors " + shared_body),
        rf(
            "Reliance on a limited number of partners for manufacturing could harm us.",
            "suppliers foundry capacity wafers lead times " * 10,
        ),  # reworded title, same body
        rf(
            "Commercial arrangements expose us to counterparty risks.",
            "financing guarantees customers partners counterparty credit default buildout " * 10,
        ),
    ]
    new, modified, removed, unchanged = compare_risk_factors(cur, old)
    assert unchanged == 1
    assert [m.heading for m in modified] == [
        "Reliance on a limited number of partners for manufacturing could harm us."
    ]
    assert modified[0].matched_prior_heading == "We depend on a limited number of suppliers."
    assert [n.heading for n in new] == ["Commercial arrangements expose us to counterparty risks."]
    assert [r.heading for r in removed] == ["Pandemics could disrupt our operations."]


def test_or_tsquery_is_sanitized():
    assert to_or_tsquery("What drove Azure's growth?!") == "what | drove | azure | growth"
    assert to_or_tsquery("a !") is None


def test_ttm_rolls_fiscal_year_forward_with_ytd_and_adjusts_splits():
    # FY ends 2025-09-27 (Apple-style 52/53-week year); 9-month YTD through 2026-06-27
    ytd = [
        YtdFact(date(2025, 9, 28), date(2025, 12, 27), 2.84, date(2026, 1, 30), "Q1"),  # 3-month: shorter, ignored
        YtdFact(date(2025, 9, 28), date(2026, 6, 27), 6.72, date(2026, 8, 1), "Q3"),  # current YTD
        YtdFact(date(2024, 9, 29), date(2025, 6, 28), 5.46, date(2026, 8, 1), "Q3"),  # prior-year comparative
    ]
    t = trailing_twelve_months(date(2024, 9, 29), date(2025, 9, 27), 7.46, date(2025, 10, 31), ytd)
    assert t.value == pytest.approx(7.46 + 6.72 - 5.46)
    assert t.period_end == date(2026, 6, 27) and t.ytd_months == 9 and t.source_accession == "Q3"

    # a 2-for-1 split after the 10-K but before the 10-Q: the FY figure is halved to today's basis
    split = [(date(2026, 3, 1), 2.0)]
    t2 = trailing_twelve_months(
        date(2024, 9, 29),
        date(2025, 9, 27),
        7.46,
        date(2025, 10, 31),
        ytd,
        split,
        per_share=True,
    )
    assert t2.fy_value == pytest.approx(3.73)

    # no 10-Q since the 10-K: TTM is just the fiscal year
    t3 = trailing_twelve_months(date(2024, 9, 29), date(2025, 9, 27), 7.46, date(2025, 10, 31), [])
    assert t3.is_annual_only and t3.value == pytest.approx(7.46)


def test_ttm_does_not_split_adjust_absolute_values():
    ytd = [
        YtdFact(date(2025, 9, 28), date(2026, 6, 27), 60.0, date(2026, 8, 1), "Q3"),
        YtdFact(date(2024, 9, 29), date(2025, 6, 28), 50.0, date(2026, 8, 1), "Q3"),
    ]
    split = [(date(2026, 3, 1), 2.0)]

    revenue = trailing_twelve_months(date(2024, 9, 29), date(2025, 9, 27), 100.0, date(2025, 10, 31), ytd, split)

    assert revenue.fy_value == pytest.approx(100.0)
    assert revenue.value == pytest.approx(110.0)


@pytest.mark.parametrize(
    ("unit", "expected_fy", "expected_ttm"),
    [("USD", 1000.0, 1100.0), ("USD/shares", 500.0, 600.0)],
)
def test_market_ttm_uses_the_metric_unit_to_decide_split_adjustment(unit, expected_fy, expected_ttm):
    class Result:
        def fetchall(self):
            return [
                {
                    "period_start": date(2025, 9, 28),
                    "period_end": date(2026, 6, 27),
                    "value": 800.0,
                    "filed": date(2026, 8, 1),
                    "accession": "Q3-current",
                },
                {
                    "period_start": date(2024, 9, 29),
                    "period_end": date(2025, 6, 28),
                    "value": 700.0,
                    "filed": date(2026, 8, 1),
                    "accession": "Q3-prior",
                },
            ]

    class Connection:
        def execute(self, *_args):
            return Result()

    fy_revenue = {
        "source_concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "period_start": date(2024, 9, 29),
        "period_end": date(2025, 9, 27),
        "value": 1000.0,
        "filed": date(2025, 10, 31),
        "unit": unit,
    }

    result = _ttm(Connection(), 1, fy_revenue, [(date(2026, 3, 1), 2.0)])  # type: ignore[arg-type]

    assert result is not None
    assert result.fy_value == pytest.approx(expected_fy)
    assert result.value == pytest.approx(expected_ttm)
