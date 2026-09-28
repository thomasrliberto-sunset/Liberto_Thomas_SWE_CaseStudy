"""Agent behavior with a scripted fake LLM: routing, tool scoping, declines, grounding.
No network or database needed."""

import json
from datetime import date

import pytest

from app.agent import agent as agent_mod
from app.agent.agent import Agent
from app.agent.grounding import check_grounding, extract_numbers
from app.agent.llm import ChatResult, LLMError, ToolCall
from app.agent.tools import MAX_RESULT_CHARS, SourceLedger, ToolSpec, run_tool
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
        sid = ledger.add("financials", "MSFT annual financials", ticker="MSFT")
        return {"source_id": sid, "rows": [{"fiscal_year": 2025, "revenue": "$281.72B (YoY +14.9%)"}]}

    def search(conn, args, ledger):
        text = "Revenue increased $36.6 billion or 15% driven by growth in Microsoft Cloud."
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
            [("get_financials", {"tickers": ["MSFT"]})],
            "Revenue was $281.72B in FY2025 [F1].",
        ]
    )
    resp = Agent(llm).ask(None, "MSFT revenue?")
    assert llm.calls[1]["tools"] == ["get_financials"]
    assert resp.tool_calls[0].ok is False and resp.tool_calls[0].error == "not available"
    assert resp.tool_calls[1].ok is True
    assert resp.grounding is not None and resp.grounding.grounded


def test_in_scope_answer_must_use_a_successful_tool_and_cite_it():
    llm = FakeLLM(
        [
            {"route": "numbers", "tickers": ["MSFT"], "rationale": "revenue"},
            "Microsoft's revenue grew strongly.",
            [("get_financials", {"tickers": ["MSFT"]})],
            "Revenue was $281.72B in FY2025 [F1].",
        ]
    )
    resp = Agent(llm).ask(None, "What was MSFT revenue?")
    assert [t.name for t in resp.tool_calls] == ["get_financials"]
    assert resp.grounding is not None and resp.grounding.grounded
    assert resp.grounding.citations_checked == 1
    assert any(
        "successfully used a source tool" in message.get("content", "")
        for message in llm.calls[2]["messages"]
        if message["role"] == "user"
    )


def test_in_scope_answer_without_a_citation_fails_grounding():
    llm = FakeLLM(
        [
            {"route": "numbers", "tickers": ["MSFT"], "rationale": "revenue"},
            [("get_financials", {"tickers": ["MSFT"]})],
            "Revenue was $281.72B in FY2025.",
        ]
    )
    resp = Agent(llm).ask(None, "What was MSFT revenue?")
    assert resp.grounding is not None and not resp.grounding.grounded
    assert resp.grounding.issues == ["answer cites no tool source"]


def test_in_scope_answer_that_ignores_tool_reminder_fails_closed():
    llm = FakeLLM(
        [
            {"route": "numbers", "tickers": ["MSFT"], "rationale": "revenue"},
            "Revenue grew strongly.",
            "I will answer from memory instead.",
        ]
    )
    with pytest.raises(LLMError, match="without using"):
        Agent(llm).ask(None, "What was MSFT revenue?")


def test_tool_evidence_is_only_the_exact_visible_payload():
    ledger = SourceLedger()

    def handler(conn, args, tool_ledger):
        tool_ledger.evidence.append({"hidden": 999})
        source_id = tool_ledger.add("financials", "visible source")
        return {"source_id": source_id, "visible": 100}

    spec = ToolSpec("test", "test", {}, handler, "numbers")
    text, ok, error = run_tool(spec, None, {}, ledger)
    assert ok and error is None
    assert ledger.evidence == [text]
    assert check_grounding("The value was 100 [F1].", ledger.evidence, set(ledger.citations)).grounded
    assert not check_grounding("The value was 999 [F1].", ledger.evidence, set(ledger.citations)).grounded


def test_truncated_tool_values_and_sources_are_not_grounding_evidence():
    ledger = SourceLedger()

    def handler(conn, args, tool_ledger):
        first = tool_ledger.add("financials", "visible source")
        second = tool_ledger.add("financials", "truncated source")
        return [
            {"source_id": first, "value": 100, "padding": "x" * MAX_RESULT_CHARS},
            {"source_id": second, "value": 999},
        ]

    spec = ToolSpec("test", "test", {}, handler, "numbers")
    text, ok, error = run_tool(spec, None, {}, ledger)
    assert ok and error is None and text.endswith('..."[truncated]')
    assert set(ledger.citations) == {"F1"}
    assert check_grounding("The value was 100 [F1].", ledger.evidence, set(ledger.citations)).grounded
    assert not check_grounding("The value was 999 [F2].", ledger.evidence, set(ledger.citations)).grounded


def test_grounding_flags_invented_numbers_and_citations():
    evidence = [{"revenue": 281_724_000_000, "display": "$281.72B", "gross_margin": 0.6882}]
    ok = check_grounding("Revenue was $281.72 billion with a 68.8% gross margin [F1].", evidence, {"F1"})
    assert ok.grounded and ok.numbers_checked == 2
    bad = check_grounding("Revenue was $300 billion and guidance is $85B [F9].", evidence, {"F1"})
    assert not bad.grounded
    assert bad.unverified_numbers == ["$300 billion", "$85B"]
    assert bad.unknown_citations == ["F9"]


def test_grounding_requires_evidence_and_a_citation():
    report = check_grounding("Microsoft's cloud business grew strongly.", [], set())
    assert not report.grounded
    assert report.issues == ["no successful tool evidence", "answer cites no tool source"]


def test_explicit_billions_do_not_ground_the_same_number_of_millions():
    evidence = ['{"revenue": "$281.72B"}']
    assert check_grounding("Revenue was $281.72 billion [F1].", evidence, {"F1"}).grounded
    assert not check_grounding("Revenue was $281.72 million [F1].", evidence, {"F1"}).grounded


def test_bare_json_numbers_are_not_assumed_to_be_millions():
    evidence = ['{"value": 100}']
    assert check_grounding("The value was 100 [F1].", evidence, {"F1"}).grounded
    assert not check_grounding("The value was $100 million [F1].", evidence, {"F1"}).grounded


def test_dates_forms_and_source_ids_do_not_create_million_scale_evidence():
    evidence = ['{"source_id": "F1", "period_end": "2025-06-30", "form": "10-K"}']
    assert not check_grounding("The value was $1 million [F1].", evidence, {"F1"}).grounded
    assert not check_grounding("The value was $30 million [F1].", evidence, {"F1"}).grounded


def test_bare_table_dollars_can_be_reported_in_millions():
    evidence = ['{"text": "Revenue (in millions) | $604"}']
    assert check_grounding("Revenue was $604 million [F1].", evidence, {"F1"}).grounded


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
