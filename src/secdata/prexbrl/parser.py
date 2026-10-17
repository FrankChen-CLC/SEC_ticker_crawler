"""SGML submission splitting and evidence-table parsing for pre-XBRL filings.

Ported from the production ``pre_xbrl_agent/parser.py`` (2026-08 archive
build). The regexes, scoring rules, immutable evidence IDs and every SGML/
legacy-HTML quirk workaround are preserved verbatim. The only structural
change is the HTML traversal: production used bs4 + lxml; this port builds a
tolerant element tree with the stdlib ``html.parser`` instead, so the whole
package stays stdlib + requests per the project rules.

What this parser preserves (real SEC quirks, do not "clean up"):
- legacy per-share rows omit the leading zero (``$.36`` must parse as 0.36,
  not 36 — see NUMBER_CORE);
- old fixed-width filings use dot leaders (``Inventories.......3,071,100``);
  the bare-fraction branch may not follow a dot (PLAIN_NUMBER_CORE);
- ``(thousands)`` on its own line is the statement's scale; missing it reads
  values 1000x low (BARE_SCALE_RE);
- accounting negatives in parentheses, currency inside or outside them,
  em/en dashes as zero placeholders *and* as minus signs;
- 1990s hybrids: a few layout ``<table>`` tags while the audited statements
  remain monospaced ASCII pseudo-tables (``<TABLE>/<S>/<C>``) — both
  representations are parsed, with globally unique IDs;
- ``colspan``/``rowspan`` grid occupation, TOC/note-title rejection for
  "real statement" headers, and the different-legal-entity filter.

Documents read here come from
``https://www.sec.gov/Archives/edgar/data/<cik>/<accession>.txt``
(the complete submission; downloading is ``download.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import hashlib
import html
import re
from typing import Iterable

SPACE_RE = re.compile(r"[\u00a0\u2007\u202f\s]+")
UNIT_RE = re.compile(
    r"(?:dollars?|amounts?|shares?)\s+in\s+(?:thousands?|millions?|billions?)"
    r"|(?:in\s+)?(?:thousands?|millions?)\s+of\s+(?:dollars?|shares?)"
    r"|\(\s*in\s+(?:thousands?|millions?|billions?)\s*\)"
    r"|except\s+per\s+share\s+amounts?"
    r"|per\s+(?:common\s+)?share"
    r"|(?:in|expressed\s+as)\s+percent(?:age)?s?"
    r"|\$\s*0{3}(?:['’]s)?|0{3}s?\s+omitted",
    re.IGNORECASE,
)
# Legacy per-share rows omit the leading zero: `$.36`, `(.02)`. The integer part
# must be optional, otherwise the engine skips the decimal point and matches the
# fractional digits as a whole number, turning $.36 into 36.
NUMBER_CORE = r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
# Same shape for the fixed-width scanner, except the bare-fraction branch may not
# follow a dot. Old filings use dot leaders (`Inventories.......3,071,100`), and
# without this guard the last leader dot would be read as a decimal point.
PLAIN_NUMBER_CORE = (
    r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|(?<![.\d])\.\d+)"
)

NUMERIC_RE = re.compile(
    r"(?:^\s*(?:--|\u2014|\u2013)\s*$)|(?:^\s*\(?\s*(?:US\s*)?[$€£¥]?\s*"
    r"(?:[-+]|\u2014|\u2013)?\s*" + NUMBER_CORE + r"\s*%?\s*\)?\s*$)"
)
PERIOD_RE = re.compile(
    r"^(?:19|20)\d{2}$|^(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?$"
    r"|^(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+\d{1,2},?\s+(?:19|20)\d{2}$",
    re.IGNORECASE,
)
PLAIN_STATEMENT_RE = re.compile(
    r"(?im)^\s*(?P<title>(?:[A-Z0-9&.,'’ -]+?\s+)?(?:CONSOLIDATED\s+)?"
    r"(?:BALANCE\s+SHEETS?|STATEMENTS?\s+OF\s+"
    r"(?:FINANCIAL\s+POSITION|CONDITION|INCOME|OPERATIONS|EARNINGS|CASH\s+FLOWS|"
    r"CASH\s+AND\s+DISTRIBUTABLE\s+CASH\s+FROM\s+OPERATIONS|"
    r"CHANGES\s+IN\s+(?:PARTNERS|PARTICIPANTS|STOCKHOLDERS|SHAREHOLDERS)['’]?\s+CAPITAL|"
    r"STOCKHOLDERS['’]?\s+EQUITY|SHAREHOLDERS['’]?\s+EQUITY)))"
    r"(?:\s{3,}[^\r\n]*|\s+(?:AS\s+OF|AT)\s+[^\r\n]*|\s*--+\s*[^\r\n]*)?\s*$"
)
PLAIN_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\$\s*)?(?:\(\s*)?" + PLAIN_NUMBER_CORE
    + r"%?(?:\s*\))?(?![A-Za-z0-9])|(?<!\S)--(?!\S)"
)


def visible_text(value: str) -> str:
    """Normalize display whitespace while preserving visible punctuation/digits."""
    decoded = html.unescape(value or "").translate(
        str.maketrans({"\x91": "‘", "\x92": "’", "\x93": "“", "\x94": "”", "\x96": "–", "\x97": "—"})
    )
    return SPACE_RE.sub(" ", decoded).strip()


# `(thousands)` on its own line is how many pre-2000 statements state their
# scale. Missing it does not merely lose the row: the value is then read at
# scale 1, so an accepted figure is understated by a factor of 1000.
BARE_SCALE_RE = re.compile(
    r"\(?\s*(?:us\s*)?[$€£¥]?\s*(?:in\s+)?(?:thousands?|millions?|billions?)"
    r"(?:\s+of\s+(?:us\s+)?(?:dollars?|shares?))?\s*[,)]?\s*",
    re.IGNORECASE,
)


def is_explicit_unit_text(value: str) -> bool:
    lower = value.lower().strip()
    # Unit phrases are nearly always at the tail of the line; require a tight
    # tail anchor so prose like "thousands of customers" cannot match.
    if re.search(
        r"\b(?:in|amounts?\s+in|dollars?\s+in|shares?\s+in)\s+"
        r"(?:thousands?|millions?|billions?)\b"
        r"(?:\s+of\s+(?:us\s+)?(?:dollars?|shares?))?"
        r"(?:\s*[,)]\s*.*)?\s*$",
        lower,
    ):
        return True
    if BARE_SCALE_RE.fullmatch(lower):
        return True
    if "except per share amount" in lower:
        return True
    if re.search(r"\$\s*0{3}(?:['’]s)?|0{3}s?\s+omitted", lower):
        return True
    if re.fullmatch(r"\(\s*0{3}\s*['’]?s?\s*\)", lower):
        return True
    if re.fullmatch(r"\(?\s*(?:per (?:common )?share|%|percent(?:age)?s?)\s*\)?", lower):
        return True
    return bool(re.search(r"(?:in|expressed\s+as)\s+percent(?:age)?s?", lower))


@dataclass(frozen=True)
class SecDocument:
    document_type: str
    sequence: str
    filename: str
    description: str
    content: str


@dataclass
class EvidenceCell:
    cell_id: str
    row_index: int
    col_start: int
    col_end: int
    text: str
    value_id: "str | None" = None
    is_period: bool = False
    is_unit: bool = False


@dataclass
class EvidenceRow:
    row_id: str
    index: int
    cells: "list[EvidenceCell]" = field(default_factory=list)


@dataclass
class ContextLine:
    context_id: str
    text: str
    is_unit: bool = False


@dataclass
class EvidenceTable:
    table_id: str
    contexts: "list[ContextLine]"
    rows: "list[EvidenceRow]"
    source_filename: str
    source_document_type: str
    html_hash: str
    financial_score: int
    statement_score: int
    parser_kind: str = "html"

    @property
    def cells(self) -> "list[EvidenceCell]":
        return [cell for row in self.rows for cell in row.cells]

    @property
    def cell_map(self) -> "dict[str, EvidenceCell]":
        return {cell.cell_id: cell for cell in self.cells}

    @property
    def value_map(self) -> "dict[str, EvidenceCell]":
        return {cell.value_id: cell for cell in self.cells if cell.value_id}

    @property
    def context_map(self) -> "dict[str, ContextLine]":
        return {line.context_id: line for line in self.contexts}


# --------------------------------------------------------------- html tree
# Minimal stdlib replacement for the bs4/lxml traversal the production parser
# used: `find_all("table")`, `find_all("tr")`, `find_parent("table")`,
# direct-child `td`/`th`, `colspan`/`rowspan` attrs, `get_text(" ", strip=True)`,
# `find_all_previous([...tags], limit=...)` and a serialization used only as a
# stable table identity hash. The builder is deliberately tolerant of the
# broken HTML legacy filings ship with (unclosed <td>, orphaned </font>,
# uppercase pseudo-table markers in ASCII text).

_VOID_TAGS = {
    "area", "base", "basefont", "br", "col", "embed", "frame", "hr", "img",
    "input", "link", "meta", "param", "source", "wbr",
}
_CONTEXT_TAGS = ("p", "div", "center", "h1", "h2", "h3", "h4")
# block-level tags that implicitly close an open <p>, like lxml does
_P_CLOSE_TAGS = {
    "address", "article", "aside", "blockquote", "center", "details", "div",
    "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "nav", "ol",
    "p", "section", "table", "ul",
}
_TABLE_SECTION_TAGS = {"table", "tbody", "thead", "tfoot"}


class _El:
    __slots__ = ("name", "attrs", "children", "parent", "order", "_builder")

    def __init__(self, name: str, attrs: "dict | None" = None) -> None:
        self.name = name
        self.attrs = attrs or {}
        self.children: "list" = []  # _El or str, interleaved in source order
        self.parent: "_El | None" = None
        self.order = 0
        self._builder = None

    def get(self, key: str, default: str = "") -> str:
        return self.attrs.get(key, default)


def _walk(el: _El) -> "Iterable[_El]":
    for child in el.children:
        if isinstance(child, _El):
            yield child
            yield from _walk(child)


def _parent_table(el: _El) -> "_El | None":
    node = el.parent
    while node is not None:
        if node.name == "table":
            return node
        node = node.parent
    return None


def _text_of(el: _El, sep: str = " ") -> str:
    """Equivalent of bs4 `get_text(sep, strip=True)` (empty runs dropped)."""
    parts = []
    stack = [el]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            stripped = node.strip()
            if stripped:
                parts.append(stripped)
            continue
        # push children in reverse so traversal is document order
        for child in reversed(node.children):
            stack.append(child)
    return sep.join(parts)


def _serialize(el: _El) -> str:
    """Deterministic serialization; only used as a table identity hash key."""
    out = []
    for child in el.children:
        if isinstance(child, str):
            out.append(child)
        else:
            attrs = "".join(f' {k}="{v}"' for k, v in child.attrs.items())
            out.append(f"<{child.name}{attrs}>{_serialize(child)}</{child.name}>")
    attrs = "".join(f' {k}="{v}"' for k, v in el.attrs.items())
    return f"<{el.name}{attrs}>{''.join(out)}</{el.name}>"


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = _El("#document")
        self.stack: "list[_El]" = [self.root]
        self.tables: "list[_El]" = []
        self.context_elements: "list[_El]" = []
        self._counter = 0
        self._text_break = True

    # -- stack helpers ----------------------------------------------------
    def _nearest(self, names: "Iterable[str]") -> "_El | None":
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].name in names:
                return self.stack[i]
        return None

    def _pop_to(self, targets: "Iterable[str]", stop: "Iterable[str]") -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].name in targets:
                del self.stack[i:]
                return
            if self.stack[i].name in stop:
                return

    def _close_cell(self) -> None:
        self._pop_to(("td", "th"), ("tr", "table"))

    def _close_row(self) -> None:
        self._pop_to(("tr",), ("table",))

    def _close_paragraph(self) -> None:
        self._pop_to(("p",), ("td", "th", "tr", "table"))

    def _new(self, name: str, attrs: "dict") -> _El:
        el = _El(name, attrs)
        self._counter += 1
        el.order = self._counter
        parent = self.stack[-1]
        el.parent = parent
        parent.children.append(el)
        if name in _CONTEXT_TAGS:
            self.context_elements.append(el)
        return el

    # -- HTMLParser hooks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: "list[tuple[str, str | None]]") -> None:
        ad = {}
        for key, value in attrs:
            if key not in ad:
                ad[key] = value or ""
        self._text_break = True
        if tag in ("td", "th"):
            self._close_cell()
            top = self.stack[-1]
            if top.name in _TABLE_SECTION_TAGS:
                # <table><tbody><td>: invent the missing <tr>, like lxml does
                tr = self._new("tr", {})
                self.stack.append(tr)
            el = self._new(tag, ad)
            self.stack.append(el)
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            el = self._new(tag, ad)
            self.stack.append(el)
            return
        if tag == "table":
            if tag in _P_CLOSE_TAGS:
                self._close_paragraph()
            el = self._new(tag, ad)
            self.tables.append(el)
            self.stack.append(el)
            return
        if tag in _VOID_TAGS:
            self._new(tag, ad)
            return
        if tag in _P_CLOSE_TAGS:
            self._close_paragraph()
        self.stack.append(self._new(tag, ad))

    def handle_startendtag(self, tag: str, attrs: "list[tuple[str, str | None]]") -> None:
        self._text_break = True
        ad = {}
        for key, value in attrs:
            if key not in ad:
                ad[key] = value or ""
        self._new(tag, ad)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or tag == "br":
            return
        self._text_break = True
        if tag in ("td", "th"):
            self._pop_to(("td", "th"), ("tr", "table"))
            return
        if tag == "tr":
            self._close_cell()
            self._pop_to(("tr",), ("table",))
            return
        if tag == "table":
            self._close_cell()
            self._close_row()
            self._pop_to(("table",), ())
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].name == tag:
                del self.stack[i:]
                return
        # orphan end tag: ignore

    def handle_data(self, data: str) -> None:
        if not data:
            return
        top = self.stack[-1]
        if (
            not self._text_break
            and top.children
            and isinstance(top.children[-1], str)
        ):
            top.children[-1] += data
        else:
            top.children.append(data)
        self._text_break = False


def build_tree(markup: str) -> _TreeBuilder:
    """Parse HTML tolerantly; a broken document must never raise."""
    builder = _TreeBuilder()
    try:
        builder.feed(markup)
        builder.close()
    except Exception:
        # keep whatever elements were recovered before the parser choked
        pass
    return builder


# ----------------------------------------------------------- submission
def decode_submission(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def parse_registrant_name(submission: str) -> "str | None":
    """Read the SEC header's exact registrant name without model inference."""
    patterns = (
        r"(?im)^\s*COMPANY\s+CONFORMED\s+NAME:\s*([^\r\n]+)",
        r"(?im)<COMPANY-CONFORMED-NAME>\s*([^\r\n<]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, submission)
        if match:
            return visible_text(match.group(1))
    return None


def _header_value(block: str, name: str) -> str:
    match = re.search(rf"(?im)^<{re.escape(name)}>\s*([^\r\n<]*)", block)
    return visible_text(match.group(1)) if match else ""


def parse_sec_documents(submission: str) -> "list[SecDocument]":
    documents: "list[SecDocument]" = []
    for match in re.finditer(r"(?is)<DOCUMENT>(.*?)</DOCUMENT>", submission):
        block = match.group(1)
        text_match = re.search(r"(?is)<TEXT>(.*?)</TEXT>", block)
        content = text_match.group(1) if text_match else block
        documents.append(
            SecDocument(
                document_type=_header_value(block, "TYPE"),
                sequence=_header_value(block, "SEQUENCE"),
                filename=_header_value(block, "FILENAME"),
                description=_header_value(block, "DESCRIPTION"),
                content=content,
            )
        )
    return documents


def select_primary_document(documents: "Iterable[SecDocument]", form: str) -> SecDocument:
    docs = list(documents)
    wanted = form.upper().strip()
    exact = [doc for doc in docs if doc.document_type.upper().strip() == wanted]
    if exact:
        return max(exact, key=lambda doc: len(doc.content))
    form_like = [doc for doc in docs if doc.document_type.upper().startswith(wanted)]
    if form_like:
        return max(form_like, key=lambda doc: len(doc.content))
    html_docs = [doc for doc in docs if "htm" in doc.filename.lower()]
    if html_docs:
        return max(html_docs, key=lambda doc: len(doc.content))
    if not docs:
        raise ValueError("submission contains no <DOCUMENT> blocks")
    return max(docs, key=lambda doc: len(doc.content))


def _previous_context(el: _El, limit: int = 10) -> "list[str]":
    """Text of the p/div/center/h1-h4 blocks before this table, outside tables.

    Mirrors bs4 `table.find_all_previous([...], limit=80)` followed by the
    inside-table filter and unique/length capping.
    """
    candidates: "list[str]" = []
    scanned = 0
    builder = getattr(el, "_builder", None)
    elements = builder.context_elements if builder is not None else []
    for node in sorted(elements, key=lambda n: n.order, reverse=True):
        if node.order >= el.order:
            continue
        scanned += 1
        if scanned > 80:
            break
        if _parent_table(node) is not None:
            continue
        text = visible_text(_text_of(node, " "))
        if not text or len(text) > 500:
            continue
        if text not in candidates:
            candidates.append(text)
        if len(candidates) >= limit:
            break
    candidates.reverse()
    return candidates


def _table_scores(contexts: "list[str]", rows: "list[EvidenceRow]") -> "tuple[int, int]":
    context_text = " ".join(contexts).lower()
    table_text = " ".join(cell.text for row in rows for cell in row.cells).lower()
    numeric = sum(1 for row in rows for cell in row.cells if cell.value_id)
    labeled_rows = sum(
        1
        for row in rows
        if any(not cell.value_id and len(cell.text) >= 3 for cell in row.cells)
        and any(cell.value_id for cell in row.cells)
    )
    financial_score = min(numeric, 10) + min(labeled_rows * 2, 10)
    for keyword in (
        "assets", "liabilities", "revenue", "sales", "income", "loss",
        "cash flows", "stockholders", "shareholders", "expense", "earnings",
        "inventory", "debt", "dividend", "property", "operations",
    ):
        if keyword in table_text:
            financial_score += 1
    if UNIT_RE.search(context_text) or UNIT_RE.search(table_text[:1500]):
        financial_score += 3

    statement_score = 0
    title_pattern = re.compile(
        r"\b(?:consolidated\s+)?(?:balance\s+sheets?|statements?\s+of\s+"
        r"(?:financial\s+position|condition|operations|income|earnings|cash\s+flows|stockholders(?:'|’)?\s+equity|"
        r"shareholders(?:'|’)?\s+equity))\b",
        re.IGNORECASE,
    )
    compact_title_pattern = re.compile(
        r"^(?:[A-Za-z0-9&.,'’ -]+\s+)?(?:consolidated\s+)?"
        r"(?:balance\s+sheets?|statements?\s+of\s+"
        r"(?:financial\s+position|condition|operations|income|earnings|cash\s+flows|"
        r"stockholders(?:'|’)?\s+equity|shareholders(?:'|’)?\s+equity))$",
        re.IGNORECASE,
    )
    # A core statement title is normally a short, display-style heading just
    # before the table. Narrative mentions and Item 15 indexes must not qualify.
    top_cell_texts = [
        cell.text
        for row in rows[:8]
        for cell in row.cells
        if len(cell.text) <= 180
    ]
    for raw_context in [*contexts[-5:], *top_cell_texts]:
        letters = [char for char in raw_context if char.isalpha()]
        uppercase_ratio = (
            sum(char.isupper() for char in letters) / len(letters) if letters else 0
        )
        is_compact_title = bool(compact_title_pattern.fullmatch(raw_context.strip()))
        if (
            len(raw_context) <= 180
            and title_pattern.search(raw_context)
            and (uppercase_ratio >= 0.70 or is_compact_title)
        ):
            statement_score += 12
            break
    if labeled_rows >= 4:
        statement_score += 2
    if numeric >= 8:
        statement_score += 2
    return financial_score, statement_score


def _parse_table(tag: _El, table_number: int, document: SecDocument) -> EvidenceTable:
    table_id = f"T{table_number:04d}"
    context_texts = _previous_context(tag)
    contexts = [
        ContextLine(f"{table_id}_X{i:03d}", text, is_explicit_unit_text(text))
        for i, text in enumerate(context_texts, start=1)
    ]
    rows: "list[EvidenceRow]" = []
    value_number = 0

    # col -> number of future rows occupied by a rowspan
    occupied: "dict[int, int]" = {}
    row_index = 0
    for tr in _walk(tag):
        if tr.name != "tr":
            continue
        if _parent_table(tr) is not tag:
            continue
        current_col = 0
        cells: "list[EvidenceCell]" = []
        direct_cells = [c for c in tr.children if isinstance(c, _El) and c.name in ("th", "td")]
        for actual_index, td in enumerate(direct_cells):
            while occupied.get(current_col, 0) > 0:
                current_col += 1
            text = visible_text(_text_of(td, " "))
            try:
                colspan = max(1, int(td.get("colspan", "1")))
            except (TypeError, ValueError):
                colspan = 1
            try:
                rowspan = max(1, int(td.get("rowspan", "1")))
            except (TypeError, ValueError):
                rowspan = 1
            col_end = current_col + colspan - 1
            if rowspan > 1:
                for col in range(current_col, col_end + 1):
                    occupied[col] = max(occupied.get(col, 0), rowspan)
            if text:
                value_id = None
                if NUMERIC_RE.fullmatch(text):
                    value_number += 1
                    value_id = f"{table_id}_V{value_number:04d}"
                cells.append(
                    EvidenceCell(
                        cell_id=f"{table_id}_R{row_index:04d}_C{actual_index:03d}",
                        row_index=row_index,
                        col_start=current_col,
                        col_end=col_end,
                        text=text,
                        value_id=value_id,
                        is_period=bool(PERIOD_RE.fullmatch(text)),
                        is_unit=is_explicit_unit_text(text),
                    )
                )
            current_col = col_end + 1
        if cells:
            rows.append(EvidenceRow(f"{table_id}_R{row_index:04d}", row_index, cells))
        for col in list(occupied):
            occupied[col] -= 1
            if occupied[col] <= 0:
                del occupied[col]
        row_index += 1

    financial_score, statement_score = _table_scores(context_texts, rows)
    return EvidenceTable(
        table_id=table_id,
        contexts=contexts,
        rows=rows,
        source_filename=document.filename,
        source_document_type=document.document_type,
        html_hash=hashlib.sha256(_serialize(tag).encode("utf-8", errors="replace")).hexdigest(),
        financial_score=financial_score,
        statement_score=statement_score,
        parser_kind="html",
    )


def _plain_line_text(value: str) -> str:
    value = html.unescape(value).translate(
        str.maketrans({"\x91": "‘", "\x92": "’", "\x93": "“", "\x94": "”", "\x96": "–", "\x97": "—"})
    )
    value = re.sub(
        r"</?(?:TABLE|CAPTION|S|C)>",
        lambda match: " " * len(match.group(0)),
        value,
        flags=re.IGNORECASE,
    )
    return value.rstrip("\r\n")


def _is_plain_separator(text: str) -> bool:
    stripped = text.strip()
    return not stripped or bool(re.fullmatch(r"[_=\-* ]{3,}", stripped)) or stripped.startswith("<PAGE>")


def _plain_statement_type(title: str) -> str:
    lower = title.lower()
    if "balance sheet" in lower or "statement of condition" in lower or "statements of condition" in lower:
        return "balance"
    if "cash flow" in lower:
        return "cashflow"
    if "stockholder" in lower or "shareholder" in lower:
        return "equity"
    return "income"


def _parse_plain_statement(
    block: str,
    title: str,
    table_number: int,
    document: SecDocument,
) -> EvidenceTable:
    table_id = f"T{table_number:04d}"
    raw_lines = [_plain_line_text(line) for line in block.splitlines()]
    unit_overrides: "dict[int, str]" = {}
    unit_continuations: "set[int]" = set()
    for line_index, raw in enumerate(raw_lines):
        text = raw.strip()
        if "(" not in text or ")" in text or not is_explicit_unit_text(text):
            continue
        parts = [text]
        for next_index in range(line_index + 1, min(line_index + 4, len(raw_lines))):
            next_text = raw_lines[next_index].strip()
            if not next_text:
                break
            parts.append(next_text)
            unit_continuations.add(next_index)
            if ")" in next_text:
                break
        unit_overrides[line_index] = " ".join(parts)
    year_headers: "dict[int, list]" = {}
    for line_index, raw in enumerate(raw_lines[:30]):
        year_matches = list(re.finditer(r"\b(?:19|20)\d{2}\b", raw))
        if len(year_matches) >= 2:
            year_headers[line_index] = year_matches
    active_year_header = (
        max(
            year_headers,
            key=lambda idx: (
                len(year_headers[idx]),
                -sum(char.isalpha() for char in raw_lines[idx]),
                idx,
            ),
        )
        if year_headers
        else None
    )
    year_column_ends = (
        [match.end() for match in year_headers[active_year_header]]
        if active_year_header is not None
        else []
    )
    contexts: "list[ContextLine]" = []
    for context_index, raw in enumerate(raw_lines[:16]):
        if context_index in unit_continuations:
            continue
        raw = unit_overrides.get(context_index, raw)
        text = visible_text(raw)
        if not text or re.fullmatch(r"<[^>]+>", text):
            continue
        if text not in [item.text for item in contexts]:
            contexts.append(
                ContextLine(
                    f"{table_id}_X{len(contexts)+1:03d}",
                    text,
                    is_explicit_unit_text(text),
                )
            )

    rows: "list[EvidenceRow]" = []
    value_number = 0
    pending: "list[tuple[int, str]]" = []

    def add_cell(row_index: int, actual_index: int, start: int, end: int, text: str) -> EvidenceCell:
        nonlocal value_number
        value_id = None
        if NUMERIC_RE.fullmatch(text):
            value_number += 1
            value_id = f"{table_id}_V{value_number:04d}"
        return EvidenceCell(
            cell_id=f"{table_id}_R{row_index:04d}_C{actual_index:03d}",
            row_index=row_index,
            col_start=start,
            col_end=max(start, end),
            text=visible_text(text),
            value_id=value_id,
            is_period=bool(PERIOD_RE.fullmatch(visible_text(text))),
            is_unit=is_explicit_unit_text(visible_text(text)),
        )

    for physical_index, raw_line in enumerate(raw_lines):
        if physical_index in unit_continuations:
            continue
        raw_line = unit_overrides.get(physical_index, raw_line)
        stripped = raw_line.strip()
        if _is_plain_separator(raw_line) or PLAIN_STATEMENT_RE.fullmatch(raw_line):
            if not stripped:
                pending.clear()
            continue
        matches = list(PLAIN_VALUE_RE.finditer(raw_line))
        matches = [
            match
            for match in matches
            if not re.search(r"(?:notes?|page)\s*$", raw_line[: match.start()], re.IGNORECASE)
        ]
        if physical_index == active_year_header:
            matches = [match for match in matches if PERIOD_RE.fullmatch(visible_text(match.group(0)))]
        elif year_column_ends and matches:
            aligned = [
                match
                for match in matches
                if min(abs(match.end() - column_end) for column_end in year_column_ends) <= 6
            ]
            # Keep only aligned values on normal statement rows. This rejects
            # note numbers, par values and share counts embedded in labels.
            matches = aligned
        # A header row containing multiple standalone years is evidence, not a
        # financial data row, and must retain the original character columns.
        is_year_header = len(matches) >= 1 and all(
            PERIOD_RE.fullmatch(visible_text(match.group(0))) for match in matches
        )
        if matches:
            first = matches[0]
            own_label = raw_line[: first.start()].strip()
            label = own_label
            label_start = len(raw_line) - len(raw_line.lstrip())
            if own_label and pending:
                prior_indent, prior_text = pending[-1]
                current_indent = len(raw_line) - len(raw_line.lstrip())
                prior_words = prior_text.split()
                if current_indent > prior_indent and len(prior_words) >= 3:
                    label = f"{prior_text} {own_label}"
                    label_start = prior_indent
            cells: "list[EvidenceCell]" = []
            actual_index = 0
            if label and not is_year_header:
                cells.append(
                    add_cell(
                        physical_index,
                        actual_index,
                        label_start,
                        max(label_start, first.start() - 1),
                        label,
                    )
                )
                actual_index += 1
            for match in matches:
                token = visible_text(match.group(0))
                cells.append(
                    add_cell(
                        physical_index,
                        actual_index,
                        match.start(),
                        match.end() - 1,
                        token,
                    )
                )
                actual_index += 1
            if cells:
                rows.append(EvidenceRow(f"{table_id}_R{physical_index:04d}", physical_index, cells))
            pending.clear()
            continue

        text = visible_text(raw_line)
        if is_explicit_unit_text(text):
            start = len(raw_line) - len(raw_line.lstrip())
            cell = add_cell(physical_index, 0, start, start + len(text) - 1, text)
            rows.append(EvidenceRow(f"{table_id}_R{physical_index:04d}", physical_index, [cell]))
            pending.clear()
            continue
        if text and not re.fullmatch(r"(?:ASSETS|LIABILITIES|REVENUES?|EXPENSES?)", text, re.IGNORECASE):
            pending.append((len(raw_line) - len(raw_line.lstrip()), text))
            pending = pending[-3:]
        else:
            pending.clear()

    financial_score, statement_score = _table_scores([item.text for item in contexts], rows)
    title_letters = [char for char in title if char.isalpha()]
    title_uppercase_ratio = (
        sum(char.isupper() for char in title_letters) / len(title_letters)
        if title_letters
        else 0
    )
    if "condensed" not in title.lower():
        statement_score = max(statement_score, 16)
    financial_score = max(financial_score, 16)
    return EvidenceTable(
        table_id=table_id,
        contexts=contexts,
        rows=rows,
        source_filename=document.filename or "plain-text-primary-document",
        source_document_type=document.document_type,
        html_hash=hashlib.sha256(block.encode("utf-8", errors="replace")).hexdigest(),
        financial_score=financial_score,
        statement_score=statement_score,
        parser_kind="plain_text",
    )


def _extract_plain_text_tables(
    document: SecDocument,
    start_number: int = 1,
) -> "list[EvidenceTable]":
    content = document.content
    matches = list(PLAIN_STATEMENT_RE.finditer(content))
    tables: "list[EvidenceTable]" = []
    for index, match in enumerate(matches, start=1):
        start = match.start()
        end = matches[index].start() if index < len(matches) else len(content)
        block = content[start:end]
        # Stop before the notes/auditor narrative when it is explicit.
        stop = re.search(
            r"(?im)^\s*(?:the accompanying notes are|notes to (?:the )?consolidated financial statements|"
            r"report of independent)",
            block[match.end() - match.start():],
        )
        if stop:
            block = block[: match.end() - match.start() + stop.start()]
        table = _parse_plain_statement(
            block,
            match.group("title"),
            start_number + index - 1,
            document,
        )
        if len(table.value_map) >= 3:
            tables.append(table)
    return tables


def select_filing_documents(documents: "Iterable[SecDocument]", form: str) -> "list[SecDocument]":
    """Select the form plus annual-report exhibits that contain incorporated statements."""
    docs = list(documents)
    primary = select_primary_document(docs, form)
    selected = [primary]
    for document in docs:
        if document is primary:
            continue
        doc_type = document.document_type.upper().strip()
        description = document.description.lower()
        is_annual_report_exhibit = (
            doc_type.startswith("EX-13")
            or doc_type == "ARS"
            or "annual report to shareholders" in description
        )
        if is_annual_report_exhibit and len(document.content) >= 5_000:
            selected.append(document)
    return selected


def extract_evidence_tables(
    document: SecDocument,
    start_number: int = 1,
) -> "list[EvidenceTable]":
    builder = build_tree(document.content)
    for table in builder.tables:
        table._builder = builder  # type: ignore[attr-defined]
    html_tables = [
        _parse_table(tag, start_number + i - 1, document)
        for i, tag in enumerate(builder.tables, start=1)
    ]
    # Some 1990s/early-2000s filings are hybrids: a few HTML tables are used
    # for layout or exhibits while the audited statements remain monospaced
    # plain text. Parse both representations and keep globally unique IDs.
    filename_lower = document.filename.lower()
    should_parse_plain = (
        not html_tables
        or not filename_lower.endswith((".htm", ".html"))
    )
    plain_tables = (
        _extract_plain_text_tables(
            document,
            start_number=start_number + len(html_tables),
        )
        if should_parse_plain
        else []
    )
    return html_tables + plain_tables


CORE_TITLE_RE = re.compile(
    r"\b(?:consolidated\s+(?:balance\s+sheets?|statements?\s+of\s+"
    r"(?:financial\s+position|condition|operations|income|earnings|cash\s+flows|"
    r"stockholders(?:'|’)?\s+equity|shareholders(?:'|’)?\s+equity))|"
    r"(?:balance\s+sheets?|statements?\s+of\s+"
    r"(?:financial\s+position|condition|operations|income|earnings|cash\s+flows|"
    r"stockholders(?:'|’)?\s+equity|shareholders(?:'|’)?\s+equity)))\b",
    re.IGNORECASE,
)
NON_CORE_SECTION_RE = re.compile(
    r"\b(?:supplemental|condensed|combined|combining|consolidating|summarized)\b.*"
    r"\b(?:balance|statements?|financial)\b"
    r"|\bselected\s+financial\s+data\b"
    r"|\baverage\s+balance\s+sheets?\b"
    r"|\bparent\s+company\s+only\b"
    r"|\bexhibits?\s+(?:indicating|index|and financial)\b",
    re.IGNORECASE,
)


def _is_core_statement_table(table: EvidenceTable) -> bool:
    lines = [line.text for line in table.contexts]
    lines.extend(cell.text for row in table.rows[:8] for cell in row.cells)
    if any(NON_CORE_SECTION_RE.search(line) for line in lines):
        return False
    for line in lines:
        if len(line) > 220 or not CORE_TITLE_RE.search(line):
            continue
        lower = line.lower()
        if "we have audited" in lower or "accompanying consolidated" in lower:
            continue
        return True
    return False


def _plain_text_has_real_statement_header(
    table: EvidenceTable, registrant_name: "str | None" = None
) -> bool:
    """Reject TOC/note mentions that the legacy parser could mistake for statements.

    A genuine fixed-width annual statement starts with its display title and has
    at least two explicit period columns near the top.  TOC entries have page
    numbers but no period header; note prose may contain a standalone statement
    name, but its dates are not a multi-column header immediately below it.
    """
    if table.parser_kind != "plain_text":
        return True
    meaningful_contexts = [
        line for line in table.contexts if not _is_plain_separator(line.text)
    ]
    if not meaningful_contexts:
        return False
    first_line = meaningful_contexts[0].text.strip()
    first_lower = first_line.lower()
    if "we have audited" in first_lower or "accompanying consolidated" in first_lower:
        return False
    title_line = first_line
    title_match = CORE_TITLE_RE.search(title_line)
    if not title_match and len(meaningful_contexts) >= 2:
        letters = [char for char in first_line if char.isalpha()]
        uppercase_ratio = (
            sum(char.isupper() for char in letters) / len(letters) if letters else 0
        )
        registrant_tokens = _entity_tokens(registrant_name or "")
        first_line_is_registrant = bool(registrant_tokens) and registrant_tokens.issubset(
            _entity_tokens(first_line)
        )
        second_line = meaningful_contexts[1].text.strip()
        second_match = CORE_TITLE_RE.search(second_line)
        if (
            (uppercase_ratio >= 0.70 or first_line_is_registrant)
            and len(first_line) <= 160
            and second_match
        ):
            title_line = second_line
            title_match = second_match
    if not title_match:
        return False
    prefix = title_line[:title_match.start()].strip(" .:-–—")
    if prefix:
        prefix_letters = [char for char in prefix if char.isalpha()]
        prefix_uppercase_ratio = (
            sum(char.isupper() for char in prefix_letters) / len(prefix_letters)
            if prefix_letters
            else 0
        )
        registrant_tokens = _entity_tokens(registrant_name or "")
        prefix_is_registrant = bool(registrant_tokens) and registrant_tokens.issubset(
            _entity_tokens(prefix)
        )
        if prefix_uppercase_ratio < 0.70 and not prefix_is_registrant:
            return False
    trailing = title_line[title_match.end():].strip()
    if trailing:
        # The trailing part of a statement title is either the registrant name
        # ("Consolidated Balance Sheets SEI Investments Company"), a date
        # anchor ("as of December 31, 2001"), or a separator+dash. Anything
        # else is prose and the title is probably wrong.
        looks_like_company = bool(
            re.search(r"\b(?:corp(?:oration)?|company|co\.?|inc\.?|ltd\.?|limited|plc|llc|"
                      r"bancorp|bankshares|subsidiaries)\b", trailing, re.IGNORECASE)
        )
        looks_like_date_anchor = bool(
            re.fullmatch(r"(?i)(?:--+\s*|(?:as\s+of|at)\s+).*(?:19|20)\d{2}.*", trailing)
        )
        if not (looks_like_company or looks_like_date_anchor):
            return False
    top_row_ids = {row.index for row in table.rows[:12]}
    periods_by_row: "dict[int, set[str]]" = {}
    for cell in table.cells:
        if cell.row_index in top_row_ids and cell.is_period:
            periods_by_row.setdefault(cell.row_index, set()).add(cell.text)
    if any(len(periods) >= 2 for periods in periods_by_row.values()):
        return True
    all_periods = {cell.text for cell in table.cells if cell.is_period}
    has_explicit_unit = any(line.is_unit for line in table.contexts) or any(
        cell.is_unit for cell in table.cells
    )
    if periods_by_row and len(all_periods) >= 2 and has_explicit_unit and len(table.rows) >= 10:
        return True
    if "equity" in title_line.lower():
        return len(all_periods) >= 2 and len(table.rows) >= 10
    return False


ENTITY_STOP_WORDS = {
    "and", "the", "co", "company", "corp", "corporation", "inc",
    "incorporated", "limited", "ltd", "llc", "plc", "holdings", "holding",
}


def _entity_tokens(value: str) -> "set[str]":
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in ENTITY_STOP_WORDS and len(token) >= 2
    }


