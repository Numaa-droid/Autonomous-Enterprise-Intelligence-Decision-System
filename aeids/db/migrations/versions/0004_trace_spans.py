"""Run tracing: trace_spans (D-022).

Revision ID: 0004_trace_spans
Revises: 0003_search
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_trace_spans"
down_revision: str | None = "0003_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "trace_spans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("inputs", JSONType, nullable=True),
        sa.Column("outputs", JSONType, nullable=True),
        sa.Column("attributes", JSONType, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(18, 10), nullable=True),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_trace_spans_status"),
        sa.PrimaryKeyConstraint("id", name="pk_trace_spans"),
    )
    op.create_index("ix_trace_spans_trace", "trace_spans", ["trace_id", "started_at"])
    op.create_index("ix_trace_spans_tenant_id", "trace_spans", ["tenant_id"])
    op.create_index("ix_trace_spans_started_at", "trace_spans", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_trace_spans_started_at", table_name="trace_spans")
    op.drop_index("ix_trace_spans_tenant_id", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace", table_name="trace_spans")
    op.drop_table("trace_spans")
