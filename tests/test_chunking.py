"""Structure validation and chunking."""

from __future__ import annotations

import json

import pytest

from aeids.ingestion.chunking import MAX_CHARS, build_chunks, heading_level
from aeids.ingestion.structure import InvalidStructure, parse_structure
from tests.factories import INCOME_ROWS, INCOME_SUBTITLE, dumps, grid_table, region, structure


def _sample() -> bytes:
    regions = [
        region(1, 1, "page header", "Apple Inc. | 2025 Form 10-K | 1", order=0),
        region(1, 2, "heading", "Segment Operating Performance", order=1),
        region(1, 3, "paragraph", "The Company reports segments by geography.", order=2),
        region(1, 4, "paragraph", "The following table shows net sales (dollars in millions):",
               order=3),
        region(1, 6, "page footer", "Apple Inc. | 1", order=9),
        region(2, 1, "heading", "CONSOLIDATED STATEMENTS OF OPERATIONS", order=1),
        region(2, 2, "chart", "", order=3,
               chart_table={"num_rows": 2, "num_cols": 2, "header": ["Date", "Apple"],
                            "rows": [["9/25/20", "100"], ["9/27/25", "230"]]},
               check={"status": "unverified", "not_in_pdf": ["100", "230"]},
               read_by="PaddleOCR-VL-1.6 · Chart Recognition:"),
    ]
    tables = [
        grid_table(1, 1, [["", "2025", "2024"], ["Americas", "$ 178,353", "$ 167,045"]], order=4),
        grid_table(2, 1, INCOME_ROWS, header_rows=2, order=2,
                   title="CONSOLIDATED STATEMENTS OF OPERATIONS", subtitle=INCOME_SUBTITLE),
    ]
    return dumps(structure(tables, regions))


def test_parse_rejects_bad_structures() -> None:
    with pytest.raises(InvalidStructure):
        parse_structure(b"not json")
    with pytest.raises(InvalidStructure):
        parse_structure(b"{}")  # no tables and no regions
    bad = json.loads(_sample())
    bad["tables"][0]["cells"][0]["row"] = 99
    with pytest.raises(InvalidStructure, match="outside"):
        parse_structure(json.dumps(bad))
    bad = json.loads(_sample())
    bad["regions"][0]["bbox_pdf_pt"] = [10, 10, 5, 5]
    with pytest.raises(InvalidStructure):
        parse_structure(json.dumps(bad))
    bad = json.loads(_sample())
    bad["regions"][0]["page"] = 0
    with pytest.raises(InvalidStructure):
        parse_structure(json.dumps(bad))


def test_inverted_cell_box_is_dropped_not_fatal() -> None:
    data = json.loads(_sample())
    data["tables"][0]["cells"][0]["bbox_pdf_pt"] = [31.3, 585.8, 197.3, 557.2]  # y1 < y0
    s = parse_structure(json.dumps(data))
    assert s.tables[0].cells[0].bbox_pdf_pt is None
    assert s.tables[0].cells[0].bbox_dropped
    assert any("dropped" in w for w in build_chunks(s).warnings)


def test_parse_strips_nul_and_ignores_unknown_fields() -> None:
    data = json.loads(_sample())
    data["regions"][2]["text"] = "seg\x00ments"
    data["regions"][2]["new_tablelens_field"] = {"x": 1}
    s = parse_structure(json.dumps(data))
    assert s.regions[2].text == "segments"
    assert s.max_page() == 2


def test_chunks_follow_reading_order_and_sections() -> None:
    result = build_chunks(parse_structure(_sample()))
    kinds = [c.kind for c in result.chunks]
    # the two short paragraphs before the first table became that table's lead-in
    assert kinds == ["table", "table", "chart"]
    first = result.chunks[0]
    assert first.section == "Segment Operating Performance"
    assert first.text.startswith("Section: Segment Operating Performance\n\n")
    assert "Form 10-K" not in " ".join(c.text for c in result.chunks)  # running header skipped
    assert all(c.text.startswith("Section: ") for c in result.chunks)
    assert all(c.meta["chunking_version"] == "2" for c in result.chunks)


