"""Upload validation, checksum dedupe and document versioning.

Versioning rule (CLAUDE.md rule 6): keep the old version, index the new one, verify indexing,
then switch the current pointer. An upload is stored as not current; ``promote_to_current`` is
called by indexing (``aeids/ingestion/indexing.py``) once the version's index is verified.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aeids.db.models import (
    Company,
    Document,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    ParserStatus,
    Tenant,
)
from aeids.storage import ObjectStore

PDF_MAGIC = b"%PDF-"
CHUNK_SIZE = 1024 * 1024
MAX_FILENAME_LENGTH = 255


class DocumentError(Exception):
    """Base for errors the API maps to HTTP responses."""

    code = "document_error"
    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidUpload(DocumentError):
    code = "invalid_upload"
    http_status = 400


class UnsupportedFileType(DocumentError):
    code = "unsupported_file_type"
    http_status = 415


class UploadTooLarge(DocumentError):
    code = "upload_too_large"
    http_status = 413


class DocumentNotFound(DocumentError):
    code = "document_not_found"
    http_status = 404


@dataclass(frozen=True)
class StagedFile:
    path: Path
    sha256: str
    size_bytes: int
    filename: str

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class UploadOutcome:
    document: Document
    version: DocumentVersion
    created: bool  # False when the checksum matched an existing version


def sanitize_filename(name: str | None) -> str:
    """Display name only (never used as a path): basename, no control chars, bounded length."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip()
    base = re.sub(r"\s+", " ", base)
    if base in {"", ".", ".."}:
        base = "document.pdf"
    if len(base) > MAX_FILENAME_LENGTH:
        stem, dot, ext = base.rpartition(".")
        ext = ext if dot and len(ext) <= 10 else ""
        keep = MAX_FILENAME_LENGTH - (len(ext) + 1 if ext else 0)
        base = (stem if dot else base)[:keep] + (f".{ext}" if ext else "")
    return base


