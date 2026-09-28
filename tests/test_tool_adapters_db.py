"""Smoke-test every real agent tool against the committed database snapshot.

These tests sit between the service/API tests and the scripted-agent tests: they
exercise the exact adapter, serialization, citation, and evidence path seen by the
LLM, without making an LLM or network call.
"""

import json
import re

import psycopg
import pytest

from app.agent.tools import SourceLedger, build_tools, run_tool
from app.config import get_settings
from app.db.connection import connect

TICKERS = ["NVDA", "MSFT", "AAPL", "GOOGL", "ETN"]
EXPECTED_TOOLS = {
    "get_financials",
    "compare_companies",
    "get_valuation",
    "get_price_performance",
    "get_insider_activity",
    "search_filings",
    "diff_risk_factors",
}


def _db_available() -> bool:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=2) as conn:
            return conn.execute("SELECT count(*) FROM company").fetchone()[0] >= len(TICKERS)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no populated database at DATABASE_URL")


@pytest.fixture(scope="module")
def conn():
    database = connect()
    try:
        yield database
    finally:
        database.close()


def test_tool_registry_exposes_the_complete_agent_surface():
    assert set(build_tools(TICKERS)) == EXPECTED_TOOLS


@pytest.mark.parametrize(
    ("name", "arguments", "citation_kind"),
    [
        pytest.param(
            "get_financials",
            {"tickers": ["MSFT"], "metrics": ["revenue", "gross_margin"], "last_n": 2},
            "financials",
            id="financials",
        ),
        pytest.param(
            "compare_companies",
            {"metric": "gross_margin", "tickers": TICKERS},
            "comparison",
            id="comparison",
        ),
        pytest.param("get_valuation", {"tickers": ["AAPL"]}, "valuation", id="valuation"),
        pytest.param(
            "get_price_performance",
            {"ticker": "AAPL", "start_date": "2025-01-01", "end_date": "2025-12-31"},
            "prices",
            id="prices",
        ),
        pytest.param("get_insider_activity", {"ticker": "NVDA", "days": 365}, "insiders", id="insiders"),
        pytest.param(
            "search_filings",
            {
                "ticker": "MSFT",
                "query": "revenue increased",
                "section": "mdna",
                "filing": "latest",
                "limit": 3,
            },
            "filing_text",
            id="filing-search",
        ),
        pytest.param("diff_risk_factors", {"ticker": "NVDA"}, "risk_diff", id="risk-diff"),
    ],
)
def test_real_tool_adapter_returns_citable_visible_evidence(conn, name, arguments, citation_kind):
    ledger = SourceLedger()

    content, ok, error = run_tool(build_tools(TICKERS)[name], conn, arguments, ledger)

    assert ok is True and error is None
    assert ledger.evidence == [content]
    payload = json.loads(content)  # each selected result should fit without truncation
    visible_ids = set(re.findall(r'"source_id":\s*"([A-Z]\d+)"', content))
    assert visible_ids == set(ledger.citations)
    assert visible_ids
    assert {citation.kind for citation in ledger.citations.values()} == {citation_kind}

    # A small semantic assertion per adapter guards against a successful but empty
    # serialization while leaving exact financial values free to change on refresh.
    if name == "get_financials":
        assert len(payload) == 1 and payload[0]["ticker"] == "MSFT"
        assert len(payload[0]["rows"]) == 2 and all("revenue" in row for row in payload[0]["rows"])
    elif name == "compare_companies":
        assert len(payload["ranking"]) == len(TICKERS)
        assert [row["rank"] for row in payload["ranking"]] == list(range(1, len(TICKERS) + 1))
    elif name == "get_valuation":
        assert len(payload) == 1 and payload[0]["ticker"] == "AAPL"
        assert payload[0]["price"].startswith("$") and payload[0]["trailing_pe"].endswith("x")
    elif name == "get_price_performance":
        assert payload["ticker"] == "AAPL" and payload["start"] <= payload["end"]
        assert payload["first_close"].startswith("$") and payload["last_close"].startswith("$")
    elif name == "get_insider_activity":
        assert payload["ticker"] == "NVDA" and payload["open_market_sales"]
        assert payload["window_days"] == 365
    elif name == "search_filings":
        assert payload and all(hit["section"] == "Item 7 MD&A" for hit in payload)
        assert all(hit["text"] for hit in payload)
    else:
        assert name == "diff_risk_factors"
        assert payload["counts"]["latest"] > 0 and payload["counts"]["prior"] > 0
        assert payload["method"]
