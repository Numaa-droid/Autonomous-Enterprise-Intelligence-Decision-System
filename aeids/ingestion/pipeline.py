"""Attach TableLens output to a document version: validate, normalise, chunk, persist.

``parser_status`` moves only along ``PARSER_TRANSITIONS``; every change is committed before the
next step, so a crash leaves a visible state (``parsing``) and a re-run is safe: the version's
chunks are replaced, never duplicated. Embedding and Qdrant indexing (``parsed -> indexing ->
indexed``) are in ``indexing.py``.
"""

from __future__ import annotations

import hashlib
import re
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aeids.db.models import (
    DocumentChunk,
    DocumentVersion,
    EvidenceRecord,
    ParserStatus,
    utcnow,
)
from aeids.documents import DocumentError, DocumentNotFound
from aeids.ingestion.chunking import ChunkResult, build_chunks
from aeids.ingestion.structure import InvalidStructure, parse_structure
from aeids.storage import ObjectStore

log = logging.getLogger(__name__)

PARSER_TRANSITIONS: dict[ParserStatus, frozenset[ParserStatus]] = {
    ParserStatus.PENDING: frozenset({ParserStatus.PARSING}),
    ParserStatus.PARSING: frozenset({ParserStatus.PARSED, ParserStatus.FAILED}),
    ParserStatus.PARSED: frozenset({ParserStatus.PARSING, ParserStatus.INDEXING}),
    ParserStatus.INDEXING: frozenset({ParserStatus.INDEXED, ParserStatus.FAILED}),
    # Re-parsing an indexed version needs its vectors removed first (with indexing).
    ParserStatus.INDEXED: frozenset(),
    # failed: parse again, or (when it was parsed and indexing failed) index again
    ParserStatus.FAILED: frozenset({ParserStatus.PARSING, ParserStatus.INDEXING}),
}


class IllegalParserTransition(DocumentError):
    code = "illegal_status_transition"
    http_status = 409


class StructureMismatch(InvalidStructure):
    code = "structure_mismatch"


def set_parser_status(version: DocumentVersion, to: ParserStatus) -> None:
    current = ParserStatus(version.parser_status)
    if to not in PARSER_TRANSITIONS[current]:
        raise IllegalParserTransition(
            f"parser_status cannot go from {current.value} to {to.value}"
        )
    version.parser_status = to.value


@dataclass(frozen=True)
class IngestResult:
    version: DocumentVersion
    chunk_count: int
    table_count: int
    financial_table_count: int
    warnings: list[str]


def pdf_page_count(path: Path) -> int | None:
    try:
        return len(PdfReader(path).pages)
    except Exception:  # noqa: BLE001 — unreadable/encrypted: the page check is skipped, with a warning
        return None


def _stem(name: str | None) -> str:
    """A file name reduced for comparison: no extension, no ``-<sha12>`` suffix that AEIDS adds
    for TableLens (``tablelens_stem``), and TableLens' character clean-up (spaces etc. -> _)."""
    stem = Path(name or "").stem.casefold()
    stem = re.sub(r"-[0-9a-f]{12}$", "", stem)
    return re.sub(r"[^a-z0-9]+", "_", stem).strip("_")


def start_parsing(
    session: Session,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
) -> DocumentVersion:
    """Move a version to ``parsing`` (committed), so its status shows while TableLens works."""
    stmt = select(DocumentVersion).where(
        DocumentVersion.id == version_id, DocumentVersion.tenant_id == tenant_id
    )
    if document_id is not None:
        stmt = stmt.where(DocumentVersion.document_id == document_id)
    version = session.scalar(stmt.with_for_update())
    if version is None:
        raise DocumentNotFound("document version not found")
    has_evidence = session.scalar(
        select(func.count()).select_from(EvidenceRecord).where(EvidenceRecord.version_id == version.id)
    )
    if has_evidence:
        raise IllegalParserTransition("version already has evidence records; it cannot be re-parsed")
    set_parser_status(version, ParserStatus.PARSING)
    version.parser_error = None
    session.commit()
    return version


def mark_failed(session: Session, version_id: uuid.UUID, message: str) -> None:
    """``parsing``/``indexing`` -> ``failed`` with a reason (committed); other states untouched."""
    session.rollback()
    version = session.get(DocumentVersion, version_id)
    if version is not None and version.parser_status in (ParserStatus.PARSING.value,
                                                          ParserStatus.INDEXING.value):
        set_parser_status(version, ParserStatus.FAILED)
        version.parser_error = message[:2000]
        session.commit()


def ingest_structure(
    session: Session,
    store: ObjectStore,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    raw: bytes,
) -> IngestResult:
    """Attach a ``_structure.json`` the caller already has (API upload / CLI)."""
    version = start_parsing(session, tenant_id, version_id, document_id)
    return finish_parsing(session, store, version, raw)


def finish_parsing(
    session: Session,
    store: ObjectStore,
    version: DocumentVersion,
    raw: bytes,
    extra_warnings: list[str] | None = None,
) -> IngestResult:
    """Validate, normalise and chunk ``raw`` for a version in ``parsing``; ``parsed`` or
    ``failed`` (committed) afterwards."""
    tenant_id, version_id = version.tenant_id, version.id
    try:
        result, warnings = _parse(store, version, raw)
        warnings = (extra_warnings or []) + warnings
        checksum = hashlib.sha256(raw).hexdigest()
        staged = store.staging_dir() / f"{uuid.uuid4().hex}.structure"
        staged.write_bytes(raw)
        version.structure_uri = store.put_file(
            staged, store.key_for(tenant_id, checksum, suffix=".json")
        )
        version.structure_checksum = checksum

        session.execute(delete(DocumentChunk).where(DocumentChunk.version_id == version.id))
        for i, c in enumerate(result.chunks):
            session.add(DocumentChunk(
                tenant_id=tenant_id, version_id=version.id, chunk_index=i,
                page_start=c.page_start, page_end=c.page_end, section=c.section,
                text=c.text, meta=c.meta,
            ))
        version.parsed_at = utcnow()
        version.parse_warnings = warnings
        set_parser_status(version, ParserStatus.PARSED)
        session.commit()
    except Exception as exc:
        message = exc.message if isinstance(exc, DocumentError) else "internal error while parsing"
        if not isinstance(exc, DocumentError):
            log.exception("ingestion of version %s failed", version_id)
        mark_failed(session, version_id, message)
        raise

    return IngestResult(
        version=version,
        chunk_count=len(result.chunks),
        table_count=len(result.tables),
        financial_table_count=sum(t.is_financial for t in result.tables),
        warnings=warnings,
    )


def _parse(
    store: ObjectStore, version: DocumentVersion, raw: bytes
) -> tuple[ChunkResult, list[str]]:
    structure = parse_structure(raw)
    warnings: list[str] = []
    pages = pdf_page_count(store.path_for(version.storage_uri))
    if pages is None:
        warnings.append("could not read the PDF's page count; page numbers were not checked")
    elif structure.max_page() > pages:
        raise StructureMismatch(
            f"the structure refers to page {structure.max_page()} but the PDF has {pages} pages: "
            "it belongs to another file"
        )
    if structure.file and _stem(structure.file) != _stem(version.filename):
        warnings.append(
            f"structure was made from {structure.file!r}, the version's file is "
            f"{version.filename!r}"
        )
    result = build_chunks(structure)
    if not result.chunks:
        raise InvalidStructure("the structure has no text or tables to index")
    return result, warnings + result.warnings
