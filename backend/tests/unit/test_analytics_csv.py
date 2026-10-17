"""Pure tests for the CSV serializer and its formula-injection defense (Phase 10)."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app.core.csv_export import (
    UTF8_BOM,
    Text,
    neutralize_formula,
    render_cell,
    row_line,
    stream_csv,
)


@pytest.mark.parametrize(
    "value",
    [
        '=HYPERLINK("http://evil","x")',
        "+cmd|' /C calc'!A0",
        "-1+1",
        "@SUM(1+1)",
        "\t=1+1",
        "\r=1+1",
        "  =1+1",
    ],
)
def test_formula_prefixed_text_is_neutralized(value):
    assert neutralize_formula(value) == "'" + value
    assert render_cell(Text(value)).startswith("'")


@pytest.mark.parametrize("value", ["Chocolate Chip", "Cookie = good", "", "5 dozen", "'=ok"])
def test_ordinary_text_is_untouched(value):
    assert render_cell(Text(value)) == value


def test_numbers_are_never_prefixed_even_when_negative():
    assert render_cell(Decimal("-12.50")) == "-12.50"
    assert render_cell(-3) == "-3"
    assert render_cell(Decimal("0.00")) == "0.00"
    assert render_cell(Decimal("1234.50")) == "1234.50"
    assert render_cell(None) == ""
    assert render_cell(Text(None)) == ""


def test_decimal_renders_plain_notation():
    assert render_cell(Decimal("1E+3")) == "1000"
    assert render_cell(Decimal("0.000001")) == "0.000001"


def test_bool_cell_rejected():
    with pytest.raises(TypeError):
        render_cell(True)


def test_rfc4180_escaping_roundtrip():
    nasty = 'He said "hi", then\nleft'
    line = row_line([Text(nasty), Decimal("1.00"), Text("=cmd")])
    assert line.endswith("\r\n")
    parsed = list(csv.reader(io.StringIO(line)))
    assert parsed == [[nasty, "1.00", "'=cmd"]]


def test_stream_has_bom_header_and_is_lazy():
    consumed: list[int] = []

    def rows():
        for i in range(3):
            consumed.append(i)
            yield [Text(f"n{i}"), i]

    gen = stream_csv(["name", "n"], rows())
    first = next(gen)
    assert first == UTF8_BOM + "name,n\r\n"
    assert consumed == []  # nothing pulled until requested
    assert next(gen) == "n0,0\r\n"
    assert consumed == [0]
    assert "".join(gen) == "n1,1\r\nn2,2\r\n"
