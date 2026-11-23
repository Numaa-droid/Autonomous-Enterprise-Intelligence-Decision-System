"""The Supervisor: plans a workflow into a task graph and moves every task through its state
machine (ARCHITECTURE.md §3, MVP_PLAN.md §3; D-011, D-024).

Only this module changes task and workflow state (rule 4); workers only claim a task and lease
expiry only returns it to QUEUED (``runner.py``). Everything is reconciliation: ``tick`` looks
at what is persisted and makes the next legal moves, so it can run any number of times, in any
process, after any crash, and never redoes VERIFIED work (rule 5). A workflow row lock keeps two
ticks from planning the same follow-up task, and unique idempotency keys make that impossible
anyway.

Agent results are accepted only from the worker that holds the task's lease (fencing): a worker
that lost its lease cannot write findings twice.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeids.agents.base import Registry
from aeids.config import Settings
from aeids.contracts import AgentResult
from aeids.db.models import (
    AgentRun,
    AgentTask,
    AuditEvent,
    CalculationRecord,
    DocumentChunk,
    DocumentVersion,
    EvidenceRecord,
    FindingEvidence,
    FindingRecord,
    TaskDependency,
    TaskStatus,
    TaskTransition,
    TraceSpan,
    VerificationResult,
    Workflow,
    WorkflowStatus,
    utcnow,
)
from aeids.documents import DocumentError
from aeids.reporting import report as report_doc
from aeids.reporting.answer import verified_findings
from aeids.orchestration.states import TASK_TERMINAL, WORKFLOW_TERMINAL, move_task, move_workflow

log = logging.getLogger(__name__)

T, W = TaskStatus, WorkflowStatus
ACTOR = "supervisor"
VERIFICATION_AGENT = "verification"
WRITER_AGENT = "report_writer"  # optional last step: a plain-English summary of the verified answer (D-028)
MOVING = {T.QUEUED, T.RUNNING, T.FAILED_RETRYABLE, T.SUCCEEDED, T.VERIFYING, T.CREATED}


class PlanningError(DocumentError):
    code = "planning_failed"
    http_status = 422


class WorkflowNotFound(DocumentError):
    code = "not_found"
    http_status = 404


class ContractViolation(Exception):
    """An agent result that is schema-valid but breaks a rule (unknown evidence, a quote that
    is not in its chunk, a result for another task). Treated like a schema error."""


def aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def root_span_id(workflow: Workflow) -> str:
    return workflow.id.hex[:16]


# --- planning -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedStep:
    key: str
    agent: str
    objective: str
    inputs: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)


class TemplatePlanner:
    """M3 plan for any question: collect evidence, then analyse it (verification is added by
    the Supervisor for every task that produces findings). M4 adds an LLM planner."""

    version = "template-1"

    def plan(self, query: str, scope: dict[str, Any]) -> list[PlannedStep]:
        return [
            PlannedStep("extract", "document_intelligence",
                        f"Find the passages and tables that answer: {query}",
                        {"query": query, "scope": scope, "top_k": 5}, [],
                        ["Every passage is quoted exactly from an indexed document in scope",
                         "Every passage has its document version, page and section"]),
            PlannedStep("analyse", "financial_analyst",
                        f"Answer from the collected evidence: {query}",
                        {"query": query}, ["extract"],
                        ["Every finding cites evidence",
                         "Numbers come from the calculation module, never from the model"]),
        ]


def _check_plan(steps: list[PlannedStep], registry: Registry) -> None:
    keys = [s.key for s in steps]
    if not steps or len(set(keys)) != len(keys):
        raise PlanningError("the plan is empty or repeats a step")
    for s in steps:
        if s.agent not in registry:
            raise PlanningError(f"step {s.key}: no agent registered as {s.agent!r}")
        unknown = [d for d in s.depends_on if d not in keys]
        if unknown:
            raise PlanningError(f"step {s.key} depends on unknown steps {unknown}")
    seen: set[str] = set()
    remaining = list(steps)
    while remaining:  # topological check: no cycles
        ready = [s for s in remaining if set(s.depends_on) <= seen]
        if not ready:
            raise PlanningError("the plan has a dependency cycle")
        seen |= {s.key for s in ready}
        remaining = [s for s in remaining if s not in ready]


def _new_task(session: Session, wf: Workflow, *, key: str, agent: str, objective: str,
              inputs: dict[str, Any], criteria: list[str], settings: Settings, rework_round: int = 0,
              parent: AgentTask | None = None, idempotency_key: str | None = None) -> AgentTask:
    task = AgentTask(
        tenant_id=wf.tenant_id, workflow_id=wf.id, parent_task_id=parent.id if parent else None,
        plan_key=key, agent_name=agent, objective=objective, status=T.CREATED.value,
        input_json=inputs, acceptance_criteria=criteria, attempts=0,
        max_attempts=settings.task_max_attempts, timeout_seconds=settings.task_timeout_seconds,
        rework_round=rework_round,
        idempotency_key=idempotency_key or f"{wf.id}:{key}:{rework_round}",
        trace_id=wf.trace_id)
    session.add(task)
    session.flush()
    move_task(session, task, T.PLANNED, reason="added to the plan", actor=ACTOR)
    return task


def create_workflow(session: Session, tenant_id: uuid.UUID, query: str, scope: dict[str, Any], *,
                    settings: Settings, registry: Registry, planner: Any = None) -> Workflow:
    """CREATED -> PLANNING -> RUNNING with the whole task graph, in one transaction."""
    planner = planner or TemplatePlanner()
    wf = Workflow(tenant_id=tenant_id, query=query, scope=scope, status=W.CREATED.value,
                  trace_id=secrets.token_hex(16))
    session.add(wf)
    session.flush()
    session.add(AuditEvent(tenant_id=tenant_id, actor="api", action="workflow.created",
                           resource=f"workflow:{wf.id}", details={"query": query[:500]}))
    move_workflow(session, wf, W.PLANNING, reason="workflow accepted")
    steps = planner.plan(query, scope)
    if WRITER_AGENT in registry and all(s.agent != WRITER_AGENT for s in steps):
        steps.append(PlannedStep("summarise", WRITER_AGENT,
                                 "Write a short plain-English answer from the verified findings",
                                 {"query": query}, [s.key for s in steps],
                                 ["Uses only verified findings", "Every number in it is a checked value"]))
    _check_plan(steps, registry)
    tasks: dict[str, AgentTask] = {}
    for s in steps:
        tasks[s.key] = _new_task(session, wf, key=s.key, agent=s.agent, objective=s.objective,
                                 inputs=s.inputs, criteria=s.acceptance_criteria, settings=settings)
    for s in steps:
        for d in s.depends_on:
            session.add(TaskDependency(task_id=tasks[s.key].id, depends_on_task_id=tasks[d].id))
    wf.plan = [{"key": s.key, "agent": s.agent, "objective": s.objective,
                "depends_on": s.depends_on, "acceptance_criteria": s.acceptance_criteria}
               for s in steps]
    move_workflow(session, wf, W.RUNNING, reason=f"task graph persisted ({planner.version})")
    session.add(TraceSpan(  # the workflow's root span; task runs join this trace (D-022)
        trace_id=wf.trace_id, span_id=root_span_id(wf), parent_span_id=None, tenant_id=tenant_id,
        name="workflow", kind="job", status="ok", started_at=utcnow(),
        inputs={"query": query, "scope": scope}, attributes={"planner": planner.version}))
    session.commit()
    return wf


# --- results from agents ----------------------------------------------------------------------

@dataclass
class RunInfo:
    worker: str
    agent_version: str
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int | None = None
    total_tokens: int | None = None
    cost_usd: Any = None
    started_at: datetime | None = None


def _fenced(task: AgentTask, worker: str) -> bool:
    return task.status == T.RUNNING.value and task.lease_owner == worker


def _record_run(session: Session, task: AgentTask, info: RunInfo, status: str,
                error: str | None = None) -> None:
    session.add(AgentRun(
        task_id=task.id, attempt=task.attempts, agent_name=task.agent_name,
        agent_version=info.agent_version, model=info.model, prompt_version=info.prompt_version,
        worker=info.worker, status=status, total_tokens=info.total_tokens, cost_usd=info.cost_usd,
        latency_ms=info.latency_ms, error=(error or None) and error[:4000], trace_id=task.trace_id,
        started_at=info.started_at or utcnow(), finished_at=utcnow()))


def _release(task: AgentTask) -> None:
    task.lease_owner = None
    task.lease_expires_at = None


def _retry_or_fail(session: Session, task: AgentTask, reason: str, settings: Settings,
                   now: datetime) -> None:
    if task.attempts < task.max_attempts:
        move_task(session, task, T.FAILED_RETRYABLE, reason=reason, actor=ACTOR)
        delay = settings.task_retry_backoff_seconds * (2 ** max(task.attempts - 1, 0))
        task.next_attempt_at = now + timedelta(seconds=delay)
    else:
        move_task(session, task, T.FAILED_FINAL, reason=f"{reason}; attempts exhausted", actor=ACTOR)


def record_failure(session: Session, task: AgentTask, kind: str, message: str, info: RunInfo, *,
                   settings: Settings, now: datetime | None = None) -> bool:
    """kind: schema | permission | transient | final. Returns False when the worker no longer
    holds the task (its result is discarded)."""
    now = now or utcnow()
    if not _fenced(task, info.worker):
        _record_run(session, task, info, "discarded", f"lease lost before the result: {message}")
        return False
    _record_run(session, task, info, f"{kind}_error", message)
    task.last_error = message[:4000]
    _release(task)
    if kind == "schema":
        earlier = session.scalar(select(func.count()).select_from(AgentRun).where(
            AgentRun.task_id == task.id, AgentRun.status == "schema_error")) or 0
        if earlier <= 1 and task.attempts < task.max_attempts:  # this one counts as the first
            move_task(session, task, T.FAILED_RETRYABLE,
                      reason=f"output failed validation, retried once: {message[:300]}", actor=ACTOR)
            task.next_attempt_at = now
        else:
            move_task(session, task, T.FAILED_FINAL,
                      reason=f"output failed validation again: {message[:300]}", actor=ACTOR)
    elif kind in ("permission", "final"):
        move_task(session, task, T.FAILED_FINAL, reason=message[:500], actor=ACTOR)
    else:
        _retry_or_fail(session, task, f"transient error: {message[:300]}", settings, now)
    return True


def accept_result(session: Session, task: AgentTask, result: AgentResult, info: RunInfo, *,
                  settings: Settings, now: datetime | None = None) -> bool:
    """Validate a result against the task and persist what it proposes, then map its status
    (MVP_PLAN.md §3). Raises ContractViolation (nothing written) for a rule-breaking result."""
    now = now or utcnow()
    if not _fenced(task, info.worker):
        _record_run(session, task, info, "discarded", "lease lost before the result")
        return False
    if (result.task_id, result.workflow_id, result.agent_name) != \
            (task.id, task.workflow_id, task.agent_name):
        raise ContractViolation("the result is for another task, workflow or agent")
    if result.verifications and task.agent_name != VERIFICATION_AGENT:
        raise ContractViolation("only the verification agent may decide on findings")

    evidence_ids = _store_evidence(session, task, result)
    _store_findings(session, task, result, evidence_ids)
    _store_verifications(session, task, result)

    task.output_json = {**result.outputs, "status": result.status, "warnings": result.warnings,
                        "errors": result.errors, "metrics": result.metrics,
                        "findings": len(result.findings), "evidence": len(result.evidence)}
    _record_run(session, task, info, result.status, "; ".join(result.errors) or None)
    _release(task)
    if result.status == "completed":
        move_task(session, task, T.SUCCEEDED, reason="agent completed", actor=ACTOR)
    elif result.status in ("needs_more_information", "blocked"):
        task.last_error = result.needs or "; ".join(result.errors) or result.status
        move_task(session, task, T.BLOCKED, reason=f"agent: {task.last_error}"[:500], actor=ACTOR)
    elif result.status == "retryable_error":
        task.last_error = "; ".join(result.errors)
        _retry_or_fail(session, task, f"agent error: {task.last_error[:300]}", settings, now)
    else:
        task.last_error = "; ".join(result.errors)
        move_task(session, task, T.FAILED_FINAL, reason=f"agent: {task.last_error[:300]}", actor=ACTOR)
    return True


def _store_evidence(session: Session, task: AgentTask, result: AgentResult) -> set[uuid.UUID]:
    """Evidence proposals become evidence_records after checks; returns their ids."""
    if not result.evidence:
        return set()
    chunk_ids = {e.chunk_id for e in result.evidence}
    chunks = {c.id: (c, v) for c, v in session.execute(
        select(DocumentChunk, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
        .where(DocumentChunk.id.in_(chunk_ids), DocumentChunk.tenant_id == task.tenant_id))}
    for e in result.evidence:
        if e.chunk_id not in chunks:
            raise ContractViolation(f"evidence {e.evidence_id}: chunk not found for this tenant")
        chunk, _v = chunks[e.chunk_id]
        if e.quote not in chunk.text:
            raise ContractViolation(f"evidence {e.evidence_id}: the quote is not in the chunk text")
        if not chunk.page_start <= e.page <= chunk.page_end:
            raise ContractViolation(f"evidence {e.evidence_id}: page {e.page} is outside the chunk")
    for e in result.evidence:
        chunk, version = chunks[e.chunk_id]
        session.add(EvidenceRecord(
            id=e.evidence_id, tenant_id=task.tenant_id, version_id=version.id, chunk_id=chunk.id,
            page=e.page, section=e.section, quote=e.quote, source_uri=version.storage_uri,
            bbox=[[e.bbox[0], e.bbox[1]], [e.bbox[2], e.bbox[1]], [e.bbox[2], e.bbox[3]],
                  [e.bbox[0], e.bbox[3]]] if e.bbox else None))
    session.flush()
    return {e.evidence_id for e in result.evidence}


def _store_findings(session: Session, task: AgentTask, result: AgentResult,
                    new_evidence: set[uuid.UUID]) -> None:
    if not result.findings:
        return
    cited = {ref.evidence_id for f in result.findings for ref in f.evidence}
    known = set(session.scalars(select(EvidenceRecord.id).where(
        EvidenceRecord.id.in_(cited - new_evidence), EvidenceRecord.tenant_id == task.tenant_id)))
    unknown = cited - new_evidence - known
    if unknown:
        raise ContractViolation(f"findings cite unknown evidence: {sorted(map(str, unknown))[:3]}")
    for f in result.findings:
        session.add(FindingRecord(
            id=f.finding_id, tenant_id=task.tenant_id, workflow_id=task.workflow_id, task_id=task.id,
            category=f.category, claim=f.claim, value=str(f.value) if f.value is not None else None,
            unit=f.unit, currency=f.currency, scale=f.scale, period=f.period,
            verification_status="pending"))
        session.flush()
        for ref in f.evidence:
            session.add(FindingEvidence(finding_id=f.finding_id, evidence_record_id=ref.evidence_id,
                                        relation_type=ref.relation))
        if f.calculation is not None:
            c = f.calculation
            session.add(CalculationRecord(
                id=c.calculation_id, finding_id=f.finding_id, task_id=task.id, function=c.function,
                formula=c.formula, inputs={k: q.model_dump(mode="json") for k, q in c.inputs.items()},
                output=str(c.output), unit=c.unit, currency=c.currency, scale=c.scale,
                calc_version=c.calc_version))
    session.flush()


def _store_verifications(session: Session, task: AgentTask, result: AgentResult) -> None:
    if task.agent_name != VERIFICATION_AGENT:
        return
    expected = {uuid.UUID(f["finding_id"]) for f in task.input_json.get("findings", [])}
    decided = {v.finding_id for v in result.verifications}
    if result.status == "completed" and decided != expected:
        raise ContractViolation("verification must decide on every finding it was given, and only those")
    for v in result.verifications:
        session.add(VerificationResult(finding_id=v.finding_id, task_id=task.id, decision=v.decision,
                                       reasons=v.reasons, rework_instructions=v.rework_instructions))
        finding = session.get(FindingRecord, v.finding_id)
        finding.verification_status = v.decision


# --- reconciliation ---------------------------------------------------------------------------

@dataclass
class TickResult:
    status: str
    queued: list[uuid.UUID] = field(default_factory=list)
    wake_at: datetime | None = None


def tick(session: Session, workflow_id: uuid.UUID, *, settings: Settings,
         now: datetime | None = None) -> TickResult:
    """Make every legal next move for one workflow, then commit. Safe to call any time."""
    now = now or utcnow()
    wf = session.scalar(select(Workflow).where(Workflow.id == workflow_id).with_for_update())
    if wf is None:
        raise WorkflowNotFound("workflow not found")
    if WorkflowStatus(wf.status) in WORKFLOW_TERMINAL or wf.status in (W.CREATED.value, W.PLANNING.value):
        session.commit()
        return TickResult(wf.status)
    queued: list[uuid.UUID] = []
    for _ in range(100):  # each pass moves at least one task, or stops
        if not _pass(session, wf, settings, now, queued):
            break
    _workflow_state(session, wf, settings)
    wake = _wake_at(session, wf)
    session.commit()
    return TickResult(wf.status, queued, wake)


def _tasks(session: Session, wf: Workflow) -> list[AgentTask]:
    return list(session.scalars(select(AgentTask).where(AgentTask.workflow_id == wf.id)
                                .order_by(AgentTask.created_at)))


def _deps(session: Session, wf: Workflow) -> dict[uuid.UUID, list[uuid.UUID]]:
    rows = session.execute(select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
                           .join(AgentTask, AgentTask.id == TaskDependency.task_id)
                           .where(AgentTask.workflow_id == wf.id)).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for t, d in rows:
        out.setdefault(t, []).append(d)
    return out


def _has_findings(session: Session, task: AgentTask) -> bool:
    return bool(session.scalar(select(func.count()).select_from(FindingRecord)
                               .where(FindingRecord.task_id == task.id)))


def _verification_task(session: Session, wf: Workflow, task: AgentTask) -> AgentTask | None:
    return session.scalar(select(AgentTask).where(
        AgentTask.idempotency_key == f"{wf.id}:verify:{task.id}"))


def _rework_task(session: Session, wf: Workflow, task: AgentTask) -> AgentTask | None:
    return session.scalar(select(AgentTask).where(AgentTask.parent_task_id == task.id,
                                                  AgentTask.agent_name == task.agent_name))


def _dead(session: Session, wf: Workflow, task: AgentTask, settings: Settings) -> bool:
    """A dependency that will never become VERIFIED."""
    if task.status in (T.FAILED_FINAL.value, T.CANCELLED.value):
        return True
    if task.status == T.REJECTED.value:
        return task.rework_round >= settings.workflow_max_rework_rounds
    return False


def _blocked_by_agent(session: Session, task: AgentTask) -> bool:
    """BLOCKED by its own agent's result (vs. by a failed dependency)."""
    last = session.scalar(select(TaskTransition.from_status).where(TaskTransition.task_id == task.id)
                          .order_by(TaskTransition.created_at.desc()).limit(1))
    return last == T.RUNNING.value


