"""Background jobs (RQ on Redis, D-004).

``process_version``: an uploaded version is sent to TableLens (the ``tablelens/``
server) for parsing, the ``_structure.json`` it returns is validated, normalised and chunked
(D-019), then the chunks are embedded and indexed in Qdrant, and the version becomes current
(``aeids/ingestion/indexing.py``). A version that is already parsed is only indexed.

State lives in Postgres (``parser_status``), not in the queue (rule 5). Retries: RQ retries
only transient failures (TableLens, OpenRouter or Qdrant down/restarted, timeouts); a failed
parse, an invalid result or a refused API key marks the version ``failed`` and is not retried.
Before parsing, an existing TableLens result for the same file (name + checksum) is reused, and
a retry after a parse skips parsing, so a retry never redoes GPU work.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.config import Settings, get_settings
from aeids.db.models import DocumentVersion, ParserStatus
from aeids.db.session import make_engine, make_sessionmaker
from aeids.documents import DocumentError, DocumentNotFound
from aeids.ingestion.indexing import IndexResult, index_version
from aeids.ingestion.pipeline import IngestResult, finish_parsing, mark_failed, start_parsing
from aeids.ingestion.tablelens import TableLensClient, TableLensError, tablelens_stem
from aeids.llm.embedding import Embedder, OpenRouterEmbedder
from aeids.llm.openrouter import LLMError
from aeids.retrieval.vector_index import VectorIndex, VectorIndexError
from aeids.storage import LocalStore, ObjectStore

log = logging.getLogger(__name__)

QUEUE_NAME = "aeids"
RETRY_INTERVALS = [60, 300, 900]  # seconds; transient failures only
ACTIVE = {"queued", "started", "scheduled", "deferred"}
# TableLens also writes the medical reference JSON after recognition (not AEIDS, D-010)
IGNORED_TABLELENS_LOGS = ("Reference JSON",)


class JobQueueUnavailable(Exception):
    pass


def make_tablelens(settings: Settings) -> TableLensClient:
    return TableLensClient(
        settings.tablelens_url,
        poll_seconds=settings.tablelens_poll_seconds,
        job_timeout_seconds=settings.tablelens_job_timeout_seconds,
    )


def job_id_for(version_id: uuid.UUID) -> str:
    return f"process-{version_id}"


@dataclass(frozen=True)
class ProcessResult:
    version: DocumentVersion
    parsed: IngestResult | None  # None: it was parsed before (only indexed now)
    indexed: IndexResult | None  # None: already indexed, nothing to do


def needs_parsing(version: DocumentVersion) -> bool:
    """Pending, or failed before a parse ever succeeded."""
    return version.parser_status == ParserStatus.PENDING.value or (
        version.parser_status == ParserStatus.FAILED.value and version.parsed_at is None)


def run_process_version(
    session: Session,
    store: ObjectStore,
    tablelens: TableLensClient,
    index: VectorIndex,
    embedder: Embedder,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    reuse_by_filename: bool = False,
    reparse: bool = False,
) -> ProcessResult:
    """Parse when needed (or asked: ``reparse``), then index. Raises TableLensError, LLMError or
    VectorIndexError (``retryable`` says whether a retry may help) or DocumentError; the version
    is ``failed`` with the reason in those cases."""
    version = session.scalar(select(DocumentVersion).where(
        DocumentVersion.id == version_id, DocumentVersion.tenant_id == tenant_id))
    if version is None:
        raise DocumentNotFound("document version not found")
    if version.parser_status == ParserStatus.INDEXED.value and not reparse:
        return ProcessResult(version, None, None)
    with tracing.span("process_version", "job", tenant_id=tenant_id, inputs={
            "version_id": version_id, "filename": version.filename,
            "status": version.parser_status, "reparse": reparse}) as sp:
        parsed = None
        if reparse or needs_parsing(version):
            parsed = parse_version(session, store, tablelens, tenant_id, version_id,
                                   reuse_by_filename=reuse_by_filename)
        indexed = index_version(session, index, embedder, tenant_id, version_id)
        result = ProcessResult(indexed.version, parsed, indexed)
        sp.set_output(summary(result))
        return result


def parse_version(
    session: Session,
    store: ObjectStore,
    tablelens: TableLensClient,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    reuse_by_filename: bool = False,
) -> IngestResult:
    """pending/failed/parsed -> parsing -> (TableLens) -> parsed | failed."""
    with tracing.span("parse_version", "chain", inputs={"version_id": version_id}) as sp:
        result = _parse_version(session, store, tablelens, tenant_id, version_id,
                                reuse_by_filename=reuse_by_filename)
        sp.set_output({"chunks": result.chunk_count, "tables": result.table_count,
                       "financial_tables": result.financial_table_count,
                       "warnings": result.warnings})
        return result


def _parse_version(session: Session, store: ObjectStore, tablelens: TableLensClient,
                   tenant_id: uuid.UUID, version_id: uuid.UUID, *,
                   reuse_by_filename: bool) -> IngestResult:
    version = start_parsing(session, tenant_id, version_id)
    stem = tablelens_stem(version.filename, version.checksum)
    warnings: list[str] = []
    try:
        with tracing.span("tablelens", "tool", inputs={"stem": stem, "file": version.filename},
                          url=tablelens.base_url) as tl_span:
            raw = _fetch_structure(tablelens, store, version, stem, reuse_by_filename, warnings)
            tl_span.set_output({"bytes": len(raw), "warnings": warnings})
    except TableLensError as exc:
        mark_failed(session, version_id, exc.message)
        raise
    with tracing.span("normalise_and_chunk", "parser", inputs={"bytes": len(raw)}) as ch:
        result = finish_parsing(session, store, version, raw, extra_warnings=warnings)
        ch.set_output({"chunks": result.chunk_count, "tables": result.table_count})
        return result


def _fetch_structure(tablelens: TableLensClient, store: ObjectStore, version: DocumentVersion,
                     stem: str, reuse_by_filename: bool, warnings: list[str]) -> bytes:
    """The TableLens result for this file: reused if TableLens already has it, else parsed."""
    raw = tablelens.existing_structure(stem)
    if raw is not None:
        warnings.append("TableLens had already parsed this file; its result was reused")
        return raw
    if reuse_by_filename:
        legacy = Path(stem).stem.rsplit("-", 1)[0]
        raw = tablelens.existing_structure(legacy)
        if raw is not None:
            warnings.append(f"reused the TableLens result made earlier for a file named "
                            f"{legacy}.pdf (matched by file name; page count checked)")
            return raw
    version_id = version.id

    def progress(ev: dict[str, Any]) -> None:
        if ev.get("type") in ("summary", "rsummary"):
            log.info("version %s: TableLens %s done", version_id, ev["type"])

    with tracing.span("tablelens.detect_and_recognise", "tool", inputs={"stem": stem}):
        raw, tl_warnings = tablelens.parse(store.path_for(version.storage_uri), stem,
                                           on_event=progress)
    warnings += [f"TableLens: {w}" for w in tl_warnings
                 if not w.startswith(IGNORED_TABLELENS_LOGS)][:20]
    return raw


def process_version_job(tenant_id: str, version_id: str, reuse_by_filename: bool = False,
                        reparse: bool = False) -> dict[str, Any]:
    """RQ entry point. Returns a small summary; raises only for retryable failures."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    if settings.tracing_enabled:
        tracing.configure(tracing.SqlTraceSink(engine))
    tablelens = make_tablelens(settings)
    index = VectorIndex.from_url(settings.qdrant_url)
    embedder = OpenRouterEmbedder.from_settings(settings)
    try:
        with make_sessionmaker(engine)() as session:
            result = run_process_version(
                session, LocalStore(settings.storage_root), tablelens, index, embedder,
                uuid.UUID(tenant_id), uuid.UUID(version_id),
                reuse_by_filename=reuse_by_filename, reparse=reparse,
            )
            return summary(result) | {"embedding_tokens": embedder.usage.total_tokens,
                                      "embedding_cost_usd": str(embedder.usage.cost)}
    except (TableLensError, LLMError, VectorIndexError) as exc:
        if exc.retryable:
            raise  # RQ retries with backoff; the version shows failed + reason meanwhile
        return {"status": "failed", "error": exc.message}
    except DocumentError as exc:
        return {"status": "failed", "error": exc.message}
    finally:
        embedder.client.close()
        index.close()
        tablelens.close()
        engine.dispose()


