"""A table extractor built on the standard library.

Zero dependencies is a distribution decision, not a purity one: this ships as a
single `pip install` with nothing to compile and nothing to pin. The parsing
need is narrow enough to be honest about — find tables, read their header and
cell text — so a full DOM library would be paying for reach we never use.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_WS = re.compile(r"\s+")


def squash(text: str) -> str:
    """Collapse runs of whitespace. Numbeo splits labels across tags, so raw
    text arrives as 'Basic Utilities for 85 m 2 Apartment' with newlines."""
    return _WS.sub(" ", text).strip()


class Table:
    """One HTML table: its id/class, its header labels, and its body rows."""

    __slots__ = ("attrs", "header", "rows")

    def __init__(self, attrs: dict) -> None:
        self.attrs = attrs
        self.header: list[str] = []
        self.rows: list[list[str]] = []

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def column_index(self, *names: str) -> int | None:
        """Find a column by header label, case-insensitively, prefix-tolerant.

        Callers must key on the *label*, never on a fixed position: Numbeo's
        verticals have between 4 and 11 columns and it renames them without
        notice. A parser pinned to column 3 is a parser that will one day
        report the pollution index as the crime index.
        """
        low = [h.lower() for h in self.header]
        for name in names:
            n = name.lower()
            for i, h in enumerate(low):
                if h == n:
                    return i
            for i, h in enumerate(low):
                if h.startswith(n) or n in h:
                    return i
        return None


class TableCollector(HTMLParser):
    """Collect every <table>, keeping header labels and body cell text.

    Deliberately tolerant: Numbeo's markup is machine-generated and not always
    well-formed, and a strict parser that raises on it would trade a working
    read for a principled crash.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self._stack: list[Table] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_is_header = False

    # -- structure ---------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "table":
            self._stack.append(Table(a))
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._stack:
            self._cell = []
            self._cell_is_header = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            text = squash("".join(self._cell))
            if self._row is not None:
                self._row.append(text)
            if self._cell_is_header and self._stack:
                self._stack[-1].header.append(text)
            self._cell = None
        elif tag == "tr" and self._stack:
            # A row of <th> is the header and is not data. Keeping it as a data
            # row is how a column label ends up counted as a city.
            if self._row and any(self._row):
                if not self._row_was_header:
                    self._stack[-1].rows.append(self._row)
            self._row = None
            self._row_was_header = False
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    _row_was_header = False

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
            if self._cell_is_header:
                self._row_was_header = True


def parse_tables(html: str) -> list[Table]:
    p = TableCollector()
    p.feed(html)
    p.close()
    return p.tables


def find_table(html: str, *, table_id: str = "", css_class: str = "") -> Table | None:
    for t in parse_tables(html):
        if table_id and t.id == table_id:
            return t
        if css_class and css_class in t.classes:
            return t
    return None


def text_of(html: str) -> str:
    """All visible text, for presence checks like 'Cannot find city id'."""
    chunks: list[str] = []

    class _T(HTMLParser):
        _skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                chunks.append(data)

    p = _T(convert_charrefs=True)
    p.feed(html)
    p.close()
    return squash(" ".join(chunks))
