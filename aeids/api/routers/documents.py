"""Document upload and versioning endpoints (ARCHITECTURE.md §5)."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile, status
from sqlalchemy.orm import Session

from aeids import documents as docs
from aeids.api.deps import SessionDep, SettingsDep, StoreDep, TenantDep
from aeids.api.processing import schedule_processing, vector_index
from aeids.api.schemas import DocumentOut, ErrorOut, UploadOut, VersionOut
from aeids.db.models import Document, DocumentType
from aeids.retrieval.vector_index import VectorIndexError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

ERROR_RESPONSES = {
    400: {"model": ErrorOut},
    404: {"model": ErrorOut},
    413: {"model": ErrorOut},
    415: {"model": ErrorOut},
}


def _document_out(session: Session, document: Document) -> DocumentOut:
    current = docs.current_version(session, document)
    return DocumentOut(
        id=document.id,
        company_id=document.company_id,
        company_name=document.company.name,
        filename=document.filename,
        document_type=document.document_type,
        fiscal_year=document.fiscal_year,
        status=document.status,
        created_at=document.created_at,
        current_version=VersionOut.model_validate(current) if current else None,
    )


def _upload_out(request: Request, session: Session, store, tenant_id: uuid.UUID,
                outcome: docs.UploadOutcome, response: Response) -> UploadOut:
    response.status_code = status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    processing, job_id = "not_needed", None
    if outcome.created:  # a new file: TableLens parses it (D-019)
        processing, job_id = schedule_processing(request.app.state, session, store, tenant_id,
                                                 outcome.version.id)
        session.refresh(outcome.version)
    return UploadOut(
        document=_document_out(session, outcome.document),
        version=VersionOut.model_validate(outcome.version),
        duplicate=not outcome.created,
        processing=processing,
        job_id=job_id,
    )


@router.post(
    "",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": UploadOut, "description": "Duplicate upload"}, **ERROR_RESPONSES},
)
def upload_document(
    request: Request,
    response: Response,
    session: SessionDep,
    store: StoreDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    file: Annotated[UploadFile, File(description="PDF file")],
    company_name: Annotated[str, Form(min_length=1, max_length=300)],
    document_type: Annotated[DocumentType, Form()] = DocumentType.ANNUAL_REPORT,
    fiscal_year: Annotated[int | None, Form(ge=1900, le=2100)] = None,
) -> UploadOut:
    """Upload a document and create its first version. Same bytes as any stored version in the
    tenant → 200 with that version (``duplicate: true``)."""
    staged = docs.stage_upload(
        file.file, file.filename, store.staging_dir(), settings.max_upload_bytes
    )
    docs.check_document_type(staged, document_type)  # an annual report is not an ESG report (D-035)
    outcome = docs.create_document(
        session,
        store,
        tenant_id,
        staged,
        company_name=company_name,
        document_type=document_type,
        fiscal_year=fiscal_year,
    )
    return _upload_out(request, session, store, tenant_id, outcome, response)


@router.post(
    "/{document_id}/versions",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": UploadOut, "description": "Duplicate upload"}, **ERROR_RESPONSES},
)
def upload_version(
    document_id: uuid.UUID,
    request: Request,
    response: Response,
    session: SessionDep,
    store: StoreDep,
    settings: SettingsDep,
    tenant_id: TenantDep,
    file: Annotated[UploadFile, File(description="Replacement PDF file")],
) -> UploadOut:
    """Upload a replacement version. Same bytes as an existing version of this document →
    200 with that version; the current pointer is unchanged."""
    document = docs.get_document(session, tenant_id, document_id)  # 404 before reading the upload
    staged = docs.stage_upload(
        file.file, file.filename, store.staging_dir(), settings.max_upload_bytes
    )
    docs.check_document_type(staged, document.document_type)  # a new version is the same kind of report
    outcome = docs.add_version(session, store, tenant_id, document_id, staged)
    return _upload_out(request, session, store, tenant_id, outcome, response)


@router.get("", response_model=list[DocumentOut])
def list_documents(session: SessionDep, tenant_id: TenantDep) -> list[DocumentOut]:
    return [_document_out(session, d) for d in docs.list_documents(session, tenant_id)]


@router.get("/{document_id}", response_model=DocumentOut, responses={404: {"model": ErrorOut}})
def get_document(document_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> DocumentOut:
    return _document_out(session, docs.get_document(session, tenant_id, document_id))


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT,
               responses={404: {"model": ErrorOut}})
def delete_document(document_id: uuid.UUID, request: Request, session: SessionDep,
                    tenant_id: TenantDep) -> Response:
    """Delete a report (D-038): archived in Postgres, so past findings keep their evidence; it
    leaves listings, search and questions, and the same PDF can be uploaded again. Its vector
    points are removed best effort (search re-checks Postgres, so leftovers are never shown)."""
    versions = docs.archive_document(session, tenant_id, document_id)
    indexed = [v for v in versions if v.index_collection]
    if indexed:
        try:
            index = vector_index(request.app.state)
            for v in indexed:
                index.delete_version(v.index_collection, tenant_id, v.id)
        except VectorIndexError as exc:
            log.warning("document %s deleted; its vector points remain: %s", document_id, exc.message)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{document_id}/versions",
    response_model=list[VersionOut],
    responses={404: {"model": ErrorOut}},
)
def list_versions(
    document_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep
) -> list[VersionOut]:
    """All versions of a document with their processing status, oldest first."""
    return [
        VersionOut.model_validate(v) for v in docs.list_versions(session, tenant_id, document_id)
    ]
