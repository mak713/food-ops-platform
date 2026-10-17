"""Streaming-friendly, formula-injection-safe CSV serialization (Spec §11.11, §13.9;
Phase 10).

Two kinds of cell are kept apart at the type level so the defense can never damage numbers:

* :class:`Text` — user-controlled or free-form strings (names, labels, order numbers).
  A text cell whose first meaningful character is a spreadsheet formula prefix
  (``= + - @``, TAB or CR) is prefixed with an apostrophe so Excel/Sheets treat it as
  literal text (OWASP "CSV injection").
* numbers (:class:`~decimal.Decimal`, ``int``) — rendered exactly (``format(d, "f")``) and
  **never** prefixed, so a legitimate negative Contribution such as ``-12.50`` stays a
  number.

``None`` renders as an empty cell. Quoting/escaping is delegated to the stdlib ``csv``
module (RFC 4180: quote on delimiter/quote/CR/LF, double embedded quotes, CRLF line
endings). Output is UTF-8 with a BOM so Excel detects the encoding.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal

UTF8_BOM = "﻿"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Text:
    value: str | None


Cell = Text | Decimal | int | None


def neutralize_formula(value: str) -> str:
    """Prefix an apostrophe when the text could be interpreted as a spreadsheet formula.
    Leading spaces are skipped when deciding, because some spreadsheets ignore them."""
    if not value:
        return value
    stripped = value.lstrip(" ")
    if value.startswith(_FORMULA_PREFIXES) or (stripped and stripped.startswith(_FORMULA_PREFIXES)):
        return "'" + value
    return value


def render_cell(cell: Cell) -> str:
    if cell is None:
        return ""
    if isinstance(cell, Text):
        return "" if cell.value is None else neutralize_formula(cell.value)
    if isinstance(cell, bool):  # bool is an int subclass; never a CSV numeric here
        raise TypeError("bool is not a supported CSV cell type")
    if isinstance(cell, Decimal):
        return format(cell, "f")
    if isinstance(cell, int):
        return str(cell)
    raise TypeError(f"Unsupported CSV cell type: {type(cell)!r}")


def _line(cells: Iterable[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL).writerow(list(cells))
    return buffer.getvalue()


def header_line(columns: Iterable[str]) -> str:
    """Header cells are fixed code constants, written verbatim (still CSV-escaped)."""
    return _line(columns)


def row_line(cells: Iterable[Cell]) -> str:
    return _line(render_cell(c) for c in cells)


def stream_csv(columns: list[str], rows: Iterable[Iterable[Cell]]) -> Iterator[str]:
    """BOM + header + one chunk per row. Nothing is accumulated: the caller's iterable is
    consumed lazily, so memory stays bounded by a single row."""
    yield UTF8_BOM + header_line(columns)
    for row in rows:
        yield row_line(row)
