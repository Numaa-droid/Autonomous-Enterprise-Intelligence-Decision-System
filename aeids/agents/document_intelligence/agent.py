"""Document Intelligence (M4): finds the reported values a question needs, with evidence.

Steps:

1. **Understand** (LLM, ``plan_v1.md``): which companies the question names, mapped to the
   tenant's companies that have a current report; which reported line items are needed; which
   fiscal years. A named company without an uploaded report stops the task
   (``needs_more_information``): AEIDS never answers from another company's data.
2. **Search** (hybrid) for each line item within each company's current reports; keep table
   chunks only (chart values are read off images and are not checked evidence).
3. **Point**: code picks the cell when the match is clear (``resolver.py``: the row is a
   printed name of the line item, the column the asked year, one value in the primary
   statements); the model (``extract_v2.md``) names the cells (``T2.R5.C1``) of whatever is
   left, never numbers. A rework round always goes to the model.
4. **Resolve** deterministically: each cell's value, unit, currency, scale and period come from
   the normalised table (ingestion), the quote is the row exactly as it appears in the chunk,
   the page and box are the cell's. A reference to a cell that does not exist, or that holds no
   number, is dropped with a warning. Nothing is inferred (MVP_PLAN.md M4).

Each value becomes a ``reported_value`` finding citing its evidence, and a typed fact in
``outputs["facts"]`` for the Financial Analyst. A question about no numbers (risks, strategy)
gets the most relevant passages as evidence instead.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aeids import doctype, knowledge
from aeids.agents.base import AgentContext, ChunkView, CompanyView
from aeids.agents.cells import FY, column_name, read_cell, row_line
from aeids.agents.document_intelligence.resolver import RESOLVER_VERSION, CellResolver
from aeids.agents.llm_json import ChatModel, Usage, ask_json, load_prompt, untrusted
from aeids.agents.stubs import _base, _filters, excerpt
from aeids.config import Settings
from aeids.contracts import AgentResult, EvidenceProposal, EvidenceRef, Finding, TaskMessage
from aeids.retrieval.search import SearchFilters, SearchHit

HERE = Path(__file__).parent
PROMPT_VERSION = "di-v2"  # + "+kb-<version>" of the metric knowledge base (D-026)
MAX_TABLES = 10
SEARCH_DEPTH = 15  # hits looked at per query; most are text, only tables are kept
TABLES_PER_QUERY = 6
PASSAGES = 5
CELL = re.compile(r"^T(\d+)\.R(\d+)\.C(\d+)$")


# --- what the model may answer ----------------------------------------------------------------

class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CompanyMatch(_Loose):
    named: str = Field(min_length=1, max_length=200)
    match: str | None = Field(default=None, max_length=300)


class MetricRequest(_Loose):
    name: str = Field(min_length=1, max_length=120)
    search_query: str = Field(min_length=1, max_length=300)


class QuestionPlan(_Loose):
    companies: list[CompanyMatch] = Field(default_factory=list, max_length=10)
    metrics: list[MetricRequest] = Field(default_factory=list, max_length=12)
    periods: list[str] = Field(default_factory=list, max_length=12)


class CellPick(_Loose):
    metric: str = Field(min_length=1, max_length=120)
    cell: str = Field(pattern=CELL.pattern)


class CellPicks(_Loose):
    facts: list[CellPick] = Field(default_factory=list, max_length=60)
    not_found: list[str] = Field(default_factory=list, max_length=30)


# --- tables shown to the model ----------------------------------------------------------------

@dataclass
class _Table:
    tid: str
    hit: SearchHit
    chunk: ChunkView
    table: dict[str, Any]

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.table.get("rows", [])


def render_table(t: _Table) -> str:
    """A table as the model sees it: rows R<n>, value cells C<k> with their period."""
    lines = [ln for ln in t.chunk.text.splitlines() if ln.startswith(("Table:", "Units:"))]
    if t.table.get("subtitle"):
        lines.append(t.table["subtitle"])
    for n, row in enumerate(t.rows):
        label = f"{row['section']}: {row['label']}" if row.get("section") else row["label"]
        cells = " | ".join(f"C{c['col']} ({c.get('period') or column_name(t.table, c['col'])}): {c['text']}"
                           for c in row.get("cells", []))
        lines.append(f"R{n} | {label}" + (f" | {cells}" if cells else ""))
    return untrusted(t.tid, "\n".join(lines), company=t.hit.company_name, file=t.hit.filename,
                     pages=f"{t.chunk.page_start}-{t.chunk.page_end}", section=t.chunk.section)


def _units_text(unit: str, currency: str | None, scale: str | None) -> str:
    parts = [currency if unit == "currency" else {"percent": "%", "count": "count"}.get(unit, "")]
    if scale and scale != "units":
        parts.append(scale)
    return " ".join(p for p in parts if p)


# Places and public bodies the planner sometimes lists as companies ("risks from US export controls").
# They are never a missing report; a company without an uploaded report is still refused (D-036).
NOT_COMPANIES = frozenset({
    "us", "usa", "united states", "united states of america", "america", "uk", "united kingdom", "britain",
    "eu", "european union", "europe", "asia", "china", "prc", "india", "japan", "taiwan", "korea",
    "south korea", "germany", "france", "canada", "mexico", "russia", "ukraine", "brazil", "hong kong",
    "singapore", "israel", "ireland", "netherlands", "switzerland", "australia", "middle east",
    "government", "us government", "federal government", "congress", "white house", "sec", "irs", "ftc",
    "doj", "fda", "epa", "federal reserve", "fed", "department of commerce", "commerce department", "bis",
    "sebi", "rbi", "mca", "opec", "wto", "un", "united nations",
})


def is_company(named: str) -> bool:
    """False for a country, region, government or regulator the planner listed as a company."""
    key = re.sub(r"[^a-z ]", "", named.casefold().replace(".", "")).strip()
    return re.sub(r"^the ", "", key) not in NOT_COMPANIES


def _no_esg_source(companies: list[str]) -> str:
    """Why an ESG question cannot be answered: only annual reports (without a BRSR section) (D-035)."""
    names = ", ".join(dict.fromkeys(companies)) or "the companies asked about"
    return ("this is an ESG question (emissions, energy, water, waste, safety or BRSR), but only annual reports "
            f"without an ESG / BRSR section are uploaded for {names}. AEIDS does not answer ESG questions from an "
            "annual report: upload the company's ESG / sustainability report or BRSR (on the Reports page, with "
            "the type ESG report or BRSR), then re-run")


class DocumentIntelligenceAgent:
    name = "document_intelligence"
    version = "1.0"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: ChatModel, settings: Settings) -> None:
        self._llm = llm
        self.model = settings.llm_model
        self._kb = knowledge.load()
        self.prompt_version = f"{PROMPT_VERSION}+kb-{self._kb.version}"
        self._plan_prompt = load_prompt(HERE / "plan_v2.md")
        self._extract_prompt = load_prompt(HERE / "extract_v2.md")
        self._matcher = CellResolver(self._kb)
        self._resolver = self._matcher if settings.di_cell_resolver == "code_first" else None

    # ---- the run ----

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        usage = Usage()
        query = str(task.inputs.get("query") or task.objective)
        base_filters = _filters(task.inputs.get("scope", {}) or {})
        available = ctx.companies()
        if base_filters.company_ids:
            available = [c for c in available if c.company_id in base_filters.company_ids]
        if not available:
            return self._needs(task, usage, "no company in this workflow's scope has a searchable report")
        esg_question = doctype.needs_esg_report(query)
        if esg_question and not ctx.esg_sources([c.company_id for c in available]):
            return self._needs(task, usage, _no_esg_source([c.name for c in available]))  # before any model call

        plan = ask_json(self._llm, QuestionPlan, self._plan_prompt,
                        self._plan_message(query, available, task), usage, model=self.model)
        plan = plan.model_copy(update={"companies": [m for m in plan.companies if is_company(m.named)]})
        by_name = {c.name: c for c in available}
        missing = [m.named for m in plan.companies if not m.match or m.match not in by_name]
        if missing:
            return self._needs(task, usage, "no report has been uploaded for "
                               + ", ".join(dict.fromkeys(missing))
                               + "; AEIDS does not answer from another company's reports")
        companies = [by_name[m.match] for m in plan.companies if m.match] or available
        companies = list({c.company_id: c for c in companies}.values())
        if esg_question:  # an ESG question is answered from ESG sources only, never from an annual report (D-035)
            have = ctx.esg_sources([c.company_id for c in companies])
            named = any(m.match for m in plan.companies)
            lacking = [c.name for c in companies if c.company_id not in have]
            if named and lacking:
                return self._needs(task, usage, _no_esg_source(lacking))
            companies = [c for c in companies if c.company_id in have]
            if not companies:
                return self._needs(task, usage, _no_esg_source(lacking))

        if not plan.metrics and (named := self._matcher.named_in(query)):
            # the planner listed no line item, yet the question names one word for word (D-030)
            years = [f"FY{y}" for y in dict.fromkeys(re.findall(r"\b((?:19|20)\d{2})\b", query))]
            plan = plan.model_copy(update={
                "metrics": [MetricRequest(name=n, search_query=f"consolidated statements {n}") for n in named],
                "periods": plan.periods or years})
        if not plan.metrics:
            return self._passages(task, ctx, usage, query, companies, base_filters,
                                  note="the question asks for no reported numbers")
        tables = self._find_tables(ctx, plan, companies, base_filters)
        if not tables:
            return self._passages(task, ctx, usage, query, companies, base_filters,
                                  note="no financial table matched the requested line items",
                                  not_found=[m.name for m in plan.metrics])

        # code picks the cells it is sure of; the model only sees what is left (D-030)
        if self._resolver is None or task.inputs.get("rework_instructions"):  # a rejected pick may be code's
            resolved, left = [], [m.name for m in plan.metrics]
        else:
            r = self._resolver.resolve([m.name for m in plan.metrics], plan.periods, companies, tables)
            resolved, left = r.picks, r.unresolved
        facts = [CellPick(metric=m, cell=c) for m, c in resolved]
        not_found: list[str] = []
        if left:
            rest = plan.model_copy(update={"metrics": [m for m in plan.metrics if m.name in left]})
            picks = ask_json(self._llm, CellPicks, self._extract_prompt,
                             self._extract_message(query, rest, companies, tables, task), usage,
                             model=self.model, max_tokens=3000)
            facts += picks.facts
            not_found = picks.not_found
        by_code = {c for _, c in resolved}
        return self._facts(task, ctx, usage, query, plan, CellPicks(facts=facts, not_found=not_found),
                           tables, companies, base_filters, by_code=by_code)

    # ---- prompts ----

    def _plan_message(self, query: str, available: list[CompanyView], task: TaskMessage) -> str:
        names = "\n".join(f"- {c.name}" for c in available)
        catalog = knowledge.planning_catalog(self._kb, query)  # ESG/BRSR entries only when asked about
        return (f"QUESTION:\n{query}\n\nAVAILABLE COMPANIES (reports uploaded):\n{names}"
                f"\n\nKNOWLEDGE BASE (version {self._kb.version}):\n{catalog}" + self._retry_notes(task))

    def _extract_message(self, query: str, plan: QuestionPlan, companies: list[CompanyView],
                         tables: dict[str, _Table], task: TaskMessage) -> str:
        metrics = "\n".join(self._metric_line(m.name) for m in plan.metrics)
        periods = ", ".join(plan.periods) or "the most recent fiscal year in the tables"
        blocks = "\n\n".join(render_table(t) for t in tables.values())
        return (f"QUESTION:\n{query}\n\nCOMPANIES: {', '.join(c.name for c in companies)}\n"
                f"METRICS:\n{metrics}\nPERIODS: {periods}\n\nTABLES:\n{blocks}" + self._retry_notes(task))

    def _metric_line(self, name: str) -> str:
        """A requested line item with the names reports print it under (knowledge base)."""
        hit = self._kb.line_item_for(name)
        if hit is None:
            return f"- {name}"
        aliases = [a for a in dict.fromkeys([hit[1].name, *hit[1].aliases]) if a.casefold() != name.casefold()]
        return f"- {name} (printed as: {', '.join(aliases[:8])})"

    @staticmethod
    def _retry_notes(task: TaskMessage) -> str:
        notes = []
        if task.inputs.get("rework_instructions"):
            notes.append(f"A reviewer rejected the previous answer: {task.inputs['rework_instructions']}")
        if task.inputs.get("previous_error"):
            notes.append(f"Your previous reply was invalid: {str(task.inputs['previous_error'])[:500]}")
        return ("\n\nNOTES:\n" + "\n".join(notes)) if notes else ""

    # ---- search ----

    def _find_tables(self, ctx: AgentContext, plan: QuestionPlan, companies: list[CompanyView],
                     base: SearchFilters) -> dict[str, _Table]:
        lists: list[list[SearchHit]] = []
        ctx.prefetch([f"{m.search_query} {m.name}" for m in plan.metrics])  # one embedding request
        for c in companies:
            filters = SearchFilters(company_ids=(c.company_id,), document_ids=base.document_ids,
                                    fiscal_years=base.fiscal_years)
            for m in plan.metrics:
                hits = ctx.search(f"{m.search_query} {m.name}", top_k=SEARCH_DEPTH, filters=filters)
                lists.append([h for h in hits if h.kind == "table"][:TABLES_PER_QUERY])
        order: list[SearchHit] = []
        seen: set[uuid.UUID] = set()
        for rank in range(TABLES_PER_QUERY):  # round-robin: every line item gets its best tables
            for hits in lists:
                if rank < len(hits) and hits[rank].chunk_id not in seen:
                    seen.add(hits[rank].chunk_id)
                    order.append(hits[rank])
        order = order[:MAX_TABLES]
        chunks = ctx.chunks([h.chunk_id for h in order])
        tables: dict[str, _Table] = {}
        for h in order:
            chunk = chunks.get(h.chunk_id)
            if chunk is None or not isinstance(chunk.meta.get("table"), dict):
                continue
            tid = f"T{len(tables) + 1}"
            tables[tid] = _Table(tid, h, chunk, chunk.meta["table"])
        return tables

    # ---- results ----

    def _facts(self, task: TaskMessage, ctx: AgentContext, usage: Usage, query: str, plan: QuestionPlan,
               picks: CellPicks, tables: dict[str, _Table], companies: list[CompanyView],
               base: SearchFilters, by_code: set[str] | None = None) -> AgentResult:
        by_code = by_code or set()
        warnings: list[str] = []
        wanted_periods = {p for p in plan.periods if FY.match(p)}
        evidence: list[EvidenceProposal] = []
        findings: list[Finding] = []
        facts: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        for pick in picks.facts:
            m = CELL.match(pick.cell)
            t = tables.get(f"T{m.group(1)}") if m else None
            r, col = (int(m.group(2)), int(m.group(3))) if m else (-1, -1)
            if t is None or not 0 <= r < len(t.rows):
                warnings.append(f"{pick.metric}: cell {pick.cell} does not exist; ignored")
                continue
            row = t.rows[r]
            cell = next((c for c in row.get("cells", []) if c.get("col") == col), None)
            cv = read_cell(cell, t.table) if cell is not None else None
            if cv is None:
                warnings.append(f"{pick.metric}: cell {pick.cell} holds no number; ignored")
                continue
            if (t.tid, r, col) in seen:
                continue
            seen.add((t.tid, r, col))
            period, unit, currency, scale = cv.period, cv.unit, cv.currency, cv.scale
            if wanted_periods and period and period not in wanted_periods:
                warnings.append(f"{pick.metric}: {pick.cell} is for {period}, not asked for; ignored")
                continue
            if cv.note:
                warnings.append(f"{pick.metric}: {pick.cell} has {cv.note}")
            quote = self._quote(t, row, cell)
            if quote is None:
                warnings.append(f"{pick.metric}: {pick.cell} could not be quoted from its chunk; ignored")
                continue
            page = cv.page or t.chunk.page_start
            page = page if t.chunk.page_start <= page <= t.chunk.page_end else t.chunk.page_start
            ev = EvidenceProposal(chunk_id=t.chunk.chunk_id, page=page, section=t.chunk.section,
                                  quote=quote, bbox=cv.bbox)
            label = (f"{row['section']}: {row['label']}" if row.get("section") else row["label"])[:200]
            units = _units_text(unit, currency, scale)
            claim = (f"{t.hit.company_name} reports {label} of {cell['text']}"
                     f"{f' ({units})' if units else ''} for {period or 'an unstated period'}"
                     f" ({t.hit.filename}, p. {page}).")
            value = cv.value
            finding = Finding(category="reported_value", claim=claim, value=value, unit=unit,
                              currency=currency, scale=scale, period=period,
                              evidence=[EvidenceRef(evidence_id=ev.evidence_id)])
            evidence.append(ev)
            findings.append(finding)
            line_item = self._kb.line_item_for(label) or self._kb.line_item_for(pick.metric)
            facts.append({"fact_id": f"F{len(facts) + 1}", "metric": pick.metric, "label": label,
                          "line_item": line_item[0] if line_item else None,
                          "company": t.hit.company_name, "period": period, "value": str(value),
                          "printed": cell["text"], "unit": unit, "currency": currency, "scale": scale,
                          "evidence_id": str(ev.evidence_id), "finding_id": str(finding.finding_id),
                          "file": t.hit.filename, "page": page,
                          "picked_by": "code" if pick.cell in by_code else "model"})
        not_found = list(dict.fromkeys(picks.not_found))
        if not facts:
            return self._passages(task, ctx, usage, query, companies, base,
                                  note="no reported value could be read from the tables",
                                  not_found=not_found or [m.name for m in plan.metrics], warnings=warnings)
        return AgentResult(
            **_base(task, self.name), status="completed", evidence=evidence, findings=findings,
            next_action="submit_for_verification",
            outputs={"facts": facts, "not_found": not_found,
                     "evidence_ids": [str(e.evidence_id) for e in evidence],
                     "sources": [f"{f['company']} · {f['file']} p.{f['page']}" for f in facts],
                     "line_items_requested": [m.name for m in plan.metrics], "periods": plan.periods,
                     "kb_version": self._kb.version},
            warnings=warnings, metrics={**usage.metrics(), "tables": len(tables), "facts": len(facts),
                                        "cells_by_code": sum(f["picked_by"] == "code" for f in facts),
                                        "cells_by_model": sum(f["picked_by"] == "model" for f in facts),
                                        "cell_resolver": RESOLVER_VERSION})

    @staticmethod
    def _quote(t: _Table, row: dict[str, Any], cell: dict[str, Any]) -> str | None:
        """The row exactly as it is in the chunk text (rows are written by ``_row_line``)."""
        line = row_line(row)
        if line and line in t.chunk.text:
            return line[:2000]
        text = str(cell.get("text") or "")
        return text if text and text in t.chunk.text else None

    def _passages(self, task: TaskMessage, ctx: AgentContext, usage: Usage, query: str,
                  companies: list[CompanyView], base: SearchFilters, *, note: str,
                  not_found: list[str] | None = None, warnings: list[str] | None = None) -> AgentResult:
        filters = SearchFilters(company_ids=tuple(c.company_id for c in companies),
                                document_ids=base.document_ids, fiscal_years=base.fiscal_years)
        hits = [h for h in ctx.search(query, top_k=PASSAGES, filters=filters) if h.kind != "chart"]
        if not hits:
            return self._needs(task, usage, "no indexed passage in the companies' reports matches the question")
        evidence = [EvidenceProposal(chunk_id=h.chunk_id, page=h.page_start, section=h.section,
                                     quote=excerpt(h.text)) for h in hits]
        return AgentResult(
            **_base(task, self.name), status="completed", evidence=evidence,
            outputs={"evidence_ids": [str(e.evidence_id) for e in evidence],
                     "sources": [f"{h.company_name} · {h.filename} p.{h.page_start}" for h in hits],
                     "not_found": not_found or [], "facts": []},
            warnings=[*(warnings or []), note], metrics={**usage.metrics(), "passages": len(hits)})

    def _needs(self, task: TaskMessage, usage: Usage, what: str) -> AgentResult:
        return AgentResult(**_base(task, self.name), status="needs_more_information", needs=what,
                           metrics=usage.metrics())
