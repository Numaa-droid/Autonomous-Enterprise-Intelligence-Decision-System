"""D-038: deleting a report archives it. It leaves listings, the agents' company list and search,
its vector points are removed, past evidence stays, and the same PDF can be uploaded again."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aeids.agents.base import AgentContext
from aeids.db.models import Document, DocumentChunk, DocumentVersion
from aeids.retrieval.vector_index import VectorIndexError
from tests.factories import pdf_bytes
from tests.test_indexing import _points, tl  # noqa: F401  (fixture)


def _upload(client: TestClient, tag: str, company: str = "Apple Inc.") -> dict:
    resp = client.post("/api/v1/documents",
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, tag), "application/pdf")},
                       data={"company_name": company, "fiscal_year": "2025"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_delete_hides_the_report_everywhere(client: TestClient, tl, vector_index,  # noqa: F811
                                            session: Session) -> None:
    body = _upload(client, "v1")
    doc, v = body["document"], body["version"]
    assert v["parser_status"] == "indexed" and len(_points(vector_index, v)) == 2

    assert client.delete(f"/api/v1/documents/{doc['id']}").status_code == 204

    assert client.get(f"/api/v1/documents/{doc['id']}").status_code == 404
    assert client.get("/api/v1/documents").json() == []
    assert client.get("/api/v1/dashboard/documents").json() == []
    assert _points(vector_index, v) == []  # vector points removed
    found = client.post("/api/v1/search", json={"query": "revenue"})
    assert found.status_code == 200, found.text
    assert found.json()["hits"] == []
    # archived, not erased: rows and chunks (evidence sources) are kept
    row = session.get(Document, uuid.UUID(doc["id"]))
    session.refresh(row)
    assert row.status == "archived"
    ctx = AgentContext(session, row.tenant_id, client.app.state.settings, None, None)
    assert ctx.companies() == []  # questions no longer see the company
    assert session.query(DocumentChunk).filter_by(version_id=uuid.UUID(v["id"])).count() == 2


def test_deleted_report_cannot_be_changed_or_deleted_again(client: TestClient, make_pdf) -> None:
    doc = _upload(client, "v1")["document"]
    assert client.delete(f"/api/v1/documents/{doc['id']}").status_code == 204
    assert client.delete(f"/api/v1/documents/{doc['id']}").status_code == 404
    resp = client.post(f"/api/v1/documents/{doc['id']}/versions",
                       files={"file": ("x.pdf", pdf_bytes(2, "v2"), "application/pdf")})
    assert resp.status_code == 404
    assert client.get(f"/api/v1/documents/{doc['id']}/versions").status_code == 404


def test_same_pdf_can_be_uploaded_again_after_delete(client: TestClient, session: Session) -> None:
    first = _upload(client, "same")
    again = _upload(client, "same")  # duplicate while the first is active
    assert again["document"]["id"] == first["document"]["id"]
    assert client.delete(f"/api/v1/documents/{first['document']['id']}").status_code == 204

    resp = client.post("/api/v1/documents",
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "same"), "application/pdf")},
                       data={"company_name": "Apple Inc.", "fiscal_year": "2025"})
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["document"]["id"] != first["document"]["id"]
    assert [d["id"] for d in client.get("/api/v1/documents").json()] == [new["document"]["id"]]
    assert session.query(DocumentVersion).count() == 2  # the old version is kept


def test_delete_succeeds_when_qdrant_is_down(client: TestClient, tl, monkeypatch) -> None:  # noqa: F811
    doc = _upload(client, "v1")["document"]

    def down(*_a, **_kw):
        raise VectorIndexError("Qdrant not reachable (delete): ConnectError")

    monkeypatch.setattr(client.app.state.vector_index, "delete_version", down)
    assert client.delete(f"/api/v1/documents/{doc['id']}").status_code == 204
    assert client.get("/api/v1/documents").json() == []


def test_delete_unknown_report_is_404(client: TestClient) -> None:
    assert client.delete(f"/api/v1/documents/{uuid.uuid4()}").status_code == 404
