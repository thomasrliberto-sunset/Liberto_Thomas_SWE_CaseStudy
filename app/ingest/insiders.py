"""Bonus dataset: SEC Form 4 insider transactions.

Why: a PM looking at fundamentals and valuation usually wants to know whether
insiders are buying or selling, and whether sales are pre-scheduled (Rule 10b5-1).
Same EDGAR client and rate limiter as the core sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

_XSL_PREFIX = re.compile(r"^xslF345X\d+/", re.I)


@dataclass(frozen=True)
class InsiderTx:
    line_no: int
    insider_name: str
    insider_cik: str | None
    relationship: str
    transaction_date: date
    security_title: str | None
    code: str
    acquired_disposed: str | None
    shares: float | None
    price: float | None
    shares_owned_after: float | None
    is_10b5_1: bool
    ownership: str | None


def raw_xml_document(primary_document: str) -> str:
    """EDGAR lists the XSL-rendered path (xslF345X05/form4.xml); the raw XML sits one level up."""
    return _XSL_PREFIX.sub("", primary_document)


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _float(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def _flag(s: str | None) -> bool:
    return (s or "").strip().lower() in {"1", "true"}


def _relationship(owner: ET.Element) -> str:
    rel = owner.find("reportingOwnerRelationship")
    parts = []
    if _flag(_text(rel, "isDirector")):
        parts.append("Director")
    if _flag(_text(rel, "isOfficer")):
        title = _text(rel, "officerTitle")
        parts.append(f"Officer ({title})" if title else "Officer")
    if _flag(_text(rel, "isTenPercentOwner")):
        parts.append("10% owner")
    if _flag(_text(rel, "isOther")):
        parts.append(_text(rel, "otherText") or "Other")
    return "; ".join(parts) or "Unknown"


def parse_form4(xml_text: str) -> list[InsiderTx]:
    root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    owners = root.findall("reportingOwner")
    names = [_text(o, "reportingOwnerId/rptOwnerName") or "Unknown" for o in owners]
    name = " / ".join(names) if names else "Unknown"
    cik = _text(owners[0], "reportingOwnerId/rptOwnerCik") if owners else None
    relationship = _relationship(owners[0]) if owners else "Unknown"

    plan_footnotes = {
        fn.get("id")
        for fn in root.findall("footnotes/footnote")
        if fn.text and re.search(r"10b5-1", fn.text, re.I)
    }
    doc_level_plan = _flag(_text(root, "aff10b5One"))

    out: list[InsiderTx] = []
    for i, tx in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction"), start=1):
        tx_date = _text(tx, "transactionDate/value")
        code = _text(tx, "transactionCoding/transactionCode")
        if not tx_date or not code:
            continue
        footnote_ids = {fn.get("id") for fn in tx.iter("footnoteId")}
        out.append(
            InsiderTx(
                line_no=i,
                insider_name=name,
                insider_cik=cik,
                relationship=relationship,
                transaction_date=date.fromisoformat(tx_date[:10]),
                security_title=_text(tx, "securityTitle/value"),
                code=code,
                acquired_disposed=_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                shares=_float(_text(tx, "transactionAmounts/transactionShares/value")),
                price=_float(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
                shares_owned_after=_float(
                    _text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
                ),
                is_10b5_1=doc_level_plan or bool(footnote_ids & plan_footnotes),
                ownership=_text(tx, "ownershipNature/directOrIndirectOwnership/value"),
            )
        )
    return out