def _pass(session: Session, wf: Workflow, settings: Settings, now: datetime,
          queued: list[uuid.UUID]) -> bool:
    tasks = _tasks(session, wf)
    by_id = {t.id: t for t in tasks}
    deps = _deps(session, wf)
    for t in tasks:
        s = TaskStatus(t.status)
        if s == T.RUNNING and t.lease_expires_at and aware(t.lease_expires_at) <= now:
            move_task(session, t, T.QUEUED, actor="lease-recovery",
                      reason=f"lease of {t.lease_owner} expired (worker lost); attempt {t.attempts} counted")
            _release(t)
            queued.append(t.id)
            return True
        if s == T.FAILED_RETRYABLE and (t.next_attempt_at is None or aware(t.next_attempt_at) <= now):
            if t.attempts < t.max_attempts:
                move_task(session, t, T.QUEUED, reason=f"retry {t.attempts + 1} of {t.max_attempts}",
                          actor=ACTOR)
                queued.append(t.id)
            else:
                move_task(session, t, T.FAILED_FINAL, reason="attempts exhausted", actor=ACTOR)
            return True
        if s == T.SUCCEEDED:
            if _has_findings(session, t):
                move_task(session, t, T.VERIFYING, reason="findings sent to verification", actor=ACTOR)
                findings = session.scalars(select(FindingRecord).where(FindingRecord.task_id == t.id))
                payload = [{"finding_id": str(f.id), "claim": f.claim, "value": f.value, "unit": f.unit,
                            "evidence_ids": [str(e) for e in session.scalars(
                                select(FindingEvidence.evidence_record_id)
                                .where(FindingEvidence.finding_id == f.id))]}
                           for f in findings]
                _new_task(session, wf, key=f"verify:{t.plan_key}", agent=VERIFICATION_AGENT,
                          objective=f"Verify every finding of {t.plan_key} against its evidence",
                          inputs={"target_task_id": str(t.id), "findings": payload},
                          criteria=["Every cited evidence record exists and quotes its source"],
                          settings=settings, rework_round=t.rework_round,
                          idempotency_key=f"{wf.id}:verify:{t.id}")
            else:
                move_task(session, t, T.VERIFIED, reason="no findings: nothing to verify", actor=ACTOR)
            return True
        if s == T.VERIFYING:
            vt = _verification_task(session, wf, t)
            if vt is None:
                continue
            if vt.status == T.VERIFIED.value:
                decisions = list(session.scalars(
                    select(VerificationResult).join(FindingRecord, FindingRecord.id == VerificationResult.finding_id)
                    .where(FindingRecord.task_id == t.id, VerificationResult.task_id == vt.id)))
                kinds = {d.decision for d in decisions}
                if "needs_review" in kinds:
                    move_task(session, t, T.NEEDS_HUMAN_REVIEW, reason="a finding needs human review",
                              actor=ACTOR)
                elif "rejected" in kinds:
                    rejected = [d for d in decisions if d.decision == "rejected"]
                    t.last_error = " ".join(dict.fromkeys(
                        f"{'; '.join(d.reasons[:3])}. {d.rework_instructions or ''}".strip()
                        for d in rejected))[:4000]
                    if t.rework_round >= settings.workflow_max_rework_rounds and "verified" in kinds:
                        # no rounds left: the rejected findings are dropped, the verified ones stand
                        t.output_json = {**(t.output_json or {}),
                                         "dropped_findings": [str(d.finding_id) for d in rejected]}
                        move_task(session, t, T.VERIFIED, actor=ACTOR,
                                  reason=f"{len(rejected)} rejected finding(s) dropped after {t.rework_round} "
                                         f"rework round(s); the verified ones stand")
                    else:
                        move_task(session, t, T.REJECTED, reason=f"verification rejected: {t.last_error[:300]}",
                                  actor=ACTOR)
                else:
                    move_task(session, t, T.VERIFIED, reason="every finding verified", actor=ACTOR)
                return True
            if vt.status in (T.FAILED_FINAL.value, T.CANCELLED.value):
                move_task(session, t, T.NEEDS_HUMAN_REVIEW, reason="verification could not run", actor=ACTOR)
                return True
        if s == T.REJECTED and t.rework_round < settings.workflow_max_rework_rounds \
                and _rework_task(session, wf, t) is None:
            rework = _new_task(
                session, wf, key=t.plan_key, agent=t.agent_name, objective=t.objective,
                inputs={**t.input_json, "rework_instructions": t.last_error or ""},
                criteria=t.acceptance_criteria, settings=settings, rework_round=t.rework_round + 1,
                parent=t)
            for d in deps.get(t.id, []):  # the rework task needs what the rejected one needed
                session.add(TaskDependency(task_id=rework.id, depends_on_task_id=d))
            for row in session.scalars(select(TaskDependency).where(TaskDependency.depends_on_task_id == t.id)):
                session.add(TaskDependency(task_id=row.task_id, depends_on_task_id=rework.id))
                session.delete(row)  # dependents now wait for the rework task
            session.flush()
            return True
        if s == T.BLOCKED and _blocked_by_agent(session, t):
            # M3 has no agent that can supply missing information: a person decides
            move_task(session, t, T.NEEDS_HUMAN_REVIEW,
                      reason=f"not resolvable by a registered agent: {t.last_error or 'blocked'}", actor=ACTOR)
            return True
        if s in (T.PLANNED, T.BLOCKED):
            mine = [by_id[d] for d in deps.get(t.id, []) if d in by_id]
            if mine and any(_dead(session, wf, d, settings) for d in mine):
                if s == T.PLANNED:
                    move_task(session, t, T.BLOCKED, reason="a dependency failed or was rejected", actor=ACTOR)
                else:
                    move_task(session, t, T.FAILED_FINAL, reason="a dependency failed for good", actor=ACTOR)
                return True
            if all(d.status == T.VERIFIED.value for d in mine):
                move_task(session, t, T.QUEUED, actor=ACTOR,
                          reason="every dependency verified" if mine else "no dependencies")
                queued.append(t.id)
                return True
    return False


