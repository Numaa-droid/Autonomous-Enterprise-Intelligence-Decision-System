"""M1 data foundation: tenants, companies, documents, versions, chunks, evidence.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)

DOCUMENT_TYPES = "'annual_report', 'esg_report', 'brsr', 'other'"
DOCUMENT_STATUSES = "'active', 'archived'"
PARSER_STATUSES = "'pending', 'parsing', 'parsed', 'indexing', 'indexed', 'failed'"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("name", name="uq_tenants_name"),
    )

    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("industry", sa.String(200), nullable=True),
        sa.Column("jurisdiction", sa.String(100), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_companies_tenant_id_tenants"
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_companies_tenant_id_name"),
    )
    op.create_index("ix_companies_tenant_id", "companies", ["tenant_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("document_type", sa.String(50), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_documents_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name="fk_documents_company_id_companies"
        ),
        sa.CheckConstraint(
            f"document_type IN ({DOCUMENT_TYPES})", name="ck_documents_document_type"
        ),
        sa.CheckConstraint(f"status IN ({DOCUMENT_STATUSES})", name="ck_documents_status"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_company_id", "documents", ["company_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "checksum", sa.String(64), nullable=False, comment="SHA-256 hex of the file bytes"
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.String(1024), nullable=False),
        sa.Column("uploaded_at", TS, nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("parser_status", sa.String(20), nullable=False),
        sa.Column("parser_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_document_versions_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_document_versions_document_id_documents"
        ),
        sa.UniqueConstraint(
            "document_id", "version_number", name="uq_document_versions_document_id_version_number"
        ),
        sa.UniqueConstraint(
            "document_id", "checksum", name="uq_document_versions_document_id_checksum"
        ),
        sa.CheckConstraint(
            f"parser_status IN ({PARSER_STATUSES})", name="ck_document_versions_parser_status"
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_document_versions_version_number_positive"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_document_versions_size_bytes_nonnegative"),
    )
    op.create_index("ix_document_versions_tenant_id", "document_versions", ["tenant_id"])
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index(
        "ix_document_versions_tenant_checksum", "document_versions", ["tenant_id", "checksum"]
    )
    op.create_index(
        "uq_document_versions_one_current",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("qdrant_point_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_document_chunks_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name="fk_document_chunks_version_id_document_versions",
        ),
        sa.UniqueConstraint(
            "version_id", "chunk_index", name="uq_document_chunks_version_id_chunk_index"
        ),
        sa.CheckConstraint(
            "page_start >= 1 AND page_end >= page_start", name="ck_document_chunks_page_range"
        ),
    )
    op.create_index("ix_document_chunks_tenant_id", "document_chunks", ["tenant_id"])
    op.create_index("ix_document_chunks_version_id", "document_chunks", ["version_id"])

    op.create_table(
        "evidence_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("bbox", JSONType, nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.String(1024), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_records"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_evidence_records_tenant_id_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name="fk_evidence_records_version_id_document_versions",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name="fk_evidence_records_chunk_id_document_chunks",
        ),
        sa.CheckConstraint("page >= 1", name="ck_evidence_records_page_positive"),
    )
    op.create_index("ix_evidence_records_tenant_id", "evidence_records", ["tenant_id"])
    op.create_index("ix_evidence_records_version_id", "evidence_records", ["version_id"])
    op.create_index("ix_evidence_records_chunk_id", "evidence_records", ["chunk_id"])


def downgrade() -> None:
    op.drop_table("evidence_records")
    op.drop_table("document_chunks")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("companies")
    op.drop_table("tenants")
