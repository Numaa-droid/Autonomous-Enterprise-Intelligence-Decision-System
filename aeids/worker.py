"""Background worker and queue tools.

    python -m aeids.worker               # run the worker (needs Redis, TableLens, the database)
    python -m aeids.worker requeue       # queue every version left pending / parsed / failed / stuck
    python -m aeids.worker status        # TableLens and queue reachability, versions by status
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter

from redis.exceptions import RedisError

from aeids.config import get_settings
from aeids.db.models import DocumentVersion
from aeids.db.session import make_engine, make_sessionmaker
from aeids.ingestion.pipeline import mark_failed
from aeids.ingestion.tablelens import TableLensError
from aeids.jobs import (
    QUEUE_NAME,
    JobQueueUnavailable,
    _queue,
    enqueue_process_version,
    job_is_active,
    make_tablelens,
    versions_to_requeue,
)


def run_worker() -> int:
    from rq import SimpleWorker, Worker

    settings = get_settings()
    queue = _queue(settings)
    # macOS: forking after Objective-C runtime use crashes; run jobs in the worker process there
    worker_cls = SimpleWorker if sys.platform == "darwin" else Worker
    worker = worker_cls([queue], connection=queue.connection)
    print(f"AEIDS worker on queue {QUEUE_NAME!r} · TableLens {settings.tablelens_url}")
    try:  # pick up workflows a previous worker left unfinished (expired leases, retries)
        from aeids.orchestration.jobs import recover

        print(f"resumed {recover(settings)} unfinished workflow(s)")
    except Exception as exc:  # noqa: BLE001 — the worker still starts; ticks will catch up
        print(f"could not resume workflows yet: {type(exc).__name__}")
    worker.work(with_scheduler=True)
    return 0


def requeue() -> int:
    settings = get_settings()
    sessions = make_sessionmaker(make_engine(settings.database_url))
    n = 0
    try:
        queue = _queue(settings)
        with sessions() as session:
            for v in versions_to_requeue(session):
                if job_is_active(queue, v.id):
                    continue  # a worker has it, or it waits in the queue
                was = v.parser_status
                if was in ("parsing", "indexing"):  # no active job: its worker was lost
                    mark_failed(session, v.id, f"interrupted while {was} (worker lost); "
                                               "queued again")
                job_id, _ = enqueue_process_version(settings, v.tenant_id, v.id, queue=queue)
                n += 1
                print(f"queued {job_id} ({v.filename}, was {was})")
    except (JobQueueUnavailable, RedisError) as exc:
        print(f"error: job queue unavailable ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"{n} version(s) queued")
    return 0


def status() -> int:
    settings = get_settings()
    try:
        with make_tablelens(settings) as tl:
            h = tl.health()
        print(f"TableLens {settings.tablelens_url}: ok · GPU server {h.get('gpu_server')} · "
              f"PaddleOCR-VL {h.get('vlm')} · jobs {h.get('jobs')}")
    except TableLensError as exc:
        print(f"TableLens {settings.tablelens_url}: {exc.message}")
    try:
        q = _queue(settings)
        print(f"queue {QUEUE_NAME!r}: {q.count} waiting")
    except Exception as exc:  # noqa: BLE001
        print(f"queue: unavailable ({type(exc).__name__})")
    sessions = make_sessionmaker(make_engine(settings.database_url))
    with sessions() as session:
        counts = Counter(v.parser_status for v in session.query(DocumentVersion))
    print("versions:", dict(counts) or "none")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m aeids.worker")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "requeue", "status"])
    args = ap.parse_args(argv)
    return {"run": run_worker, "requeue": requeue, "status": status}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
