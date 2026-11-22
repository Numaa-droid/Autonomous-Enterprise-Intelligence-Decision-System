"""Workers run tasks; the Supervisor decides what happens next (rule 4).

``run_task``: (1) claim: QUEUED -> RUNNING with a lease (``timeout_seconds``) and attempt + 1,
by a conditional update so two workers can never both claim it; (2) run the agent outside any
database transaction, with the task's trace span; (3) hand the result (or the error) to the
Supervisor, which accepts it only if this worker still holds the lease. If the worker dies
between (1) and (3), the lease expires and the Supervisor's tick returns the task to QUEUED;
VERIFIED tasks are never claimed again, so finished work is never redone.

``drive`` runs a workflow to the end in this process (tests, ``jobs_mode=inline``); the job
queue runs the same steps across worker processes (``jobs.py``).
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from aeids import tracing
from aeids.agents.base import AgentContext, Registry
from aeids.config import Settings
from aeids.contracts import AgentResult, TaskConstraints, TaskMessage
from aeids.db.models import AgentTask, TaskDependency, TaskStatus, TaskTransition, Workflow, utcnow
from aeids.llm.embedding import Embedder
from aeids.llm.openrouter import LLMError
from aeids.orchestration import supervisor as sup
from aeids.orchestration.states import WORKFLOW_TERMINAL, move_task
from aeids.retrieval.vector_index import VectorIndex, VectorIndexError

log = logging.getLogger(__name__)
T = TaskStatus
Clock = Callable[[], datetime]


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(session: Session, task_id: uuid.UUID, worker: str, now: datetime) -> AgentTask | None:
    """QUEUED -> RUNNING for exactly one worker. Returns the task, or None if someone else had it
    or it is not queued (e.g. already done)."""
    task = session.get(AgentTask, task_id)
    if task is None or task.status != T.QUEUED.value:
        return None
    result = session.execute(
        update(AgentTask)
        .where(AgentTask.id == task_id, AgentTask.status == T.QUEUED.value,
               AgentTask.attempts == task.attempts)
        .values(status=T.RUNNING.value, attempts=task.attempts + 1, lease_owner=worker,
                lease_expires_at=now + timedelta(seconds=task.timeout_seconds), updated_at=now)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        session.rollback()
        return None
    session.add(TaskTransition(task_id=task_id, from_status=T.QUEUED.value, to_status=T.RUNNING.value,
                               reason=f"claimed, attempt {task.attempts + 1}", actor=worker))
    session.commit()
    session.refresh(task)
    if task.attempts > task.max_attempts:  # lost leases count as attempts
        move_task(session, task, T.FAILED_FINAL, reason="attempts exhausted (worker lost too often)",
                  actor=sup.ACTOR)
        sup._release(task)
        session.commit()
        return None
    return task


def build_message(session: Session, task: AgentTask) -> TaskMessage:
    """The task with the minimum context: its inputs plus what its dependencies produced."""
    inputs = dict(task.input_json or {})
    deps = list(session.scalars(select(AgentTask).join(
        TaskDependency, TaskDependency.depends_on_task_id == AgentTask.id)
        .where(TaskDependency.task_id == task.id)))
    for d in deps:
        out = d.output_json or {}
        for key in ("evidence_ids", "sources", "facts", "not_found"):
            if key in out:
                inputs[key] = [*inputs.get(key, []), *out[key]]
    if task.attempts > 1 and task.last_error:  # a retry sees why the last attempt failed (rule 8)
        inputs["previous_error"] = task.last_error[:1000]
    return TaskMessage(
        workflow_id=task.workflow_id, task_id=task.id, parent_task_id=task.parent_task_id,
        rework_round=task.rework_round, idempotency_key=task.idempotency_key,
        tenant_id=task.tenant_id, assigned_agent=task.agent_name, objective=task.objective,
        inputs=inputs, dependencies=[d.id for d in deps],
        acceptance_criteria=list(task.acceptance_criteria or []),
        constraints=TaskConstraints(max_attempts=task.max_attempts, timeout_seconds=task.timeout_seconds),
        deadline=task.deadline, trace_id=task.trace_id, attempt=task.attempts)


def _usage_from_metrics(info: sup.RunInfo, metrics: dict) -> None:
    """Model, tokens and cost an agent reports in its metrics, for ``agent_runs``."""
    if isinstance(metrics.get("total_tokens"), int):
        info.total_tokens = metrics["total_tokens"]
    if metrics.get("cost_usd") is not None:
        try:
            info.cost_usd = Decimal(str(metrics["cost_usd"]))
        except InvalidOperation:
            pass
    if isinstance(metrics.get("llm_model"), str):
        info.model = metrics["llm_model"][:200]


def run_task(sessions: sessionmaker, task_id: uuid.UUID, *, registry: Registry, settings: Settings,
             index: VectorIndex | None, embedder: Embedder | None, worker: str | None = None,
             clock: Clock = utcnow, crash_after_claim: bool = False) -> str:
    """Claim, run, report. Returns what happened: claimed-elsewhere | done | discarded | crashed.
    ``crash_after_claim`` simulates a worker that dies mid-task (tests)."""
    worker = worker or worker_id()
    with sessions() as session:
        task = claim(session, task_id, worker, clock())
        if task is None:
            return "claimed-elsewhere"
        message = build_message(session, task)
        wf = session.get(Workflow, task.workflow_id)
        trace_id, parent = wf.trace_id, sup.root_span_id(wf)
        tenant_id, plan_key = task.tenant_id, task.plan_key
        session.commit()
    if crash_after_claim:
        return "crashed"

    agent = registry.get(message.assigned_agent)
    info = sup.RunInfo(worker=worker, agent_version=agent.version, model=agent.model,
                       prompt_version=agent.prompt_version, started_at=clock())
    result: AgentResult | None = None
    failure: tuple[str, str] | None = None
    t0 = time.perf_counter()
    with tracing.span(f"task:{plan_key}", "job", tenant_id=tenant_id, trace_id=trace_id,
                      parent_span_id=parent, agent=agent.name, attempt=message.attempt,
                      inputs={"objective": message.objective, "agent": agent.name,
                              "attempt": message.attempt, "rework_round": message.rework_round}) as sp:
        try:
            with sessions() as read_session:
                ctx = AgentContext(read_session, tenant_id, settings, index, embedder)
                raw = agent.run(message, ctx)
            result = AgentResult.model_validate(raw.model_dump(mode="json"))  # never trust the type
            _usage_from_metrics(info, result.metrics)
            sp.set_output({"status": result.status, "findings": len(result.findings),
                           "evidence": len(result.evidence), "warnings": result.warnings})
        except ValidationError as exc:
            failure = ("schema", f"agent output failed validation: {exc.errors()[:3]}")
        except PermissionError as exc:
            failure = ("permission", f"permission denied: {exc}")
        except (LLMError, VectorIndexError) as exc:
            failure = ("transient" if exc.retryable else "final", exc.message)
        except Exception as exc:  # noqa: BLE001 — unknown errors: retried, bounded by max_attempts
            log.exception("task %s failed", task_id)
            failure = ("transient", f"{type(exc).__name__}: {exc}")
        if failure:
            sp.fail(failure[1][:2000])
    info.latency_ms = int((time.perf_counter() - t0) * 1000)

    with sessions() as session:
        task = session.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
        try:
            if result is not None:
                ok = sup.accept_result(session, task, result, info, settings=settings, now=clock())
            else:
                ok = sup.record_failure(session, task, failure[0], failure[1], info,
                                        settings=settings, now=clock())
            session.commit()
        except sup.ContractViolation as exc:
            session.rollback()
            task = session.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update())
            ok = sup.record_failure(session, task, "schema", f"result breaks the contract: {exc}", info,
                                    settings=settings, now=clock())
            session.commit()
    return "done" if ok else "discarded"


def drive(sessions: sessionmaker, workflow_id: uuid.UUID, *, registry: Registry, settings: Settings,
          index: VectorIndex | None, embedder: Embedder | None, clock: Clock = utcnow,
          max_steps: int = 200, max_wait_seconds: float = 300) -> str:
    """Run a workflow to a terminal (or human-review) state in this process."""
    waited = 0.0
    for _ in range(max_steps):
        with sessions() as session:
            r = sup.tick(session, workflow_id, settings=settings, now=clock())
        if r.status in {s.value for s in WORKFLOW_TERMINAL} or r.status == "NEEDS_HUMAN_REVIEW":
            return r.status
        if r.queued:
            for task_id in r.queued:
                run_task(sessions, task_id, registry=registry, settings=settings, index=index,
                         embedder=embedder, clock=clock)
            continue
        with sessions() as session:  # anything queued earlier but not run (e.g. after a restart)?
            pending = list(session.scalars(select(AgentTask.id).where(
                AgentTask.workflow_id == workflow_id, AgentTask.status == T.QUEUED.value)))
        if pending:
            for task_id in pending:
                run_task(sessions, task_id, registry=registry, settings=settings, index=index,
                         embedder=embedder, clock=clock)
            continue
        if r.wake_at is None:
            return r.status
        delay = max(0.0, (r.wake_at - clock()).total_seconds())
        if waited + delay > max_wait_seconds:
            return r.status
        time.sleep(delay)
        waited += delay
    return r.status
