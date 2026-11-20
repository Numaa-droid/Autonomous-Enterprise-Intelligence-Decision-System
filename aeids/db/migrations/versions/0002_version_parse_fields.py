"""M2: record which TableLens output a version was parsed from.

Revision ID: 0002_version_parse_fields
Revises: 0001_foundation
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_version_parse_fields"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("document_versions") as batch:
        batch.add_column(sa.Column("structure_uri", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("structure_checksum", sa.String(64), nullable=True))
        batch.add_column(sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("parse_warnings", JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("document_versions") as batch:
        batch.drop_column("parse_warnings")
        batch.drop_column("parsed_at")
        batch.drop_column("structure_checksum")
        batch.drop_column("structure_uri")
