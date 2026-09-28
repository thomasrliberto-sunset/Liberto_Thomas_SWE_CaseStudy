from app.models import AskResponse, Citation, GroundingReport, RouteDecision, ToolCallTrace
from scripts.eval_questions import QUESTIONS, assess_response


def _valid_in_scope_response() -> AskResponse:
    return AskResponse(
        question="What was revenue?",
        answer="Revenue was $1.0 billion [F1].",
        route=RouteDecision(route="numbers", tickers=["NVDA"]),
        citations=[
            Citation(
                id="F1",
                kind="financials",
                ticker="NVDA",
                description="NVDA FY2026 financials",
            )
        ],
        tool_calls=[
            ToolCallTrace(
                name="get_financials",
                arguments={"ticker": "NVDA"},
                ok=True,
                latency_ms=1,
                sources=["F1"],
            )
        ],
        grounding=GroundingReport(
            numbers_checked=1,
            unverified_numbers=[],
            unknown_citations=[],
            grounded=True,
        ),
        model="test-model",
        latency_ms=1,
    )


def test_quarterly_question_must_be_declined() -> None:
    question, expected_routes = QUESTIONS[10]
    assert "most recent quarter" in question
    assert expected_routes == {"out_of_scope"}


def test_in_scope_response_requires_tools_citations_answer_and_grounding() -> None:
    response = _valid_in_scope_response()
    assert assess_response(response, {"numbers", "both"}) == []

    response.answer = ""
    response.tool_calls = []
    response.citations = []
    response.grounding = None
    assert assess_response(response, {"numbers"}) == [
        "answer is empty",
        "no tool calls",
        "no citations",
        "grounding report is missing",
    ]


def test_in_scope_response_requires_a_successful_tool_and_grounding() -> None:
    response = _valid_in_scope_response()
    response.tool_calls[0].ok = False
    assert response.grounding is not None
    response.grounding.grounded = False
    assert assess_response(response, {"numbers"}) == [
        "no successful tool calls",
        "grounding check failed",
    ]


def test_in_scope_response_may_recover_from_an_initial_tool_error() -> None:
    response = _valid_in_scope_response()
    response.tool_calls.insert(
        0,
        ToolCallTrace(
            name="get_financials",
            arguments={"ticker": "BAD"},
            ok=False,
            latency_ms=1,
            error="bad arguments",
        ),
    )
    assert assess_response(response, {"numbers"}) == []


def test_out_of_scope_response_must_not_call_tools() -> None:
    response = _valid_in_scope_response()
    response.route = RouteDecision(route="out_of_scope", tickers=[])
    assert assess_response(response, {"out_of_scope"}) == ["out-of-scope response made tool calls"]
