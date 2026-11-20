"""Data-foundation tables (ARCHITECTURE.md §4, MVP_PLAN.md M1).

Every table that holds tenant data carries ``tenant_id``; every query must filter on it.
Workflow/task tables arrive in M3.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on Postgres, plain JSON elsewhere (SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


# Created by migrations but not mapped (Postgres-only full-text column and its index); schema
# comparisons skip them.
UNMAPPED = {"search_vector", "ix_document_chunks_search_vector"}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class DocumentType(StrEnum):
    ANNUAL_REPORT = "annual_report"
    ESG_REPORT = "esg_report"
    BRSR = "brsr"
    OTHER = "other"


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ParserStatus(StrEnum):
    """Processing status of one document version. Transitions are added with ingestion (M2)."""

    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"


def _in_check(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    industry: Mapped[str | None] = mapped_column(String(200))
    jurisdiction: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    """A logical document (e.g. "Acme annual report FY2025"); its files are versions."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_in_check("document_type", DocumentType), name="document_type"),
        CheckConstraint(_in_check("status", DocumentStatus), name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[str] = mapped_column(String(50))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    company: Mapped[Company] = relationship()
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", order_by="DocumentVersion.version_number"
    )


class DocumentVersion(Base):
    """One immutable stored file. At most one version per document is current."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number"),
        UniqueConstraint("document_id", "checksum"),
        CheckConstraint(_in_check("parser_status", ParserStatus), name="parser_status"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        # At most one current version per document (partial unique index).
        Index(
            "uq_document_versions_one_current",
            "document_id",
            unique=True,
            postgresql_where="is_current",
            sqlite_where="is_current",
        ),
        Index("ix_document_versions_tenant_checksum", "tenant_id", "checksum"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64), comment="SHA-256 hex of the file bytes")
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_uri: Mapped[str] = mapped_column(String(1024))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    parser_status: Mapped[str] = mapped_column(String(20), default=ParserStatus.PENDING.value)
    parser_error: Mapped[str | None] = mapped_column(Text)
    # TableLens output this version was parsed from (a derived artifact in the object store)
    structure_uri: Mapped[str | None] = mapped_column(String(1024))
    structure_checksum: Mapped[str | None] = mapped_column(String(64))
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parse_warnings: Mapped[list[str] | None] = mapped_column(JSONType)
    # vector index this version was indexed into (D-007)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_collection: Mapped[str | None] = mapped_column(String(200))
    index_point_count: Mapped[int | None] = mapped_column(Integer)

    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentChunk(Base):
    """A retrievable piece of a version, with page range and section; filled by ingestion (M2)."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "chunk_index"),
        CheckConstraint("page_start >= 1 AND page_end >= page_start", name="page_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text)
    # "metadata" is reserved on declarative classes, so the attribute is ``meta``.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceRecord(Base):
    """Traceable source evidence: version, page, section, bbox (PDF points) and quote."""

    __tablename__ = "evidence_records"
    __table_args__ = (CheckConstraint("page >= 1", name="page_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id"), index=True)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_chunks.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    # Polygon in PDF points from the parser, e.g. [[x0, y0], [x1, y0], ...]; null when unknown.
    bbox: Mapped[list[Any] | None] = mapped_column(JSONType)
    quote: Mapped[str] = mapped_column(Text)
    source_uri: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraceSpan(Base):
    """One timed step of a run (job, parse, embedding call, search, LLM call); spans with the
    same ``trace_id`` form a trace, ``parent_span_id`` makes the tree (D-022). Written by
    ``aeids.tracing.SqlTraceSink``."""

    __tablename__ = "trace_spans"
    __table_args__ = (
        CheckConstraint("status IN ('ok', 'error')", name="status"),
        Index("ix_trace_spans_trace", "trace_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trace_id: Mapped[str] = mapped_column(String(32))
    span_id: Mapped[str] = mapped_column(String(16))
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    inputs: Mapped[Any] = mapped_column(JSONType, nullable=True)
    outputs: Mapped[Any] = mapped_column(JSONType, nullable=True)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(200))
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))


class QueryEmbedding(Base):
    """A cached question vector (D-023): the same question, model and provider always get the
    same vector, so search and the benchmark are repeatable. ``key`` is the SHA-256 of model,
    provider and the exact embedded text (instruction + question). Not tenant data: it holds no
    document content, only the question text's vector."""

    __tablename__ = "query_embeddings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(100))
    dimension: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)  # little-endian float32
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- M3: workflows, tasks and their audit trail (ARCHITECTURE.md §3.1, §4) -------------------


class WorkflowStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    CANCELLED = "CANCELLED"


class Workflow(Base):
    """A user's analysis request: a question, its scope, the plan and its state."""

    __tablename__ = "workflows"
    __table_args__ = (CheckConstraint(_in_check("status", WorkflowStatus), name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    query: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # company/document ids, years
    status: Mapped[str] = mapped_column(String(30), default=WorkflowStatus.CREATED.value)
    plan: Mapped[list[Any] | None] = mapped_column(JSONType)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTask(Base):
    """One unit of agent work. Only the Supervisor changes ``status`` (rule 4); a worker only
    claims it (QUEUED -> RUNNING with a lease) and an expired lease returns it to QUEUED."""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint(_in_check("status", TaskStatus), name="status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts"),
        Index("ix_agent_tasks_workflow_status", "workflow_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id"), index=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_tasks.id"))
    plan_key: Mapped[str] = mapped_column(String(100))  # the step's name in the plan
    agent_name: Mapped[str] = mapped_column(String(50))
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.CREATED.value)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSONType, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=180)
    rework_round: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), primary_key=True)
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"),
                                                          primary_key=True)


class TaskTransition(Base):
    """Every task state change, persisted with its reason and actor (rule 5)."""

    __tablename__ = "task_transitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    """Workflow state changes and other audited actions (ARCHITECTURE.md §4)."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    resource: Mapped[str] = mapped_column(String(200), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    """One execution attempt of a task: model, prompt version, usage, latency, error."""

    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("task_id", "attempt"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    agent_name: Mapped[str] = mapped_column(String(50))
    agent_version: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    worker: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))  # agent result status, or schema_error / lost
    tool_calls: Mapped[list[Any] | None] = mapped_column(JSONType)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FindingRecord(Base):
    """A claim with its value and evidence; reports use only verified ones."""

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # the agent's finding_id
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflows.id"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    category: Mapped[str] = mapped_column(String(50))
    claim: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(String(80))  # Decimal as a string (rule)
    unit: Mapped[str | None] = mapped_column(String(20))
    currency: Mapped[str | None] = mapped_column(String(3))
    scale: Mapped[str | None] = mapped_column(String(20))
    period: Mapped[str | None] = mapped_column(String(40))
    verification_status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FindingEvidence(Base):
    __tablename__ = "finding_evidence"

    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"), primary_key=True)
    evidence_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_records.id"),
                                                          primary_key=True)
    relation_type: Mapped[str] = mapped_column(String(20), default="supports")


class CalculationRecord(Base):
    """Rule 2: function, formula, typed inputs, output and version of every calculation."""

    __tablename__ = "calculations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    function: Mapped[str] = mapped_column(String(100))
    formula: Mapped[str] = mapped_column(String(500))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONType)
    output: Mapped[str] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str | None] = mapped_column(String(3))
    scale: Mapped[str | None] = mapped_column(String(20))
    calc_version: Mapped[str] = mapped_column(String(20))


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    decision: Mapped[str] = mapped_column(String(20))
    reasons: Mapped[list[str]] = mapped_column(JSONType, default=list)
    rework_instructions: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
