"""End-to-end ingestion test: the real Pipeline and store against Postgres, with SEC and
Yahoo replaced by fixtures. Checks wiring, idempotency and incremental Form 4 loading.
Uses a throwaway ticker and removes it afterwards. Skipped when no database is reachable."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.config import CompanyConfig, IngestConfig, Universe, get_settings, load_metrics
from app.db.connection import apply_schema, connect
from app.ingest import pipeline as pipeline_mod
from app.ingest.pipeline import Pipeline
from app.ingest.prices import PriceBar, PriceHistory

FIXTURES = Path(__file__).parent / "fixtures"
TICKER, CIK = "ZZZT", 999999
ACC_10K, ACC_F4 = "0000999999-26-000001", "0000999999-26-000002"


def _db_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(get_settings().database_url, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no database at DATABASE_URL")


def fact(start, end, val, fy, form="10-K", accn=ACC_10K, filed="2026-02-20"):
    return {"start": start, "end": end, "val": val, "fy": fy, "fp": "FY", "form": form, "accn": accn, "filed": filed}


class FakeSec:
    """Serves fixture payloads for the handful of EDGAR URLs the pipeline requests."""

    def __init__(self):
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.urls.append(url)
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": CIK, "ticker": TICKER, "title": "Test Corp"}}
        if "/submissions/" in url:
            f4_date = (date.today() - timedelta(days=10)).isoformat()
            return {
                "name": "Test Corp",
                "fiscalYearEnd": "1231",
                "sicDescription": "Testing",
                "filings": {
                    "recent": {
                        "accessionNumber": [ACC_F4, ACC_10K],
                        "filingDate": [f4_date, "2026-02-20"],
                        "reportDate": [f4_date, "2025-12-31"],
                        "form": ["4", "10-K"],
                        "primaryDocument": ["xslF345X05/form4.xml", "tenk.htm"],
                    }
                },
            }
        if "/companyfacts/" in url:
            return {
                "facts": {
                    "us-gaap": {
                        "Revenues": {
                            "units": {
                                "USD": [
                                    fact("2024-01-01", "2024-12-31", 90.0, 2025),
                                    fact("2025-01-01", "2025-12-31", 100.0, 2025),
                                ]
                            }
                        },
                        "CostOfRevenue": {
                            "units": {
                                "USD": [
                                    fact("2024-01-01", "2024-12-31", 50.0, 2025),
                                    fact("2025-01-01", "2025-12-31", 55.0, 2025),
                                ]
                            }
                        },
                        "NetIncomeLoss": {"units": {"USD": [fact("2025-01-01", "2025-12-31", 20.0, 2025)]}},
                    }
                }
            }
        raise AssertionError(f"unexpected JSON url {url}")

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        if url.endswith("/tenk.htm"):
            return (FIXTURES / "tenk_sample.html").read_text()
        if url.endswith("/form4.xml"):
            return (FIXTURES / "form4_sample.xml").read_text()
        raise AssertionError(f"unexpected text url {url}")


def fake_prices(ticker: str, years: int) -> PriceHistory:
    bars = [
        PriceBar(date=date(2026, 9, d), open=10, high=11, low=9, close=10 + d / 10, adj_close=None, volume=100)
        for d in (22, 23, 24)
    ]
    return PriceHistory(source="test", bars=bars, splits=[])


def counts(conn) -> dict[str, int]:
    q = {
        "annual": "SELECT count(*) FROM annual_financial a JOIN company c ON c.id = a.company_id WHERE c.ticker = %s",
        "sections": "SELECT count(*) FROM filing_section s JOIN filing f ON f.id = s.filing_id "
        "JOIN company c ON c.id = f.company_id WHERE c.ticker = %s",
        "risks": "SELECT count(*) FROM risk_factor r JOIN filing f ON f.id = r.filing_id "
        "JOIN company c ON c.id = f.company_id WHERE c.ticker = %s",
        "prices": "SELECT count(*) FROM price_daily p JOIN company c ON c.id = p.company_id WHERE c.ticker = %s",
        "insiders": "SELECT count(*) FROM insider_transaction i JOIN company c ON c.id = i.company_id "
        "WHERE c.ticker = %s",
    }
    return {k: conn.execute(sql, (TICKER,)).fetchone()["count"] for k, sql in q.items()}


def test_pipeline_end_to_end_is_idempotent(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "fetch_prices", fake_prices)
    universe = Universe(
        companies=(CompanyConfig(TICKER),),
        ingest=IngestConfig(tenk_filings=1, price_history_years=1, insider_lookback_days=365, sections=("1A", "7")),
    )
    conn = connect()
    try:
        apply_schema(conn)
        summary = Pipeline(conn, FakeSec(), universe, load_metrics()).run()  # type: ignore[arg-type]
        assert summary == {TICKER: {"xbrl": "ok", "filings": "ok", "prices": "ok", "insiders": "ok"}}
        first = counts(conn)
        # revenue, cost of revenue, gross profit x FY2024-25, plus FY2025 net income
        assert first == {"annual": 7, "sections": 2, "risks": 3, "prices": 3, "insiders": 2}
        gp = conn.execute(
            "SELECT value::float8 AS v, derivation FROM annual_financial a JOIN company c ON c.id = a.company_id "
            "WHERE c.ticker = %s AND metric = 'gross_profit' AND fiscal_year = 2025",
            (TICKER,),
        ).fetchone()
        assert gp == {"v": 45.0, "derivation": "revenue - cost_of_revenue"}

        # second run converges to the same state; Form 4s are loaded incrementally
        sec = FakeSec()
        Pipeline(conn, sec, universe, load_metrics()).run()  # type: ignore[arg-type]
        assert counts(conn) == first
        assert not any(u.endswith("form4.xml") for u in sec.urls)
    finally:
        conn.rollback()
        conn.execute("DELETE FROM company WHERE ticker = %s", (TICKER,))
        conn.execute("DELETE FROM ingestion_run WHERE ticker = %s", (TICKER,))
        conn.commit()
        conn.close()
