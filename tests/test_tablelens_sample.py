"""Checks against a real TableLens output: Apple's FY2025 10-K (public filing).

The file is not in git (CLAUDE.md rule 11). Set AEIDS_SAMPLE_STRUCTURE, or keep it where TableLens
writes it: tablelens/output/... in this repo (D-020), else the old ../IntelligencePDF/output/...;
the test is skipped when the file is absent.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from aeids.ingestion.chunking import build_chunks
from aeids.ingestion.structure import parse_structure

_REL = Path("output") / "NASDAQ_AAPL_2025" / "structure" / "NASDAQ_AAPL_2025_structure.json"
_ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = [_ROOT / "tablelens" / _REL, _ROOT.parent / "IntelligencePDF" / _REL]
DEFAULT = next((p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0])
SAMPLE = Path(os.environ.get("AEIDS_SAMPLE_STRUCTURE", DEFAULT))

pytestmark = pytest.mark.skipif(not SAMPLE.is_file(), reason=f"sample not found: {SAMPLE}")


@pytest.fixture(scope="module")
def result():
    return build_chunks(parse_structure(SAMPLE.read_bytes()))


def _cell(result, key: str, label: str, nth: int = 0, col: int = 0):
    table = next(t for t in result.tables if t.key == key)
    return [r for r in table.rows if r.label == label][nth].cells[col]


def test_income_statement(result) -> None:
    c = _cell(result, "p32-t1", "Total net sales")
    assert (c.value, c.unit, c.currency, c.scale, c.period) == (
        Decimal("416161"), "currency", "USD", "million", "FY2025")
    assert c.page == 32 and c.bbox is not None
    assert _cell(result, "p32-t1", "Net income", col=2).value == Decimal("96995")  # FY2023
    eps = _cell(result, "p32-t1", "Diluted", 0)
    assert (eps.value, eps.scale) == (Decimal("7.46"), "units")
    shares = _cell(result, "p32-t1", "Diluted", 1)
    assert (shares.unit, shares.scale) == ("count", "thousand")


def test_segments_unit_from_caption(result) -> None:
    c = _cell(result, "p25-t1", "Greater China")
    assert (c.value, c.scale, c.scale_source) == (Decimal("64377"), "million", "context")
    change = _cell(result, "p25-t1", "Greater China", col=1)
    assert (change.value, change.unit) == (Decimal("-4"), "percent")


def test_balance_sheet_wrapped_label_and_negatives(result) -> None:
    table = next(t for t in result.tables if t.key == "p34-t1")
    common = next(r for r in table.rows if r.label.startswith("Common stock and additional"))
    assert common.label_rows == [31, 32]
    assert common.cells[0].value == Decimal("93568")
    assert common.cells[0].unit == "currency"  # "shares" in the label, but dollars
    assert _cell(result, "p34-t1", "Accumulated deficit").value == Decimal("-14264")


def test_section_paths(result) -> None:
    seg = next(c for c in result.chunks if c.kind == "table" and c.meta["table_key"] == "p25-t1")
    assert seg.section.startswith("PART II > Item 7. Management")
    assert seg.section.endswith("> Segment Operating Performance")
    assert "The following table shows net sales by reportable segment" in seg.text  # lead-in
    risk = [c for c in result.chunks if c.section and "Item 1A. Risk Factors" in c.section]
    assert 30 <= len(risk) <= 60  # risk sentences are text, not one-line sections
    assert all(c.text.startswith(f"Section: {c.section}") for c in result.chunks if c.section)
    assert sum(not c.section for c in result.chunks) <= 1  # only the cover, before any heading
    assert max(len(c.section or "") for c in result.chunks) <= 500  # fits document_chunks.section


def test_whole_document(result) -> None:
    assert len(result.tables) == 47
    unitless = {t.key for t in result.tables if "no unit or scale stated for this table" in t.warnings}
    assert unitless <= {"p3-t1", "p31-t1", "p57-t1", "p57-t2+p58-t1+p59-t1+p60-t1"}  # indexes
    assert sum(c.kind == "chart" for c in result.chunks) == 1
    assert max(c.page_end for c in result.chunks) <= 80
