"""Agent behavior with a scripted fake LLM: routing, tool scoping, declines, grounding.
No network or database needed."""

import json
from datetime import date

import pytest

from app.agent import agent as agent_mod
from app.agent.agent import Agent
from app.agent.grounding import check_grounding, extract_numbers
from app.agent.llm import ChatResult, ToolCall
from app.agent.tools import ToolSpec
from app.models import CompanySummary

COMPANIES = [
    CompanySummary(
        ticker=t,
        name=n,
        cik=i,
        fiscal_year_end="1231",
        fiscal_years_available=[2024, 2025],
        tenk_fiscal_years=[2025, 2024],
        latest_price_date=date(2026, 9, 25),
        insider_transactions=0,
    )
    for i, (t, n) in enumerate([("NVDA", "NVIDIA"), ("MSFT", "Microsoft"), ("AAPL", "Apple")], start=1)
]


class FakeLLM:
    models = ["fake-model"]

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None, json_mode=False, model=None):
        self.calls.append(
            {"messages": messages, "tools": [t["function"]["name"] for t in tools or []], "json_mode": json_mode}
        )
        step = self.script.pop(0)
        if isinstance(step, dict):  # router JSON
            return ChatResult(content=json.dumps(step), tool_calls=[], raw_message={"role": "assistant"})
        if isinstance(step, list):  # tool calls
            calls = [
                ToolCall(id=f"c{i}", name=n, arguments=a, raw_arguments=json.dumps(a)) for i, (n, a) in enumerate(step)
            ]
            raw = {
                "role": "assistant",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw_arguments}}
                    for c in calls
                ],
            }
            return ChatResult(content=None, tool_calls=calls, raw_message=raw)
        return ChatResult(content=step, tool_calls=[], raw_message={"role": "assistant", "content": step})


def fake_tools(_tickers):
    def fin(conn, args, ledger):
        ledger.evidence.append({"revenue": 281_724_000_000, "yoy_growth": 0.1493})
        sid = ledger.add("financials", "MSFT annual financials", ticker="MSFT")
        return {"source_id": sid, "rows": [{"fiscal_year": 2025, "revenue": "$281.72B (YoY +14.9%)"}]}

    def search(conn, args, ledger):
        text = "Revenue increased $36.6 billion or 15% driven by growth in Microsoft Cloud."
        ledger.evidence.append([{"text": text}])
        sid = ledger.add("filing_text", "MSFT FY2025 10-K Item 7", ticker="MSFT", excerpt=text)
        return [{"source_id": sid, "text": text}]

    obj = {"type": "object", "properties": {}}
    return {
        "get_financials": ToolSpec("get_financials", "numbers", obj, fin, "numbers"),
        "search_filings": ToolSpec("search_filings", "text", obj, search, "narrative"),
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(agent_mod, "list_companies", lambda conn: COMPANIES)
    monkeypatch.setattr(agent_mod, "build_tools", fake_tools)


def test_out_of_scope_is_declined_without_tool_calls():
    llm = FakeLLM(
        [
            {
                "route": "out_of_scope",
                "tickers": [],
                "rationale": "guidance",
                "out_of_scope_reason": "Forward guidance is not in 10-K filings or XBRL data.",
            }
        ]
    )
    resp = Agent(llm).ask(None, "What is the company's forward guidance for next quarter?")
    assert resp.route.route == "out_of_scope"
    assert resp.tool_calls == [] and len(llm.calls) == 1
    assert "can't answer" in resp.answer and "Forward guidance" in resp.answer


def test_unknown_ticker_is_forced_out_of_scope():
    llm = FakeLLM([{"route": "numbers", "tickers": ["TSLA"], "rationale": "revenue"}])
    resp = Agent(llm).ask(None, "What was Tesla's revenue?")
    assert resp.route.route == "out_of_scope"
    assert "TSLA is not in the tracked universe" in resp.answer


def test_invalid_router_json_is_repaired_once():
    llm = FakeLLM(["not json", {"route": "out_of_scope", "tickers": [], "rationale": "x"}])
    resp = Agent(llm).ask(None, "hello?")
    assert resp.route.route == "out_of_scope" and len(llm.calls) == 2


def test_route_scopes_the_toolset_and_answer_is_grounded():
    llm = FakeLLM(
        [
            {"route": "both", "tickers": ["MSFT"], "rationale": "growth + attribution"},
            [
                ("get_financials", {"tickers": ["MSFT"]}),
                ("search_filings", {"ticker": "MSFT", "query": "revenue increased"}),
            ],
            "Revenue grew 14.9% to $281.72B in FY2025 [F1]; management attributed the $36.6 billion increase "
            "to Microsoft Cloud [S1].",
        ]
    )
    resp = Agent(llm).ask(None, "How did MSFT's revenue grow last year and why?")
    assert set(llm.calls[1]["tools"]) == {"get_financials", "search_filings"}
    assert [t.name for t in resp.tool_calls] == ["get_financials", "search_filings"]
    assert {c.id for c in resp.citations} == {"F1", "S1"}
    assert resp.grounding.grounded, resp.grounding
    assert resp.usage.calls == 3  # router + one tool round + final answer


def test_numbers_route_cannot_use_text_tools():
    llm = FakeLLM(
        [
            {"route": "numbers", "tickers": ["MSFT"], "rationale": "revenue"},
            [("search_filings", {"ticker": "MSFT", "query": "x"})],
            "I could not retrieve that.",
        ]
    )
    resp = Agent(llm).ask(None, "MSFT revenue?")
    assert llm.calls[1]["tools"] == ["get_financials"]
    assert resp.tool_calls[0].ok is False and resp.tool_calls[0].error == "not available"


def test_grounding_flags_invented_numbers_and_citations():
    evidence = [{"revenue": 281_724_000_000, "display": "$281.72B", "gross_margin": 0.6882}]
    ok = check_grounding("Revenue was $281.72 billion with a 68.8% gross margin [F1].", evidence, {"F1"})
    assert ok.grounded and ok.numbers_checked == 2
    bad = check_grounding("Revenue was $300 billion and guidance is $85B [F9].", evidence, {"F1"})
    assert not bad.grounded
    assert bad.unverified_numbers == ["$300 billion", "$85B"]
    assert bad.unknown_citations == ["F9"]


def test_number_extraction_ignores_years_dates_and_small_counts():
    toks = [t for t, _ in extract_numbers("In FY2025 (ended 2025-06-30), 3 new risks; EPS $13.64; P/E 45.6x")]
    assert toks == ["$13.64", "45.6x"]


def test_rate_limited_model_falls_back_to_next_model_for_the_whole_question():
    from app.agent.llm import LLMUnavailable

    class FlakyLLM(FakeLLM):
        models = ["primary", "fallback"]

        def chat(self, messages, tools=None, json_mode=False, model=None):
            if model == "primary":
                raise LLMUnavailable("429")
            return super().chat(messages, tools, json_mode, model)

    llm = FlakyLLM([{"route": "out_of_scope", "tickers": [], "rationale": "x", "out_of_scope_reason": "No."}])
    resp = Agent(llm).ask(None, "What's the price target?")
    assert resp.model == "fallback" and resp.route.route == "out_of_scope"
