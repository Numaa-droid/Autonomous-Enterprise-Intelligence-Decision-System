"""Workflow steps as RQ jobs (D-004, D-024): a tick job moves the workflow and queues a run job
per task it made QUEUED; a run job runs one task and queues the next tick. A tick also schedules
itself for the next lease expiry or retry time, and ``recover`` (run when a worker starts) ticks
every unfinished workflow, so a killed worker's task is picked up again once its lease expires.
Duplicate jobs are harmless: claiming a task is atomic and ticks are idempotent.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from aeids import tracing
from aeids.agents.stubs import default_registry
from aeids.config import Settings, get_settings
from aeids.db.models import AgentTask, TaskStatus, Workflow, WorkflowStatus
from aeids.db.session import make_engine, make_sessionmaker
from aeids.jobs import QUEUE_NAME, JobQueueUnavailable
from aeids.llm.embedding import OpenRouterEmbedder
from aeids.orchestration import runner
from aeids.orchestration import supervisor as sup
from aeids.retrieval.query_cache import SqlQueryCache
from aeids.retrieval.vector_index import VectorIndex

log = logging.getLogger(__name__)
ACTIVE_WORKFLOW = (WorkflowStatus.RUNNING.value, WorkflowStatus.NEEDS_HUMAN_REVIEW.value)


def _queue(settings: Settings, queue: Any = None):
    if queue is not None:
        return queue
    from redis import Redis
    from rq import Queue

    return Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))


def enqueue_tick(settings: Settings, workflow_id: uuid.UUID, *, at: datetime | None = None,
                 queue: Any = None) -> None:
    from redis.exceptions import RedisError

    try:
        q = _queue(settings, queue)
        if at is None:
            q.enqueue(workflow_tick_job, str(workflow_id), result_ttl=3600, failure_ttl=86400)
        else:
            q.enqueue_at(at, workflow_tick_job, str(workflow_id), result_ttl=3600, failure_ttl=86400)
    except RedisError as exc:
        raise JobQueueUnavailable(f"job queue unavailable: {type(exc).__name__}") from None


def enqueue_task(settings: Settings, task_id: uuid.UUID, *, queue: Any = None) -> None:
    from redis.exceptions import RedisError

    try:
        _queue(settings, queue).enqueue(
            run_task_job, str(task_id), job_timeout=settings.task_timeout_seconds + 60,
            result_ttl=3600, failure_ttl=86400)
    except RedisError as exc:
        raise JobQueueUnavailable(f"job queue unavailable: {type(exc).__name__}") from None


class _Env:
    """What a job needs, built once per job and closed after."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.engine = make_engine(self.settings.database_url)
        if self.settings.tracing_enabled:
            tracing.configure(tracing.SqlTraceSink(self.engine))
        self.sessions = make_sessionmaker(self.engine)
        self.index = VectorIndex.from_url(self.settings.qdrant_url)
        self.embedder = OpenRouterEmbedder.from_settings(self.settings, cache=SqlQueryCache(self.engine))
        self.registry = default_registry()

    def close(self) -> None:
        self.embedder.client.close()
        self.index.close()
        self.engine.dispose()


def workflow_tick_job(workflow_id: str) -> dict[str, Any]:
    env = _Env()
    try:
        with env.sessions() as session:
            result = sup.tick(session, uuid.UUID(workflow_id), settings=env.settings)
        for task_id in result.queued:
            enqueue_task(env.settings, task_id)
        if result.wake_at is not None and result.status in ACTIVE_WORKFLOW:
            enqueue_tick(env.settings, uuid.UUID(workflow_id), at=result.wake_at)
        return {"status": result.status, "queued": [str(t) for t in result.queued]}
    finally:
        env.close()


def run_task_job(task_id: str) -> dict[str, Any]:
    env = _Env()
    try:
        outcome = runner.run_task(env.sessions, uuid.UUID(task_id), registry=env.registry,
                                  settings=env.settings, index=env.index, embedder=env.embedder)
        with env.sessions() as session:
            workflow_id = session.scalar(select(AgentTask.workflow_id).where(AgentTask.id == uuid.UUID(task_id)))
        if workflow_id is not None:
            enqueue_tick(env.settings, workflow_id)
        return {"outcome": outcome}
    finally:
        env.close()


def recover(settings: Settings | None = None) -> int:
    """Tick every unfinished workflow and queue its runnable tasks (worker start-up). Returns how
    many workflows were looked at."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    try:
        sessions = make_sessionmaker(engine)
        with sessions() as session:
            ids = list(session.scalars(select(Workflow.id).where(Workflow.status.in_(ACTIVE_WORKFLOW))))
        for wid in ids:
            with sessions() as session:
                result = sup.tick(session, wid, settings=settings)
                queued = result.queued + list(session.scalars(select(AgentTask.id).where(
                    AgentTask.workflow_id == wid, AgentTask.status == TaskStatus.QUEUED.value)))
            for task_id in dict.fromkeys(queued):
                enqueue_task(settings, task_id)
            if result.wake_at is not None:
                enqueue_tick(settings, wid, at=result.wake_at)
        return len(ids)
    finally:
        engine.dispose()
