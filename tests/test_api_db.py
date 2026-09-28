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


def test_prices_window_and_summary(client):
    response = client.get("/companies/AAPL/prices", params={"start": "2025-01-01", "end": "2025-12-31"})
    assert response.status_code == 200
    p = response.json()
    bars = p["bars"]
    assert bars and "2025-01-01" <= p["start"] <= p["end"] <= "2025-12-31"
    assert [b["date"] for b in bars] == sorted(b["date"] for b in bars)
    assert (p["first_close"], p["last_close"]) == (bars[0]["close"], bars[-1]["close"])
    assert p["change_pct"] == pytest.approx((p["last_close"] / p["first_close"] - 1) * 100)
    assert p["high"] == max(b["high"] for b in bars if b["high"] is not None)
    assert p["low"] == min(b["low"] for b in bars if b["low"] is not None)
    assert client.get("/companies/AAPL/prices", params={"start": "1990-01-01", "end": "1990-12-31"}).status_code == 404


def test_insider_summary_is_internally_consistent(client):
    s = client.get("/companies/NVDA/insiders").json()
    assert s["window_start"] < s["window_end"]  # anchored on the last ingest, not the wall clock
    assert s["open_market_sales"] > 0
    assert s["net_open_market_value"] == pytest.approx(s["open_market_buy_value"] - s["open_market_sale_value"])
    assert 0 <= s["sale_value_under_10b5_1_pct"] <= 100
    values = [t["value"] for t in s["top_sellers"]]
    assert 0 < len(values) <= 5 and values == sorted(values, reverse=True)
    assert sum(values) <= s["open_market_sale_value"] * (1 + 1e-9)
    assert client.get("/companies/NVDA/insiders", params={"days": 3}).status_code == 422


def test_ask_without_an_llm_key_returns_503_and_the_rest_still_works(client, monkeypatch):
    from app.api.routers import ask as ask_router
    from app.config import get_settings

    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    ask_router.get_agent.cache_clear()
    try:
        r = client.post("/ask", json={"question": "What is AAPL's trailing P/E right now?"})
        assert r.status_code == 503 and "LLM_API_KEY" in r.json()["detail"]
        assert client.get("/health").json()["llm_configured"] is False
        assert client.get("/companies/AAPL/valuation").status_code == 200
    finally:
        get_settings.cache_clear()
        ask_router.get_agent.cache_clear()
