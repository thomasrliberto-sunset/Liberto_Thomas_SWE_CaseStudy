"""The /ask pipeline:

    question -> [LLM] route (structured JSON) -> validate against universe
             -> out_of_scope? deterministic decline, no further LLM calls
             -> [LLM] tool loop with ONLY the route's tools -> answer with [source ids]
             -> deterministic grounding check (numbers + citations) -> response

The LLM decides *what* to fetch and writes the prose. Everything numeric (metrics,
growth, multiples, risk-factor diff) is computed by code before the model sees it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import ValidationError

from app.agent.grounding import check_grounding, cited_ids
from app.agent.llm import ChatModel, LLMError, LLMUnavailable
from app.agent.prompts import ANSWER_PROMPT, DECLINE_TEMPLATE, ROUTE_HINTS, ROUTER_PROMPT
from app.agent.tools import SourceLedger, build_tools, run_tool, tools_for_route
from app.db.connection import DbConn
from app.models import AskResponse, RouteDecision, ToolCallTrace
from app.services.companies import list_companies

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, llm: ChatModel, max_tool_rounds: int = 6) -> None:
        self.llm = llm
        self.max_tool_rounds = max_tool_rounds

    # ------------------------------------------------------------------ public

    def ask(self, conn: DbConn, question: str) -> AskResponse:
        """Answer one question. If the model is rate-limited or overloaded mid-way, restart the
        whole question on the next fallback model (a conversation never mixes models)."""
        for i, model in enumerate(self.llm.models):
            try:
                return self._ask(conn, question, model)
            except LLMUnavailable:
                if i == len(self.llm.models) - 1:
                    raise
                log.warning("%s unavailable; retrying the question on %s", model, self.llm.models[i + 1])
        raise LLMError("no model configured")

    def _ask(self, conn: DbConn, question: str, model: str) -> AskResponse:
        t0 = time.monotonic()
        companies = list_companies(conn)
        tickers = [c.ticker for c in companies]
        route = self.route(question, companies, model)

        if route.route == "out_of_scope":
            return AskResponse(
                question=question,
                answer=DECLINE_TEMPLATE.format(
                    reason=route.out_of_scope_reason or "The question needs data outside this service.",
                    tickers=", ".join(tickers),
                ),
                route=route,
                citations=[],
                tool_calls=[],
                grounding=None,
                model=model,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        tools = tools_for_route(build_tools(tickers), route.route)
        ledger = SourceLedger()
        answer, traces = self._tool_loop(conn, question, route, tools, ledger, self._catalog(companies), model)

        grounding = check_grounding(answer, ledger.evidence, set(ledger.citations))
        cited = [ledger.citations[c] for c in cited_ids(answer) if c in ledger.citations]
        return AskResponse(
            question=question,
            answer=answer,
            route=route,
            citations=cited,
            tool_calls=traces,
            grounding=grounding,
            model=model,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    # ------------------------------------------------------------------ routing

    def route(self, question: str, companies: list, model: str | None = None) -> RouteDecision:
        universe = {c.ticker for c in companies}
        listing = "\n".join(f"- {c.ticker}: {c.name}" for c in companies)
        messages = [
            {"role": "system", "content": ROUTER_PROMPT.format(companies=listing)},
            {"role": "user", "content": question},
        ]
        decision: RouteDecision | None = None
        for attempt in range(2):
            result = self.llm.chat(messages, json_mode=True, model=model)
            try:
                decision = RouteDecision.model_validate(json.loads(_strip_fences(result.content or "")))
                break
            except (json.JSONDecodeError, ValidationError) as exc:
                log.warning("router returned invalid JSON (attempt %d): %s", attempt + 1, exc)
                messages += [
                    {"role": "assistant", "content": result.content or ""},
                    {"role": "user", "content": f"That was not valid. Error: {exc}. Return only the JSON object."},
                ]
        if decision is None:
            raise LLMError("router failed to return a valid decision")

        # Never trust the model's ticker list blindly: constrain it to the configured universe.
        requested = [t.upper() for t in decision.tickers]
        valid = [t for t in dict.fromkeys(requested) if t in universe]
        unknown = [t for t in requested if t not in universe]
        decision.tickers = valid
        if decision.route != "out_of_scope" and not valid:
            decision.route = "out_of_scope"
            decision.out_of_scope_reason = (
                f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not in the tracked universe."
                if unknown
                else "The question doesn't identify which tracked company it is about."
            )
        return decision

    # ------------------------------------------------------------------ tool loop

    def _tool_loop(self, conn, question, route, tools, ledger, catalog, model) -> tuple[str, list[ToolCallTrace]]:
        schemas = [t.schema() for t in tools.values()]
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": ANSWER_PROMPT.format(
                    catalog=catalog, route=route.route, tickers=route.tickers, route_hint=ROUTE_HINTS[route.route]
                ),
            },
            {"role": "user", "content": question},
        ]
        traces: list[ToolCallTrace] = []
        for _ in range(self.max_tool_rounds):
            result = self.llm.chat(messages, tools=schemas, model=model)
            if not result.tool_calls:
                return (result.content or "").strip(), traces
            messages.append(result.raw_message)
            for call in result.tool_calls:
                spec = tools.get(call.name)
                t = time.monotonic()
                before = set(ledger.citations)
                err: str | None
                if spec is None:  # model tried a tool outside its route
                    content, ok, err = (
                        json.dumps({"error": f"tool {call.name} is not available"}),
                        False,
                        "not available",
                    )
                else:
                    content, ok, err = run_tool(spec, conn, call.arguments, ledger)
                traces.append(
                    ToolCallTrace(
                        name=call.name,
                        arguments=call.arguments,
                        ok=ok,
                        latency_ms=int((time.monotonic() - t) * 1000),
                        sources=[c for c in ledger.citations if c not in before],
                        error=err,
                    )
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

        # Out of rounds: ask for a final answer with what has been gathered.
        messages.append({"role": "user", "content": "Answer now using only the tool results above."})
        result = self.llm.chat(messages, model=model)
        return (result.content or "").strip(), traces

    @staticmethod
    def _catalog(companies: list) -> str:
        lines = []
        for c in companies:
            fys = c.fiscal_years_available
            fy_end = f"{c.fiscal_year_end[:2]}/{c.fiscal_year_end[2:]}" if c.fiscal_year_end else "?"
            lines.append(
                f"- {c.ticker} ({c.name}): fiscal year ends ~{fy_end} (MM/DD); annual financials "
                f"FY{fys[0] if fys else '?'}-FY{fys[-1] if fys else '?'}; 10-K text for FY"
                f"{', FY'.join(str(y) for y in c.tenk_fiscal_years)}; prices through {c.latest_price_date}"
            )
        return "\n".join(lines)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
