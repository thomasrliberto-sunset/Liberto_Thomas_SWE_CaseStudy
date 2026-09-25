from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.routers import ask, companies, filings, meta
from app.config import get_settings
from app.db.connection import close_pool, get_pool
from app.models import BadRequest, NotFound


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    get_pool()
    yield
    close_pool()


app = FastAPI(
    title="Fundamentals Tracker",
    version="1.0.0",
    description=(
        "Company fundamentals from SEC XBRL, 10-K narrative (Risk Factors, MD&A), market prices and "
        "Form 4 insider trades, plus a routed, tool-using natural-language Q&A endpoint (POST /ask)."
    ),
    lifespan=lifespan,
)


@app.exception_handler(NotFound)
async def not_found(_: Request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(BadRequest)
async def bad_request(_: Request, exc: BadRequest):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


app.include_router(meta.router)
app.include_router(companies.router)
app.include_router(filings.router)
app.include_router(ask.router)
