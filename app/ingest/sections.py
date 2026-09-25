"""10-K narrative extraction: HTML -> lines -> Item 1A / Item 7 -> risk factors + chunks.

EDGAR 10-Ks are inline-XBRL HTML with no semantic section markup, so boundaries are
found from "Item N." heading lines. The table of contents repeats every heading, so for
each item we keep the candidate span with the most text. Emphasis (bold / italic /
underline) is tracked per line because that is how filers mark risk-factor headings
and MD&A subheadings; it drives risk-factor splitting and chunk headings.
"""

from __future__ import annotations

import re
import warnings
from collections import Counter
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "table",
    "tbody",
    "thead",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "center",
    "blockquote",
    "body",
    "html",
}
SKIP_TAGS = {"script", "style", "head", "title", "ix:header"}

_BOLD = re.compile(r"font-weight\s*:\s*(bold|bolder|[6-9]00)", re.I)
_NOT_BOLD = re.compile(r"font-weight\s*:\s*(normal|lighter|[1-5]00)", re.I)
_ITALIC = re.compile(r"font-style\s*:\s*italic", re.I)
_NOT_ITALIC = re.compile(r"font-style\s*:\s*normal", re.I)
_UNDERLINE = re.compile(r"text-decoration[^;]*underline", re.I)
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)
_WS = re.compile(r"\s+")

SECTION_TITLES = {"1A": "Risk Factors", "7": "Management's Discussion and Analysis"}


@dataclass
class Line:
    text: str
    emphasized: bool = False  # (almost) the whole line is bold/italic/underlined
    lead: str | None = None  # emphasized run-in text at the start of a mixed line


# --------------------------------------------------------------------------- HTML -> lines


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


@dataclass
class _Buf:
    parts: list[tuple[str, bool]] = field(default_factory=list)

    def add(self, text: str, emph: bool) -> None:
        self.parts.append((text, emph))

    def flush(self, out: list[Line]) -> None:
        text = _norm("".join(t for t, _ in self.parts))
        if text:
            visible = sum(len(t.strip()) for t, _ in self.parts)
            emph = sum(len(t.strip()) for t, e in self.parts if e)
            emphasized = visible > 0 and emph / visible >= 0.9
            lead = None
            if not emphasized and emph:
                run = []
                for t, e in self.parts:
                    if not t.strip() or e:
                        run.append(t)
                    else:
                        break
                lead = _norm("".join(run)) or None
            out.append(Line(text=text, emphasized=emphasized, lead=lead))
        self.parts.clear()


def html_to_lines(html: str) -> list[Line]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)  # 10-Ks are XHTML; lxml-html copes
        soup = BeautifulSoup(html, "lxml")
    out: list[Line] = []
    buf = _Buf()
    body = soup.body or soup
    _walk(body, bold=False, italic=False, underline=False, buf=buf, out=out)
    buf.flush(out)
    return _drop_page_furniture(out)


def _style_flags(tag: Tag, bold: bool, italic: bool, underline: bool) -> tuple[bool, bool, bool]:
    style = str(tag.get("style") or "")
    if tag.name in ("b", "strong") or _BOLD.search(style):
        bold = True
    elif _NOT_BOLD.search(style):
        bold = False
    if tag.name in ("i", "em") or _ITALIC.search(style):
        italic = True
    elif _NOT_ITALIC.search(style):
        italic = False
    if tag.name == "u" or _UNDERLINE.search(style):
        underline = True
    return bold, italic, underline


