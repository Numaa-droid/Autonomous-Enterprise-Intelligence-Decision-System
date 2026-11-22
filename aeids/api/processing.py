"""Start processing (TableLens parse, then indexing) for an uploaded version, per ``jobs_mode``."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from aeids.documents import DocumentError
from aeids.ingestion.tablelens import TableLensError
from aeids.jobs import JobQueueUnavailable, enqueue_process_version, make_tablelens, run_process_version
from aeids.llm.embedding import Embedder, OpenRouterEmbedder
from aeids.llm.openrouter import LLMError
from aeids.retrieval.vector_index import VectorIndex, VectorIndexError
from aeids.storage import ObjectStore

log = logging.getLogger(__name__)


def tablelens_client(state: Any):
    if getattr(state, "tablelens", None) is None:
        state.tablelens = make_tablelens(state.settings)
    return state.tablelens


def vector_index(state: Any) -> VectorIndex:
    if getattr(state, "vector_index", None) is None:
        state.vector_index = VectorIndex.from_url(state.settings.qdrant_url)
    return state.vector_index


def embedder(state: Any) -> Embedder:
    if getattr(state, "embedder", None) is None:
        from aeids.retrieval.query_cache import SqlQueryCache

        state.embedder = OpenRouterEmbedder.from_settings(state.settings,
                                                          cache=SqlQueryCache(state.engine))
    return state.embedder


def schedule_processing(
    state: Any,
    session: Session,
    store: ObjectStore,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    reuse_by_filename: bool = False,
    reparse: bool = False,
) -> tuple[str, str | None]:
    """-> (processing state, job id). States: queued, already_queued, not_queued (queue down;
    ``python -m aeids.worker requeue`` picks it up later), done, failed (inline), off."""
    settings = state.settings
    if settings.jobs_mode == "off":
        return "off", None
    if settings.jobs_mode == "inline":
        try:
            run_process_version(session, store, tablelens_client(state), vector_index(state),
                                embedder(state), tenant_id, version_id,
                                reuse_by_filename=reuse_by_filename, reparse=reparse)
            return "done", None
        except (TableLensError, LLMError, VectorIndexError, DocumentError) as exc:
            log.warning("inline processing of %s failed: %s", version_id, exc)
            return "failed", None
    try:
        job_id, new = enqueue_process_version(settings, tenant_id, version_id,
                                              reuse_by_filename=reuse_by_filename,
                                              reparse=reparse, queue=getattr(state, "queue", None))
    except JobQueueUnavailable as exc:
        log.warning("version %s not queued: %s", version_id, exc)
        return "not_queued", None
    return ("queued" if new else "already_queued"), job_id


def registry(state: Any):
    if getattr(state, "registry", None) is None:
        from aeids.agents.registry import build_registry

        state.registry = build_registry(state.settings)
    return state.registry


def resume_workflow(state: Any, workflow_id: uuid.UUID, queued_task: uuid.UUID | None = None) -> str:
    """Continue a workflow after a human review; ``queued_task`` is a task the review re-queued
    (a tick only enqueues tasks it moves itself)."""
    from aeids.orchestration.jobs import enqueue_task

    if state.settings.jobs_mode == "queue" and queued_task is not None:
        try:
            enqueue_task(state.settings, queued_task, queue=getattr(state, "queue", None))
        except JobQueueUnavailable as exc:
            log.warning("task %s not queued: %s", queued_task, exc)
            return "not_queued"
    return schedule_workflow(state, workflow_id)


def schedule_workflow(state: Any, workflow_id: uuid.UUID) -> str:
    """Start a planned workflow per ``jobs_mode``: queued (worker runs it), done (inline), off,
    or not_queued (queue down; a worker resumes it when it starts)."""
    from aeids.orchestration import runner
    from aeids.orchestration.jobs import enqueue_tick

    settings = state.settings
    if settings.jobs_mode == "off":
        return "off"
    if settings.jobs_mode == "inline":
        return runner.drive(state.sessionmaker, workflow_id, registry=registry(state), settings=settings,
                            index=vector_index(state), embedder=embedder(state))
    try:
        enqueue_tick(settings, workflow_id, queue=getattr(state, "queue", None))
    except JobQueueUnavailable as exc:
        log.warning("workflow %s not queued: %s", workflow_id, exc)
        return "not_queued"
    return "queued"
