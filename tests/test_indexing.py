"""Indexing for search and the versioning rule (rule 6): keep old -> index new -> verify ->
switch. In-memory Qdrant, offline HashEmbedder, fake TableLens; processing inline."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aeids.db.models import DocumentChunk, DocumentVersion
from aeids.llm.embedding import HashEmbedder
from aeids.llm.openrouter import LLMAuthError, LLMTransientError
from aeids.retrieval.vector_index import VectorIndex, VectorIndexError
from tests.factories import pdf_bytes
from tests.test_tablelens import FakeTableLens, _client


class FailingEmbedder(HashEmbedder):
    def __init__(self, exc: Exception) -> None:
        super().__init__(64)
        self.exc = exc

    def embed_documents(self, texts):
        raise self.exc


@pytest.fixture
def tl(client: TestClient) -> FakeTableLens:
    fake = FakeTableLens()
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(fake)
    return fake


def _upload(client: TestClient, tag: str) -> dict:
    resp = client.post("/api/v1/documents",
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, tag), "application/pdf")},
                       data={"company_name": "Apple Inc.", "fiscal_year": "2025"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload_version(client: TestClient, doc: str, tag: str) -> dict:
    resp = client.post(f"/api/v1/documents/{doc}/versions",
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, tag), "application/pdf")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _versions(client: TestClient, doc: str) -> list[dict]:
    return client.get(f"/api/v1/documents/{doc}/versions").json()


def _points(index: VectorIndex, version: dict) -> list[dict]:
    """Payloads of a version's points in the (only) collection."""
    collections = [c.name for c in index.client.get_collections().collections]
    assert collections == ["aeids_chunks__test-hash-64__64"]
    points, _ = index.client.scroll(collections[0], limit=1000, with_payload=True)
    return [p.payload for p in points if p.payload["document_version_id"] == version["id"]]


def test_upload_is_indexed_and_becomes_current(client: TestClient, tl, vector_index,
                                                session: Session) -> None:
    body = _upload(client, "v1")
    v = body["version"]
    assert v["parser_status"] == "indexed" and v["is_current"] is True
    assert body["processing"] == "done"
    payloads = _points(vector_index, v)
    assert len(payloads) == v["index_point_count"] == 2
    p = payloads[0]
    assert p["tenant_id"] and p["company_id"] == body["document"]["company_id"]
    assert p["document_id"] == body["document"]["id"] and p["financial_year"] == 2025
    assert p["is_current"] is True and p["document_type"] == "annual_report"
    chunks = session.query(DocumentChunk).filter_by(version_id=uuid.UUID(v["id"])).all()
    assert all(c.qdrant_point_id == c.id for c in chunks)


def test_new_version_switches_current_after_indexing(client: TestClient, tl, vector_index) -> None:
    v1 = _upload(client, "v1")
    doc = v1["document"]["id"]
    v2 = _upload_version(client, doc, "v2")["version"]
    assert v2["parser_status"] == "indexed" and v2["is_current"] is True
    assert [v["is_current"] for v in _versions(client, doc)] == [False, True]
    old = _points(vector_index, v1["version"])
    assert old and not any(p["is_current"] for p in old)  # old version kept, not current
    assert all(p["is_current"] for p in _points(vector_index, v2))


def test_failed_indexing_keeps_old_version_current_then_retry(client: TestClient, tl,
                                                               vector_index) -> None:
    v1 = _upload(client, "v1")
    doc = v1["document"]["id"]
    state = client.app.state
    good = state.embedder
    state.embedder = FailingEmbedder(LLMTransientError("OpenRouter HTTP 503: overloaded"))
    body = _upload_version(client, doc, "v2")
    v2 = body["version"]
    assert body["processing"] == "failed"
    assert v2["parser_status"] == "failed" and v2["is_current"] is False
    assert v2["parser_error"] == "indexing: OpenRouter HTTP 503: overloaded"
    assert v2["parsed_at"] is not None  # the parse itself succeeded
    assert [v["is_current"] for v in _versions(client, doc)] == [True, False]

    state.embedder = good
    resp = client.post(f"/api/v1/documents/{doc}/versions/{v2['id']}/process")
    assert resp.status_code == 200
    v2 = resp.json()["version"]
    assert v2["parser_status"] == "indexed" and v2["is_current"] is True
    assert len(tl.detected) == 2  # v1 and v2 parsed once each: the retry only indexed
    assert len(_points(vector_index, v2)) == v2["index_point_count"]


