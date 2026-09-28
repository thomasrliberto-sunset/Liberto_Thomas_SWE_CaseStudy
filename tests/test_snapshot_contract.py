"""Contract checks for the committed reviewer snapshot.

These assertions mirror the case-study data requirements rather than individual
implementation details: all five companies must expose the required annual
metrics, two usable 10-Ks, and market data that joins into valuation output.
"""

import psycopg
import pytest

from app.config import get_settings

TICKERS = ("NVDA", "MSFT", "AAPL", "GOOGL", "ETN")
REQUIRED_METRICS = (
    "revenue",
    "net_income",
    "eps_diluted",
    "gross_margin",
    "operating_margin",
)
REQUIRED_SECTIONS = {"1A", "7"}


def _db_available() -> bool:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=2) as conn:
            return conn.execute("SELECT count(*) FROM company").fetchone()[0] > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no populated database at DATABASE_URL")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def companies(client):
    response = client.get("/companies")
    assert response.status_code == 200
    return {company["ticker"]: company for company in response.json()}


def test_snapshot_contains_exact_required_universe(companies):
    assert set(companies) == set(TICKERS)


@pytest.mark.parametrize("ticker", TICKERS)
def test_company_snapshot_satisfies_case_study_contract(client, companies, ticker):
    summary = companies[ticker]
    assert summary["latest_price_date"] is not None
    assert len(summary["tenk_fiscal_years"]) >= 2

    fundamentals_response = client.get(
        f"/companies/{ticker}/fundamentals",
        params={"metrics": ",".join(REQUIRED_METRICS), "last_n": 2},
    )
    assert fundamentals_response.status_code == 200
    periods = fundamentals_response.json()["periods"]
    assert len(periods) >= 2
    for period in periods[:2]:
        assert period["accession"]
        assert set(period["metrics"]) == set(REQUIRED_METRICS)
        for metric in period["metrics"].values():
            assert metric["value"] is not None
            assert metric["source"]
    latest_metrics = periods[0]["metrics"]
    assert all(latest_metrics[name]["yoy_growth"] is not None for name in ("revenue", "net_income", "eps_diluted"))
    assert all(latest_metrics[name]["yoy_change_pp"] is not None for name in ("gross_margin", "operating_margin"))

    filings_response = client.get(f"/companies/{ticker}/filings")
    assert filings_response.status_code == 200
    filings = filings_response.json()
    assert len(filings) >= 2
    for filing in filings[:2]:
        assert filing["form"] == "10-K"
        sections = {section["item"]: section for section in filing["sections"]}
        assert sections.keys() >= REQUIRED_SECTIONS
        for item in REQUIRED_SECTIONS:
            assert sections[item]["char_count"] > 0
            assert sections[item]["chunks"] > 0

    valuation_response = client.get(f"/companies/{ticker}/valuation")
    assert valuation_response.status_code == 200
    valuation = valuation_response.json()
    assert valuation["price"] > 0
    assert valuation["price_date"] == summary["latest_price_date"]
    assert valuation["trailing_pe"] == pytest.approx(valuation["price"] / valuation["eps_diluted_adjusted"])

    prices_response = client.get(
        f"/companies/{ticker}/prices",
        params={"start": valuation["price_date"], "end": valuation["price_date"]},
    )
    assert prices_response.status_code == 200
    prices = prices_response.json()
    assert prices["start"] == prices["end"] == valuation["price_date"]
    assert prices["last_close"] == pytest.approx(valuation["price"])
