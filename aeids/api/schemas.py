"""API response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    version_number: int
    checksum: str
    filename: str
    size_bytes: int
    uploaded_at: datetime
    is_current: bool
    parser_status: str
    parser_error: str | None
    parsed_at: datetime | None = None
    structure_checksum: str | None = None
    parse_warnings: list[str] | None = None
    indexed_at: datetime | None = None
    index_point_count: int | None = None


class IngestOut(BaseModel):
    version: VersionOut
    chunk_count: int
    table_count: int
    financial_table_count: int
    warnings: list[str]
    processing: str = "off"  # indexing after the attach: queued / done / failed / off ...
    job_id: str | None = None


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chunk_index: int
    page_start: int
    page_end: int
    section: str | None
    kind: str
    text: str


class DocumentOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    company_name: str
    filename: str
    document_type: str
    fiscal_year: int | None
    status: str
    created_at: datetime
    current_version: VersionOut | None


class UploadOut(BaseModel):
    document: DocumentOut
    version: VersionOut
    duplicate: bool  # True when the same bytes were already stored; nothing new was created
    # what happened to parsing: queued, already_queued, not_queued, done, failed, off,
    # not_needed (duplicate upload)
    processing: str = "not_needed"
    job_id: str | None = None


class ProcessOut(BaseModel):
    version: VersionOut
    processing: str
    job_id: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody
