"""Financial normalisation: ported 581640e self-check cases plus 10-K and Indian report formats."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aeids.ingestion.normalize import (
    exceptions_of,
    fiscal_period,
    is_nil,
    normalize_table,
    parse_number,
    unit_of,
)
from aeids.ingestion.structure import Table
from tests.factories import INCOME_ROWS, INCOME_SUBTITLE, grid_table


# ---------- numbers (581640e self-check, now Decimal) ----------

@pytest.mark.parametrize(
    ("text", "value", "negative", "percent"),
    [
        ("1,783", "1783", False, False),
        ("5.0", "5.0", False, False),
        ("2024", "2024", False, False),
        ("(1.234,5)", "-1234.5", True, False),
        ("-43%", "-43", True, True),
        ("$ 100", "100", False, False),
        ("1.234", "1.234", False, False),
        ("3 402", "3402", False, False),
        ("12 %", "12", False, True),
        ("(4) %", "-4", True, True),
        ("$ 416,161", "416161", False, False),
        ("(14,264)", "-14264", True, False),
        ("$ 7.49", "7.49", False, False),
        # Indian formats
        ("1,23,456", "123456", False, False),
        ("₹ 1,23,456.78", "123456.78", False, False),
        ("Rs. 4,500", "4500", False, False),
        ("(12,34,567)", "-1234567", True, False),
    ],
)
def test_parse_number(text: str, value: str, negative: bool, percent: bool) -> None:
    parsed = parse_number(text)
    assert parsed is not None
    assert parsed.value == Decimal(value)
    assert isinstance(parsed.value, Decimal)
    assert (parsed.negative, parsed.percent) == (negative, percent)


def test_parse_number_decimal_comma_and_rejects() -> None:
    assert parse_number("1.234", decimal_comma=True).value == Decimal("1234")
    assert parse_number("17,8", decimal_comma=True).value == Decimal("17.8")
    assert parse_number("$ 100").currency_symbol == "$"
    for text in ("25/24", "Sales", "8-K", "Level 1:", "", "September 27, 2025"):
        assert parse_number(text) is None


@pytest.mark.parametrize("text", ["—", "— %", "$ —", "n/a", "N.M.", "-", "nil"])
def test_nil(text: str) -> None:
    assert is_nil(text)


# ---------- units ----------

@pytest.mark.parametrize(
    ("text", "currency", "scale", "percent"),
    [
        ("EUR million", "EUR", "million", False),
        ("€m", "EUR", "million", False),
        ("Thousands of euros", "EUR", "thousand", False),
        ("Revenue (EUR)", "EUR", None, False),
        ("Change (%)", None, None, True),
        ("(dollars in millions)", "USD", "million", False),
        ("(In millions)", None, "million", False),
        (INCOME_SUBTITLE, None, "million", False),
        ("(₹ in crore)", "INR", "crore", False),
        ("Rs. in Lakhs", "INR", "lakh", False),
        ("₹ Cr", "INR", "crore", False),
        ("(Rs. in lacs)", "INR", "lakh", False),
        ("INR billion", "INR", "billion", False),
    ],
)
def test_unit_of(text: str, currency: str | None, scale: str | None, percent: bool) -> None:
    hint = unit_of(text)
    assert hint is not None
    assert (hint.currency, hint.scale, hint.percent) == (currency, scale, percent)


@pytest.mark.parametrize("text", ["Margin", "Summary", "USD market share", None, ""])
def test_unit_of_none(text: str | None) -> None:
    hint = unit_of(text)
    assert hint is None or hint.scale is None


def test_exceptions() -> None:
    exc = exceptions_of(INCOME_SUBTITLE)
    assert (exc.per_share, exc.shares, exc.shares_scale) == (True, True, "thousand")
    assert exceptions_of("(In millions, except per-share amounts)").shares is False
    assert exceptions_of("(₹ in crore, except per share data)").per_share is True
    assert exceptions_of("(In millions)").per_share is False


# ---------- periods ----------

@pytest.mark.parametrize(
    ("labels", "period"),
    [
        (["Years ended", "September 27, 2025"], "FY2025"),
        (["2025"], "FY2025"),
        (["FY25"], "FY2025"),
        (["FY 2024-25"], "FY2025"),
        (["2024-25"], "FY2025"),
        (["2023-2024"], "FY2024"),
        (["31 March 2025"], "FY2025"),
        (["31.03.2024"], "FY2024"),
        (["Fiscal 2025"], "FY2025"),
        (["2024", "Americas"], "FY2024"),
        (["Change"], None),
        (["2025 vs 2024"], None),
        (["Q4 2025"], None),
        ([], None),
    ],
)
def test_fiscal_period(labels: list[str], period: str | None) -> None:
    assert fiscal_period(labels) == period


# ---------- tables ----------

def _income() -> Table:
    return Table.model_validate(grid_table(32, 1, INCOME_ROWS, header_rows=2,
                                           title="CONSOLIDATED STATEMENTS OF OPERATIONS",
                                           subtitle=INCOME_SUBTITLE))


def _row(table, label: str, nth: int = 0):
    return [r for r in table.rows if r.label == label][nth]


def test_statement_values_units_periods() -> None:
    t = normalize_table(_income())
    assert t.unit_caption == INCOME_SUBTITLE
    assert t.column_periods == [None, "FY2025", "FY2024", "FY2023"]
    products = _row(t, "Products")
    c = products.cells[0]
    assert (c.value, c.unit, c.currency, c.scale, c.scale_source, c.period) == (
        Decimal("307003"), "currency", "USD", "million", "subtitle", "FY2025")
    assert c.period_label == "Years ended / September 27, 2025"
    assert c.bbox is not None and c.page == 32
    # "$" printed only in the first row still applies to the column
    assert _row(t, "Services").cells[0].currency == "USD"
    other = _row(t, "Other income/(expense), net")
    assert [x.value for x in other.cells] == [Decimal("-321"), Decimal("269"), Decimal("-565")]
    assert other.cells[0].is_negative


def test_sections_totals_and_wrapped_labels() -> None:
    t = normalize_table(_income())
    assert _row(t, "Net sales:").row_type == "section"
    assert _row(t, "Products").section == "Net sales"
    assert _row(t, "Total net sales").row_type == "total"
    assert _row(t, "Gross margin").section is None  # the total closed "Net sales"
    wrapped = [r for r in t.rows if r.label.startswith("Cash, cash equivalents")][0]
    assert wrapped.label.endswith("beginning balances")
    assert wrapped.label_rows == [8, 9]
    assert wrapped.cells[0].value == Decimal("29943")


def test_per_share_and_share_count_rows() -> None:
    t = normalize_table(_income())
    eps = _row(t, "Basic", 0)
    assert (eps.cells[0].value, eps.cells[0].unit, eps.cells[0].scale) == (
        Decimal("7.49"), "currency", "units")
    shares = _row(t, "Basic", 1)
    c = shares.cells[0]
    assert (c.unit, c.currency, c.scale, c.scale_source) == (
        "count", None, "thousand", "exception:shares")


def test_notes_nil_percent() -> None:
    t = normalize_table(_income())
    row = _row(t, "Services", 1)
    assert row.note_refs == ["1"]
    assert [c.cell_type for c in row.cells] == ["nil", "nil", "percent"]
    pct = row.cells[2]
    assert (pct.value, pct.unit, pct.scale) == (Decimal("12"), "percent", "units")


def test_unit_from_context_and_previous_table() -> None:
    rows = [["", "2025", "Change", "2024"], ["Americas", "$ 178,353", "7 %", "$ 167,045"]]
    table = Table.model_validate(grid_table(25, 1, rows))
    ctx = ["The following table shows net sales by reportable segment (dollars in millions):"]
    t = normalize_table(table, context=ctx)
    c = t.rows[0].cells[0]
    assert (c.unit, c.currency, c.scale, c.scale_source) == ("currency", "USD", "million", "context")
    assert t.rows[0].cells[1].unit == "percent"
    assert t.column_periods == [None, "FY2025", None, "FY2024"]

    inherited = normalize_table(table, previous_caption="(in millions)")
    assert inherited.rows[0].cells[0].scale_source == "previous_table"
    bare = normalize_table(table)
    assert bare.rows[0].cells[0].scale is None
    assert "no unit or scale stated for this table" in bare.warnings


def test_header_continuation_and_share_columns() -> None:
    rows = [
        ["", "Total Number of Shares", "Average Price Paid Per", "Approximate Dollar Value"],
        ["Periods", "Purchased", "Share", "of Shares Remaining"],
        ["Open market purchases", "33,265", "$ 210.43", ""],
        ["Total", "89,498", "", "$ 99,779"],
    ]
    table = Table.model_validate(grid_table(22, 1, rows, header_rows=1))
    t = normalize_table(table, context=[
        "(in millions, except number of shares, which are reflected in thousands, and "
        "per-share amounts):"])
    assert [r.label for r in t.rows] == ["Open market purchases", "Total"]  # row 1 is header
    shares, price = t.rows[0].cells
    assert (shares.unit, shares.scale) == ("count", "thousand")
    assert (price.unit, price.currency, price.scale) == ("currency", "USD", "units")
    dollars = t.rows[1].cells[1]
    assert (dollars.unit, dollars.currency, dollars.scale) == ("currency", "USD", "million")


def test_indian_crore_table() -> None:
    rows = [["Particulars", "FY 2024-25", "FY 2023-24"],
            ["Revenue from operations", "1,23,456.78", "1,10,000.00"],
            ["Profit after tax", "(1,234.50)", "2,000.00"]]
    table = Table.model_validate(grid_table(5, 1, rows, title="Standalone results",
                                            subtitle="(₹ in crore)"))
    t = normalize_table(table)
    rev = t.rows[0].cells[0]
    assert (rev.value, rev.currency, rev.scale, rev.period) == (
        Decimal("123456.78"), "INR", "crore", "FY2025")
    assert t.rows[1].cells[0].value == Decimal("-1234.50")


def test_text_table_is_not_financial() -> None:
    rows = [["Exhibit", "Description"], ["3.1", "Restated Articles"], ["4.1", "Indenture"]]
    t = normalize_table(Table.model_validate(grid_table(57, 2, rows)))
    assert not t.is_financial
    assert [r.label for r in t.rows] == ["3.1", "4.1"]  # no header extension in text tables


def test_decimals_serialise_as_strings() -> None:
    dumped = normalize_table(_income()).model_dump(mode="json")
    value = dumped["rows"][1]["cells"][0]["value"]
    assert value == "307003"
