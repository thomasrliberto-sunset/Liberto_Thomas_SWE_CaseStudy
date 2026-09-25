from __future__ import annotations

from datetime import date

import psycopg
from fastapi import APIRouter, Depends, Query

from app.api.deps import csv_list, get_conn
from app.models import CompanySummary, Fundamentals, InsiderSummary, PriceSeries, Valuation
from app.services import companies, fundamentals, insiders, market

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanySummary], summary="Tracked universe and data coverage")
def list_companies(conn: psycopg.Connection = Depends(get_conn)):
    return companies.list_companies(conn)


@router.get("/{ticker}/fundamentals", response_model=Fundamentals, summary="Annual fundamentals with margins and YoY")
def get_fundamentals(
    ticker: str,
    metrics: str | None = Query(None, description="Comma-separated, e.g. revenue,net_income,gross_margin"),
    fiscal_years: str | None = Query(None, description="Comma-separated fiscal years, e.g. 2024,2025"),
    last_n: int = Query(5, ge=1, le=20, description="Most recent N fiscal years (ignored if fiscal_years set)"),
    conn: psycopg.Connection = Depends(get_conn),
):
    years = [int(y) for y in csv_list(fiscal_years) or []] or None
    return fundamentals.get_fundamentals(conn, ticker, csv_list(metrics), years, last_n)


@router.get("/{ticker}/valuation", response_model=Valuation, summary="Trailing P/E and P/S (EDGAR x market data)")
def get_valuation(ticker: str, conn: psycopg.Connection = Depends(get_conn)):
    return market.get_valuation(conn, ticker)


@router.get("/{ticker}/prices", response_model=PriceSeries, summary="Daily prices")
def get_prices(
    ticker: str,
    start: date | None = None,
    end: date | None = None,
    conn: psycopg.Connection = Depends(get_conn),
):
    return market.get_prices(conn, ticker, start, end)


@router.get("/{ticker}/insiders", response_model=InsiderSummary, summary="Form 4 insider activity (bonus)")
def get_insiders(ticker: str, days: int = Query(365, ge=7, le=730), conn: psycopg.Connection = Depends(get_conn)):
    return insiders.insider_summary(conn, ticker, days)
