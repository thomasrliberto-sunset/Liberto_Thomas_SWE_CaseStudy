from __future__ import annotations

from app.db.connection import DbConn
from app.models import CompanySummary, IngestionRun, NotFound


def get_company(conn: DbConn, ticker: str) -> dict:
    row = conn.execute(
        "SELECT id, ticker, cik, name, fiscal_year_end FROM company WHERE ticker = %s", (ticker.upper(),)
    ).fetchone()
    if row is None:
        known = [r["ticker"] for r in conn.execute("SELECT ticker FROM company ORDER BY ticker").fetchall()]
        raise NotFound(f"{ticker.upper()} is not in the tracked universe {known}")
    return row


def list_companies(conn: DbConn) -> list[CompanySummary]:
    rows = conn.execute(
        """
        SELECT c.ticker, c.name, c.cik, c.fiscal_year_end,
            (SELECT array_agg(DISTINCT fiscal_year ORDER BY fiscal_year)
               FROM annual_financial a WHERE a.company_id = c.id AND a.metric = 'revenue') AS fys,
            (SELECT array_agg(fiscal_year ORDER BY fiscal_year DESC)
               FROM filing f WHERE f.company_id = c.id AND f.form = '10-K') AS tenk_fys,
            (SELECT max(date) FROM price_daily p WHERE p.company_id = c.id) AS last_price,
            (SELECT count(*) FROM insider_transaction i WHERE i.company_id = c.id) AS insider_n
        FROM company c ORDER BY c.ticker
        """
    ).fetchall()
    return [
        CompanySummary(
            ticker=r["ticker"],
            name=r["name"],
            cik=r["cik"],
            fiscal_year_end=r["fiscal_year_end"],
            fiscal_years_available=r["fys"] or [],
            tenk_fiscal_years=[y for y in (r["tenk_fys"] or []) if y is not None],
            latest_price_date=r["last_price"],
            insider_transactions=r["insider_n"],
        )
        for r in rows
    ]


def list_ingestion_runs(conn: DbConn, limit: int = 50) -> list[IngestionRun]:
    rows = conn.execute("SELECT * FROM ingestion_run ORDER BY id DESC LIMIT %s", (min(max(limit, 1), 500),)).fetchall()
    return [IngestionRun(**r) for r in rows]