def _workflow_state(session: Session, wf: Workflow, settings: Settings) -> None:
    tasks = _tasks(session, wf)
    states = {TaskStatus(t.status) for t in tasks}
    if all(TaskStatus(t.status) in TASK_TERMINAL for t in tasks):
        if wf.status == W.NEEDS_HUMAN_REVIEW.value:
            move_workflow(session, wf, W.RUNNING, reason="review resolved")
        move_workflow(session, wf, W.REPORTING, reason="every task is terminal")
        wf.report = build_report(session, wf, tasks)
        session.flush()
        try:
            rendered = report_doc.publish(session, wf)
        except report_doc.ReportCheckFailed as e:  # the report would say more than was verified
            wf.error = f"report check failed: {e}"[:4000]
            move_workflow(session, wf, W.FAILED, reason=wf.error)
            _finish_root_span(session, wf)
            return
        move_workflow(session, wf, W.COMPLETED, reason=f"report v{rendered.version} ({rendered.status}) built from "
                      f"{len(wf.report['verified_findings'])} verified finding(s)")
        _finish_root_span(session, wf)
        return
    moving = states & MOVING
    if wf.status == W.RUNNING.value and not moving and T.NEEDS_HUMAN_REVIEW in states:
        move_workflow(session, wf, W.NEEDS_HUMAN_REVIEW, reason="a task waits on a reviewer and nothing else can run")
    elif wf.status == W.NEEDS_HUMAN_REVIEW.value and moving:
        move_workflow(session, wf, W.RUNNING, reason="work can continue")


