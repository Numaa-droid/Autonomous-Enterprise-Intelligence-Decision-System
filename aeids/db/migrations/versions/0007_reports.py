"""M7: rendered reports (D-031).

Revision ID: 0007_reports
Revises: 0006_workflows
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007_reports'
down_revision: str | None = '0006_workflows'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('reports',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('schema_version', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('markdown', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_reports_tenant_id_tenants')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_reports_workflow_id_workflows')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports')),
    sa.UniqueConstraint('workflow_id', 'version', name=op.f('uq_reports_workflow_id_version'))
    )
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reports_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_reports_workflow_id'), ['workflow_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reports_workflow_id'))
        batch_op.drop_index(batch_op.f('ix_reports_tenant_id'))
    op.drop_table('reports')
