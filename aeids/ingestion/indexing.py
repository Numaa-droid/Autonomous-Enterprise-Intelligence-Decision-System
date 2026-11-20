"""Index a parsed version for search: embed its chunks, store them in Qdrant, verify, and only
then make it the document's current version (rule 6: keep old -> index new -> verify -> switch).

``parsed`` (or ``failed`` after a parse) -> ``indexing`` -> ``indexed`` | ``failed``, each
committed. A re-run replaces the version's points, never duplicates them.

The switch is ordered so search never loses the document: (1) the new version's points are
marked current in Qdrant, (2) Postgres switches the pointer (the source of truth: search
re-checks it), (3) the old versions' points are marked not current. A version that finishes
after a newer one does not take the pointer back.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.db.models import Document, DocumentChunk, DocumentVersion, ParserStatus, utcnow
from aeids.documents import DocumentError, DocumentNotFound, current_version, promote_to_current
from aeids.ingestion.pipeline import IllegalParserTransition, mark_failed, set_parser_status
from aeids.llm.embedding import Embedder
from aeids.llm.openrouter import LLMError
from aeids.retrieval.vector_index import ChunkPoint, VectorIndex, VectorIndexError, collection_name

log = logging.getLogger(__name__)


class IndexingError(DocumentError):
    code = "indexing_failed"
    http_status = 500


@dataclass(frozen=True)
class IndexResult:
    version: DocumentVersion
    collection: str
    point_count: int
    became_current: bool


def chunk_payload(chunk: DocumentChunk, version: DocumentVersion, document: Document) -> dict:
    """The Qdrant payload of one chunk (ARCHITECTURE.md §4.1)."""
    return {
        "tenant_id": str(version.tenant_id),
        "company_id": str(document.company_id),
        "document_id": str(document.id),
        "document_version_id": str(version.id),
        "document_type": document.document_type,
        "financial_year": document.fiscal_year,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "section": chunk.section,
        "kind": (chunk.meta or {}).get("kind"),
        "chunk_index": chunk.chunk_index,
        "is_current": False,  # switched on after the index is verified
    }


def start_indexing(session: Session, tenant_id: uuid.UUID, version_id: uuid.UUID) -> DocumentVersion:
    version = session.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.id == version_id, DocumentVersion.tenant_id == tenant_id)
        .with_for_update()
    )
    if version is None:
        raise DocumentNotFound("document version not found")
    if version.parser_status == ParserStatus.FAILED.value and version.parsed_at is None:
        raise IllegalParserTransition("the version has not been parsed; parse it first")
    set_parser_status(version, ParserStatus.INDEXING)
    version.parser_error = None
    session.commit()
    return version


def index_version(
    session: Session,
    index: VectorIndex,
    embedder: Embedder,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
) -> IndexResult:
    """Raises VectorIndexError / LLMError (``retryable`` says whether a retry may help) or
    DocumentError; the version is ``failed`` with the reason in those cases."""
    version = start_indexing(session, tenant_id, version_id)
    try:
        with tracing.span("index_version", "index", tenant_id=tenant_id,
                          inputs={"version_id": version_id, "file": version.filename}) as sp:
            result = _index(session, index, embedder, version)
            sp.set(collection=result.collection, points=result.point_count,
                   became_current=result.became_current)
            sp.set_output({"points": result.point_count, "collection": result.collection,
                           "became_current": result.became_current})
            return result
    except Exception as exc:
        if isinstance(exc, (DocumentError, LLMError, VectorIndexError)):
            message = exc.message
        else:
            log.exception("indexing of version %s failed", version_id)
            message = "internal error while indexing"
        mark_failed(session, version_id, f"indexing: {message}")
        raise


def _index(session: Session, index: VectorIndex, embedder: Embedder,
           version: DocumentVersion) -> IndexResult:
    chunks = list(session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.version_id == version.id, DocumentChunk.tenant_id == version.tenant_id)
        .order_by(DocumentChunk.chunk_index)
    ))
    if not chunks:
        raise IndexingError("the version has no chunks to index")
    document = version.document

    with tracing.span("embed_chunks", "chain", inputs={"chunks": len(chunks)},
                      model=embedder.model):
        vectors = embedder.embed_documents([c.text for c in chunks])
    if len(vectors) != len(chunks):
        raise IndexingError(f"{len(vectors)} vectors for {len(chunks)} chunks")
    dimension = len(vectors[0])
    name = collection_name(embedder.model, dimension)
    index.ensure_collection(name, dimension)

    points = [ChunkPoint(c.id, v, chunk_payload(c, version, document)) for c, v in zip(chunks, vectors)]
    with tracing.span("qdrant.replace_version", "vectordb", collection=name,
                      inputs={"points": len(points)}) as qs:
        stored = index.replace_version(name, version.tenant_id, version.id, points)
        qs.set_output({"stored": stored})
    if stored != len(chunks):  # verify before anything points at it
        raise IndexingError(f"the index holds {stored} points for this version, expected {len(chunks)}")

    # read the pointer and mark Qdrant before changing rows, so no write is pending meanwhile
    current = current_version(session, document)
    promote = current is None or current.id == version.id or (
        current.version_number < version.version_number)
    if promote:
        with tracing.span("qdrant.set_current", "vectordb", collection=name):
            index.set_current(name, version.tenant_id, document.id, version.id,
                              others=False)                                                   # (1)

    for c in chunks:
        c.qdrant_point_id = c.id
    version.indexed_at = utcnow()
    version.index_collection = name
    version.index_point_count = stored
    set_parser_status(version, ParserStatus.INDEXED)
    if promote:
        promote_to_current(session, version)                                              # (2)
    session.commit()
    if promote:
        try:
            index.set_current(name, version.tenant_id, document.id, version.id)            # (3)
        except VectorIndexError as exc:  # search re-checks Postgres, so old points are dropped
            log.warning("version %s is current, but old points are still flagged current: %s",
                        version.id, exc.message)
    return IndexResult(version, name, stored, promote)


def reindex_version(
    session: Session,
    index: VectorIndex,
    embedder: Embedder,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
) -> IndexResult:
    """Embed an ``indexed`` version again (e.g. after pinning the embedding provider, D-023) and
    overwrite its points in place. The status stays ``indexed`` and the payload keeps
    ``is_current``, so the document never drops out of search; the count is verified."""
    version = session.scalar(select(DocumentVersion).where(
        DocumentVersion.id == version_id, DocumentVersion.tenant_id == tenant_id))
    if version is None:
        raise DocumentNotFound("document version not found")
    if version.parser_status != ParserStatus.INDEXED.value:
        raise IllegalParserTransition("only an indexed version can be re-indexed in place")
    with tracing.span("reindex_version", "index", tenant_id=tenant_id,
                      inputs={"version_id": version_id, "file": version.filename}) as sp:
        chunks = list(session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.version_id == version.id, DocumentChunk.tenant_id == tenant_id)
            .order_by(DocumentChunk.chunk_index)))
        if not chunks:
            raise IndexingError("the version has no chunks to index")
        document = version.document
        with tracing.span("embed_chunks", "chain", inputs={"chunks": len(chunks)}, model=embedder.model):
            vectors = embedder.embed_documents([c.text for c in chunks])
        dimension = len(vectors[0])
        name = collection_name(embedder.model, dimension)
        index.ensure_collection(name, dimension)
        points = []
        for c, v in zip(chunks, vectors):
            payload = chunk_payload(c, version, document)
            payload["is_current"] = version.is_current
            points.append(ChunkPoint(c.id, v, payload))
        with tracing.span("qdrant.upsert", "vectordb", collection=name, inputs={"points": len(points)}):
            index.upsert(name, points)
        stored = index.count_version(name, tenant_id, version.id)
        if stored != len(chunks):
            raise IndexingError(f"the index holds {stored} points for this version, expected {len(chunks)}")
        version.indexed_at = utcnow()
        version.index_collection = name
        version.index_point_count = stored
        for c in chunks:
            c.qdrant_point_id = c.id
        session.commit()
        sp.set_output({"points": stored, "collection": name})
        return IndexResult(version, name, stored, version.is_current)
