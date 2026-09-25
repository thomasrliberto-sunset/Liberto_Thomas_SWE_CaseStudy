from __future__ import annotations

from functools import lru_cache

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from app.agent.agent import Agent
from app.agent.llm import LLMError, LLMNotConfigured, OpenAICompatibleChat
from app.api.deps import get_conn
from app.config import get_settings
from app.models import AskRequest, AskResponse

router = APIRouter(tags=["ask"])


@lru_cache
def get_agent() -> Agent:
    return Agent(OpenAICompatibleChat(), max_tool_rounds=get_settings().llm_max_tool_rounds)


@router.post("/ask", response_model=AskResponse, summary="Natural-language Q&A over numbers and filings")
def ask(req: AskRequest, conn: psycopg.Connection = Depends(get_conn)):
    try:
        agent = get_agent()
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        return agent.ask(conn, req.question)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}") from exc
