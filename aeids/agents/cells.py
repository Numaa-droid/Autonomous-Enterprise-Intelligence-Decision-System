"""How a normalised table cell becomes a typed value (shared by Document Intelligence and
Verification, so the two can never read a cell differently).

A table chunk's ``meta["table"]`` holds the rows ingestion normalised (``NormTable``): each value
cell has its number, unit, currency, scale, period, page and box. ``read_cell`` maps that to the
contract's vocabulary; ``locate`` finds the row an evidence quote cites and the cell its box
points at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aeids.calc.functions import SCALE_FACTORS
from aeids.ingestion.chunking import _row_line
from aeids.ingestion.normalize import NormRow

ISO = re.compile(r"^[A-Z]{3}$")
FY = re.compile(r"^FY\d{4}$")
BOX_TOLERANCE = 0.5  # PDF points


@dataclass(frozen=True)
class CellValue:
    value: Decimal
    unit: str
    currency: str | None
    scale: str | None
    period: str | None
    page: int | None
    bbox: list[float] | None
    text: str
    note: str | None = None  # why the unit was downgraded, if it was


def column_period(table: dict[str, Any], col: int) -> str | None:
    periods = table.get("column_periods") or []
    return periods[col] if 0 <= col < len(periods) else None


def column_name(table: dict[str, Any], col: int) -> str:
    names = table.get("columns") or []
    return column_period(table, col) or (names[col] if 0 <= col < len(names) and names[col] else "period not stated")


def read_cell(cell: dict[str, Any], table: dict[str, Any]) -> CellValue | None:
    """The typed value of a value cell, or None when it holds no number."""
    if cell.get("value") is None or cell.get("cell_type") not in ("numeric", "percent"):
        return None
    period = cell.get("period") or column_period(table, int(cell.get("col", -1)))
    period = period if period and FY.match(period) else None
    unit, currency, scale = cell.get("unit"), cell.get("currency"), cell.get("scale")
    note = None
    if unit == "currency" and not (currency and ISO.match(currency)):
        unit, currency, note = "other", None, "no ISO currency; kept as a plain amount"
    unit = unit if unit in ("currency", "percent", "count") else "other"
    scale = scale if scale in SCALE_FACTORS and unit != "percent" else None
    bbox = cell.get("bbox") if isinstance(cell.get("bbox"), list) and len(cell["bbox"]) == 4 else None
    return CellValue(Decimal(str(cell["value"])), unit, currency if unit == "currency" else None, scale, period,
                     cell.get("page"), bbox, str(cell.get("text") or ""), note)


def row_line(row: dict[str, Any]) -> str:
    """A row exactly as the chunk text writes it."""
    try:
        return _row_line(NormRow.model_validate(row))
    except ValueError:
        return ""


def polygon_box(bbox: Any) -> list[float] | None:
    """[x0, y0, x1, y1] from an evidence record's stored polygon (or a plain box)."""
    if isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(p, list) and len(p) == 2 for p in bbox):
        xs, ys = [p[0] for p in bbox], [p[1] for p in bbox]
        return [min(xs), min(ys), max(xs), max(ys)]
    if isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
        return [float(v) for v in bbox]
    return None


def same_box(a: list[float] | None, b: Any) -> bool:
    return (a is not None and isinstance(b, list) and len(b) == 4
            and all(abs(x - y) <= BOX_TOLERANCE for x, y in zip(a, b, strict=True)))


@dataclass(frozen=True)
class Located:
    row: dict[str, Any] | None  # the row the quote is
    cell: dict[str, Any] | None  # the cell the box points at (None when there is no box)
    candidates: list[dict[str, Any]]  # value cells of the row


def locate(table: dict[str, Any] | None, quote: str, box: list[float] | None) -> Located:
    """The cited row (the quote is its line) and the cell the evidence box marks."""
    if not isinstance(table, dict):
        return Located(None, None, [])
    rows = [r for r in table.get("rows", []) if (line := row_line(r)) and (line == quote or quote in line)]
    if not rows:
        return Located(None, None, [])
    if box is not None:
        for r in rows:
            for c in r.get("cells", []):
                if same_box(box, c.get("bbox")):
                    return Located(r, c, [x for x in r.get("cells", []) if x.get("value") is not None])
    return Located(rows[0], None, [c for c in rows[0].get("cells", []) if c.get("value") is not None])
