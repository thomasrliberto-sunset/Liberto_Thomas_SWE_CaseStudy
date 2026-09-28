"""API tests against the populated database (seed snapshot or a live ingest).
Skipped automatically when no database is reachable at DATABASE_URL.

    docker compose run --rm --entrypoint pytest api
"""

import psycopg
import pytest

from app.config import get_settings


def _db_available() -> bool:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=2) as c:
            return c.execute("SELECT count(*) FROM company").fetchone()[0] > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no populated database at DATABASE_URL")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as c:
        yield c


def test_companies_cover_the_universe(client):
    tickers = {c["ticker"] for c in client.get("/companies").json()}
    assert {"NVDA", "MSFT", "AAPL", "GOOGL", "ETN"} <= tickers


def test_fundamentals_have_growth_and_margins(client):
    body = client.get("/companies/NVDA/fundamentals?metrics=revenue,gross_margin&last_n=3").json()
    assert len(body["periods"]) == 3
    latest = body["periods"][0]["metrics"]
    assert latest["revenue"]["value"] > 0 and latest["revenue"]["yoy_growth"] is not None
    assert 0 < latest["gross_margin"]["value"] < 1


def test_compare_ranks_and_flags_fiscal_misalignment(client):
    body = client.get("/compare/gross_margin").json()
    values = [r["value"] for r in body["rows"]]
    assert values == sorted(values, reverse=True) and len(values) == 5
    assert "not calendar-aligned" in body["alignment_note"]


def test_valuation_joins_price_and_eps(client):
    v = client.get("/companies/AAPL/valuation").json()
    assert v["trailing_pe"] == pytest.approx(v["price"] / v["eps_diluted_adjusted"])


def test_search_and_risk_diff(client):
    hits = client.get("/companies/MSFT/filings/search", params={"q": "revenue increased", "section": "mdna"}).json()
    assert hits and all(h["item"] == "7" for h in hits)
    diff = client.get("/companies/NVDA/risk-factors/diff").json()
    assert diff["counts"]["latest"] > 10


def test_valuation_has_ttm_when_a_10q_follows_the_10k(client):
    v = client.get("/companies/AAPL/valuation").json()
    assert v["ttm"] is not None and v["ttm"]["period_end"] > v["eps_period_end"]
    assert v["ttm"]["pe"] == pytest.approx(v["price"] / v["ttm"]["eps_diluted"])


def test_quality_report_discloses_fallbacks(client):
    q = client.get("/companies/ETN/quality").json()
    checks = {c["check"]: c for c in q["checks"]}
    assert checks["accounting_identities"]["status"] == "ok"
    assert "operating_income = gross_profit - sga_expense - rnd_expense" in checks["derived_metrics"]["detail"]
    assert "title-heading fallback" in checks["filing_text"]["detail"]


def test_errors(client):
    assert client.get("/companies/TSLA/fundamentals").status_code == 404
    assert client.get("/compare/not_a_metric").status_code == 400
    assert client.get("/companies/NVDA/fundamentals", params={"fiscal_years": "2025,not-a-year"}).status_code == 400
    unknown_ticker = client.get("/compare/revenue", params={"tickers": "NVDA,TSLA"})
    assert unknown_ticker.status_code == 400
    assert unknown_ticker.json()["detail"] == "unknown tickers ['TSLA']"
