"""Code picks the table cell when the match is clear; the model is asked only when it is not (D-030).

The model's job in Document Intelligence step 3 is to point at the cell that holds a requested
line item for a company and year. Most of the time that needs no model: the knowledge base
lists the names reports print a line item under, and ingestion already gave every value cell its
fiscal year. A cell is picked here when, for one company, line item and year:

* its row label is one of the line item's printed names, exactly (footnote marks ignored), and
  that name belongs to no other line item (an ambiguous name such as "Income from operations",
  revenue in some Ind AS reports and operating income in US GAAP ones, is left to the model);
* the cell holds a number of a fitting unit (an amount for an amount, never a percentage), for
  the requested fiscal year (or the latest one when the question names none);
* among such cells, those in a primary statement (consolidated statements of operations,
  balance sheets, cash flows, ...) win over other tables (segments, quarters, summaries), and the
  remaining candidates all hold the same value. Otherwise the choice is the model's.

A line item is resolved by code for every company and year or not at all: anything left over
goes to the model with the same prompt as before. The picks are cell references like the
model's (``T2.R5.C1``), so everything after them (reading the value, quoting the row, evidence,
verification) is unchanged. The verifier checks code's picks exactly as it checks the model's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aeids.agents.cells import FY, CellValue, read_cell
from aeids.knowledge.metrics import KnowledgeBase, _norm

RESOLVER_VERSION = "kb-match-v1"
_STATEMENT = re.compile(r"consolidated\s+(statements?|balance\s+sheets?)|statements?\s+of\s+(operations|income|"
                        r"earnings|cash\s+flows?|financial\s+position|profit\s+and\s+loss)|balance\s+sheets?", re.I)
_FOOTNOTE = re.compile(r"(\s*\(\d{1,2}\)|\s*\*+|\s*\[\d{1,2}\])+$")
_UNITS = {"currency": {"currency", "other"}, "count": {"count", "other"}, "percent": {"percent"},
          "other": {"currency", "count", "other"}}


@dataclass(frozen=True)
class Candidate:
    primary: bool  # in a primary financial statement
    order: int  # the table's search rank
    tid: str
    row: int
    col: int
    value: CellValue

    @property
    def cell(self) -> str:
        return f"{self.tid}.R{self.row}.C{self.col}"


@dataclass(frozen=True)
class Resolved:
    picks: list[tuple[str, str]]  # (metric name, cell reference)
    unresolved: list[str]  # metric names left to the model


def _label(row: dict[str, Any]) -> str:
    return _norm(_FOOTNOTE.sub("", str(row.get("label") or "")))


def _title(table: dict[str, Any], chunk_text: str) -> str:
    first = next((ln for ln in chunk_text.splitlines() if ln.startswith("Table:")), "")
    return f"{table.get('title') or ''} {first}"


class CellResolver:
    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        owners: dict[str, set[str]] = {}
        for key, item in kb.line_items.items():
            for n in item.names():
                owners.setdefault(_norm(n), set()).add(key)
        self._ambiguous = {n for n, keys in owners.items() if len(keys) > 1}

    def named_in(self, question: str) -> list[str]:
        """Line items the question names word for word (longest names first, no overlaps, no
        ambiguous names). Used when the planner listed none for a question that asks for one."""
        text = f" {_norm(question)} "
        found: list[tuple[int, int, str]] = []  # (start, end, line item name)
        names = sorted(((n, item.name) for item in self.kb.line_items.values() if not item.reported_ratio_of
                        for n in {_norm(x) for x in item.names()} if len(n) >= 6 and n not in self._ambiguous),
                       key=lambda x: -len(x[0]))
        for n, item_name in names:
            start = text.find(f" {n} ")
            while start >= 0:
                end = start + len(n) + 1
                if not any(s < end and start < e for s, e, _ in found):
                    found.append((start, end, item_name))
                start = text.find(f" {n} ", start + 1)
        return list(dict.fromkeys(name for _, _, name in sorted(found)))

    def names(self, metric: str) -> tuple[str, set[str]] | None:
        """The line item a metric name is, with the printed names that are its alone."""
        hit = self.kb.line_item_for(metric)
        if hit is None:
            return None
        names = {_norm(n) for n in hit[1].names()} - self._ambiguous
        return (hit[1].unit, names) if names else None

    def candidates(self, unit: str, names: set[str], company_id: Any, tables: dict[str, Any]) -> list[Candidate]:
        out = []
        for order, (tid, t) in enumerate(tables.items()):
            if t.hit.company_id != company_id:
                continue
            primary = bool(_STATEMENT.search(_title(t.table, t.chunk.text)))
            for r, row in enumerate(t.rows):
                if _label(row) not in names:
                    continue
                for cell in row.get("cells", []):
                    cv = read_cell(cell, t.table)
                    if cv is not None and cv.period and cv.unit in _UNITS.get(unit, {"other"}):
                        out.append(Candidate(primary, order, tid, r, int(cell["col"]), cv))
        return out

    def resolve(self, metrics: list[str], periods: list[str], companies: list[Any],
                tables: dict[str, Any]) -> Resolved:
        wanted = [p for p in dict.fromkeys(periods) if FY.match(p)]
        picks: list[tuple[str, str]] = []
        unresolved: list[str] = []
        for metric in dict.fromkeys(metrics):
            found = self._resolve_metric(metric, wanted, companies, tables)
            if found is None:
                unresolved.append(metric)
            else:
                picks += [(metric, c) for c in found]
        return Resolved(picks, unresolved)

    def _resolve_metric(self, metric: str, wanted: list[str], companies: list[Any],
                        tables: dict[str, Any]) -> list[str] | None:
        spec = self.names(metric)
        if spec is None:
            return None
        cells = []
        for company in companies:
            cands = self.candidates(*spec, company.company_id, tables)
            if not cands:
                return None
            for period in wanted or [max(c.value.period for c in cands)]:
                chosen = self._one(c for c in cands if c.value.period == period)
                if chosen is None:
                    return None
                cells.append(chosen.cell)
        return cells

    @staticmethod
    def _one(cands) -> Candidate | None:
        """The cell, when the candidates of the best tier all hold the same value."""
        cands = list(cands)
        if not cands:
            return None
        tier = [c for c in cands if c.primary] or cands
        values = {(c.value.value, c.value.unit, c.value.currency, c.value.scale) for c in tier}
        return min(tier, key=lambda c: (c.order, c.row, c.col)) if len(values) == 1 else None
