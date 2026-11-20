"""Repeatable embeddings (D-023): pinned provider, cached question vectors, in-place re-index."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeids.db.models import DocumentVersion, QueryEmbedding
from aeids.ingestion.indexing import reindex_version
from aeids.ingestion.pipeline import IllegalParserTransition
from aeids.llm.embedding import OpenRouterEmbedder, to_float32
from aeids.llm.openrouter import OpenRouterClient
from aeids.retrieval.query_cache import SqlQueryCache
from tests.factories import pdf_bytes
from tests.test_openrouter import _settings
from tests.test_tablelens import FakeTableLens, _client


class Recorder:
    """OpenRouter stand-in: answers with a slightly different vector every call, like a real
    provider would, and records requests."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        n = len(self.requests)
        data = [{"index": i, "embedding": [0.1 + n * 1e-3, 0.2, 0.3]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": data, "provider": "Nebius",
                                         "usage": {"total_tokens": 1}})


def _embedder(rec: Recorder, cache=None, **kw) -> OpenRouterEmbedder:
    client = OpenRouterClient(_settings(), transport=httpx.MockTransport(rec), sleep=lambda _s: None)
    return OpenRouterEmbedder(client, model="qwen/qwen3-embedding-8b", query_instruction="Find",
                              provider="nebius", allow_fallbacks=False, cache=cache, **kw)


def test_requests_pin_the_provider() -> None:
    rec = Recorder()
    _embedder(rec).embed_documents(["a"])
    assert rec.requests[0]["provider"] == {"order": ["nebius"], "allow_fallbacks": False}
    client = OpenRouterClient(_settings(), transport=httpx.MockTransport(rec), sleep=lambda _s: None)
    client.embed(["b"])  # no provider: OpenRouter chooses
    assert "provider" not in rec.requests[1]


def test_settings_pin_nebius_by_default() -> None:
    from aeids.config import Settings

    s = Settings(_env_file=None)
    assert s.embedding_provider == "nebius" and s.embedding_allow_fallbacks is False


def test_same_question_same_vector(session: Session, engine) -> None:
    rec = Recorder()
    emb = _embedder(rec, cache=SqlQueryCache(engine))
    first = emb.embed_query("What were total net sales in 2025?")
    second = emb.embed_query("What were total net sales in 2025?")
    assert first == second  # exactly, although the "provider" changes its answer each call
    assert len(rec.requests) == 1 and emb.cache_hits == 1
    assert first == to_float32(first)  # stored and returned as float32, like Qdrant
    other = emb.embed_query("Something else?")
    assert len(rec.requests) == 2 and other != first
    rows = session.scalar(select(func.count()).select_from(QueryEmbedding))
    assert rows == 2
    # the key covers model and provider: another provider means another vector
    emb2 = _embedder(rec, cache=SqlQueryCache(engine))
    emb2.provider = "deepinfra"
    emb2.embed_query("What were total net sales in 2025?")
    assert len(rec.requests) == 3


def test_a_broken_cache_is_not_fatal() -> None:
    """Cache unreachable: every question is embedded again, nothing fails."""
    from sqlalchemy import create_engine

    rec = Recorder()
    emb = _embedder(rec, cache=SqlQueryCache(create_engine("sqlite:////nonexistent/dir/x.db")))
    emb.embed_query("q")
    emb.embed_query("q")
    assert len(rec.requests) == 2 and emb.cache_hits == 0


def test_reindex_in_place_keeps_the_document_searchable(client: TestClient, session: Session,
                                                        vector_index, embedder) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(FakeTableLens())
    body = client.post("/api/v1/documents", data={"company_name": "Apple Inc."},
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "r"), "application/pdf")}).json()
    v = session.get(DocumentVersion, uuid.UUID(body["version"]["id"]))
    before = v.indexed_at
    res = reindex_version(session, vector_index, embedder, v.tenant_id, v.id)
    assert res.point_count == 2 and res.became_current
    session.refresh(v)
    assert v.parser_status == "indexed" and v.is_current and v.indexed_at >= before
    points, _ = vector_index.client.scroll(res.collection, limit=10, with_payload=True)
    assert len(points) == 2 and all(p.payload["is_current"] for p in points)  # never dropped out
    hits = client.post("/api/v1/search", json={"query": "smartphones", "mode": "dense"}).json()["hits"]
    assert hits


def test_only_indexed_versions_are_reindexed(client: TestClient, session: Session, vector_index,
                                             embedder) -> None:
    body = client.post("/api/v1/documents", data={"company_name": "Apple Inc."},
                       files={"file": ("a.pdf", pdf_bytes(2, "p"), "application/pdf")}).json()
    v = session.get(DocumentVersion, uuid.UUID(body["version"]["id"]))
    with pytest.raises(IllegalParserTransition):
        reindex_version(session, vector_index, embedder, v.tenant_id, v.id)