def _wake_at(session: Session, wf: Workflow) -> datetime | None:
    times = [aware(t.lease_expires_at) for t in _tasks(session, wf)
             if t.status == T.RUNNING.value and t.lease_expires_at]
    times += [aware(t.next_attempt_at) for t in _tasks(session, wf)
              if t.status == T.FAILED_RETRYABLE.value and t.next_attempt_at]
    return min(times) if times else None


def build_report(session: Session, wf: Workflow, tasks: list[AgentTask]) -> dict[str, Any]:
    """Verified findings with their evidence; everything else is a limitation (M7 renders it)."""
    findings = verified_findings(session, wf.id)
    superseded = {t.parent_task_id for t in tasks if t.parent_task_id}
    limitations = [f"{t.plan_key} ({t.agent_name}) ended {t.status}"
                   + (f": {t.last_error[:200]}" if t.last_error else "")
                   for t in tasks if t.status != T.VERIFIED.value and t.id not in superseded
                   and t.agent_name != WRITER_AGENT]  # a missing summary is not a gap in the answer
    writer = next((t for t in tasks if t.agent_name == WRITER_AGENT and t.status == T.VERIFIED.value), None)
    summary = (writer.output_json or {}).get("summary") if writer else None
    summary_source = (writer.output_json or {}).get("summary_source") if writer else None
    not_found = [m for t in tasks if t.id not in superseded for m in (t.output_json or {}).get("not_found") or []]
    if not_found:  # values the agents looked for and did not find: said, never estimated
        limitations.append("Not found in the reports: " + ", ".join(dict.fromkeys(not_found)))
    live = [t.id for t in tasks if t.id not in superseded and t.agent_name != VERIFICATION_AGENT]
    for f in session.scalars(select(FindingRecord).where(
            FindingRecord.workflow_id == wf.id, FindingRecord.task_id.in_(live),
            FindingRecord.verification_status.in_(("rejected", "needs_review"))).order_by(FindingRecord.created_at)):
        last = session.scalar(select(VerificationResult).where(VerificationResult.finding_id == f.id)
                              .order_by(VerificationResult.verified_at.desc()).limit(1))
        why = f" ({last.reasons[0][:200]})" if last is not None and last.reasons else ""
        label = "Dropped after verification" if f.verification_status == "rejected" else "Not reviewed"
        limitations.append(f"{label}: {f.claim[:200]}{why}")
    return {"query": wf.query, "generated_at": utcnow().isoformat(), "summary": summary,
            "summary_source": summary_source, "verified_findings": findings,
            "limitations": limitations,
            "notice": "Decision support only: not a legal, financial or compliance determination."}


