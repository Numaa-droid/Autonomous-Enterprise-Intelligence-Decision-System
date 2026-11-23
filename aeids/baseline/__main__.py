"""Ask the single-agent RAG baseline one question from the command line.

    .venv/bin/python -m aeids.baseline "What were Apple's total net sales in fiscal 2025?" --tenant eval
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aeids.baseline")
    ap.add_argument("question")
    ap.add_argument("--tenant", default=None, help="tenant name (default: the configured default tenant)")
    ap.add_argument("--top-k", type=int, default=None)
    args = ap.parse_args(argv)

    from sqlalchemy import select

    from aeids import tracing
    from aeids.baseline.rag import DEFAULT_TOP_K, answer
    from aeids.config import get_settings
    from aeids.db.models import Tenant
    from aeids.db.session import make_engine, make_sessionmaker
    from aeids.llm.embedding import OpenRouterEmbedder
    from aeids.llm.openrouter import OpenRouterClient
    from aeids.retrieval.query_cache import SqlQueryCache
    from aeids.retrieval.vector_index import VectorIndex

    settings = get_settings()
    engine = make_engine(settings.database_url)
    if settings.tracing_enabled:
        tracing.configure(tracing.SqlTraceSink(engine))
    index = VectorIndex.from_url(settings.qdrant_url)
    embedder = OpenRouterEmbedder.from_settings(settings, cache=SqlQueryCache(engine))
    llm = OpenRouterClient(settings)
    name = args.tenant or settings.default_tenant_name
    try:
        with make_sessionmaker(engine)() as session:
            tenant = session.scalar(select(Tenant).where(Tenant.name == name))
            if tenant is None:
                print(f"no tenant named {name!r}", file=sys.stderr)
                return 1
            out = answer(session, index, embedder, llm, tenant.id, args.question,
                         top_k=args.top_k or DEFAULT_TOP_K)
    finally:
        llm.close()
        embedder.client.close()
        index.close()
        engine.dispose()
    if out.error:
        print(f"error: {out.error}", file=sys.stderr)
    print(out.answer or "(no answer)")
    for c in out.citations:
        print(f"  [{c.source_id}] {c.company} · {c.filename} p. {c.page_start}"
              + (f"-{c.page_end}" if c.page_end != c.page_start else ""))
    cost = f"${out.cost_usd:.5f}" if out.cost_usd is not None else "n/a"
    print(f"({out.model}, {out.total_tokens} tokens, {cost}, {out.latency_ms / 1000:.1f} s, trace {out.trace_id})")
    return 0 if not out.error else 1


if __name__ == "__main__":
    sys.exit(main())
