"""Minimal stdlib stand-in for the bs4/lxml HTML traversal this pipeline used
in production (``BeautifulSoup(doc, "lxml")``).

Only the surface ``proxy14a`` needs is implemented — ``find_all`` (accepts a
tag name string, a list/tuple/set, a compiled regex (``.search``) or a
callable; returns descendants in depth-first document order, nested-table
duplication included, exactly like bs4), ``.get(attr)`` returning ``None``
when absent, ``get_text(sep, strip=False)``, ``.name`` and
``find_next_sibling()``.

Like the lxml html parser, the builder is deliberately tolerant of the
broken HTML EDGAR filings ship with: unclosed <p>/<td>/<tr>, orphan end tags,
``<table><td>`` without a row (a <tr> is invented, mirroring lxml's tree
fix-ups) so the Summary Compensation Table grid expansion still sees the same
cells it saw in production.
"""
import re
from html.parser import HTMLParser

_VOID_TAGS = {
    "area", "base", "basefont", "br", "col", "embed", "frame", "hr", "img",
    "input", "link", "meta", "param", "source", "wbr",
}
# block-level tags that implicitly close an open <p>, like lxml does
_P_CLOSE_TAGS = {
    "address", "article", "aside", "blockquote", "center", "details", "div",
    "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "main", "nav", "ol",
    "p", "section", "table", "ul",
}
_TABLE_SECTION_TAGS = {"table", "tbody", "thead", "tfoot"}


def _matches(name, spec):
    if isinstance(spec, str):
        return name == spec
    if hasattr(spec, "search"):
        return bool(spec.search(name))
    if isinstance(spec, (list, tuple, set, frozenset)):
        return name in spec
    if callable(spec):
        return bool(spec(name))
    return False


class Element:
    __slots__ = ("name", "attrs", "children", "parent")

    def __init__(self, name, attrs=None):
        self.name = name
        self.attrs = attrs or {}
        self.children = []          # Element | str, interleaved in source order
        self.parent = None

    # ------------------------------------------------------------------ attrs
    def get(self, key, default=None):
        return self.attrs.get(key, default)

    # ---------------------------------------------------------------- search
    def find_all(self, spec=None):
        out = []

        def walk(el):
            for ch in el.children:
                if isinstance(ch, Element):
                    if spec is None or _matches(ch.name, spec):
                        out.append(ch)
                    walk(ch)
        walk(self)
        return out

    # ------------------------------------------------------------------- text
    def get_text(self, sep="", strip=False):
        parts = []

        def collect(el):
            for ch in el.children:
                if isinstance(ch, str):
                    parts.append(ch)
                else:
                    collect(ch)
        collect(self)
        if strip:
            parts = [p.strip() for p in parts if p.strip()]
        return sep.join(parts)

    # --------------------------------------------------------------- siblings
    def find_next_sibling(self):
        if self.parent is None:
            return None
        sibs = [c for c in self.parent.children if isinstance(c, Element)]
        for i, s in enumerate(sibs):
            if s is self:
                return sibs[i + 1] if i + 1 < len(sibs) else None
        return None


class Soup(Element):
    """Document root; ``soup.find_all(...)`` searches the whole tree."""

    def __init__(self):
        Element.__init__(self, "#document")


class _TreeBuilder(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Soup()
        self.stack = [self.root]
        self._text_break = True

    # -- stack helpers ------------------------------------------------------
    def _nearest(self, names):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].name in names:
                return i
        return 0

    def _pop_to(self, targets, stop):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].name in targets:
                del self.stack[i:]
                return
            if self.stack[i].name in stop:
                return

    def _close_cell(self):
        self._pop_to(("td", "th"), ("tr", "table"))

    def _close_row(self):
        self._pop_to(("tr",), ("table",))

    def _close_paragraph(self):
        self._pop_to(("p",), ("td", "th", "tr", "table"))

    def _new(self, name, attrs):
        el = Element(name, attrs)
        el.parent = self.stack[-1]
        el.parent.children.append(el)
        return el

    def _attrs(self, attrs):
        ad = {}
        for key, value in attrs:
            if key not in ad:
                ad[key] = value if value is not None else ""
        return ad

    # -- HTMLParser hooks ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        ad = self._attrs(attrs)
        self._text_break = True
        if tag in ("td", "th"):
            self._close_cell()
            if self.stack[-1].name in _TABLE_SECTION_TAGS:
                # <table><td> with no <tr>: invent the missing row, lxml-style
                self.stack.append(self._new("tr", {}))
            self.stack.append(self._new(tag, ad))
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            self.stack.append(self._new(tag, ad))
            return
        if tag in _P_CLOSE_TAGS:
            self._close_paragraph()
        if tag in _VOID_TAGS:
            self._new(tag, ad)
            return
        self.stack.append(self._new(tag, ad))

    def handle_startendtag(self, tag, attrs):
        self._text_break = True
        self._new(tag, self._attrs(attrs))

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
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
        # orphan end tag: ignore, like the lenient html parsers do

    def handle_data(self, data):
        if not data:
            return
        top = self.stack[-1]
        if (not self._text_break and top.children
                and isinstance(top.children[-1], str)):
            top.children[-1] += data
        else:
            top.children.append(data)
        self._text_break = False


def parse_html(markup):
    """Tolerant HTML parse -> :class:`Soup` tree; a broken document never raises."""
    tb = _TreeBuilder()
    try:
        tb.feed(markup)
        tb.close()
    except Exception:
        pass          # keep whatever was recovered before the parser choked
    return tb.root
