"""Financial normalisation of TableLens tables (D-010).

Ported from IntelligencePDF ``reference.py`` at commit 581640e (``number``, ``unit_of``,
``decimal_comma``, ``table_entry``), with these changes:

* values are ``Decimal`` (never float); unit, currency and scale are separate fields
  (``unit="currency", currency="USD", scale="million"``), currency as ISO 4217;
* Indian digit grouping (``1,23,456.78``), ``Rs.``, ``Cr`` / ``Lacs`` short forms;
* the table's unit may come from the text printed just before it ("(dollars in millions)");
* "except" clauses ("In millions, except number of shares, which are reflected in thousands,
  and per-share amounts") give per-share rows scale ``units`` and share-count rows their own
  scale and unit ``count``;
* a label wrapped over two rows (the first without values, the second starting lowercase) is
  joined into one row;
* periods: the printed header path is kept as ``period_label``; ``period`` is ``FY<year>`` when a
  header names one fiscal year (``FY25``, ``2024-25``, ``September 27, 2025``, ``2025``);
* label indent levels are not computed: they need the PDF words, which _structure.json does
  not carry (``level`` is None).

Every derived field records where it came from (``scale_source``) so Verification can check it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aeids.ingestion.structure import Cell, JoinedTable, Table

NORMALIZE_VERSION = "1"

# ---------- currencies, scales, numbers ----------

_CUR_WORDS = r"EUR|USD|GBP|INR|JPY|CHF|CNY|RMB|SEK|NOK|DKK|PLN|CAD|AUD|euros?|dollars?|rupees?"
CUR = rf"(?:\b(?:{_CUR_WORDS})\b|\bRs\b\.?|[€$£¥₹])"
CUR_RE = re.compile(CUR, re.I)
_ISO = {
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "£": "GBP", "gbp": "GBP",
    "₹": "INR", "inr": "INR", "rs": "INR", "rs.": "INR", "rupee": "INR", "rupees": "INR",
    "jpy": "JPY", "cny": "CNY", "rmb": "CNY",
    "chf": "CHF", "sek": "SEK", "nok": "NOK", "dkk": "DKK", "pln": "PLN", "cad": "CAD",
    "aud": "AUD",
}  # "¥" is JPY or CNY: left unknown


def iso_currency(token: str | None) -> str | None:
    return _ISO.get((token or "").strip().casefold()) if token else None


SCALES: dict[str, int] = {
    "units": 1,
    "thousand": 1_000,
    "lakh": 100_000,
    "million": 1_000_000,
    "crore": 10_000_000,
    "billion": 1_000_000_000,
}
_LONG = {"thousand": "thousand", "lakh": "lakh", "lac": "lakh", "million": "million",
         "crore": "crore", "billion": "billion"}
_SHORT = {"k": "thousand", "'000": "thousand", "000s": "thousand", "m": "million", "mn": "million",
          "mln": "million", "bn": "billion", "cr": "crore", "crs": "crore", "lacs": "lakh"}
LONG_RE = re.compile(r"\b(thousand|lakh|lac|million|crore|billion)s?\b", re.I)
SHORT_RE = re.compile(rf"(?:{CUR}|\bin)\s*(k|'000|000s|mn|mln|bn|crs|cr|lacs|m)(?![\w'])", re.I)
PCT_RE = re.compile(r"(?:^|[\s(])%(?:$|[\s)])")

_INDIAN = r"\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?"
NUMBER = re.compile(rf"{_INDIAN}|\d{{1,3}}(?:[ ,.'  ]\d{{3}})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
NIL = {"-", "–", "—", "−", "n/a", "na", "n.a.", "nm", "n.m.", "n.d.", "nil"}
NOTE_RE = re.compile(r"\s*(?:\((\d{1,2}|[a-z])\)|(\*+))$")


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal
    negative: bool
    percent: bool
    currency_symbol: str | None


def parse_number(text: str, decimal_comma: bool = False) -> ParsedNumber | None:
    """One printed number -> value; None when the text is not exactly one number.
    "1,783" 1783 · "5.0" 5.0 · "(1.234,5)" -1234.5 · "-43%" -43 · "$ 100" 100 · "(4) %" -4 %."""
    s = " ".join(text.split()).rstrip("*").strip()
    cur = CUR_RE.search(s)
    if cur:
        s = CUR_RE.sub("", s).replace(" ", "")
    negative = percent = False
    for _ in range(2):  # "(12%)", "(12)%", "(12) %", "-12%"
        if s.endswith("%"):
            percent, s = True, s[:-1].strip()
        if len(s) > 2 and s[0] == "(" and s[-1] == ")":
            negative, s = True, s[1:-1].strip()
        if s[:1] in ("-", "–", "−"):
            negative, s = True, s[1:].strip()
    if not NUMBER.fullmatch(s):
        return None
    digits = re.sub(r"[ '  ]", "", s)
    seps = re.findall(r"[.,]", digits)
    if not seps:
        value = Decimal(digits)
    elif len(set(seps)) == 2:  # both kinds: the last one is the decimal separator
        value = Decimal(digits.replace(seps[0], "").replace(seps[-1], "."))
    elif len(seps) > 1:  # one kind, several times: thousands (or Indian lakh grouping)
        value = Decimal(digits.replace(seps[0], ""))
    else:
        whole, tail = digits.split(seps[0])
        thousands = len(tail) == 3 and seps[0] != ("," if decimal_comma else ".")
        value = Decimal(whole + tail) if thousands else Decimal(f"{whole}.{tail}")
    return ParsedNumber(-value if negative else value, negative, percent,
                        cur.group(0) if cur else None)


def is_nil(text: str) -> bool:
    s = " ".join(CUR_RE.sub("", text).replace("%", " ").split()).casefold()
    return s in NIL


@dataclass(frozen=True)
class UnitHint:
    """What a header / title / caption states about units."""

    printed: str
    currency: str | None = None
    scale: str | None = None
    percent: bool = False


def unit_of(text: str | None) -> UnitHint | None:
    """The unit a text states ("USD million", "(₹ in crore)", "€m", "(In millions)", "(%)"), or
    None. Only the part before "except" counts; see :func:`exceptions_of` for the rest."""
    if not text:
        return None
    main = re.split(r"\bexcept\b", text, maxsplit=1, flags=re.I)[0]
    scale = None
    if m := LONG_RE.search(main):
        scale = _LONG[m.group(1).lower()]
    elif m := SHORT_RE.search(main):
        scale = _SHORT[m.group(1).lower()]
    cur = CUR_RE.search(main)
    currency = iso_currency(cur.group(0)) if cur else None
    percent = bool(PCT_RE.search(main)) and scale is None and currency is None
    if scale is None and currency is None and not percent:
        return None
    return UnitHint(" ".join(main.split()).strip(" ()-–:"), currency, scale, percent)


@dataclass(frozen=True)
class Exceptions:
    """From "except ..." in a unit caption."""

    per_share: bool = False
    shares: bool = False
    shares_scale: str = "units"


def exceptions_of(text: str | None) -> Exceptions:
    parts = re.split(r"\bexcept\b", text or "", maxsplit=1, flags=re.I)
    if len(parts) < 2:
        return Exceptions()
    rest = parts[1]
    shares = re.search(r"\bshares\b", rest, re.I)
    shares_scale = "units"
    if shares and (m := LONG_RE.search(rest)):
        shares_scale = _LONG[m.group(1).lower()]
    return Exceptions(
        per_share=bool(re.search(r"per[- ]share", rest, re.I)),
        shares=bool(shares),
        shares_scale=shares_scale,
    )


def decimal_comma(texts: Iterable[str]) -> bool:
    """True when decimals are printed with a comma ("17,8") more often than with a dot."""
    texts = list(texts)
    count = lambda rx: sum(len(re.findall(rx, s)) for s in texts)  # noqa: E731
    return count(r"\d,\d{1,2}(?!\d)") > count(r"\d\.\d{1,2}(?!\d)")


# ---------- periods ----------

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_YEAR = r"((?:19|20)\d{2})"
_PERIOD_PATTERNS = [
    re.compile(r"\bF\.?Y\.?\s*'?((?:19|20)\d{2}|\d{2})\b(?!\s*[-–/]\s*\d)", re.I),
    re.compile(rf"\b(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+{_YEAR}\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})[a-z]*\.?,?\s+{_YEAR}\b", re.I),
    re.compile(rf"\b\d{{1,2}}[./-]\d{{1,2}}[./-]{_YEAR}\b"),
    re.compile(r"^\s*(?:fiscal\s+(?:year\s+)?|year\s+)?((?:19|20)\d{2})\s*$", re.I),
]
_RANGE = re.compile(r"\b(?:F\.?Y\.?\s*)?((?:19|20)\d{2})\s*[-–/]\s*(\d{2}|(?:19|20)\d{2})\b", re.I)


def _years_in(label: str) -> set[int]:
    years: set[int] = set()
    for m in _RANGE.finditer(label):
        start, end = int(m.group(1)), m.group(2)
        end_year = int(end) if len(end) == 4 else (start // 100) * 100 + int(end)
        if end_year == start + 1:
            years.add(end_year)
    label = _RANGE.sub(" ", label)
    for rx in _PERIOD_PATTERNS:
        for m in rx.finditer(label):
            y = m.group(1)
            years.add(int(y) if len(y) == 4 else 2000 + int(y))
    return {y for y in years if 1990 <= y <= 2100}


def fiscal_period(labels: Sequence[str]) -> str | None:
    """``FY<year>`` named by a column's header path (read bottom-up), else None. A fiscal year is
    named by the year it ends in. A header naming two years ("2025 vs 2024") gives None."""
    for label in reversed(labels):
        years = _years_in(label)
        if len(years) > 1:
            return None
        if years:
            return f"FY{years.pop()}"
    return None


# ---------- tables ----------

CellType = Literal["empty", "nil", "text", "numeric", "percent"]
RowType = Literal["header", "section", "total", "data"]
Unit = Literal["currency", "percent", "count"]

PER_SHARE_RE = re.compile(r"per[- ]share|earnings per share|\beps\b", re.I)
SHARES_RE = re.compile(r"^(?:number of |weighted[- ]average )?(?:\w+ )?shares\b", re.I)
TOTAL_RE = re.compile(r"\btotal\b", re.I)
PER_SHARE_COL_RE = re.compile(r"per[- ]share|price paid per", re.I)
SHARES_COL_RE = re.compile(r"\bshares\b|\bnumber of (?:shares|units)\b", re.I)
MONEY_COL_RE = re.compile(rf"dollar|value|price|amount|{CUR}", re.I)


class NormCell(BaseModel):
    model_config = ConfigDict(frozen=True)

    row: int  # TableLens row/col: evidence points back at them
    col: int
    page: int
    bbox: list[float] | None
    text: str
    cell_type: CellType
    value: Decimal | None = None
    is_negative: bool = False
    unit: Unit | None = None
    currency: str | None = None
    scale: str | None = None
    scale_source: str | None = None
    period_label: str | None = None
    period: str | None = None


class NormRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    row: int  # the TableLens row holding the values
    label_rows: list[int]  # rows the label was printed on (two when it wrapped)
    row_type: RowType
    label: str
    section: str | None
    note_refs: list[str] = Field(default_factory=list)
    level: int | None = None
    cells: list[NormCell] = Field(default_factory=list)  # value columns with text, left to right


class TablePartRef(BaseModel):
    page: int
    table_id: int
    bbox: list[float] | None = None


class NormTable(BaseModel):
    key: str  # "p32-t1", or "p57-t2+p58-t1+..." for a joined table
    parts: list[TablePartRef]
    page_start: int
    page_end: int
    title: str | None
    subtitle: str | None
    unit_caption: str | None  # the text the table-level unit came from
    num_rows: int
    num_cols: int
    columns: list[str]  # header path per column (" / ")
    column_periods: list[str | None]
    is_financial: bool
    rows: list[NormRow]
    warnings: list[str] = Field(default_factory=list)
    normalize_version: str = NORMALIZE_VERSION


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def normalize_table(
    table: Table | JoinedTable,
    *,
    context: Sequence[str] = (),
    previous_caption: str | None = None,
    comma: bool = False,
    parts: Sequence[TablePartRef] | None = None,
) -> NormTable:
    """One TableLens table -> NormTable. ``context``: texts printed just before the table on its
    page, nearest first (a caption such as "(dollars in millions)"). ``previous_caption``: the
    unit caption of the table before it on the same page, used last ("The following tables show
    ... (in millions):" above several tables)."""
    nrows, ncols = table.num_rows, table.num_cols
    default_page = table.page if isinstance(table, Table) else table.parts[0].page
    if parts is None:
        parts = [TablePartRef(page=table.page, table_id=table.table_id, bbox=table.bbox_pdf_pt)] \
            if isinstance(table, Table) else [TablePartRef(page=p.page, table_id=p.table_id)
                                              for p in table.parts]

    own: dict[tuple[int, int], Cell] = {}
    for c in table.cells:
        for r in range(c.row, min(c.row + c.row_span, nrows)):
            for q in range(c.col, min(c.col + c.col_span, ncols)):
                own.setdefault((r, q), c)
    header = sorted({r for (r, _), c in own.items() if c.column_header})
    # header rows contiguous from the top only (as TableLens marks them)
    header_rows = set()
    for r in range(nrows):
        if r in header:
            header_rows.add(r)
        else:
            break

    def text(r: int, q: int) -> str:
        return _norm(own[(r, q)].text) if (r, q) in own else ""

    # a header printed on several lines whose later lines TableLens did not flag: the next row has
    # words, and no numbers, in its value columns. Only in tables with numbers, at most 2 rows.
    def numeric(v: str) -> bool:
        return bool(v) and (parse_number(v, comma) is not None or is_nil(v))

    body_has_numbers = any(numeric(text(r, q)) for r in range(nrows) if r not in header_rows
                           for q in range(1, ncols))
    for _ in range(2 if body_has_numbers and header_rows else 0):
        r = max(header_rows) + 1
        values = [text(r, q) for q in range(1, ncols)] if r < nrows else []
        if not any(values) or any(numeric(v) for v in values):
            break
        header_rows.add(r)

    def heads(q: int) -> list[str]:
        out: list[str] = []
        for r in sorted(header_rows):
            if (s := text(r, q)) and s not in out:
                out.append(s)
        return out

    # table-level unit: title, subtitle, then the text before the table
    captions = [(s, src) for s, src in [(table.title, "title"), (table.subtitle, "subtitle")] if s]
    captions += [(s, "context") for s in context]
    if previous_caption:
        captions.append((previous_caption, "previous_table"))
    table_hint, caption, exc = None, None, Exceptions()
    for s, src in captions:
        if (hint := unit_of(s)) is not None:
            table_hint, caption = (hint, src), s
            exc = exceptions_of(s)
            break

    def column_hint(q: int) -> tuple[UnitHint, str] | None:
        """The column's own unit; currency and scale fall back to the table's separately
        ("Dollar Value of ..." over a table "(in millions)": USD from the column, million from
        the table)."""
        own_hint: tuple[UnitHint, str] | None = None
        for s in heads(q)[::-1] + heads(0)[::-1]:
            if h := unit_of(s):
                own_hint = (h, "column_header" if s in heads(q) else "label_header")
                break
        if own_hint is None:
            return table_hint
        if own_hint[0].scale is None and not own_hint[0].percent and table_hint \
                and table_hint[0].scale:
            merged = UnitHint(own_hint[0].printed, own_hint[0].currency or table_hint[0].currency,
                              table_hint[0].scale)
            return merged, table_hint[1]
        return own_hint

    # currency printed in a column's cells ("$ 178,353" in its first row)
    def column_symbol(q: int) -> str | None:
        found = Counter(
            iso for r in range(nrows) if r not in header_rows
            if (m := CUR_RE.search(text(r, q))) and (iso := iso_currency(m.group(0)))
        )
        return found.most_common(1)[0][0] if found else None

    hints = [column_hint(q) for q in range(ncols)]
    symbols = [column_symbol(q) for q in range(ncols)]
    columns = [" / ".join(heads(q)) for q in range(ncols)]
    periods = [fiscal_period(heads(q)) for q in range(ncols)]
    flat = [" ".join(heads(q)) for q in range(ncols)]
    col_per_share = [bool(PER_SHARE_COL_RE.search(h)) for h in flat]
    col_shares = [bool(SHARES_COL_RE.search(h)) and not col_per_share[q]
                  and not MONEY_COL_RE.search(h) for q, h in enumerate(flat)]

    def has_values(r: int) -> bool:
        return any(text(r, q) for q in range(1, ncols))

    rows: list[NormRow] = []
    warnings: list[str] = []
    section: str | None = None
    pending_label: tuple[int, str] | None = None
    figures = words = 0
    for r in range(nrows):
        if r in header_rows:
            continue
        label = text(r, 0)
        cells_flagged_section = any(
            own[(r, q)].row_section for q in range(ncols) if (r, q) in own
        )
        nxt = text(r + 1, 0) if r + 1 < nrows and r + 1 not in header_rows else ""
        if label and not has_values(r) and not label.endswith(":") and nxt[:1].islower():
            pending_label = (r, label)  # label wrapped onto the next row
            continue
        label_rows = [r]
        if pending_label is not None:
            label_rows = [pending_label[0], r]
            label = f"{pending_label[1]} {label}".strip()
            pending_label = None
        note_refs: list[str] = []
        while m := NOTE_RE.search(label):
            note_refs.insert(0, m.group(1) or m.group(2))
            label = label[: m.start()].rstrip()
        if not has_values(r):
            if label:
                section = label.rstrip(":").strip()
                rows.append(NormRow(row=r, label_rows=label_rows, row_type="section",
                                    label=label, section=None, note_refs=note_refs))
            continue
        row_type: RowType = "total" if TOTAL_RE.search(label) else "data"
        if cells_flagged_section and row_type == "data" and label.endswith(":"):
            row_type = "section"
        # "Shares used in computing earnings per share: Basic" is a share count, not per-share
        row_has_currency = any(CUR_RE.search(text(r, q)) for q in range(1, ncols))
        share_count = not row_has_currency and bool(
            SHARES_RE.search(label) or SHARES_RE.search(section or "")
        )
        per_share = not share_count and bool(PER_SHARE_RE.search(f"{section or ''} {label}"))

        out_cells: list[NormCell] = []
        for q in range(1, ncols):
            s = text(r, q)
            if not s:
                continue
            c = own[(r, q)]
            if (c.row, c.col) != (r, q):
                continue  # a span copy: the origin cell carries it
            page = c.page or default_page
            parsed = parse_number(s, comma)
            base = dict(row=r, col=q, page=page, bbox=c.bbox_pdf_pt, text=s,
                        period_label=columns[q] or None, period=periods[q])
            if is_nil(s):
                out_cells.append(NormCell(cell_type="nil", **base))
                continue
            if parsed is None:
                words += 1
                out_cells.append(NormCell(cell_type="text", **base))
                continue
            figures += 1
            hint = hints[q]
            if parsed.percent or (hint and hint[0].percent):
                out_cells.append(NormCell(cell_type="percent", value=parsed.value,
                                          is_negative=parsed.negative, unit="percent",
                                          scale="units", scale_source="printed", **base))
                continue
            cell_currency = iso_currency(parsed.currency_symbol)
            currency = (hint[0].currency if hint and hint[0].currency else None) \
                or cell_currency or symbols[q] or (table_hint[0].currency if table_hint else None)
            if col_shares[q] and not cell_currency:
                unit, currency = "count", None
                scale, source = (exc.shares_scale, "exception:shares") if exc.shares else (
                    "units", "column_header")
            elif per_share or col_per_share[q]:
                unit, scale, source = "currency", "units", "per_share"
            elif share_count:
                unit, currency = "count", None
                scale, source = (exc.shares_scale, "exception:shares") if exc.shares else (
                    (hint[0].scale, hint[1]) if hint and hint[0].scale else (None, None))
            else:
                unit = "currency" if currency else None
                scale, source = (hint[0].scale, hint[1]) if hint and hint[0].scale else (None, None)
                if scale is None and currency:
                    scale, source = None, None  # a currency amount with no stated scale
            out_cells.append(NormCell(cell_type="numeric", value=parsed.value,
                                      is_negative=parsed.negative, unit=unit,
                                      currency=currency if unit == "currency" else None,
                                      scale=scale, scale_source=source, **base))
        rows.append(NormRow(row=r, label_rows=label_rows, row_type=row_type, label=label,
                            section=section, note_refs=note_refs, cells=out_cells))
        if row_type == "total":
            section = None  # "Total net sales" closes "Net sales:"

    if table_hint is None and figures and not any(hints):
        warnings.append("no unit or scale stated for this table")
    pages = sorted({p.page for p in parts})
    key = "+".join(f"p{p.page}-t{p.table_id}" for p in parts)
    return NormTable(
        key=key, parts=list(parts), page_start=pages[0], page_end=pages[-1],
        title=_norm(table.title) or None, subtitle=_norm(table.subtitle) or None,
        unit_caption=caption, num_rows=nrows, num_cols=ncols, columns=columns,
        column_periods=periods, is_financial=figures > words, rows=rows, warnings=warnings,
    )
