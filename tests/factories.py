"""Builders for synthetic TableLens structures and real (blank-page) PDFs."""

from __future__ import annotations

import io
import json
from typing import Any

from pypdf import PdfWriter


def pdf_bytes(pages: int, tag: str = "") -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": f"test {tag}"})  # different tag -> different checksum
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def cell(r: int, q: int, text: str, head: bool = False, cs: int = 1, rs: int = 1,
         section: bool = False, bbox: list[float] | None = None) -> dict[str, Any]:
    return {"row": r, "col": q, "row_span": rs, "col_span": cs, "column_header": head,
            "row_header": False, "row_section": section, "text": text,
            "bbox_pdf_pt": bbox or [10.0 + 50 * q, 10.0 + 12 * r, 55.0 + 50 * q, 20.0 + 12 * r]}


def grid_table(page: int, table_id: int, rows: list[list[str]], header_rows: int = 1,
               **extra: Any) -> dict[str, Any]:
    cells = [cell(r, q, s, head=r < header_rows) for r, row in enumerate(rows)
             for q, s in enumerate(row)]
    return {"page": page, "table_id": table_id, "bbox_pdf_pt": [10, 10, 400, 300],
            "orientation": 0, "complete": True, "engine": "tatr", "route": "text layer",
            "num_rows": len(rows), "num_cols": len(rows[0]), "cells": cells, **extra}


def region(page: int, rid: int, label: str, text: str, order: int | None = None,
           **extra: Any) -> dict[str, Any]:
    return {"page": page, "id": rid, "label": label, "text": text, "order": order,
            "bbox_pdf_pt": [10, 10 + rid, 500, 20 + rid], "complete": True, **extra}


def structure(tables: list[dict], regions: list[dict], pages: list[dict] | None = None,
              file: str = "report.pdf", joined: list[dict] | None = None) -> dict[str, Any]:
    if pages is None:  # reading order: regions and tables by (page, order)
        items: dict[int, list[tuple[int, list]]] = {}
        for r in regions:
            items.setdefault(r["page"], []).append((r.get("order") or 0, [r["label"], r["id"]]))
        for t in tables:
            items.setdefault(t["page"], []).append((t.get("order") or 0, ["table", t["table_id"]]))
        pages = [{"page": p, "items": [x for _, x in sorted(v, key=lambda z: z[0])]}
                 for p, v in sorted(items.items())]
    return {"file": file, "model": "test", "tables_json": "x", "pages": pages,
            "tables": tables, "joined_tables": joined or [], "regions": regions}


def dumps(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()


INCOME_ROWS = [
    ["", "Years ended", "", ""],
    ["", "September 27, 2025", "September 28, 2024", "September 30, 2023"],
    ["Net sales:", "", "", ""],
    ["Products", "$ 307,003", "$ 294,866", "$ 298,085"],
    ["Services", "109,158", "96,169", "85,200"],
    ["Total net sales", "416,161", "391,035", "383,285"],
    ["Gross margin", "195,201", "180,683", "169,148"],
    ["Other income/(expense), net", "(321)", "269", "(565)"],
    ["Cash, cash equivalents, and restricted cash and cash equivalents, beginning", "", "", ""],
    ["balances", "$ 29,943", "$ 30,737", "$ 24,977"],
    ["Earnings per share:", "", "", ""],
    ["Basic", "$ 7.49", "$ 6.11", "$ 6.16"],
    ["Shares used in computing earnings per share:", "", "", ""],
    ["Basic", "14,948,500", "15,343,783", "15,744,231"],
    ["Services (1)", "—", "n/a", "12 %"],
]
INCOME_SUBTITLE = ("(In millions, except number of shares, which are reflected in thousands, "
                   "and per-share amounts)")


def text_pdf(pages: list[str], tag: str = "") -> bytes:
    """A PDF whose pages hold the given text (Helvetica), so its type can be read (D-035)."""
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        lines = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in text.splitlines() or [""]]
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 11 Tf 14 TL 72 740 Td " + " ".join(f"({ln}) '" for ln in lines) + " ET").encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    writer.add_metadata({"/Title": f"test {tag}"})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
