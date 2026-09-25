from datetime import date

import pytest

from app.services.filings import compare_risk_factors, to_or_tsquery
from app.services.metrics import consecutive_years, fmt_value, pe_ratio, split_factor, yoy_growth


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
        rf("Reliance on a limited number of partners for manufacturing could harm us.",
           "suppliers foundry capacity wafers lead times " * 10),  # reworded title, same body
        rf("Commercial arrangements expose us to counterparty risks.",
           "financing guarantees customers partners counterparty credit default buildout " * 10),
    ]
    new, modified, removed, unchanged = compare_risk_factors(cur, old)
    assert unchanged == 1
    assert [m.heading for m in modified] == ["Reliance on a limited number of partners for manufacturing could harm us."]
    assert modified[0].matched_prior_heading == "We depend on a limited number of suppliers."
    assert [n.heading for n in new] == ["Commercial arrangements expose us to counterparty risks."]
    assert [r.heading for r in removed] == ["Pandemics could disrupt our operations."]


def test_or_tsquery_is_sanitized():
    assert to_or_tsquery("What drove Azure's growth?!") == "what | drove | azure | growth"
    assert to_or_tsquery("a !") is None
