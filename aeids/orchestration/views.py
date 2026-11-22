"""Read models of workflows for the API and the console (no state changes here)."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids.reporting.answer import arrange, counts
from aeids.db.models import (
    AgentRun,
    AgentTask,
    AuditEvent,
    CalculationRecord,
    DocumentVersion,
    EvidenceRecord,
    FindingEvidence,
    FindingRecord,
    TaskDependency,
    TaskTransition,
    VerificationResult,
    Workflow,
)
from aeids.orchestration.supervisor import WorkflowNotFound, aware


def get_workflow(session: Session, tenant_id: uuid.UUID, workflow_id: uuid.UUID) -> Workflow:
    wf = session.scalar(select(Workflow).where(Workflow.id == workflow_id, Workflow.tenant_id == tenant_id))
    if wf is None:
        raise WorkflowNotFound("workflow not found")
    return wf


def summary(session: Session, wf: Workflow) -> dict[str, Any]:
    counts = Counter(session.scalars(select(AgentTask.status).where(AgentTask.workflow_id == wf.id)))
    return {"id": wf.id, "query": wf.query, "scope": wf.scope, "status": wf.status,
            "trace_id": wf.trace_id, "created_at": aware(wf.created_at), "updated_at": aware(wf.updated_at),
            "completed_at": aware(wf.completed_at), "error": wf.error, "task_counts": dict(counts),
            "verified_findings": len((wf.report or {}).get("verified_findings", []))}


def list_workflows(session: Session, tenant_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(select(Workflow).where(Workflow.tenant_id == tenant_id)
                           .order_by(Workflow.created_at.desc()).limit(limit))
    return [summary(session, wf) for wf in rows]


def tasks(session: Session, wf: Workflow) -> list[dict[str, Any]]:
    rows = list(session.scalars(select(AgentTask).where(AgentTask.workflow_id == wf.id)
                                .order_by(AgentTask.created_at)))
    ids = [t.id for t in rows]
    deps: dict[uuid.UUID, list[str]] = {}
    for t, d in session.execute(select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
                                .where(TaskDependency.task_id.in_(ids))):
        deps.setdefault(t, []).append(str(d))
    transitions: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for tr in session.scalars(select(TaskTransition).where(TaskTransition.task_id.in_(ids))
                              .order_by(TaskTransition.created_at)):
        transitions.setdefault(tr.task_id, []).append(
            {"from": tr.from_status, "to": tr.to_status, "reason": tr.reason, "actor": tr.actor,
             "at": aware(tr.created_at)})
    runs: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in session.scalars(select(AgentRun).where(AgentRun.task_id.in_(ids)).order_by(AgentRun.attempt)):
        runs.setdefault(r.task_id, []).append(
            {"attempt": r.attempt, "status": r.status, "worker": r.worker, "agent_version": r.agent_version,
             "model": r.model, "prompt_version": r.prompt_version, "latency_ms": r.latency_ms,
             "total_tokens": r.total_tokens, "error": r.error, "finished_at": aware(r.finished_at)})
    return [{"id": t.id, "plan_key": t.plan_key, "agent": t.agent_name, "objective": t.objective,
             "status": t.status, "attempts": t.attempts, "max_attempts": t.max_attempts,
             "rework_round": t.rework_round, "parent_task_id": t.parent_task_id,
             "idempotency_key": t.idempotency_key, "depends_on": deps.get(t.id, []),
             "verifies": (t.input_json or {}).get("target_task_id"),
             "acceptance_criteria": t.acceptance_criteria, "last_error": t.last_error,
             "lease_owner": t.lease_owner, "lease_expires_at": aware(t.lease_expires_at),
             "output": t.output_json, "transitions": transitions.get(t.id, []), "runs": runs.get(t.id, []),
             "created_at": aware(t.created_at), "updated_at": aware(t.updated_at)} for t in rows]


def calculation(session: Session, finding_id: uuid.UUID) -> dict[str, Any] | None:
    """The stored calculation behind a finding (rule 2), or None for a reported value."""
    c = session.scalar(select(CalculationRecord).where(CalculationRecord.finding_id == finding_id))
    if c is None:
        return None
    return {"function": c.function, "formula": c.formula, "inputs": c.inputs, "output": c.output,
            "unit": c.unit, "currency": c.currency, "scale": c.scale, "calc_version": c.calc_version}


def findings(session: Session, wf: Workflow) -> list[dict[str, Any]]:
    out = []
    for f in session.scalars(select(FindingRecord).where(FindingRecord.workflow_id == wf.id)
                             .order_by(FindingRecord.created_at)):
        evidence = [{"evidence_id": e.id, "file": name, "version_id": e.version_id, "page": e.page,
                     "section": e.section, "quote": e.quote, "relation": rel}
                    for e, name, rel in session.execute(
                        select(EvidenceRecord, DocumentVersion.filename, FindingEvidence.relation_type)
                        .join(FindingEvidence, FindingEvidence.evidence_record_id == EvidenceRecord.id)
                        .join(DocumentVersion, DocumentVersion.id == EvidenceRecord.version_id)
                        .where(FindingEvidence.finding_id == f.id))]
        checks = [{"decision": v.decision, "reasons": v.reasons, "rework_instructions": v.rework_instructions,
                   "at": aware(v.verified_at)}
                  for v in session.scalars(select(VerificationResult).where(VerificationResult.finding_id == f.id))]
        out.append({"id": f.id, "task_id": f.task_id, "category": f.category, "claim": f.claim,
                    "value": f.value, "unit": f.unit, "currency": f.currency, "scale": f.scale,
                    "period": f.period, "verification_status": f.verification_status,
                    "calculation": calculation(session, f.id),
                    "evidence": evidence, "verifications": checks})
    return out


def answer(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """The report as an answer (``aeids.reporting.answer``): a plain-English summary when one was
    written, then calculated results, the reported figures they rest on, and cited passages."""
    if not report:
        return None
    arranged = arrange(report.get("verified_findings", []))
    return {"summary": counts(arranged), "text": report.get("summary"), "text_source": report.get("summary_source"),
            **arranged, "limitations": report.get("limitations", []), "notice": report.get("notice")}


def detail(session: Session, wf: Workflow) -> dict[str, Any]:
    events = [{"action": e.action, "details": e.details, "actor": e.actor, "at": aware(e.created_at)}
              for e in session.scalars(select(AuditEvent).where(AuditEvent.resource == f"workflow:{wf.id}")
                                       .order_by(AuditEvent.created_at))]
    return {**summary(session, wf), "plan": wf.plan, "report": wf.report, "answer": answer(wf.report), "events": events,
            "tasks": tasks(session, wf), "findings": findings(session, wf)}
