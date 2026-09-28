"""Run a fixed question set through /ask and record route, tools, grounding and answer.

    docker compose exec api python scripts/eval_questions.py  # prints a summary
    make eval                                                 # also writes docs/eval_results.md

Expected routes allow `both` where it is a reasonable alternative to a single modality. In-scope
answers must use successful tools, cite their sources, and pass grounding; out-of-scope questions
must be declined without any tool calls.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.agent import Agent  # noqa: E402
from app.agent.llm import OpenAICompatibleChat  # noqa: E402
from app.db.connection import connect  # noqa: E402
from app.models import AskResponse  # noqa: E402

QUESTIONS = [
    ("What were NVDA's revenue and net income for the last three fiscal years?", {"numbers", "both"}),
    ("Which of the five companies had the highest gross margin last year?", {"numbers", "both"}),
    ("What is AAPL's trailing P/E right now?", {"numbers", "both"}),
    ("What new risk factors did NVDA add in its latest 10-K versus the prior year?", {"narrative", "both"}),
    ("How did MSFT's revenue grow last year, and what did management attribute it to?", {"both"}),
    ("What is the company's forward guidance for next quarter?", {"out_of_scope"}),
    # variants
    ("Compare operating margins for Alphabet and Microsoft over the last two fiscal years.", {"numbers", "both"}),
    ("Are Nvidia insiders net buyers or sellers over the past year?", {"numbers", "both"}),
    ("What does Eaton's MD&A say drove its sales growth, and how much did sales grow?", {"both"}),
    ("What was Tesla's revenue last year?", {"out_of_scope"}),
    ("What was Microsoft's revenue in its most recent quarter?", {"out_of_scope"}),
    ("What does Apple's latest 10-K say about tariffs as a risk?", {"narrative", "both"}),
    ("Which of the five trades at the highest trailing P/E?", {"numbers", "both"}),
    ("Should I buy NVDA here?", {"out_of_scope"}),
]


def assess_response(response: AskResponse, expected_routes: set[str]) -> list[str]:
    """Return human-readable reasons an evaluation response should fail."""
    failures: list[str] = []
    if response.route.route not in expected_routes:
        failures.append(f"route {response.route.route!r} not in {sorted(expected_routes)}")

    if response.route.route == "out_of_scope":
        if response.tool_calls:
            failures.append("out-of-scope response made tool calls")
        return failures

    if not response.answer.strip():
        failures.append("answer is empty")
    if not response.tool_calls:
        failures.append("no tool calls")
    elif not any(tool.ok for tool in response.tool_calls):
        failures.append("no successful tool calls")
    if not response.citations:
        failures.append("no citations")
    if response.grounding is None:
        failures.append("grounding report is missing")
    elif not response.grounding.grounded:
        failures.append("grounding check failed")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write docs/eval_results.md")
    parser.add_argument("--pause", type=float, default=4.0, help="seconds between questions (free-tier RPM)")
    parser.add_argument("--only", help="comma-separated 1-based question numbers")
    parser.add_argument("--append", action="store_true", help="append to docs/eval_results.md instead of replacing")
    args = parser.parse_args()
    only = {int(x) for x in args.only.split(",")} if args.only else None

    agent = Agent(OpenAICompatibleChat())
    md = [
        "# /ask evaluation run",
        "",
        f"Models configured: `{', '.join(agent.llm.models)}` (each answer records the model that produced it)",
        "",
    ]
    failures = 0
    with connect() as conn:
        for i, (q, expected) in enumerate(QUESTIONS, start=1):
            if only and i not in only:
                continue
            if i > 1:
                time.sleep(args.pause)
            r = agent.ask(conn, q)
            failure_reasons = assess_response(r, expected)
            grounded = r.grounding.grounded if r.grounding else None
            ok = not failure_reasons
            failures += not ok
            tools = ", ".join(f"{t.name}{'' if t.ok else '(err)'}" for t in r.tool_calls) or "-"
            print(
                f"[{'PASS' if ok else 'FAIL'}] {i}. {q}\n    route={r.route.route} tickers={r.route.tickers} "
                f"tools=[{tools}] grounded={grounded} {r.latency_ms} ms"
            )
            if failure_reasons:
                print(f"    failures={'; '.join(failure_reasons)}")
            if r.grounding and not r.grounding.grounded:
                print(
                    f"    unverified={r.grounding.unverified_numbers} unknown_citations={r.grounding.unknown_citations}"
                )
            md += [
                f"## {i}. {q}",
                "",
                f"- **Route:** `{r.route.route}` {r.route.tickers} - {r.route.rationale}",
                f"- **Tools:** {tools}",
                f"- **Grounding:** {r.grounding.model_dump() if r.grounding else 'n/a (declined)'}",
                f"- **Model:** `{r.model}` · **Latency:** {r.latency_ms} ms",
                "",
                "\n".join("> " + line if line else ">" for line in r.answer.splitlines()),
                "",
                "**Citations**",
                "",
                *[f"- `{c.id}` {c.description}" + (f" ([link]({c.url}))" if c.url else "") for c in r.citations],
                "",
            ]
    if args.write:
        path = ROOT / "docs" / "eval_results.md"
        if args.append and path.exists():
            md = [path.read_text().rstrip(), "", *md[4:]]  # drop the header block
        path.write_text("\n".join(md) + "\n")
        print("wrote docs/eval_results.md")
    n = len(only) if only else len(QUESTIONS)
    print(f"\n{n - failures}/{n} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
