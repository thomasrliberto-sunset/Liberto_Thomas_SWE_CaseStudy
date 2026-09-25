"""Persistence for the ingest pipeline. Every write is idempotent (replace-by-key),
so re-running ingestion converges instead of duplicating rows."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from app.db.connection import DbConn, one
from app.ingest.edgar import CompanyInfo, FilingRef
from app.ingest.insiders import InsiderTx
from app.ingest.prices import PriceHistory
from app.ingest.sections import Chunk, RiskFactor, Section
from app.ingest.xbrl import AnnualValue, Fact


def _r(x: float | None, digits: int = 4) -> float | None:
    return None if x is None else round(x, digits)


def upsert_company(conn: DbConn, info: CompanyInfo) -> int:
    row = one(
        conn.execute(
            """
        INSERT INTO company (ticker, cik, name, fiscal_year_end, sic_description)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (ticker) DO UPDATE SET
            cik = EXCLUDED.cik, name = EXCLUDED.name, fiscal_year_end = EXCLUDED.fiscal_year_end,
            sic_description = EXCLUDED.sic_description, updated_at = now()
        RETURNING id
        """,
            (info.ticker, info.cik, info.name, info.fiscal_year_end, info.sic_description),
        )
    )
    return row["id"]


def replace_xbrl_facts(conn: DbConn, company_id: int, facts: Iterable[Fact]) -> int:
    conn.execute("DELETE FROM xbrl_fact WHERE company_id = %s", (company_id,))
    rows, seen = [], set()
    for f in facts:
        key = (f.taxonomy, f.concept, f.unit, f.start, f.end, f.accession)
        if key in seen:  # companyfacts occasionally repeats a fact under two frames
            continue
        seen.add(key)
        rows.append(
            (
                company_id,
                f.taxonomy,
                f.concept,
                f.unit,
                f.start,
                f.end,
                f.value,
                f.fy,
                f.fp,
                f.form,
                f.accession,
                f.filed,
                f.frame,
            )
        )
    with conn.cursor() as cur:
        # executemany runs in pipeline mode in psycopg 3; fast enough for ~30k rows/company
        cur.executemany(
            "INSERT INTO xbrl_fact (company_id, taxonomy, concept, unit, period_start, period_end, value,"
            " fy, fp, form, accession, filed, frame) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )
    return len(rows)


def replace_annual_financials(conn: DbConn, company_id: int, values: list[AnnualValue]) -> int:
    conn.execute("DELETE FROM annual_financial WHERE company_id = %s", (company_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO annual_financial (company_id, metric, fiscal_year, period_start, period_end,
                value, unit, source_concept, derivation, accession, filed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    company_id,
                    v.metric,
                    v.fiscal_year,
                    v.start,
                    v.end,
                    v.value,
                    v.unit,
                    v.source_concept,
                    v.derivation,
                    v.accession,
                    v.filed,
                )
                for v in values
            ],
        )
    return len(values)


def fiscal_year_for_period(conn: DbConn, company_id: int, period_end: date | None, accession: str) -> int | None:
    """Label a filing with the same fiscal year the financials use."""
    if period_end:
        row = conn.execute(
            "SELECT fiscal_year FROM annual_financial WHERE company_id = %s AND period_end = %s LIMIT 1",
            (company_id, period_end),
        ).fetchone()
        if row:
            return row["fiscal_year"]
    row = conn.execute(
        "SELECT fy FROM xbrl_fact WHERE accession = %s AND fy IS NOT NULL LIMIT 1", (accession,)
    ).fetchone()
    if row:
        return row["fy"]
    return period_end.year if period_end else None


def replace_filing(
    conn: DbConn,
    company_id: int,
    ref: FilingRef,
    fiscal_year: int | None,
    sections: dict[str, Section],
    chunks: dict[str, list[Chunk]],
    risk_factors: list[RiskFactor],
) -> int:
    conn.execute("DELETE FROM filing WHERE accession = %s", (ref.accession,))  # cascades
    filing_id = one(
        conn.execute(
            """
        INSERT INTO filing (company_id, accession, form, filing_date, report_date, fiscal_year,
                            primary_document, url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
            (
                company_id,
                ref.accession,
                ref.form,
                ref.filing_date,
                ref.report_date,
                fiscal_year,
                ref.primary_document,
                ref.url,
            ),
        )
    )["id"]

    with conn.cursor() as cur:
        for item, sec in sections.items():
            text = sec.text
            section_id = one(
                cur.execute(
                    """
                INSERT INTO filing_section (filing_id, item, title, text, char_count, extraction_method)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                    (filing_id, item, sec.title, text, len(text), sec.method),
                )
            )["id"]
            cur.executemany(
                "INSERT INTO filing_chunk (section_id, seq, heading, text) VALUES (%s, %s, %s, %s)",
                [(section_id, c.seq, c.heading, c.text) for c in chunks.get(item, [])],
            )
        cur.executemany(
            "INSERT INTO risk_factor (filing_id, seq, category, heading, body) VALUES (%s, %s, %s, %s, %s)",
            [(filing_id, r.seq, r.category, r.heading, r.body) for r in risk_factors],
        )
    return filing_id


def prune_filings(conn: DbConn, company_id: int, form: str, keep: list[str]) -> int:
    cur = conn.execute(
        "DELETE FROM filing WHERE company_id = %s AND form = %s AND NOT (accession = ANY(%s))",
        (company_id, form, keep),
    )
    return cur.rowcount


def upsert_prices(conn: DbConn, company_id: int, history: PriceHistory) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO price_daily (company_id, date, open, high, low, close, adj_close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (company_id, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
                adj_close = EXCLUDED.adj_close, volume = EXCLUDED.volume, source = EXCLUDED.source
            """,
            [
                (
                    company_id,
                    b.date,
                    _r(b.open),
                    _r(b.high),
                    _r(b.low),
                    _r(b.close),
                    _r(b.adj_close),
                    b.volume,
                    history.source,
                )
                for b in history.bars
            ],
        )
        cur.executemany(
            """
            INSERT INTO stock_split (company_id, date, ratio) VALUES (%s, %s, %s)
            ON CONFLICT (company_id, date) DO UPDATE SET ratio = EXCLUDED.ratio
            """,
            [(company_id, s.date, s.ratio) for s in history.splits],
        )
    return len(history.bars)


def known_insider_accessions(conn: DbConn, company_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT accession FROM insider_transaction WHERE company_id = %s", (company_id,)
    ).fetchall()
    return {r["accession"] for r in rows}


def insert_insider_transactions(
    conn: DbConn, company_id: int, accession: str, filing_date: date, txs: list[InsiderTx]
) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO insider_transaction (company_id, accession, line_no, filing_date, insider_name,
                insider_cik, relationship, transaction_date, security_title, code, acquired_disposed,
                shares, price, shares_owned_after, is_10b5_1, ownership)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (accession, line_no) DO NOTHING
            """,
            [
                (
                    company_id,
                    accession,
                    t.line_no,
                    filing_date,
                    t.insider_name,
                    t.insider_cik,
                    t.relationship,
                    t.transaction_date,
                    t.security_title,
                    t.code,
                    t.acquired_disposed,
                    t.shares,
                    t.price,
                    t.shares_owned_after,
                    t.is_10b5_1,
                    t.ownership,
                )
                for t in txs
            ],
        )
    return len(txs)


def start_run(conn: DbConn, source: str, ticker: str) -> int:
    row = one(conn.execute("INSERT INTO ingestion_run (source, ticker) VALUES (%s, %s) RETURNING id", (source, ticker)))
    conn.commit()
    return row["id"]


def finish_run(conn: DbConn, run_id: int, status: str, rows: int | None, detail: str | None) -> None:
    conn.execute(
        "UPDATE ingestion_run SET finished_at = now(), status = %s, rows_written = %s, detail = %s WHERE id = %s",
        (status, rows, detail, run_id),
    )
    conn.commit()
