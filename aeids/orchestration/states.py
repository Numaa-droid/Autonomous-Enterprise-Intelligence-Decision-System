"""Task and workflow state machines (ARCHITECTURE.md §3.1, D-011).

Only the transitions listed here are legal; anything else raises ``IllegalTransition`` and
nothing is written. Every task change is persisted as a ``task_transitions`` row and every
workflow change as an ``audit_events`` row, with reason and actor (rule 5), in the caller's
transaction.
"""

from __future__ import annotations

from aeids.db.models import (
    AgentTask,
    AuditEvent,
    TaskStatus,
    TaskTransition,
    Workflow,
    WorkflowStatus,
    utcnow,
)
from aeids.documents import DocumentError

T, W = TaskStatus, WorkflowStatus

TASK_TERMINAL = frozenset({T.VERIFIED, T.REJECTED, T.FAILED_FINAL, T.CANCELLED})
WORKFLOW_TERMINAL = frozenset({W.COMPLETED, W.FAILED, W.CANCELLED})

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    T.CREATED: frozenset({T.PLANNED}),
    T.PLANNED: frozenset({T.QUEUED, T.BLOCKED}),
    T.QUEUED: frozenset({T.RUNNING}),
    T.RUNNING: frozenset({T.SUCCEEDED, T.FAILED_RETRYABLE, T.FAILED_FINAL, T.BLOCKED,
                          T.NEEDS_HUMAN_REVIEW, T.QUEUED}),  # QUEUED: lease expired
    T.FAILED_RETRYABLE: frozenset({T.QUEUED, T.FAILED_FINAL}),
    T.SUCCEEDED: frozenset({T.VERIFYING, T.VERIFIED}),
    T.VERIFYING: frozenset({T.VERIFIED, T.REJECTED, T.NEEDS_HUMAN_REVIEW}),
    T.BLOCKED: frozenset({T.QUEUED, T.NEEDS_HUMAN_REVIEW, T.FAILED_FINAL}),
    T.NEEDS_HUMAN_REVIEW: frozenset({T.QUEUED, T.VERIFIED, T.FAILED_FINAL}),
    T.VERIFIED: frozenset(),
    T.REJECTED: frozenset(),
    T.FAILED_FINAL: frozenset(),
    T.CANCELLED: frozenset(),
}
# any non-terminal task can be cancelled with its workflow
for _s in TaskStatus:
    if _s not in TASK_TERMINAL:
        TASK_TRANSITIONS[_s] = TASK_TRANSITIONS[_s] | {T.CANCELLED}

WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    W.CREATED: frozenset({W.PLANNING}),
    W.PLANNING: frozenset({W.RUNNING, W.FAILED}),
    W.RUNNING: frozenset({W.NEEDS_HUMAN_REVIEW, W.REPORTING, W.FAILED}),
    W.NEEDS_HUMAN_REVIEW: frozenset({W.RUNNING}),
    W.REPORTING: frozenset({W.COMPLETED, W.FAILED}),
    W.COMPLETED: frozenset(),
    W.FAILED: frozenset(),
    W.CANCELLED: frozenset(),
}
for _w in WorkflowStatus:
    if _w not in WORKFLOW_TERMINAL:
        WORKFLOW_TRANSITIONS[_w] = WORKFLOW_TRANSITIONS[_w] | {W.CANCELLED}


class IllegalTransition(DocumentError):
    code = "illegal_transition"
    http_status = 409


def move_task(session, task: AgentTask, to: TaskStatus, *, reason: str, actor: str) -> None:
    """Change a task's state (only the Supervisor, a claiming worker or lease recovery call
    this). Raises IllegalTransition without writing anything."""
    current = TaskStatus(task.status)
    if to not in TASK_TRANSITIONS[current]:
        raise IllegalTransition(f"task {task.plan_key}: {current.value} -> {to.value} is not allowed")
    task.status = to.value
    task.updated_at = utcnow()
    session.add(TaskTransition(task_id=task.id, from_status=current.value, to_status=to.value,
                               reason=reason[:2000], actor=actor))


def move_workflow(session, workflow: Workflow, to: WorkflowStatus, *, reason: str,
                  actor: str = "supervisor") -> None:
    current = WorkflowStatus(workflow.status)
    if to not in WORKFLOW_TRANSITIONS[current]:
        raise IllegalTransition(f"workflow: {current.value} -> {to.value} is not allowed")
    workflow.status = to.value
    workflow.updated_at = utcnow()
    if to in WORKFLOW_TERMINAL:
        workflow.completed_at = utcnow()
    session.add(AuditEvent(tenant_id=workflow.tenant_id, actor=actor, action="workflow.transition",
                           resource=f"workflow:{workflow.id}",
                           details={"from": current.value, "to": to.value, "reason": reason[:2000]}))
