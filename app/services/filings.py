"""Filing narrative: listing, full-text search over chunks, and the risk-factor diff."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Literal

import psycopg

from app.models import (
    BadRequest,
    FilingInfo,
    FilingRefOut,
    FilingSectionInfo,
    NotFound,
    RiskFactorDiff,
    RiskFactorItem,
    SearchHit,
)
from app.services.companies import get_company

SECTION_ALIASES = {
    "1a": "1A", "risk": "1A", "risk_factors": "1A", "risks": "1A",
    "7": "7", "mdna": "7", "md&a": "7", "mda": "7", "management_discussion": "7",
}
Which = Literal["latest", "prior", "all"]


def normalize_section(section: str | None) -> str | None:
    if section in (None, "", "any", "all"):
        return None
    key = section.strip().lower()
    if key not in SECTION_ALIASES:
        raise BadRequest(f"unknown section {section!r}; use 'risk_factors' (Item 1A) or 'mdna' (Item 7)")
    return SECTION_ALIASES[key]


def _tenks(conn: psycopg.Connection, company_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT id, accession, form, fiscal_year, filing_date, report_date, url
        FROM filing WHERE company_id = %s AND form = '10-K' ORDER BY filing_date DESC
        """,
        (company_id,),
    ).fetchall()


def list_filings(conn: psycopg.Connection, ticker: str) -> list[FilingInfo]:
    company = get_company(conn, ticker)
    out = []
    for f in _tenks(conn, company["id"]):
        sections = conn.execute(
            """
            SELECT s.item, s.title, s.char_count, count(c.id) AS chunks
            FROM filing_section s LEFT JOIN filing_chunk c ON c.section_id = s.id
            WHERE s.filing_id = %s GROUP BY s.id ORDER BY s.item
            """,
            (f["id"],),
        ).fetchall()
        n_risks = conn.execute("SELECT count(*) AS n FROM risk_factor WHERE filing_id = %s", (f["id"],)).fetchone()
        out.append(
            FilingInfo(
                accession=f["accession"], form=f["form"], fiscal_year=f["fiscal_year"],
                filing_date=f["filing_date"], report_date=f["report_date"], url=f["url"],
                sections=[FilingSectionInfo(**s) for s in sections], risk_factors=n_risks["n"],
            )
        )
    return out


_WORD = re.compile(r"[A-Za-z0-9]+")


def to_or_tsquery(query: str) -> str | None:
    """OR together the query's words: natural-language questions rarely have every word
    in one chunk, and ts_rank_cd already rewards chunks that match more of them."""
    words = [w.lower() for w in _WORD.findall(query) if len(w) > 1]
    return " | ".join(dict.fromkeys(words)) or None


def search_filings(
    conn: psycopg.Connection,
    ticker: str,
    query: str,
    section: str | None = None,
    which: Which = "latest",
    fiscal_year: int | None = None,
    limit: int = 6,
) -> list[SearchHit]:
    company = get_company(conn, ticker)
    item = normalize_section(section)
    filings = _tenks(conn, company["id"])
    if fiscal_year is not None:
        filings = [f for f in filings if f["fiscal_year"] == fiscal_year]
    elif which == "latest":
        filings = filings[:1]
    elif which == "prior":
        filings = filings[1:2]
    if not filings:
        raise NotFound(f"no matching 10-K text for {company['ticker']}")
    tsq = to_or_tsquery(query)
    if tsq is None:
        return []
    rows = conn.execute(
        """
        WITH q AS (SELECT to_tsquery('english', %(tsq)s) AS query)
        SELECT c.id AS chunk_id, c.heading, c.text, s.item, s.title AS section,
               f.fiscal_year, f.filing_date, f.url,
               ts_rank_cd(c.tsv, q.query, 32)::float8 AS rank
        FROM filing_chunk c
        JOIN filing_section s ON s.id = c.section_id
        JOIN filing f ON f.id = s.filing_id, q
        WHERE f.id = ANY(%(filings)s) AND (%(item)s::text IS NULL OR s.item = %(item)s) AND c.tsv @@ q.query
        ORDER BY rank DESC, c.id
        LIMIT %(limit)s
        """,
        {"tsq": tsq, "filings": [f["id"] for f in filings], "item": item, "limit": max(1, min(limit, 20))},
    ).fetchall()
    return [SearchHit(ticker=company["ticker"], **r) for r in rows]


# --------------------------------------------------------------------------- risk diff

