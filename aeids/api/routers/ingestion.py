"""Attach TableLens output to a document version, and inspect the resulting chunks."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, UploadFile
from sqlalchemy import select

from aeids import documents as docs
from aeids.api.deps import SessionDep, SettingsDep, StoreDep, TenantDep
from aeids.api.processing import schedule_processing
from aeids.api.schemas import ChunkOut, ErrorOut, IngestOut, ProcessOut, VersionOut
from aeids.db.models import DocumentChunk, DocumentVersion, ParserStatus
from aeids.ingestion.pipeline import PARSER_TRANSITIONS, IllegalParserTransition, ingest_structure

router = APIRouter(prefix="/api/v1/documents", tags=["ingestion"])


def _read_limited(upload: UploadFile, limit: int) -> bytes:
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise docs.UploadTooLarge(f"structure file exceeds the {limit}-byte limit")
    if not data:
        raise docs.InvalidUpload("structure file is empty")
    return data


@router.post(
    "/{document_id}/versions/{version_id}/structure",
    response_model=IngestOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut},
               413: {"model": ErrorOut}},
)
def attach_structure(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    store: StoreDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    file: Annotated[UploadFile, File(description="TableLens <name>_structure.json")],
) -> IngestOut:
    """Parse a version from a TableLens ``_structure.json`` you already have: normalise tables,
    build chunks (in the request). Indexing for search then starts like after an upload."""
    raw = _read_limited(file, settings.max_structure_bytes)
    result = ingest_structure(session, store, tenant_id, document_id, version_id, raw)
    processing, job_id = schedule_processing(request.app.state, session, store, tenant_id,
                                             version_id)
    session.refresh(result.version)
    return IngestOut(
        version=VersionOut.model_validate(result.version),
        processing=processing,
        job_id=job_id,
        chunk_count=result.chunk_count,
        table_count=result.table_count,
        financial_table_count=result.financial_table_count,
        warnings=result.warnings,
    )


@router.post(
    "/{document_id}/versions/{version_id}/process",
    response_model=ProcessOut,
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}},
)
def process_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    store: StoreDep,
    tenant_id: TenantDep,
    reuse_by_filename: Annotated[bool, Query(
        description="Use a TableLens result made earlier for a file of the same name "
                    "(page count is checked)")] = False,
    reparse: Annotated[bool, Query(
        description="Parse again even if the version was parsed (not for indexed versions)")] = False,
) -> ProcessOut:
    """Process a version (again): parse it if it was never parsed (or ``reparse``), then index
    it for search. For a failure, or a version uploaded while processing was off or the queue
    was down."""
    version = _version(session, tenant_id, document_id, version_id)
    status = ParserStatus(version.parser_status)
    if status in (ParserStatus.PARSING, ParserStatus.INDEXING):
        raise IllegalParserTransition(f"the version is {status.value} already")
    if status == ParserStatus.INDEXED:
        raise IllegalParserTransition("the version is indexed already")
    if reparse and ParserStatus.PARSING not in PARSER_TRANSITIONS[status]:
        raise IllegalParserTransition(f"a version in status {status.value} cannot be parsed again")
    processing, job_id = schedule_processing(request.app.state, session, store, tenant_id,
                                             version.id, reuse_by_filename=reuse_by_filename,
                                             reparse=reparse)
    session.refresh(version)
    return ProcessOut(version=VersionOut.model_validate(version), processing=processing,
                      job_id=job_id)


def _version(session, tenant_id: uuid.UUID, document_id: uuid.UUID,
             version_id: uuid.UUID) -> DocumentVersion:
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.tenant_id == tenant_id,
        )
    )
    if version is None:
        raise docs.DocumentNotFound("document version not found")
    return version


@router.get(
    "/{document_id}/versions/{version_id}/chunks",
    response_model=list[ChunkOut],
    responses={404: {"model": ErrorOut}},
)
def list_chunks(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    tenant_id: TenantDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ChunkOut]:
    version = _version(session, tenant_id, document_id, version_id)
    chunks = session.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.version_id == version.id, DocumentChunk.tenant_id == tenant_id)
        .order_by(DocumentChunk.chunk_index)
        .limit(limit)
        .offset(offset)
    )
    return [
        ChunkOut(id=c.id, chunk_index=c.chunk_index, page_start=c.page_start, page_end=c.page_end,
                 section=c.section, kind=c.meta.get("kind", "text"), text=c.text)
        for c in chunks
    ]
