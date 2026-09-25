"""Ingestion orchestration: for each configured ticker, run each source as its own
transaction and record the outcome in ingestion_run. One failing source or ticker
never blocks the rest."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, timedelta

import psycopg

from app.config import MetricCatalog, Universe
from app.ingest import edgar, store
from app.ingest.http import SecClient
from app.ingest.insiders import parse_form4, raw_xml_document
from app.ingest.prices import fetch_prices
from app.ingest.sections import chunk_section, extract_risk_factors, extract_sections
from app.ingest.xbrl import build_annual_financials, flatten_companyfacts

log = logging.getLogger(__name__)

SOURCES = ("xbrl", "filings", "prices", "insiders")


class Pipeline:
    def __init__(
        self,
        conn: psycopg.Connection,
        client: SecClient,
        universe: Universe,
        catalog: MetricCatalog,
    ) -> None:
        self.conn = conn
        self.client = client
        self.universe = universe
        self.catalog = catalog

    # ------------------------------------------------------------------ driver

    def run(self, tickers: list[str] | None = None, sources: tuple[str, ...] = SOURCES) -> dict[str, dict[str, str]]:
        wanted = {t.upper() for t in tickers} if tickers else None
        companies = [c for c in self.universe.companies if wanted is None or c.ticker in wanted]
        cik_map: dict[str, int] = {}
        if any(c.cik is None for c in companies):
            cik_map = edgar.resolve_ciks(self.client)

        summary: dict[str, dict[str, str]] = {}
        for cfg in companies:
            ticker = cfg.ticker
            cik = cfg.cik or cik_map.get(ticker)
            if cik is None:
                log.error("%s: no CIK found on SEC's ticker list; skipping", ticker)
                summary[ticker] = {"resolve": "error: unknown ticker"}
                continue
            submissions = edgar.fetch_submissions(self.client, cik)
            info = edgar.company_info(ticker, cik, submissions)
            company_id = store.upsert_company(self.conn, info)
            self.conn.commit()
            log.info("%s -> CIK %d (%s)", ticker, cik, info.name)

            steps: dict[str, Callable[[], tuple[int, str | None]]] = {
                "xbrl": lambda: self._xbrl(company_id, cik),
                "filings": lambda: self._filings(company_id, cik, submissions),
                "prices": lambda: self._prices(company_id, ticker),
                "insiders": lambda: self._insiders(company_id, cik, submissions),
            }
            summary[ticker] = {s: self._run_step(s, ticker, steps[s]) for s in sources}
        return summary

    def _run_step(self, source: str, ticker: str, fn: Callable[[], tuple[int, str | None]]) -> str:
        run_id = store.start_run(self.conn, source, ticker)
        t0 = time.monotonic()
        try:
            rows, detail = fn()
            self.conn.commit()
        except Exception as exc:  # isolate failures per (ticker, source)
            self.conn.rollback()
            log.exception("%s/%s failed", ticker, source)
            store.finish_run(self.conn, run_id, "error", None, f"{type(exc).__name__}: {exc}"[:2000])
            return f"error: {exc}"
        store.finish_run(self.conn, run_id, "ok", rows, detail)
        log.info("%s/%s ok: %d rows in %.1fs %s", ticker, source, rows, time.monotonic() - t0, detail or "")
        return "ok"

    # ------------------------------------------------------------------ sources

    def _xbrl(self, company_id: int, cik: int) -> tuple[int, str | None]:
        facts = flatten_companyfacts(edgar.fetch_companyfacts(self.client, cik))
        n_facts = store.replace_xbrl_facts(self.conn, company_id, facts)
        values = build_annual_financials(facts, self.catalog)
        n_vals = store.replace_annual_financials(self.conn, company_id, values)
        years = sorted({v.fiscal_year for v in values})
        span = f"FY{years[0]}-FY{years[-1]}" if years else "no annual data"
        return n_facts, f"{n_vals} canonical values, {span}"

    def _filings(self, company_id: int, cik: int, submissions: dict) -> tuple[int, str | None]:
        n = self.universe.ingest.tenk_filings
        refs = edgar.list_filings(submissions, cik, {"10-K"})[:n]
        if not refs:
            return 0, "no 10-K filings found"
        notes, rows = [], 0
        for ref in refs:
            html = self.client.get_text(ref.url)
            sections = extract_sections(html, self.universe.ingest.sections)
            missing = [i for i in self.universe.ingest.sections if i not in sections]
            if missing:
                notes.append(f"{ref.accession}: missing items {missing}")
            chunks = {item: chunk_section(sec) for item, sec in sections.items()}
            risks = extract_risk_factors(sections["1A"]) if "1A" in sections else []
            fy = store.fiscal_year_for_period(self.conn, company_id, ref.report_date, ref.accession)
            store.replace_filing(self.conn, company_id, ref, fy, sections, chunks, risks)
            rows += sum(len(c) for c in chunks.values())
            notes.append(
                f"FY{fy} {ref.accession}: "
                + ", ".join(f"Item {i} {len(s.text):,} chars" for i, s in sections.items())
                + f", {len(risks)} risk factors"
            )
        store.prune_filings(self.conn, company_id, "10-K", [r.accession for r in refs])
        return rows, "; ".join(notes)

    def _prices(self, company_id: int, ticker: str) -> tuple[int, str | None]:
        history = fetch_prices(ticker, self.universe.ingest.price_history_years)
        n = store.upsert_prices(self.conn, company_id, history)
        last = history.bars[-1] if history.bars else None
        return n, f"source={history.source}, last={last.date if last else None}, splits={len(history.splits)}"

    def _insiders(self, company_id: int, cik: int, submissions: dict) -> tuple[int, str | None]:
        since = date.today() - timedelta(days=self.universe.ingest.insider_lookback_days)
        refs = [r for r in edgar.list_filings(submissions, cik, {"4"}) if r.filing_date >= since]
        known = store.known_insider_accessions(self.conn, company_id)
        todo = [r for r in refs if r.accession not in known]  # incremental
        rows, failed = 0, 0
        for ref in todo:
            url = edgar.document_url(cik, ref.accession, raw_xml_document(ref.primary_document))
            try:
                txs = parse_form4(self.client.get_text(url))
            except Exception as exc:  # one malformed filing shouldn't sink the batch
                failed += 1
                log.warning("Form 4 %s unparseable: %s", ref.accession, exc)
                continue
            rows += store.insert_insider_transactions(self.conn, company_id, ref.accession, ref.filing_date, txs)
        return rows, f"{len(refs)} Form 4s in window, {len(todo)} new, {failed} failed"
