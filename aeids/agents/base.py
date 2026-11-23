"""What an agent is, and the only services it may use (ARCHITECTURE.md §3).

An agent receives a ``TaskMessage`` and an ``AgentContext`` and returns an ``AgentResult``. It
cannot change task state (rule 4), call another agent (rule 3) or write to the database: the
context offers tenant-scoped reads only (search, evidence). Everything it proposes (findings,
evidence, verification decisions) is validated and stored by the Supervisor. Document text it
reads is untrusted data, never instructions (rule 7).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids.config import Settings
from aeids.contracts import AgentResult, TaskMessage
from aeids.db.models import (
    CalculationRecord,
    Company,
    Document,
    DocumentChunk,
    DocumentStatus,
    DocumentVersion,
    EvidenceRecord,
    FindingEvidence,
    FindingRecord,
)
from aeids.llm.embedding import Embedder
from aeids.retrieval.search import SearchFilters, SearchHit, search
from aeids.retrieval.vector_index import VectorIndex

log = logging.getLogger(__name__)


class Agent(Protocol):
    name: str
    version: str
    model: str | None
    prompt_version: str | None

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult: ...


@dataclass(frozen=True)
class EvidenceView:
    evidence_id: uuid.UUID
    version_id: uuid.UUID
    chunk_id: uuid.UUID | None
    page: int
    section: str | None
    quote: str
    chunk_text: str | None
    filename: str
    bbox: Any = None  # stored polygon (PDF points)
    chunk_meta: dict | None = None
    chunk_pages: tuple[int, int] | None = None
    version_is_current: bool = True
    document_active: bool = True


@dataclass(frozen=True)
class CalculationView:
    function: str
    formula: str
    inputs: dict[str, dict]
    output: str
    unit: str
    currency: str | None
    scale: str | None
    calc_version: str


@dataclass(frozen=True)
class FindingView:
    finding_id: uuid.UUID
    task_id: uuid.UUID
    category: str
    claim: str
    value: str | None
    unit: str | None
    currency: str | None
    scale: str | None
    period: str | None
    evidence_ids: tuple[uuid.UUID, ...]
    calculation: CalculationView | None


class AgentContext:
    """Tenant-scoped, read-only services for one task."""

    def __init__(self, session: Session, tenant_id: uuid.UUID, settings: Settings,
                 index: VectorIndex | None, embedder: Embedder | None) -> None:
        self._session = session
        self.tenant_id = tenant_id
        self.settings = settings
        self._index = index
        self._embedder = embedder

    def search(self, query: str, *, top_k: int = 5, filters: SearchFilters | None = None,
               mode: str = "hybrid") -> list[SearchHit]:
        return search(self._session, self._index, self._embedder, self.tenant_id, query,
                      mode=mode, top_k=top_k, filters=filters or SearchFilters()).hits

    def prefetch(self, queries: list[str]) -> None:
        """Embed the queries of several coming searches in one request (they then hit the cache).
        Best effort: a failure here is ignored, and each search embeds on its own as before."""
        embed_many = getattr(self._embedder, "embed_queries", None)
        if embed_many is None or not queries:
            return
        try:
            embed_many([" ".join(q.split()) for q in dict.fromkeys(queries)])
        except Exception as exc:  # noqa: BLE001 - search still works without the prefetch
            log.warning("query prefetch failed (%s); searches embed one by one", exc)

    def evidence(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, EvidenceView]:
        """Evidence records of this tenant (others are simply not found)."""
        if not ids:
            return {}
        rows = self._session.execute(
            select(EvidenceRecord, DocumentChunk, DocumentVersion, Document.status)
            .join(DocumentVersion, DocumentVersion.id == EvidenceRecord.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .outerjoin(DocumentChunk, DocumentChunk.id == EvidenceRecord.chunk_id)
            .where(EvidenceRecord.id.in_(ids), EvidenceRecord.tenant_id == self.tenant_id)
        ).all()
        return {ev.id: EvidenceView(
            ev.id, ev.version_id, ev.chunk_id, ev.page, ev.section, ev.quote,
            chunk.text if chunk else None, version.filename, bbox=ev.bbox,
            chunk_meta=dict(chunk.meta or {}) if chunk else None,
            chunk_pages=(chunk.page_start, chunk.page_end) if chunk else None,
            version_is_current=bool(version.is_current), document_active=doc_status == DocumentStatus.ACTIVE.value)
            for ev, chunk, version, doc_status in rows}

    def verified_findings(self, workflow_id: uuid.UUID) -> list[dict]:
        """A workflow's verified findings as the report holds them (for the report writer)."""
        from aeids.db.models import Workflow
        from aeids.reporting.answer import verified_findings

        wf = self._session.get(Workflow, workflow_id)
        if wf is None or wf.tenant_id != self.tenant_id:
            return []
        return verified_findings(self._session, workflow_id)

    def findings(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, FindingView]:
        """Findings of this tenant as stored: values, cited evidence and calculation."""
        if not ids:
            return {}
        return self._finding_views(select(FindingRecord).where(
            FindingRecord.id.in_(ids), FindingRecord.tenant_id == self.tenant_id))

    def task_findings(self, task_id: uuid.UUID) -> dict[uuid.UUID, FindingView]:
        """All findings one task produced (e.g. the steps of a multi-step metric)."""
        return self._finding_views(select(FindingRecord).where(
            FindingRecord.task_id == task_id, FindingRecord.tenant_id == self.tenant_id))

    def _finding_views(self, stmt) -> dict[uuid.UUID, FindingView]:
        records = list(self._session.scalars(stmt))
        if not records:
            return {}
        fids = [f.id for f in records]
        cited: dict[uuid.UUID, list[uuid.UUID]] = {}
        for fid, eid in self._session.execute(select(FindingEvidence.finding_id, FindingEvidence.evidence_record_id)
                                              .where(FindingEvidence.finding_id.in_(fids))):
            cited.setdefault(fid, []).append(eid)
        calcs = {c.finding_id: c for c in self._session.scalars(
            select(CalculationRecord).where(CalculationRecord.finding_id.in_(fids)))}
        out = {}
        for f in records:
            c = calcs.get(f.id)
            out[f.id] = FindingView(
                f.id, f.task_id, f.category, f.claim, f.value, f.unit, f.currency, f.scale, f.period,
                tuple(cited.get(f.id, [])),
                None if c is None else CalculationView(c.function, c.formula, dict(c.inputs), c.output, c.unit,
                                                       c.currency, c.scale, c.calc_version))
        return out

    def companies(self) -> list[CompanyView]:
        """Companies of this tenant with at least one current (searchable) report version."""
        rows = self._session.execute(
            select(Company.id, Company.name)
            .join(Document, Document.company_id == Company.id)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(Company.tenant_id == self.tenant_id, DocumentVersion.is_current.is_(True))
            .distinct().order_by(Company.name)).all()
        return [CompanyView(cid, name) for cid, name in rows]

    def chunks(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, ChunkView]:
        """Chunks of this tenant with their metadata (normalised table rows for tables)."""
        if not ids:
            return {}
        rows = self._session.execute(
            select(DocumentChunk).where(DocumentChunk.id.in_(ids),
                                        DocumentChunk.tenant_id == self.tenant_id)).scalars()
        return {c.id: ChunkView(c.id, c.version_id, c.page_start, c.page_end, c.section, c.text,
                                dict(c.meta or {})) for c in rows}


@dataclass(frozen=True)
class CompanyView:
    company_id: uuid.UUID
    name: str


@dataclass(frozen=True)
class ChunkView:
    chunk_id: uuid.UUID
    version_id: uuid.UUID
    page_start: int
    page_end: int
    section: str | None
    text: str
    meta: dict


class Registry:
    def __init__(self, agents: list[Agent], closers: list | None = None) -> None:
        self._agents = {a.name: a for a in agents}
        self._closers = list(closers or [])

    def close(self) -> None:
        """Release what the agents hold (e.g. the LLM client's connections)."""
        for close in self._closers:
            close()

    def get(self, name: str) -> Agent:
        try:
            return self._agents[name]
        except KeyError:
            raise KeyError(f"no agent registered as {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        return sorted(self._agents)