def _finish_root_span(session: Session, wf: Workflow) -> None:
    root = session.scalar(select(TraceSpan).where(TraceSpan.trace_id == wf.trace_id,
                                                  TraceSpan.span_id == root_span_id(wf)))
    if root is not None:
        root.ended_at = utcnow()
        root.duration_ms = (aware(root.ended_at) - aware(root.started_at)).total_seconds() * 1000
        root.status = "ok" if wf.status == W.COMPLETED.value else "error"
        root.outputs = {"status": wf.status,
                        "verified_findings": len((wf.report or {}).get("verified_findings", []))}


class ReviewNotAllowed(DocumentError):
    code = "review_not_allowed"
    http_status = 409


REVIEW_DECISIONS = ("approve", "reject", "rerun")


def review(session: Session, workflow_id: uuid.UUID, tenant_id: uuid.UUID, task_id: uuid.UUID, *,
           decision: str, note: str, reviewer: str) -> AgentTask:
    """A person decides on a task waiting in NEEDS_HUMAN_REVIEW (ARCHITECTURE.md §3.1):

    * ``approve``: its findings still awaiting a decision become verified; the task VERIFIED;
    * ``reject``: those findings become rejected; the task FAILED_FINAL;
    * ``rerun``: the task is QUEUED again with one more attempt. Only for a task without findings
      (e.g. blocked because a report was missing and has since been uploaded): re-running a task
      with findings would duplicate them; reject it instead.

    The decision is recorded on every finding it touches (``verification_results``), in the task's
    transitions and in the audit log. Call ``tick`` afterwards."""
    if decision not in REVIEW_DECISIONS:
        raise ReviewNotAllowed(f"decision must be one of {', '.join(REVIEW_DECISIONS)}")
    wf = session.scalar(select(Workflow).where(Workflow.id == workflow_id, Workflow.tenant_id == tenant_id)
                        .with_for_update())
    if wf is None:
        raise WorkflowNotFound("workflow not found")
    task = session.scalar(select(AgentTask).where(AgentTask.id == task_id, AgentTask.workflow_id == wf.id))
    if task is None:
        raise WorkflowNotFound("task not found in this workflow")
    if task.status != T.NEEDS_HUMAN_REVIEW.value:
        raise ReviewNotAllowed(f"task {task.plan_key} is {task.status}; only a task waiting for review can be reviewed")
    actor = f"reviewer:{reviewer}"[:100]
    note = note.strip()[:2000]
    reason = f"{decision} by {reviewer}" + (f": {note}" if note else "")
    open_findings = list(session.scalars(select(FindingRecord).where(
        FindingRecord.task_id == task.id, FindingRecord.verification_status.in_(("needs_review", "pending")))))
    if decision == "rerun":
        if _has_findings(session, task):
            raise ReviewNotAllowed("this task has findings: re-running it would duplicate them; approve or reject it")
        task.max_attempts = max(task.max_attempts, task.attempts + 1)
        task.next_attempt_at = None
        move_task(session, task, T.QUEUED, reason=reason, actor=actor)
    else:
        status = "verified" if decision == "approve" else "rejected"
        for f in open_findings:
            f.verification_status = status
            session.add(VerificationResult(finding_id=f.id, task_id=task.id, decision=status,
                                           reasons=[f"human review: {reason}"]))
        if decision == "approve":
            move_task(session, task, T.VERIFIED, reason=reason, actor=actor)
        else:
            task.last_error = f"rejected in review: {note or 'no reason given'}"[:4000]
            move_task(session, task, T.FAILED_FINAL, reason=reason, actor=actor)
    session.add(AuditEvent(tenant_id=tenant_id, actor=actor, action="workflow.review",
                           resource=f"workflow:{wf.id}",
                           details={"task": task.plan_key, "task_id": str(task.id), "decision": decision,
                                    "note": note, "findings": len(open_findings)}))
    session.commit()
    return task


def cancel(session: Session, workflow_id: uuid.UUID, tenant_id: uuid.UUID, *, actor: str = "user") -> Workflow:
    wf = session.scalar(select(Workflow).where(Workflow.id == workflow_id, Workflow.tenant_id == tenant_id)
                        .with_for_update())
    if wf is None:
        raise WorkflowNotFound("workflow not found")
    for t in _tasks(session, wf):
        if TaskStatus(t.status) not in TASK_TERMINAL:
            move_task(session, t, T.CANCELLED, reason="workflow cancelled", actor=actor)
            _release(t)
    move_workflow(session, wf, W.CANCELLED, reason="cancelled by the user", actor=actor)
    _finish_root_span(session, wf)
    session.commit()
    return wf
