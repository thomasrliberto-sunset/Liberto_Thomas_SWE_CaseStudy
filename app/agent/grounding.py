"""Post-hoc grounding check: every number in the answer should be traceable to a tool
result, and every citation should point at a source the tools actually returned.

This is a heuristic guardrail, not a proof. It catches the common failure (a model
filling a gap with a remembered or invented figure) and surfaces it in the response,
rather than silently trusting the prose.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.models import GroundingReport

_NUM = re.compile(
    r"(?<![\w.])(-|−)?\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*"
    r"(%|percent\b|pp\b|percentage points?\b|trillion\b|billion\b|million\b|thousand\b|[TBMK]\b|x\b)?",
    re.I,
)
_CITE_BLOCK = re.compile(r"\[([A-Z]\d+(?:\s*[,;]\s*[A-Z]\d+)*)\]")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_FISCAL = re.compile(r"\b(?:FY|fiscal(?: year)?\s*)'?\d{2,4}\b", re.I)
_FORM = re.compile(r"\b(?:10-[KQ]|8-K)\b", re.I)
_BARE_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w.])")
_IN_MILLIONS = re.compile(r"\bin\s+millions?\b", re.I)
_EXPLICIT_UNIT = re.compile(
    r"\s*(?:%|percent\b|pp\b|percentage points?\b|trillion\b|billion\b|million\b|thousand\b|[TBMK]\b|x\b)",
    re.I,
)

SCALE = {"trillion": 1e12, "t": 1e12, "billion": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def extract_numbers(text: str, keep_all: bool = False) -> list[tuple[str, list[tuple[float, float]]]]:
    """-> [(token, [(candidate raw value, rounding tolerance), ...])]. A token like '14.2%'
    can mean 0.142 (a fraction) or 14.2 (percent units), so several candidates are allowed."""
    text = _CITE_BLOCK.sub(" ", text)
    text = _DATE.sub(" ", text)
    text = _FISCAL.sub(" ", text)
    out = []
    for m in _NUM.finditer(text):
        sign, whole, frac, unit = m.group(1), m.group(2), m.group(3) or "", (m.group(4) or "").lower()
        value = float(whole.replace(",", "") + frac)
        if sign:
            value = -value
        decimals = len(frac) - 1 if frac else 0
        half_ulp = 0.5 * 10 ** (-decimals)
        if not unit and not keep_all:
            if not frac and "," not in whole and abs(value) <= 31 and "$" not in m.group(0):
                continue  # counts, ranks, day-of-month: not financial figures
            if not frac and 1990 <= value <= 2040 and "$" not in m.group(0):
                continue  # years
        if unit in ("%", "percent", "pp", "percentage point", "percentage points"):
            cands = [(value / 100, half_ulp / 100), (value, half_ulp)]
        elif unit in SCALE or unit.rstrip("s") in SCALE:
            mult = SCALE.get(unit, SCALE.get(unit.rstrip("s"), 1))
            cands = [(value * mult, half_ulp * mult)]
        else:  # plain number or 'x' multiple
            cands = [(value, half_ulp)]
        out.append((m.group(0).strip(), cands))
    return out


def evidence_numbers(obj: Any) -> list[float]:
    """Every number in the tool evidence, including numbers inside strings
    (filing passages, pre-formatted displays like '$130.50B' or '75.0%')."""
    found: list[float] = []

    def walk(x: Any) -> None:
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            found.append(float(x))
        elif isinstance(x, str):
            structured_payload = x.lstrip().startswith(("{", "["))
            if structured_payload:
                try:
                    walk(json.loads(x))
                    return
                except json.JSONDecodeError:
                    pass  # a size-limited payload can be deliberately truncated
            for _, cands in extract_numbers(x, keep_all=True):
                found.extend(v for v, _ in cands)
            if structured_payload:
                return  # never infer "millions" from bare JSON numbers
            # MD&A tables often show "(In millions)" and bare cells. A rejoined
            # '$604' cell may likewise be reported as '$604 million'. Do not infer
            # that scale from dates, form names, citation ids, or ordinary counts.
            bare_text = _FORM.sub(" ", _FISCAL.sub(" ", _DATE.sub(" ", x)))
            in_millions = bool(_IN_MILLIONS.search(bare_text))
            for m in _BARE_NUM.finditer(bare_text):
                if _EXPLICIT_UNIT.match(bare_text[m.end() :]):
                    continue  # already represented at its explicit scale by extract_numbers()
                v = float(m.group(1).replace(",", ""))
                found.append(v)
                if in_millions or (m.start() > 0 and bare_text[m.start() - 1] == "$"):
                    found.append(v * 1e6)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return found


def _matches(cands: list[tuple[float, float]], evidence: list[float]) -> bool:
    """A figure matches evidence within its display rounding, or 0.5% relative; the sign is
    ignored because prose says 'a decline of $597 million' for a -597 value."""
    for c, tol in cands:
        for e in evidence:
            if abs(abs(c) - abs(e)) <= max(tol, abs(e) * 0.005):
                return True
    return False


def check_grounding(answer: str, evidence: list[Any], known_citations: set[str]) -> GroundingReport:
    ev = evidence_numbers(evidence)
    numbers = extract_numbers(answer)
    unverified = [tok for tok, cands in numbers if not _matches(cands, ev)]
    cited = {c.strip() for block in _CITE_BLOCK.findall(answer) for c in re.split(r"[,;]", block)}
    unknown = sorted(c for c in cited if c not in known_citations)
    issues = []
    if not evidence:
        issues.append("no successful tool evidence")
    if not cited:
        issues.append("answer cites no tool source")
    return GroundingReport(
        numbers_checked=len(numbers),
        citations_checked=len(cited),
        unverified_numbers=list(dict.fromkeys(unverified)),
        unknown_citations=unknown,
        issues=issues,
        grounded=not unverified and not unknown and not issues,
    )


def cited_ids(answer: str) -> list[str]:
    ids = [c.strip() for block in _CITE_BLOCK.findall(answer) for c in re.split(r"[,;]", block)]
    return list(dict.fromkeys(ids))