def _filter_to_registrant(
    tables: "list[EvidenceTable]", registrant_name: "str | None"
) -> "list[EvidenceTable]":
    """Keep only tables that are not the reporting entity's competitor or subsidiary.

    A pre-2000s statement often prints just "Consolidated Balance Sheets" without
    repeating the registrant name in the table-local context. Such headings are
    the entity's own statements; rejecting them on a missing name match would
    silently lose every consolidated schedule. The filter therefore only drops a
    table when it explicitly names a different legal entity.
    """
    target = _entity_tokens(registrant_name or "")
    if not target:
        return tables
    legal_entity_line = re.compile(
        r"\b(?:corp(?:oration)?|company|co\.?|inc\.?|ltd\.?|limited|plc|llc|"
        r"bancorp|bankshares|and\s+subsidiaries)\b",
        re.IGNORECASE,
    )
    out = []
    for table in tables:
        meaningful = [
            line.text for line in table.contexts[:6] if not _is_plain_separator(line.text)
        ]
        entity_lines = [line for line in meaningful if legal_entity_line.search(line)]
        conflict = False
        for line in entity_lines:
            line_tokens = _entity_tokens(line)
            # Same-company variance: the line's tokens include the registrant's
            # tokens (or vice versa) plus corporate-structure words like
            # "investments" or "subsidiaries". A registrant may file as "SEI
            # CORP" and print its statements as "SEI Investments Company".
            if line_tokens and (
                target.issubset(line_tokens) or line_tokens.issubset(target)
            ):
                continue
            # Real conflict: a different corporate brand name shows up.
            new_brand_tokens = line_tokens - target
            if new_brand_tokens:
                conflict = True
                break
        if not conflict:
            out.append(table)
    # No fallback: a table naming a different legal entity is genuinely not
    # this filing's statement, even if the filter would otherwise empty out.
    return out


