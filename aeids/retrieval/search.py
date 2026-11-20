"""Search over indexed chunks: dense (Qdrant), keyword (Postgres full-text), hybrid (RRF).

Every search is scoped to one tenant (rule 10) and, by default, to current versions (rule 6).
Results always come from Postgres: candidate ids from Qdrant and the keyword search are
re-checked against the tenant, the version's ``is_current`` and ``indexed`` status, the
document's status and the filters, so a stale Qdrant payload can never leak a superseded or
foreign chunk. Only ``indexed`` versions are searchable (both lists then cover the same chunks).

Keyword search is Postgres full-text search on section + text (``english`` configuration):
the question's words are OR-ed, so a long question still matches, and ``ts_rank`` orders by how
many of them a chunk has (D-007). SQLite (tests) has no full-text search; there a small BM25
scorer in Python stands in.

Hybrid: reciprocal rank fusion, score = sum over the two lists of 1 / (RRF_K + rank). If dense
search is unavailable (Qdrant or OpenRouter down), hybrid falls back to keyword with a warning.

Chunk text is untrusted document content (rule 7): it is returned as data, never interpreted.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import Select, func, literal_column, select, text
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.db.models import (
    Company,
    Document,
    DocumentChunk,
    DocumentStatus,
    DocumentVersion,
    ParserStatus,
)
from aeids.documents import DocumentError
from aeids.llm.embedding import Embedder
from aeids.llm.openrouter import LLMError
from aeids.retrieval.vector_index import VectorIndex, VectorIndexError, collection_name

Mode = Literal["hybrid", "dense", "keyword"]
RRF_K = 60
CANDIDATES = 50  # per list, before fusion and the Postgres re-check


class SearchUnavailable(DocumentError):
    code = "search_unavailable"
    http_status = 503


class InvalidQuery(DocumentError):
    code = "invalid_query"
    http_status = 400


@dataclass(frozen=True)
class SearchFilters:
    company_ids: tuple[uuid.UUID, ...] = ()
    document_ids: tuple[uuid.UUID, ...] = ()
    fiscal_years: tuple[int, ...] = ()
    include_superseded: bool = False  # default: current versions only


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    is_current: bool
    company_id: uuid.UUID
    company_name: str
    filename: str
    document_type: str
    fiscal_year: int | None
    page_start: int
    page_end: int
    section: str | None
    kind: str
    text: str
    score: float  # the fused (hybrid) or the list's own score
    dense_rank: int | None = None  # 1-based rank in each list, None when not in it
    keyword_rank: int | None = None


@dataclass
class SearchResult:
    query: str
    mode: Mode  # the mode actually used (hybrid may fall back to keyword)
    hits: list[SearchHit]
    warnings: list[str] = field(default_factory=list)


# --- candidate lists ----------------------------------------------------------

def _scoped(stmt: Select, tenant_id: uuid.UUID, filters: SearchFilters) -> Select:
    """Restrict a select over DocumentChunk to searchable chunks of one tenant."""
    stmt = (stmt.join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentChunk.tenant_id == tenant_id,
                   DocumentVersion.tenant_id == tenant_id,
                   Document.tenant_id == tenant_id,
                   Document.status == DocumentStatus.ACTIVE.value,
                   DocumentVersion.parser_status == ParserStatus.INDEXED.value))
    if not filters.include_superseded:
        stmt = stmt.where(DocumentVersion.is_current.is_(True))
    if filters.company_ids:
        stmt = stmt.where(Document.company_id.in_(filters.company_ids))
    if filters.document_ids:
        stmt = stmt.where(Document.id.in_(filters.document_ids))
    if filters.fiscal_years:
        stmt = stmt.where(Document.fiscal_year.in_(filters.fiscal_years))
    return stmt


def _tsquery(session: Session, query: str) -> str | None:
    """The question's lexemes (english stemming, stop words dropped), OR-ed, quoted for
    to_tsquery('simple', ...): no user text reaches the tsquery parser unquoted."""
    lexemes = session.scalar(text("SELECT tsvector_to_array(to_tsvector('english', :q))"),
                             {"q": query}) or []
    quoted = ["'" + lx.replace("\\", "").replace("'", "''") + "'" for lx in lexemes if lx]
    return " | ".join(quoted) or None


def keyword_candidates(session: Session, tenant_id: uuid.UUID, query: str,
                       filters: SearchFilters, limit: int = CANDIDATES) -> list[tuple[uuid.UUID, float]]:
    if session.get_bind().dialect.name != "postgresql":
        return _bm25_candidates(session, tenant_id, query, filters, limit)
    tsq = _tsquery(session, query)
    if tsq is None:
        return []
    q = func.to_tsquery("simple", tsq)
    vector = literal_column("document_chunks.search_vector")
    rank = func.ts_rank(vector, q, 1).label("rank")  # 1: divided by 1 + log(length)
    stmt = _scoped(select(DocumentChunk.id, rank), tenant_id, filters).where(vector.op("@@")(q))
    rows = session.execute(stmt.order_by(rank.desc(), DocumentChunk.id).limit(limit)).all()
    return [(r.id, float(r.rank)) for r in rows]


def dense_candidates(index: VectorIndex, embedder: Embedder, tenant_id: uuid.UUID, query: str,
                     filters: SearchFilters, limit: int = CANDIDATES) -> list[tuple[uuid.UUID, float]]:
    vector = embedder.embed_query(query)
    name = collection_name(embedder.model, len(vector))
    hits = index.search(name, tenant_id, vector, limit=limit,
                        current_only=not filters.include_superseded,
                        company_ids=filters.company_ids, document_ids=filters.document_ids,
                        fiscal_years=filters.fiscal_years)
    return [(h.chunk_id, h.score) for h in hits]


def rrf(*lists: Sequence[tuple[uuid.UUID, float]], k: int = RRF_K) -> list[tuple[uuid.UUID, float]]:
    """Reciprocal rank fusion of ranked (id, score) lists -> (id, fused score), best first."""
    fused: dict[uuid.UUID, float] = {}
    for ranked in lists:
        for rank, (chunk_id, _score) in enumerate(ranked, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: (-kv[1], str(kv[0])))


# --- search -------------------------------------------------------------------

def search(
    session: Session,
    index: VectorIndex | None,
    embedder: Embedder | None,
    tenant_id: uuid.UUID,
    query: str,
    *,
    mode: Mode = "hybrid",
    top_k: int = 10,
    filters: SearchFilters = SearchFilters(),
) -> SearchResult:
    query = " ".join(query.split())
    if not query:
        raise InvalidQuery("the query is empty")
    with tracing.span("search", "retrieval", tenant_id=tenant_id, mode=mode, top_k=top_k,
                      inputs={"query": query, "mode": mode, "top_k": top_k,
                              "filters": filters}) as sp:
        result = _search(session, index, embedder, tenant_id, query, mode, top_k, filters)
        sp.set(mode_used=result.mode, hits=len(result.hits))
        sp.set_output({"mode": result.mode, "warnings": result.warnings, "hits": [
            {"rank": i, "file": h.filename, "pages": f"{h.page_start}-{h.page_end}",
             "section": h.section, "kind": h.kind, "score": round(h.score, 5),
             "dense_rank": h.dense_rank, "keyword_rank": h.keyword_rank}
            for i, h in enumerate(result.hits, start=1)]})
        return result


def _search(session: Session, index: VectorIndex | None, embedder: Embedder | None,
            tenant_id: uuid.UUID, query: str, mode: Mode, top_k: int,
            filters: SearchFilters) -> SearchResult:
    warnings: list[str] = []
    dense: list[tuple[uuid.UUID, float]] = []
    keyword: list[tuple[uuid.UUID, float]] = []

    if mode in ("hybrid", "dense"):
        try:
            if index is None or embedder is None:
                raise SearchUnavailable("dense search is not configured")
            with tracing.span("dense_search", "vectordb", model=embedder.model) as ds:
                dense = dense_candidates(index, embedder, tenant_id, query, filters)
                ds.set_output({"candidates": len(dense),
                               "best_score": round(dense[0][1], 5) if dense else None})
        except (VectorIndexError, LLMError, SearchUnavailable) as exc:
            if mode == "dense":
                raise SearchUnavailable(f"dense search unavailable: {exc.message}") from None
            warnings.append(f"dense search unavailable, keyword results only: {exc.message}")
            mode = "keyword"
    if mode in ("hybrid", "keyword"):
        engine = ("postgres full-text" if session.get_bind().dialect.name == "postgresql"
                  else "bm25 (sqlite)")
        with tracing.span("keyword_search", "retrieval", engine=engine) as ks:
            keyword = keyword_candidates(session, tenant_id, query, filters)
            ks.set_output({"candidates": len(keyword)})

    if mode == "hybrid":
        with tracing.span("rrf_fuse", "chain", k=RRF_K,
                          inputs={"dense": len(dense), "keyword": len(keyword)}) as fs:
            ranked = rrf(dense, keyword)
            fs.set_output({"fused": len(ranked),
                           "in_both": len(set(c for c, _ in dense) & set(c for c, _ in keyword))})
    else:
        ranked = dense if mode == "dense" else keyword
    dense_rank = {cid: i for i, (cid, _) in enumerate(dense, start=1)}
    keyword_rank = {cid: i for i, (cid, _) in enumerate(keyword, start=1)}

    rows = _load(session, tenant_id, filters, [cid for cid, _ in ranked])
    hits: list[SearchHit] = []
    for cid, score in ranked:
        row = rows.get(cid)
        if row is None:  # failed the Postgres re-check (superseded, other tenant, filtered)
            continue
        chunk, version, document, company = row
        hits.append(SearchHit(
            chunk_id=chunk.id, document_id=document.id, version_id=version.id,
            version_number=version.version_number, is_current=version.is_current,
            company_id=company.id, company_name=company.name, filename=version.filename,
            document_type=document.document_type, fiscal_year=document.fiscal_year,
            page_start=chunk.page_start, page_end=chunk.page_end, section=chunk.section,
            kind=(chunk.meta or {}).get("kind", "text"), text=chunk.text, score=score,
            dense_rank=dense_rank.get(cid), keyword_rank=keyword_rank.get(cid),
        ))
        if len(hits) == top_k:
            break
    return SearchResult(query=query, mode=mode, hits=hits, warnings=warnings)


def _load(session: Session, tenant_id: uuid.UUID, filters: SearchFilters,
          ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple]:
    if not ids:
        return {}
    stmt = _scoped(select(DocumentChunk, DocumentVersion, Document, Company), tenant_id, filters)
    stmt = stmt.join(Company, Company.id == Document.company_id).where(DocumentChunk.id.in_(ids))
    return {row[0].id: tuple(row) for row in session.execute(stmt)}


# --- SQLite stand-in for full-text search (tests only) -----------------------

_STOP = frozenset("""a an and are as at be by did do does for from had has have how in is it its
of on or than that the their this to was were what when where which who why will with""".split())


def _terms(value: str) -> list[str]:
    out = []
    for word in re.findall(r"[a-z0-9]+", value.casefold()):
        if word in _STOP:
            continue
        for suffix in ("ing", "es", "ed", "s"):
            if len(word) > len(suffix) + 2 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        out.append(word)
    return out


def _bm25_candidates(session: Session, tenant_id: uuid.UUID, query: str, filters: SearchFilters,
                     limit: int, k1: float = 1.2, b: float = 0.75) -> list[tuple[uuid.UUID, float]]:
    q = set(_terms(query))
    if not q:
        return []
    stmt = _scoped(select(DocumentChunk.id, DocumentChunk.section, DocumentChunk.text),
                   tenant_id, filters)
    docs = [(cid, Counter(_terms(f"{section or ''} {body}"))) for cid, section, body in
            session.execute(stmt)]
    if not docs:
        return []
    avg = sum(sum(c.values()) for _, c in docs) / len(docs)
    df = Counter(t for _, c in docs for t in q if t in c)
    scored = []
    for cid, counts in docs:
        length = sum(counts.values())
        s = 0.0
        for t in q:
            tf = counts.get(t, 0)
            if tf:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / (avg or 1)))
        if s > 0:
            scored.append((cid, s))
    scored.sort(key=lambda x: (-x[1], str(x[0])))
    return scored[:limit]