def _walk(node: Tag, bold: bool, italic: bool, underline: bool, buf: _Buf, out: list[Line]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if type(child) is NavigableString:  # skip comments / CDATA / doctype
                buf.add(str(child), bold or italic or underline)
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in SKIP_TAGS or _HIDDEN.search(str(child.get("style") or "")):
            continue
        b, i, u = _style_flags(child, bold, italic, underline)
        if name == "br":
            buf.flush(out)
        elif name == "tr":
            buf.flush(out)
            row = " | ".join(_merge_cells(_WS.sub(" ", c.get_text(" ")).strip() for c in child.find_all(["td", "th"])))
            if row:
                out.append(Line(text=row))
        elif name in BLOCK_TAGS:
            buf.flush(out)
            _walk(child, b, i, u, buf, out)
            buf.flush(out)
        else:
            _walk(child, b, i, u, buf, out)


def _merge_cells(cells) -> list[str]:
    """Filers split '$', '%' and ')' into their own table cells; glue them back onto the
    number so '$ | 27,448' reads '$27,448' and '10 | %' reads '10%'."""
    out: list[str] = []
    for c in cells:
        if not c:
            continue
        if c in {"%", ")", "%)"} and out:
            out[-1] += c
        elif out and out[-1] == "$":
            out[-1] = "$" + c
        else:
            out.append(c)
    return out


_PAGE_NUM = re.compile(r"^(page\s*)?\d{1,3}$|^[ivxl]{1,5}$|^table of contents$", re.I)
_FOOTER = re.compile(r"form 10-k\s*\|?\s*\d{1,3}$", re.I)  # "Apple Inc. | 2024 Form 10-K | 26"


def _drop_page_furniture(lines: list[Line]) -> list[Line]:
    """Remove page numbers and running headers/footers that repeat on every page."""
    counts = Counter(ln.text for ln in lines if len(ln.text) < 90)
    repeated = {t for t, n in counts.items() if n >= 8}
    return [
        ln
        for ln in lines
        if not _PAGE_NUM.match(ln.text)
        and ln.text not in repeated
        and not (len(ln.text) < 90 and _FOOTER.search(ln.text))
    ]


# --------------------------------------------------------------------------- sections

_ITEM = re.compile(r"^\s*item\s*(\d{1,2}[a-d]?)\s*[\.\:\-–—]?\s*(.*)$", re.I)


@dataclass
class Section:
    item: str
    title: str
    lines: list[Line]
    method: str

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


def _item_headers(lines: list[Line]) -> list[tuple[int, str, str]]:
    headers = []
    for idx, line in enumerate(lines):
        if len(line.text) > 200:
            continue
        m = _ITEM.match(line.text)
        if not m:
            continue
        code, rest = m.group(1).upper(), m.group(2).strip()
        if not rest and idx + 1 < len(lines):  # "Item 1A." with title on its own line
            rest = lines[idx + 1].text
        headers.append((idx, code, rest))
    return headers


def find_section(lines: list[Line], item: str) -> Section | None:
    """Longest span from an 'Item <item>' heading to the next different Item heading."""
    item = item.upper()
    headers = _item_headers(lines)
    best: tuple[int, int, int, str] | None = None  # (size, start, end, title)
    for pos, (idx, code, rest) in enumerate(headers):
        if code != item:
            continue
        end = next((h[0] for h in headers[pos + 1 :] if h[1] != item), len(lines))
        size = sum(len(ln.text) for ln in lines[idx + 1 : end])
        if best is None or size > best[0]:
            best = (size, idx, end, rest)
    if best is None:
        return None
    _, idx, end, rest = best
    body = lines[idx + 1 : end]
    if body and body[0].text == rest:  # title was on its own line
        body = body[1:]
    title = rest.rstrip(".").strip() or SECTION_TITLES.get(item, f"Item {item}")
    return Section(item=item, title=title, lines=body, method="item-heading")


_TITLE_PATTERNS = {
    "1A": re.compile(r"^risk factors\.?$", re.I),
    "7": re.compile(
        r"^management.s discussion and analysis of financial condition and results of operations\.?$", re.I
    ),
}
_TERMINATORS = re.compile(
    r"^(signatures|report of independent registered public accounting firm|consolidated statements? of"
    r"|exhibit index|quantitative and qualitative disclosures? about market risk"
    r"|financial statements and supplementary data)\b",
    re.I,
)
MIN_SECTION_CHARS = 2000


def find_titled_section(lines: list[Line], item: str) -> Section | None:
    """Fallback for filers whose 'Item 7' just points elsewhere in the document
    (Eaton: "Information required by this Item is presented in 'Management's Discussion
    and Analysis...' of this Form 10-K"). Looks for the bare section title as a heading."""
    pattern = _TITLE_PATTERNS.get(item.upper())
    if pattern is None:
        return None
    best: tuple[int, int, int] | None = None
    for idx, line in enumerate(lines):
        if len(line.text) > 150 or not pattern.match(line.text):
            continue
        end = next(
            (
                j
                for j in range(idx + 1, len(lines))
                if (len(lines[j].text) < 200 and _ITEM.match(lines[j].text))
                or (lines[j].emphasized and _TERMINATORS.match(lines[j].text))
            ),
            len(lines),
        )
        size = sum(len(ln.text) for ln in lines[idx + 1 : end])
        if best is None or size > best[0]:
            best = (size, idx, end)
    if best is None:
        return None
    _, idx, end = best
    title = lines[idx].text.rstrip(".")
    return Section(item=item.upper(), title=title, lines=lines[idx + 1 : end], method="title-heading")


def extract_sections(html: str, items: tuple[str, ...]) -> dict[str, Section]:
    lines = html_to_lines(html)
    out = {}
    for item in items:
        sec = find_section(lines, item)
        if sec is None or len(sec.text) < MIN_SECTION_CHARS:  # missing, or a cross-reference stub
            titled = find_titled_section(lines, item)
            if titled is not None and len(titled.text) > (len(sec.text) if sec else 0):
                sec = titled
        if sec is not None and sec.lines:
            out[item] = sec
    return out


# --------------------------------------------------------------------------- risk factors


@dataclass
class RiskFactor:
    seq: int
    category: str | None
    heading: str
    body: str


_RISK_WORD = re.compile(r"\brisks?\b", re.I)
_SENTENCE_END = (".", ".”", '."', ".’")


def classify_line(line: Line) -> tuple[str, str | None, str]:
    """-> (kind, heading, remaining_text). kind: category | risk | subheading | text.

    Filers mark headings three ways, all seen in this universe:
      * a short emphasized line: a category ("Risks Related to Our Industry", ALL CAPS)
        or a plain subheading ("Competition in the technology sector")
      * a full emphasized sentence on its own line: a risk-factor heading (NVDA, AAPL)
      * an emphasized sentence run into its paragraph: a run-in risk heading (MSFT)
    """
    t = line.text
    if line.emphasized and len(t) <= 800:
        if len(t) < 120 and not t.endswith(_SENTENCE_END):
            if t.isupper() or _RISK_WORD.search(t):
                return "category", t, ""
            return "subheading", t, ""
        if len(t) >= 40:
            return "risk", t, ""
        return "subheading", t, ""
    lead = line.lead
    if lead and len(lead) >= 40 and lead.endswith(_SENTENCE_END) and t.startswith(lead):
        return "risk", lead, t[len(lead) :].strip()
    return "text", None, t


def extract_risk_factors(section: Section, min_body_chars: int = 50) -> list[RiskFactor]:
    factors: list[RiskFactor] = []
    category: str | None = None
    heading: str | None = None
    body: list[str] = []

    def close() -> None:
        text = "\n".join(body).strip()
        if heading and len(text) >= min_body_chars:  # drops emphasized intro sentences
            factors.append(RiskFactor(len(factors) + 1, category, heading, text))

    for line in section.lines:
        kind, head, rest = classify_line(line)
        if kind == "risk":
            close()
            heading, body = head, [rest] if rest else []
        elif kind == "category":
            close()
            category, heading, body = head, None, []
        elif heading:
            body.append(line.text)
    close()
    return factors


# --------------------------------------------------------------------------- chunks


@dataclass
class Chunk:
    seq: int
    heading: str | None
    text: str


def chunk_section(section: Section, target_chars: int = 1500, max_chars: int = 2500) -> list[Chunk]:
    """Paragraph-preserving chunks that break at headings and carry a heading path
    (e.g. 'Risks Related to Our Industry > Competition could adversely affect...')."""
    chunks: list[Chunk] = []
    path: dict[str, str | None] = {"category": None, "risk": None, "subheading": None}
    cur: list[str] = []
    cur_heading: str | None = None

    def emit() -> None:
        nonlocal cur
        text = "\n".join(cur).strip()
        if text:
            chunks.append(Chunk(len(chunks) + 1, cur_heading, text))
        cur = []

    def heading_path() -> str | None:
        return " > ".join(h for h in path.values() if h) or None

    for line in section.lines:
        kind, head, rest = classify_line(line)
        if kind != "text":
            emit()
            if kind == "category":
                path.update(category=head, risk=None, subheading=None)
            elif kind == "risk":
                path.update(risk=head, subheading=None)
            else:
                path["subheading"] = head
            cur_heading = heading_path()
            if not rest:
                continue
        paragraph = rest
        if len(paragraph) > max_chars:  # very long paragraph: split on sentences
            for piece in _split_long(paragraph, target_chars):
                if sum(len(p) for p in cur) + len(piece) > target_chars:
                    emit()
                    cur_heading = heading_path()
                cur.append(piece)
            continue
        if cur and sum(len(p) for p in cur) + len(paragraph) > target_chars:
            emit()
            cur_heading = heading_path()
        if not cur:
            cur_heading = heading_path()
        cur.append(paragraph)
    emit()
    return chunks


def _split_long(text: str, size: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > size:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out