def test_table_chunk_has_lead_in_units_and_normalised_rows() -> None:
    result = build_chunks(parse_structure(_sample()))
    seg = result.chunks[0]
    assert seg.meta["table_key"] == "p1-t1"
    assert seg.page_start == seg.page_end == 1
    assert "The following table shows net sales (dollars in millions):" in seg.text
    assert [b["ref"] for b in seg.meta["boxes"]] == ["region:1:3", "region:1:4", "table:1:1"]
    assert "Units: USD million" in seg.text
    cell = seg.meta["table"]["rows"][0]["cells"][0]
    assert (cell["value"], cell["scale"], cell["scale_source"]) == ("178353", "million", "context")

    income = result.chunks[1]
    assert income.section == "CONSOLIDATED STATEMENTS OF OPERATIONS"
    assert income.meta["section_path"] == ["CONSOLIDATED STATEMENTS OF OPERATIONS"]
    assert "Columns: Years ended / September 27, 2025 (FY2025)" in income.text
    assert "Net sales: Products | $ 307,003" in income.text
    assert income.meta["is_financial"] is True


@pytest.mark.parametrize(
    ("heading", "level"),
    [
        ("PART II", 0), ("Part I", 0),
        ("Item 7. Management’s Discussion and Analysis of Financial Condition and Results of "
         "Operations", 1),
        ("ITEM 1A. RISK FACTORS", 1),
        ("CONSOLIDATED STATEMENTS OF OPERATIONS", 2), ("Consolidated Balance Sheets", 2),
        ("Note 4 – Financial Instruments", 2), ("NOTE 2 — EARNINGS PER SHARE", 2),
        ("Note 1. Summary of Significant Accounting Policies", 2),
        ("OPERATING SEGMENTS", 2),
        ("Segment Operating Performance", 3), ("Apple Inc.", 3),
        ("The Company depends on component and product manufacturing and logistical services "
         "provided by outsourcing partners, many of which are located outside of the U.S.", None),
        ("We face intense competition. If we do not continue to innovate, we may lose users.",
         None),
        ("", None),
    ],
)
def test_heading_levels(heading: str, level: int | None) -> None:
    assert heading_level(heading) == level


def _tree_sample() -> bytes:
    risk = "The Company’s operations depend significantly on global and regional economic " \
           "conditions and adverse conditions can materially affect results."
    regions = [
        region(5, 1, "heading", "PART I", order=1),
        region(5, 2, "heading", "Item 1A. Risk Factors", order=2),
        region(5, 3, "heading", "Macroeconomic and Industry Risks", order=3),
        region(5, 4, "heading", risk, order=4),  # a bold sentence, not a section
        region(5, 5, "paragraph", "Adverse economic conditions can lead to credit risk. " * 12,
               order=5),
        region(6, 1, "heading", "PART II", order=1),
        region(6, 2, "heading", "Item 7. Management’s Discussion and Analysis", order=2),
        region(6, 3, "heading", "Seasonality", order=3),
        region(6, 4, "paragraph", "Sales are higher in the first quarter.", order=4),
        region(6, 5, "heading", "Fiscal Period", order=5),
        region(6, 6, "paragraph", "The fiscal year ends in September.", order=6),
        region(6, 7, "heading", "Item 8. Financial Statements", order=7),
        region(6, 8, "paragraph", "See the following pages.", order=8),
    ]
    return dumps(structure([], regions))


def test_section_tree_paths() -> None:
    chunks = build_chunks(parse_structure(_tree_sample())).chunks
    risk = chunks[0]
    assert risk.section == "PART I > Item 1A. Risk Factors > Macroeconomic and Industry Risks"
    assert risk.meta["section_path"] == ["PART I", "Item 1A. Risk Factors",
                                         "Macroeconomic and Industry Risks"]
    # the bold risk sentence is the chunk's first paragraph, not a section of its own
    assert risk.text.split("\n\n")[1].startswith("The Company’s operations depend")
    assert chunks[-1].section == "PART II > Item 8. Financial Statements"  # Item 7 path cleared


