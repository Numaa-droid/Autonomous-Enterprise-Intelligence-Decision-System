"""Search: keyword (Postgres full-text; BM25 stand-in on SQLite), dense (Qdrant), hybrid (RRF),
with tenant, current-version and filter checks. Offline: in-memory Qdrant, HashEmbedder."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aeids import documents as docs
from aeids.db.models import DocumentType
from aeids.ingestion.indexing import index_version
from aeids.ingestion.pipeline import ingest_structure
from aeids.llm.openrouter import LLMTransientError
from aeids.retrieval.search import SearchFilters, rrf, search
from tests.factories import dumps, grid_table, pdf_bytes, region, structure
from tests.test_indexing import FailingEmbedder
from tests.test_tablelens import FakeTableLens, _client

RISK = ("Our operations depend on a global supply chain. Disruptions at contract manufacturers "
        "in Asia, shortages of components and logistics delays could hurt our results. ")
MDNA = ("Services revenue grew because of higher advertising, cloud and payment services. "
        "Net sales of iPhone declined in Greater China. ")
INCOME = [["", "2025", "2024"], ["Net sales", "416,161", "391,035"],
          ["Cost of sales", "220,960", "210,352"], ["Net income", "112,010", "93,736"]]


def report(risk: str = RISK, mdna: str = MDNA) -> bytes:
    regions = [region(1, 1, "heading", "Item 1A. Risk Factors", order=1),
               region(1, 2, "paragraph", risk * 4, order=2),
               region(2, 3, "heading", "Item 7. Management's Discussion and Analysis", order=1),
               region(2, 4, "paragraph", mdna * 5, order=2)]
    tables = [grid_table(2, 1, INCOME, header_rows=1, order=3,
                         title="CONSOLIDATED STATEMENTS OF OPERATIONS",
                         subtitle="(In millions, except per-share amounts)")]
    return dumps(structure(tables, regions, file="NASDAQ_AAPL_2025.pdf"))


@pytest.fixture
def inline(client: TestClient):
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})

    def use(structure_bytes: bytes) -> None:
        state.tablelens = _client(FakeTableLens(structure_bytes))

    use(report())
    return use


def _upload(client: TestClient, tag: str = "a", company: str = "Apple Inc.", year: str = "2025"):
    resp = client.post("/api/v1/documents",
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, tag), "application/pdf")},
                       data={"company_name": company, "fiscal_year": year})
    assert resp.status_code == 201 and resp.json()["version"]["parser_status"] == "indexed", resp.text
    return resp.json()


def _search(client: TestClient, query: str, **body):
    resp = client.post("/api/v1/search", json={"query": query, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("mode", ["keyword", "dense", "hybrid"])
def test_each_mode_finds_the_passage(client: TestClient, inline, mode: str) -> None:
    _upload(client)
    out = _search(client, "supply chain disruptions at contract manufacturers", mode=mode)
    assert out["mode"] == mode and out["warnings"] == []
    top = out["hits"][0]
    assert top["section"] == "Item 1A. Risk Factors" and top["page_start"] == 1
    assert top["kind"] == "text" and top["company_name"] == "Apple Inc."
    assert top["fiscal_year"] == 2025 and top["is_current"] is True
    if mode == "hybrid":
        assert top["dense_rank"] == 1 and top["keyword_rank"] == 1


def test_table_chunk_is_found_by_its_words(client: TestClient, inline) -> None:
    _upload(client)
    top = _search(client, "consolidated statements of operations net income", mode="keyword")["hits"][0]
    assert top["kind"] == "table" and top["page_start"] == 2


def test_only_current_versions_by_default(client: TestClient, inline) -> None:
    v1 = _upload(client, "v1")
    doc = v1["document"]["id"]
    inline(report(risk="Our business depends on semiconductor foundries in Taiwan. "))
    v2 = client.post(f"/api/v1/documents/{doc}/versions",
                     files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "v2"), "application/pdf")})
    assert v2.json()["version"]["is_current"] is True

    for mode in ("keyword", "dense", "hybrid"):
        hits = _search(client, "contract manufacturers logistics delays", mode=mode)["hits"]
        assert all(h["version_number"] == 2 for h in hits), mode  # v1's text is not returned
    old = _search(client, "contract manufacturers logistics delays", include_superseded=True)
    top = old["hits"][0]
    assert top["version_number"] == 1 and top["is_current"] is False


def test_filters(client: TestClient, inline) -> None:
    apple = _upload(client, "a", company="Apple Inc.", year="2025")
    _upload(client, "b", company="Microsoft Corporation", year="2024")
    q = "supply chain disruptions"
    assert {h["company_name"] for h in _search(client, q)["hits"]} == {"Apple Inc.",
                                                                       "Microsoft Corporation"}
    only = _search(client, q, company_ids=[apple["document"]["company_id"]])["hits"]
    assert {h["company_name"] for h in only} == {"Apple Inc."}
    fy = _search(client, q, fiscal_years=[2024])["hits"]
    assert {h["fiscal_year"] for h in fy} == {2024}
    by_doc = _search(client, q, document_ids=[apple["document"]["id"]], mode="dense")["hits"]
    assert {h["document_id"] for h in by_doc} == {apple["document"]["id"]}


def test_other_tenants_chunks_are_never_returned(client: TestClient, inline, session: Session,
                                                 store, vector_index, embedder) -> None:
    other = docs.get_or_create_tenant(session, "other-tenant")
    session.commit()
    staged = docs.stage_upload(_io(pdf_bytes(2, "theirs")), "theirs.pdf", store.staging_dir(),
                               10_000_000)
    theirs = docs.create_document(session, store, other.id, staged, company_name="Other Co",
                                  document_type=DocumentType.ANNUAL_REPORT, fiscal_year=2025)
    ingest_structure(session, store, other.id, theirs.document.id, theirs.version.id, report())
    index_version(session, vector_index, embedder, other.id, theirs.version.id)

    for mode in ("keyword", "dense", "hybrid"):
        assert _search(client, "supply chain disruptions", mode=mode)["hits"] == []  # no own docs
    _upload(client)
    hits = _search(client, "supply chain disruptions")["hits"]
    assert hits and {h["company_name"] for h in hits} == {"Apple Inc."}


def test_parsed_but_not_indexed_is_not_searchable(client: TestClient, inline) -> None:
    client.app.state.embedder = FailingEmbedder(LLMTransientError("OpenRouter HTTP 503"))
    resp = client.post("/api/v1/documents",
                       files={"file": ("r.pdf", pdf_bytes(2, "x"), "application/pdf")},
                       data={"company_name": "Apple Inc."})
    assert resp.json()["version"]["parser_status"] == "failed"
    assert _search(client, "supply chain", mode="keyword")["hits"] == []


def test_hybrid_falls_back_to_keyword_when_dense_is_down(client: TestClient, inline) -> None:
    _upload(client)

    class Down(FailingEmbedder):
        def embed_query(self, text):
            raise self.exc

    client.app.state.embedder = Down(LLMTransientError("OpenRouter HTTP 503: overloaded"))
    out = _search(client, "supply chain disruptions")
    assert out["mode"] == "keyword" and out["hits"]
    assert "dense search unavailable" in out["warnings"][0]
    resp = client.post("/api/v1/search", json={"query": "supply chain", "mode": "dense"})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "search_unavailable"


def test_bad_queries(client: TestClient) -> None:
    assert client.post("/api/v1/search", json={"query": ""}).status_code == 422
    resp = client.post("/api/v1/search", json={"query": "   "})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_query"
    assert client.post("/api/v1/search", json={"query": "x", "top_k": 500}).status_code == 422


def test_query_text_is_data_not_syntax(client: TestClient, inline) -> None:
    """Characters that mean something to tsquery or SQL are only search words."""
    _upload(client)
    for q in ["supply & chain | !delays", "o'reilly's \\ 'x' :* (a <-> b)", "'; DROP TABLE x; --"]:
        assert client.post("/api/v1/search", json={"query": q, "mode": "keyword"}).status_code == 200
    hits = _search(client, "supply & chain | !delays", mode="keyword")["hits"]
    assert hits[0]["section"] == "Item 1A. Risk Factors"


def test_rrf_rewards_agreement() -> None:
    a, b, c = (uuid.uuid4() for _ in range(3))
    fused = rrf([(a, 0.9), (b, 0.8)], [(b, 5.0), (c, 4.0)])
    assert [cid for cid, _ in fused] == [b, a, c]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_search_with_no_documents(session: Session, vector_index, embedder) -> None:
    out = search(session, vector_index, embedder, uuid.uuid4(), "net sales",
                 filters=SearchFilters())
    assert out.hits == [] and out.mode == "hybrid"


def _io(data: bytes):
    import io

    return io.BytesIO(data)