def test_auth_error_is_reported(client: TestClient, tl) -> None:
    client.app.state.embedder = FailingEmbedder(LLMAuthError("OpenRouter HTTP 401: bad key", 401))
    v = _upload(client, "v1")["version"]
    assert v["parser_status"] == "failed"
    assert "401" in v["parser_error"]


def test_older_version_finishing_late_does_not_take_current(client: TestClient, tl) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "off"})
    v1 = _upload(client, "v1")
    doc = v1["document"]["id"]
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    v2 = _upload_version(client, doc, "v2")["version"]
    assert v2["is_current"] is True
    late = client.post(f"/api/v1/documents/{doc}/versions/{v1['version']['id']}/process").json()
    assert late["version"]["parser_status"] == "indexed"
    assert late["version"]["is_current"] is False
    assert [v["is_current"] for v in _versions(client, doc)] == [False, True]


def test_qdrant_down_fails_the_version(client: TestClient, tl, monkeypatch) -> None:
    def down(*_a, **_kw):
        raise VectorIndexError("Qdrant not reachable (upsert): ConnectError")

    monkeypatch.setattr(client.app.state.vector_index, "replace_version", down)
    v = _upload(client, "v1")["version"]
    assert v["parser_status"] == "failed" and v["is_current"] is False
    assert "Qdrant not reachable" in v["parser_error"]


def test_count_mismatch_is_not_promoted(client: TestClient, tl, monkeypatch) -> None:
    index = client.app.state.vector_index
    real = index.replace_version
    monkeypatch.setattr(index, "replace_version", lambda *a, **kw: real(*a, **kw) - 1)
    v = _upload(client, "v1")["version"]
    assert v["parser_status"] == "failed" and v["is_current"] is False
    assert "expected 2" in v["parser_error"]


def test_reindex_does_not_duplicate_points(client: TestClient, tl, vector_index,
                                           session: Session) -> None:
    from aeids.ingestion.indexing import index_version

    v = _upload(client, "v1")["version"]
    row = session.get(DocumentVersion, uuid.UUID(v["id"]))
    row.parser_status = "failed"  # e.g. the worker was lost after the upsert
    session.commit()
    index_version(session, vector_index, client.app.state.embedder, row.tenant_id, row.id)
    assert len(_points(vector_index, v)) == 2


def test_never_parsed_failed_version_cannot_be_indexed(client: TestClient, session: Session,
                                                       vector_index) -> None:
    from aeids.ingestion.indexing import index_version
    from aeids.ingestion.pipeline import IllegalParserTransition

    client.app.state.settings = client.app.state.settings.model_copy(update={"jobs_mode": "off"})
    v = _upload(client, "v1")["version"]
    row = session.get(DocumentVersion, uuid.UUID(v["id"]))
    with pytest.raises(IllegalParserTransition):  # pending -> indexing is not a transition
        index_version(session, vector_index, HashEmbedder(64), row.tenant_id, row.id)
    row.parser_status = "failed"
    session.commit()
    with pytest.raises(IllegalParserTransition, match="not been parsed"):
        index_version(session, vector_index, HashEmbedder(64), row.tenant_id, row.id)


def test_vector_search_is_tenant_scoped(vector_index: VectorIndex) -> None:
    from aeids.retrieval.vector_index import ChunkPoint

    t1, t2, doc, ver = (uuid.uuid4() for _ in range(4))
    vector_index.ensure_collection("c", 3)
    vector_index.replace_version("c", t1, ver, [ChunkPoint(uuid.uuid4(), [1, 0, 0], {
        "tenant_id": str(t1), "document_id": str(doc), "document_version_id": str(ver),
        "is_current": True})])
    assert len(vector_index.search("c", t1, [1, 0, 0], limit=5)) == 1
    assert vector_index.search("c", t2, [1, 0, 0], limit=5) == []