def test_tiny_sibling_subsections_merge_under_parent() -> None:
    chunks = build_chunks(parse_structure(_tree_sample())).chunks
    mdna = [c for c in chunks if "Item 7" in (c.section or "")]
    assert len(mdna) == 1
    c = mdna[0]
    assert c.section == "PART II > Item 7. Management’s Discussion and Analysis"
    assert c.meta["subsections"] == ["Seasonality", "Fiscal Period"]
    assert "Seasonality\n\nSales are higher" in c.text
    assert "Fiscal Period\n\nThe fiscal year ends" in c.text


def test_big_subsections_do_not_merge() -> None:
    long = "Revenue grew across all regions this year. " * 15
    regions = [region(1, 1, "heading", "Item 7. MD&A", order=1),
               region(1, 2, "heading", "Overview", order=2),
               region(1, 3, "paragraph", long, order=3),
               region(1, 4, "heading", "Outlook", order=4),
               region(1, 5, "paragraph", "Demand is expected to stay strong.", order=5)]
    chunks = build_chunks(parse_structure(dumps(structure([], regions)))).chunks
    assert [c.section for c in chunks] == ["Item 7. MD&A > Overview", "Item 7. MD&A > Outlook"]


def test_chart_is_flagged_image_read() -> None:
    chart = build_chunks(parse_structure(_sample())).chunks[-1]
    assert chart.meta["image_read"] is True
    assert chart.meta["check_status"] == "unverified"
    assert "not in the PDF text" in chart.text


def test_long_text_and_big_tables_are_split() -> None:
    para = "This is a sentence about supply chains. " * 200
    rows = [["", "2025"]] + [[f"Line item {i}", f"{i},000"] for i in range(400)]
    data = structure([grid_table(1, 1, rows, order=2, subtitle="(In millions)")],
                     [region(1, 1, "paragraph", para, order=1)])
    result = build_chunks(parse_structure(dumps(data)))
    assert all(len(c.text) <= MAX_CHARS + 400 for c in result.chunks)
    texts = [c for c in result.chunks if c.kind == "text"]
    tables = [c for c in result.chunks if c.kind == "table"]
    assert len(texts) > 1 and len(tables) > 1
    assert all("Columns: 2025 (FY2025)" in c.text for c in tables)  # header repeated
    assert sum(len(c.meta["table"]["rows"]) for c in tables) == 400
    assert {c.meta["table_parts_total"] for c in tables} == {len(tables)}


def test_joined_table_is_one_table() -> None:
    p1 = grid_table(1, 1, [["Exhibit", "Description"], ["3.1", "Articles"]], order=1)
    p2 = grid_table(2, 1, [["4.1", "Indenture"]], header_rows=0, order=1)
    joined = {"parts": [{"page": 1, "table_id": 1}, {"page": 2, "table_id": 1}],
              "title": "Exhibits", "subtitle": None, "num_rows": 3, "num_cols": 2,
              "cells": [{**c, "page": 1, "table_id": 1} for c in p1["cells"]]
              + [{**c, "row": 2, "page": 2, "table_id": 1} for c in p2["cells"]]}
    result = build_chunks(parse_structure(dumps(structure([p1, p2], [], joined=[joined]))))
    assert len(result.tables) == 1
    t = result.tables[0]
    assert t.key == "p1-t1+p2-t1" and (t.page_start, t.page_end) == (1, 2)
    assert [r.label for r in t.rows] == ["3.1", "4.1"]
    assert result.chunks[0].page_end == 2


def test_unknown_reading_order_items_warn() -> None:
    data = structure([], [region(1, 1, "paragraph", "hello", order=1)])
    data["pages"][0]["items"].append(["table", 7])
    result = build_chunks(parse_structure(dumps(data)))
    assert any("table 7" in w for w in result.warnings)