_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    return " ".join(_PUNCT.sub(" ", s.lower().replace("’", "'")).split())


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(_norm(a).split()), set(_norm(b).split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def similarity(a_heading: str, a_body: str, b_heading: str, b_body: str) -> tuple[float, float, float]:
    """-> (match score, heading similarity, body overlap). The score is heading similarity,
    rescued by body overlap: a reworded title over the same body is a modification, not a new risk."""
    h = SequenceMatcher(None, _norm(a_heading), _norm(b_heading)).ratio()
    body = _jaccard(a_body, b_body)
    return max(h, body), h, body


UNCHANGED, MODIFIED, BODY_REVISED = 0.9, 0.55, 0.6


def compare_risk_factors(
    cur: list[dict], old: list[dict], excerpt_chars: int = 600
) -> tuple[list[RiskFactorItem], list[RiskFactorItem], list[RiskFactorItem], int]:
    """Pure diff core. Each current factor is matched to its most similar prior factor:
    score >= UNCHANGED  -> same risk (flagged 'modified' if the body was substantially rewritten)
    score >= MODIFIED   -> reworded / reframed risk ('modified')
    otherwise           -> 'new'. Prior factors with no match >= MODIFIED are 'removed'."""

    def best_match(rf: dict, pool: list[dict]) -> tuple[dict | None, float, float, float]:
        best: tuple[dict | None, float, float, float] = (None, 0.0, 0.0, 0.0)
        for p in pool:
            score, head, body = similarity(rf["heading"], rf["body"], p["heading"], p["body"])
            if score > best[1]:
                best = (p, score, head, body)
        return best

    new, modified, unchanged = [], [], 0
    for rf in cur:
        match, score, head, body = best_match(rf, old)
        if head >= UNCHANGED and body >= BODY_REVISED:
            unchanged += 1
        elif score >= MODIFIED:
            modified.append(RiskFactorItem(
                heading=rf["heading"], category=rf["category"], excerpt=rf["body"][:excerpt_chars],
                matched_prior_heading=match["heading"] if match else None, similarity=round(score, 2),
            ))
        else:
            new.append(RiskFactorItem(
                heading=rf["heading"], category=rf["category"], excerpt=rf["body"][:excerpt_chars],
                similarity=round(score, 2),
            ))
    removed = [
        RiskFactorItem(heading=rf["heading"], category=rf["category"], similarity=round(score, 2))
        for rf in old
        if (score := best_match(rf, cur)[1]) < MODIFIED
    ]
    return new, modified, removed, unchanged


def diff_risk_factors(conn: psycopg.Connection, ticker: str, excerpt_chars: int = 600) -> RiskFactorDiff:
    """Deterministic diff of Item 1A risk factors between the two latest 10-Ks."""
    company = get_company(conn, ticker)
    filings = _tenks(conn, company["id"])
    if len(filings) < 2:
        raise NotFound(f"need two 10-Ks to diff risk factors for {company['ticker']}, have {len(filings)}")
    latest, prior = filings[0], filings[1]

    def load(fid: int) -> list[dict]:
        return conn.execute(
            "SELECT heading, category, body FROM risk_factor WHERE filing_id = %s ORDER BY seq", (fid,)
        ).fetchall()

    cur, old = load(latest["id"]), load(prior["id"])
    if not cur or not old:
        raise NotFound(f"risk factors could not be extracted for one of {company['ticker']}'s 10-Ks")

    new, modified, removed, unchanged = compare_risk_factors(cur, old, excerpt_chars)

    def ref(f: dict) -> FilingRefOut:
        return FilingRefOut(accession=f["accession"], fiscal_year=f["fiscal_year"], period_end=f["report_date"],
                            filing_date=f["filing_date"], url=f["url"])

    return RiskFactorDiff(
        ticker=company["ticker"],
        latest=ref(latest),
        prior=ref(prior),
        counts={"latest": len(cur), "prior": len(old), "new": len(new), "modified": len(modified),
                "removed": len(removed), "unchanged": unchanged},
        new=new,
        modified=modified,
        removed=removed,
        method=(
            "Risk-factor headings extracted from Item 1A of each 10-K and matched by heading similarity, "
            f"with body-text overlap as a fallback; >= {UNCHANGED} same risk, >= {MODIFIED} reworded; a same-titled "
            f"risk whose body overlap falls below {BODY_REVISED} counts as modified; otherwise new (or removed)."
        ),
    )