def stage_upload(
    stream: BinaryIO, filename: str | None, staging_dir: Path, max_bytes: int
) -> StagedFile:
    """Copy an upload to a staging file while hashing it; check it is a non-empty PDF."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=staging_dir, suffix=".upload")
    tmp_path = Path(tmp_name)
    digest = hashlib.sha256()
    size = 0
    header = b""
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(f"file exceeds the {max_bytes}-byte upload limit")
                if len(header) < len(PDF_MAGIC):
                    header += chunk[: len(PDF_MAGIC) - len(header)]
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise InvalidUpload("file is empty")
        if header != PDF_MAGIC:
            raise UnsupportedFileType("only PDF files are accepted")
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return StagedFile(tmp_path, digest.hexdigest(), size, sanitize_filename(filename))


def normalize_company_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", unicodedata.normalize("NFC", name)).strip()
    if not cleaned:
        raise InvalidUpload("company_name must not be blank")
    return cleaned


def get_or_create_tenant(session: Session, name: str) -> Tenant:
    tenant = session.scalar(select(Tenant).where(Tenant.name == name))
    if tenant is not None:
        return tenant
    try:
        with session.begin_nested():
            tenant = Tenant(name=name)
            session.add(tenant)
    except IntegrityError:  # created concurrently
        tenant = session.scalars(select(Tenant).where(Tenant.name == name)).one()
    return tenant


def get_or_create_company(session: Session, tenant_id: uuid.UUID, name: str) -> Company:
    stmt = select(Company).where(Company.tenant_id == tenant_id, Company.name == name)
    company = session.scalar(stmt)
    if company is not None:
        return company
    try:
        with session.begin_nested():
            company = Company(tenant_id=tenant_id, name=name)
            session.add(company)
    except IntegrityError:  # created concurrently
        company = session.scalars(stmt).one()
    return company


def get_document(session: Session, tenant_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    doc = session.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    )
    if doc is None:
        raise DocumentNotFound("document not found")
    return doc


def list_documents(session: Session, tenant_id: uuid.UUID) -> list[Document]:
    return list(
        session.scalars(
            select(Document).where(Document.tenant_id == tenant_id).order_by(Document.created_at)
        )
    )


def list_versions(
    session: Session, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> list[DocumentVersion]:
    get_document(session, tenant_id, document_id)
    return list(
        session.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.document_id == document_id,
            )
            .order_by(DocumentVersion.version_number)
        )
    )


def current_version(session: Session, document: Document) -> DocumentVersion | None:
    return session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.tenant_id == document.tenant_id,
            DocumentVersion.is_current.is_(True),
        )
    )


def create_document(
    session: Session,
    store: ObjectStore,
    tenant_id: uuid.UUID,
    staged: StagedFile,
    *,
    company_name: str,
    document_type: DocumentType,
    fiscal_year: int | None,
) -> UploadOutcome:
    """Create a document and its first version, or return the existing version with the same
    checksum anywhere in the tenant (duplicate upload)."""
    try:
        existing = session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.tenant_id == tenant_id, DocumentVersion.checksum == staged.sha256)
            .order_by(DocumentVersion.uploaded_at)
            .limit(1)
        )
        if existing is not None:
            staged.discard()
            return UploadOutcome(existing.document, existing, created=False)

        company = get_or_create_company(session, tenant_id, normalize_company_name(company_name))
        document = Document(
            tenant_id=tenant_id,
            company_id=company.id,
            filename=staged.filename,
            document_type=document_type.value,
            fiscal_year=fiscal_year,
            status=DocumentStatus.ACTIVE.value,
        )
        session.add(document)
        session.flush()
        version = _store_version(session, store, tenant_id, document, staged, version_number=1)
        session.commit()
        return UploadOutcome(document, version, created=True)
    except BaseException:
        session.rollback()
        staged.discard()
        raise


def add_version(
    session: Session,
    store: ObjectStore,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    staged: StagedFile,
) -> UploadOutcome:
    """Add a replacement version. Same bytes as an existing version → that version is returned
    and the current pointer is left unchanged."""
    try:
        # Lock the document row (Postgres) so concurrent uploads get distinct version numbers.
        document = session.scalar(
            select(Document)
            .where(Document.id == document_id, Document.tenant_id == tenant_id)
            .with_for_update()
        )
        if document is None:
            raise DocumentNotFound("document not found")

        existing = session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.checksum == staged.sha256,
            )
        )
        if existing is not None:
            staged.discard()
            session.commit()  # release the row lock
            return UploadOutcome(document, existing, created=False)

        last = session.scalar(
            select(func.max(DocumentVersion.version_number)).where(
                DocumentVersion.document_id == document.id
            )
        )
        version = _store_version(
            session, store, tenant_id, document, staged, version_number=(last or 0) + 1
        )
        session.commit()
        return UploadOutcome(document, version, created=True)
    except BaseException:
        session.rollback()
        staged.discard()
        raise


def promote_to_current(session: Session, version: DocumentVersion) -> None:
    """Make ``version`` the only current version of its document. Old versions are kept."""
    session.execute(
        update(DocumentVersion)
        .where(
            DocumentVersion.document_id == version.document_id,
            DocumentVersion.id != version.id,
            DocumentVersion.is_current.is_(True),
        )
        .values(is_current=False)
        .execution_options(synchronize_session="fetch")
    )
    # Clear the old pointer before setting the new one: the partial unique index is checked
    # per statement.
    version.is_current = True
    session.flush()


def _store_version(
    session: Session,
    store: ObjectStore,
    tenant_id: uuid.UUID,
    document: Document,
    staged: StagedFile,
    *,
    version_number: int,
) -> DocumentVersion:
    # Stored before the DB commit; if the commit fails, the content-addressed file is simply
    # reused by the next upload of the same bytes.
    uri = store.put_file(staged.path, store.key_for(tenant_id, staged.sha256))
    version = DocumentVersion(
        tenant_id=tenant_id,
        document_id=document.id,
        version_number=version_number,
        checksum=staged.sha256,
        filename=staged.filename,
        size_bytes=staged.size_bytes,
        storage_uri=uri,
        is_current=False,
        parser_status=ParserStatus.PENDING.value,
    )
    session.add(version)
    session.flush()
    return version
