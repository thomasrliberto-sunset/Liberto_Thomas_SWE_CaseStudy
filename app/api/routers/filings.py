from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from app.api.deps import Conn
from app.models import FilingInfo, RiskFactorDiff, SearchHit
from app.services import filings

router = APIRouter(prefix="/companies/{ticker}", tags=["filings"])


@router.get("/filings", response_model=list[FilingInfo], summary="Ingested 10-Ks and extracted sections")
def list_filings(ticker: str, conn: Conn):
    return filings.list_filings(conn, ticker)


@router.get("/filings/search", response_model=list[SearchHit], summary="Full-text search over 10-K narrative")
def search(
    ticker: str,
    conn: Conn,
    q: str = Query(..., min_length=2),
    section: str | None = Query(None, description="risk_factors | mdna (default: both)"),
    which: Literal["latest", "prior", "all"] = "latest",
    fiscal_year: int | None = None,
    limit: int = Query(6, ge=1, le=20),
):
    return filings.search_filings(conn, ticker, q, section, which, fiscal_year, limit)


@router.get("/risk-factors/diff", response_model=RiskFactorDiff, summary="New / removed / reworded risk factors")
def risk_diff(ticker: str, conn: Conn):
    return filings.diff_risk_factors(conn, ticker)
