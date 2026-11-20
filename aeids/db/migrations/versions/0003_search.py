"""M2: search — index bookkeeping on versions; Postgres full-text search on chunks (D-007).

Revision ID: 0003_search
Revises: 0002_version_parse_fields
Create Date: 2026-11-20

``document_chunks.search_vector`` is a Postgres generated ``tsvector`` (section + text) with a
GIN index. It is not mapped in the ORM (SQLite has no tsvector; there keyword search falls back
to an in-process scorer), so ``aeids.db.models.UNMAPPED`` lists it for schema comparisons.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_search"
down_revision: str | None = "0002_version_parse_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("document_versions") as batch:
        batch.add_column(sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("index_collection", sa.String(200), nullable=True))
        batch.add_column(sa.Column("index_point_count", sa.Integer(), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE document_chunks ADD COLUMN search_vector tsvector "
            "GENERATED ALWAYS AS (to_tsvector('english', "
            "coalesce(section, '') || ' ' || text)) STORED"
        )
        op.execute(
            "CREATE INDEX ix_document_chunks_search_vector ON document_chunks "
            "USING gin (search_vector)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_chunks_search_vector")
        op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_vector")
    with op.batch_alter_table("document_versions") as batch:
        batch.drop_column("index_point_count")
        batch.drop_column("index_collection")
        batch.drop_column("indexed_at")
