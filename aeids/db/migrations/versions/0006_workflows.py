"""M3: workflows, agent tasks, dependencies, transitions, runs, findings, calculations,
verification results and audit events (ARCHITECTURE.md §3.1, §4; D-011, D-024).

Revision ID: 0006_workflows
Revises: 0005_query_embeddings
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006_workflows'
down_revision: str | None = '0005_query_embeddings'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('actor', sa.String(length=100), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('resource', sa.String(length=200), nullable=False),
    sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_audit_events_tenant_id_tenants')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_events_resource'), ['resource'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('workflows',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('query', sa.Text(), nullable=False),
    sa.Column('scope', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('plan', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('report', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('trace_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('CREATED', 'PLANNING', 'RUNNING', 'NEEDS_HUMAN_REVIEW', 'REPORTING', 'COMPLETED', 'FAILED', 'CANCELLED')", name=op.f('ck_workflows_status')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_workflows_tenant_id_tenants')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workflows'))
    )
    with op.batch_alter_table('workflows', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workflows_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('agent_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('parent_task_id', sa.Uuid(), nullable=True),
    sa.Column('plan_key', sa.String(length=100), nullable=False),
    sa.Column('agent_name', sa.String(length=50), nullable=False),
    sa.Column('objective', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('input_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('output_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('acceptance_criteria', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('timeout_seconds', sa.Integer(), nullable=False),
    sa.Column('rework_round', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('lease_owner', sa.String(length=100), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('trace_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('CREATED', 'PLANNED', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'VERIFYING', 'VERIFIED', 'REJECTED', 'FAILED_RETRYABLE', 'FAILED_FINAL', 'BLOCKED', 'NEEDS_HUMAN_REVIEW', 'CANCELLED')", name=op.f('ck_agent_tasks_status')),
    sa.CheckConstraint('attempts >= 0 AND max_attempts >= 1', name=op.f('ck_agent_tasks_attempts')),
    sa.ForeignKeyConstraint(['parent_task_id'], ['agent_tasks.id'], name=op.f('fk_agent_tasks_parent_task_id_agent_tasks')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_agent_tasks_tenant_id_tenants')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_agent_tasks_workflow_id_workflows')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_tasks')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_agent_tasks_idempotency_key'))
    )
    with op.batch_alter_table('agent_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_tasks_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_tasks_workflow_id'), ['workflow_id'], unique=False)
        batch_op.create_index('ix_agent_tasks_workflow_status', ['workflow_id', 'status'], unique=False)

    op.create_table('agent_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('agent_name', sa.String(length=50), nullable=False),
    sa.Column('agent_version', sa.String(length=20), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=True),
    sa.Column('prompt_version', sa.String(length=50), nullable=True),
    sa.Column('worker', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('tool_calls', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('total_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=18, scale=10), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('trace_id', sa.String(length=32), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_agent_runs_task_id_agent_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_runs')),
    sa.UniqueConstraint('task_id', 'attempt', name=op.f('uq_agent_runs_task_id_attempt'))
    )
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_runs_task_id'), ['task_id'], unique=False)

    op.create_table('findings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('workflow_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('claim', sa.Text(), nullable=False),
    sa.Column('value', sa.String(length=80), nullable=True),
    sa.Column('unit', sa.String(length=20), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('scale', sa.String(length=20), nullable=True),
    sa.Column('period', sa.String(length=40), nullable=True),
    sa.Column('verification_status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_findings_task_id_agent_tasks')),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_findings_tenant_id_tenants')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_findings_workflow_id_workflows')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_findings'))
    )
    with op.batch_alter_table('findings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_findings_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_findings_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_findings_workflow_id'), ['workflow_id'], unique=False)

    op.create_table('task_dependencies',
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('depends_on_task_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['depends_on_task_id'], ['agent_tasks.id'], name=op.f('fk_task_dependencies_depends_on_task_id_agent_tasks')),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_task_dependencies_task_id_agent_tasks')),
    sa.PrimaryKeyConstraint('task_id', 'depends_on_task_id', name=op.f('pk_task_dependencies'))
    )
    op.create_table('task_transitions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('from_status', sa.String(length=30), nullable=False),
    sa.Column('to_status', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('actor', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_task_transitions_task_id_agent_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_transitions'))
    )
    with op.batch_alter_table('task_transitions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_task_transitions_task_id'), ['task_id'], unique=False)

    op.create_table('calculations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('finding_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('function', sa.String(length=100), nullable=False),
    sa.Column('formula', sa.String(length=500), nullable=False),
    sa.Column('inputs', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('output', sa.String(length=80), nullable=False),
    sa.Column('unit', sa.String(length=20), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('scale', sa.String(length=20), nullable=True),
    sa.Column('calc_version', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['finding_id'], ['findings.id'], name=op.f('fk_calculations_finding_id_findings')),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_calculations_task_id_agent_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_calculations'))
    )
    with op.batch_alter_table('calculations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_calculations_finding_id'), ['finding_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_calculations_task_id'), ['task_id'], unique=False)

    op.create_table('verification_results',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('finding_id', sa.Uuid(), nullable=False),
    sa.Column('task_id', sa.Uuid(), nullable=False),
    sa.Column('decision', sa.String(length=20), nullable=False),
    sa.Column('reasons', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('rework_instructions', sa.Text(), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['finding_id'], ['findings.id'], name=op.f('fk_verification_results_finding_id_findings')),
    sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], name=op.f('fk_verification_results_task_id_agent_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_verification_results'))
    )
    with op.batch_alter_table('verification_results', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_verification_results_finding_id'), ['finding_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_verification_results_task_id'), ['task_id'], unique=False)

    op.create_table('finding_evidence',
    sa.Column('finding_id', sa.Uuid(), nullable=False),
    sa.Column('evidence_record_id', sa.Uuid(), nullable=False),
    sa.Column('relation_type', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['evidence_record_id'], ['evidence_records.id'], name=op.f('fk_finding_evidence_evidence_record_id_evidence_records')),
    sa.ForeignKeyConstraint(['finding_id'], ['findings.id'], name=op.f('fk_finding_evidence_finding_id_findings')),
    sa.PrimaryKeyConstraint('finding_id', 'evidence_record_id', name=op.f('pk_finding_evidence'))
    )


def downgrade() -> None:
    op.drop_table('finding_evidence')
    with op.batch_alter_table('verification_results', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_verification_results_task_id'))
        batch_op.drop_index(batch_op.f('ix_verification_results_finding_id'))

    op.drop_table('verification_results')
    with op.batch_alter_table('calculations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_calculations_task_id'))
        batch_op.drop_index(batch_op.f('ix_calculations_finding_id'))

    op.drop_table('calculations')
    with op.batch_alter_table('task_transitions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_transitions_task_id'))

    op.drop_table('task_transitions')
    op.drop_table('task_dependencies')
    with op.batch_alter_table('findings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_findings_workflow_id'))
        batch_op.drop_index(batch_op.f('ix_findings_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_findings_task_id'))

    op.drop_table('findings')
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_runs_task_id'))

    op.drop_table('agent_runs')
    with op.batch_alter_table('agent_tasks', schema=None) as batch_op:
        batch_op.drop_index('ix_agent_tasks_workflow_status')
        batch_op.drop_index(batch_op.f('ix_agent_tasks_workflow_id'))
        batch_op.drop_index(batch_op.f('ix_agent_tasks_tenant_id'))

    op.drop_table('agent_tasks')
    with op.batch_alter_table('workflows', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workflows_tenant_id'))

    op.drop_table('workflows')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_events_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_resource'))

    op.drop_table('audit_events')