def select_candidate_tables(
    tables: "Iterable[EvidenceTable]",
    scope: str = "core_statements",
    registrant_name: "str | None" = None,
) -> "list[EvidenceTable]":
    if scope not in {
        "core_statements", "all_entity_core_statements", "all_financial_tables"
    }:
        raise ValueError(
            "scope must be core_statements, all_entity_core_statements, "
            "or all_financial_tables"
        )
    selected = []
    for table in tables:
        numeric_count = len(table.value_map)
        labeled_rows = sum(
            1
            for row in table.rows
            if any(cell.value_id for cell in row.cells)
            and any(not cell.value_id and len(cell.text) >= 3 for cell in row.cells)
        )
        if scope in {"core_statements", "all_entity_core_statements"}:
            include = (
                table.statement_score >= 8
                and numeric_count >= 4
                and labeled_rows >= 2
                and _is_core_statement_table(table)
                and _plain_text_has_real_statement_header(table, registrant_name)
            )
        else:
            include = table.financial_score >= 12 and numeric_count >= 3 and labeled_rows >= 2
        if include:
            selected.append(table)
    if scope == "core_statements":
        return _filter_to_registrant(selected, registrant_name)
    return selected


def chunk_evidence_table(
    table: EvidenceTable,
    max_data_rows: int = 32,
    max_values: int = 120,
) -> "list[EvidenceTable]":
    """Create bounded views while preserving immutable source IDs.

    Period/unit header rows are repeated in each view. Hydration still uses the
    original cell/value IDs, so chunking cannot manufacture or rewrite values.
    """
    if max_data_rows < 1 or max_values < 1:
        raise ValueError("chunk limits must be positive")
    if len(table.rows) <= max_data_rows and len(table.value_map) <= max_values:
        return [table]

    header_rows = [
        row
        for row in table.rows[:12]
        if any(cell.is_period or cell.is_unit for cell in row.cells)
    ]
    header_ids = {row.row_id for row in header_rows}
    data_rows = [row for row in table.rows if row.row_id not in header_ids]
    groups: "list[list[EvidenceRow]]" = []
    current: "list[EvidenceRow]" = []
    current_values = 0
    for row in data_rows:
        row_values = sum(1 for cell in row.cells if cell.value_id)
        if current and (
            len(current) >= max_data_rows
            or current_values + row_values > max_values
        ):
            groups.append(current)
            current = []
            current_values = 0
        current.append(row)
        current_values += row_values
    if current:
        groups.append(current)

    chunks = []
    for index, group in enumerate(groups, start=1):
        selected = sorted(
            {row.row_id: row for row in [*header_rows, *group]}.values(),
            key=lambda row: row.index,
        )
        chunks.append(
            EvidenceTable(
                table_id=table.table_id,
                contexts=table.contexts,
                rows=selected,
                source_filename=table.source_filename,
                source_document_type=table.source_document_type,
                html_hash=table.html_hash,
                financial_score=table.financial_score,
                statement_score=table.statement_score,
                parser_kind=table.parser_kind,
            )
        )
    return chunks or [table]
