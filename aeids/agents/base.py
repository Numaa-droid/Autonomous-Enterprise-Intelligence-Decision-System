"""What an agent is, and the only services it may use (ARCHITECTURE.md §3).

An agent receives a ``TaskMessage`` and an ``AgentContext`` and returns an ``AgentResult``. It
cannot change task state (rule 4), call another agent (rule 3) or write to the database: the
context offers tenant-scoped reads only (search, evidence). Everything it proposes (findings,
evidence, verification decisions) is validated and stored by the Supervisor. Document text it
reads is untrusted data, never instructions (rule 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids.config import Settings
from aeids.contracts import AgentResult, TaskMessage
from aeids.db.models import DocumentChunk, DocumentVersion, EvidenceRecord
from aeids.llm.embedding import Embedder
from aeids.retrieval.search import SearchFilters, SearchHit, search
from aeids.retrieval.vector_index import VectorIndex


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

    def evidence(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, EvidenceView]:
        """Evidence records of this tenant (others are simply not found)."""
        if not ids:
            return {}
        rows = self._session.execute(
            select(EvidenceRecord, DocumentChunk.text, DocumentVersion.filename)
            .join(DocumentVersion, DocumentVersion.id == EvidenceRecord.version_id)
            .outerjoin(DocumentChunk, DocumentChunk.id == EvidenceRecord.chunk_id)
            .where(EvidenceRecord.id.in_(ids), EvidenceRecord.tenant_id == self.tenant_id)
        ).all()
        return {ev.id: EvidenceView(ev.id, ev.version_id, ev.chunk_id, ev.page, ev.section,
                                    ev.quote, text, filename)
                for ev, text, filename in rows}


class Registry:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = {a.name: a for a in agents}

    def get(self, name: str) -> Agent:
        try:
            return self._agents[name]
        except KeyError:
            raise KeyError(f"no agent registered as {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        return sorted(self._agents)