def summary(result: ProcessResult) -> dict[str, Any]:
    out: dict[str, Any] = {"status": result.version.parser_status}
    if result.parsed:
        out |= {"chunks": result.parsed.chunk_count, "tables": result.parsed.table_count,
                "warnings": result.parsed.warnings}
    if result.indexed:
        out |= {"indexed_points": result.indexed.point_count,
                "collection": result.indexed.collection,
                "became_current": result.indexed.became_current}
    return out


def _queue(settings: Settings):
    from redis import Redis
    from rq import Queue

    return Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))


def job_is_active(queue: Any, version_id: uuid.UUID) -> bool:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        job = Job.fetch(job_id_for(version_id), connection=queue.connection)
    except NoSuchJobError:
        return False
    return job.get_status(refresh=True) in ACTIVE


def enqueue_process_version(
    settings: Settings,
    tenant_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    reuse_by_filename: bool = False,
    reparse: bool = False,
    queue: Any = None,
) -> tuple[str, bool]:
    """Queue ``process_version`` once per version. Returns (job id, newly queued); an active
    job for the version is not duplicated."""
    from redis.exceptions import RedisError
    from rq import Retry

    job_id = job_id_for(version_id)
    try:
        queue = queue or _queue(settings)
        if job_is_active(queue, version_id):
            return job_id, False
        queue.enqueue(
            process_version_job, str(tenant_id), str(version_id), reuse_by_filename, reparse,
            job_id=job_id,
            retry=Retry(max=len(RETRY_INTERVALS), interval=RETRY_INTERVALS),
            job_timeout=int(settings.tablelens_job_timeout_seconds) + 600,
            result_ttl=7 * 86400,
            failure_ttl=30 * 86400,
        )
    except RedisError as exc:
        raise JobQueueUnavailable(f"job queue unavailable: {type(exc).__name__}") from None
    return job_id, True


def versions_to_requeue(session: Session) -> list[DocumentVersion]:
    """Versions a lost worker or a queue outage may have left behind (parsed: indexing never
    started)."""
    return list(session.scalars(
        select(DocumentVersion).where(DocumentVersion.parser_status.in_(
            ("pending", "parsing", "parsed", "indexing", "failed")))
    ))
