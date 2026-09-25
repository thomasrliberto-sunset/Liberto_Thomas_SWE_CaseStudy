from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import Conn, csv_list
from app.config import get_settings, load_metrics
from app.models import Comparison, IngestionRun
from app.services import companies, fundamentals

router = APIRouter(tags=["meta"])


@router.get("/health")
def health(conn: Conn):
    conn.execute("SELECT 1")
    s = get_settings()
    return {
        "status": "ok",
        "database": "ok",
        "llm_configured": bool(s.llm_api_key),
        "llm_base_url": s.llm_base_url,
        "llm_model": s.llm_model,
        "llm_fallback_models": [m.strip() for m in s.llm_fallback_models.split(",") if m.strip()],
    }


@router.get("/metrics", summary="Metric catalog (reported from XBRL, derived at read time)")
def metric_catalog():
    cat = load_metrics()
    return {
        "reported": {
            k: {"label": m.label, "unit": m.unit, "concepts": m.concepts, "fallback": m.fallback}
            for k, m in cat.reported.items()
        },
        "derived": {
            k: {"label": d.label, "formula": f"{d.numerator} / {d.denominator}"} for k, d in cat.derived.items()
        },
    }


@router.get("/compare/{metric}", response_model=Comparison, summary="Rank companies on one metric")
def compare(
    metric: str,
    conn: Conn,
    fiscal_year: int | None = Query(None, description="Default: each company's latest fiscal year"),
    tickers: str | None = Query(None, description="Comma-separated subset; default all"),
):
    return fundamentals.compare_metric(conn, metric, fiscal_year, csv_list(tickers, upper=True))


@router.get("/ingestion/runs", response_model=list[IngestionRun], summary="Pipeline provenance")
def ingestion_runs(conn: Conn, limit: int = 50):
    return companies.list_ingestion_runs(conn, limit)
