"""Attaching TableLens output to a version: status transitions, chunks, mismatches, tenancy."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeids.db.models import DocumentChunk, DocumentVersion, ParserStatus
from aeids.ingestion.pipeline import (
    PARSER_TRANSITIONS,
    IllegalParserTransition,
    set_parser_status,
)
from tests.factories import (
    INCOME_ROWS,
    INCOME_SUBTITLE,
    dumps,
    grid_table,
    pdf_bytes,
    region,
    structure,
)


def _structure(pages: int = 2, file: str = "report.pdf") -> bytes:
    tables = [grid_table(pages, 1, INCOME_ROWS, header_rows=2, order=2,
                         title="CONSOLIDATED STATEMENTS OF OPERATIONS", subtitle=INCOME_SUBTITLE)]
    # long enough to stay a text chunk of its own (not the table's lead-in)
    regions = [region(1, 1, "heading", "Business", order=1),
               region(1, 2, "paragraph", "The Company designs smartphones. " * 20, order=2)]
    return dumps(structure(tables, regions, file=file))


def _upload(client: TestClient, pages: int = 2, tag: str = "a") -> tuple[str, str]:
    body = client.post(
        "/api/v1/documents",
        files={"file": ("report.pdf", pdf_bytes(pages, tag), "application/pdf")},
        data={"company_name": "Apple Inc.", "fiscal_year": "2025"},
    ).json()
    return body["document"]["id"], body["version"]["id"]


def _attach(client: TestClient, doc: str, ver: str, raw: bytes):
    return client.post(f"/api/v1/documents/{doc}/versions/{ver}/structure",
                       files={"file": ("report_structure.json", raw, "application/json")})


def _status(client: TestClient, doc: str) -> str:
    return client.get(f"/api/v1/documents/{doc}/versions").json()[0]["parser_status"]


def test_attach_parses_version(client: TestClient) -> None:
    doc, ver = _upload(client)
    resp = _attach(client, doc, ver, _structure())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"]["parser_status"] == "parsed"
    assert body["version"]["parsed_at"] is not None
    assert body["version"]["structure_checksum"]
    assert body["table_count"] == 1 and body["financial_table_count"] == 1
    assert body["chunk_count"] == 2

    chunks = client.get(f"/api/v1/documents/{doc}/versions/{ver}/chunks").json()
    assert [c["kind"] for c in chunks] == ["text", "table"]
    assert chunks[1]["page_start"] == 2
    assert "Units: USD million" in chunks[1]["text"]


def test_reattach_replaces_chunks(client: TestClient, session: Session) -> None:
    doc, ver = _upload(client)
    _attach(client, doc, ver, _structure())
    assert _attach(client, doc, ver, _structure()).status_code == 200
    n = session.scalar(select(func.count()).select_from(DocumentChunk))
    assert n == 2  # replaced, not duplicated


def test_structure_from_another_file_is_rejected(client: TestClient) -> None:
    doc, ver = _upload(client, pages=2)
    resp = _attach(client, doc, ver, _structure(pages=5))  # refers to page 5 of a 2-page PDF
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "structure_mismatch"
    versions = client.get(f"/api/v1/documents/{doc}/versions").json()
    assert versions[0]["parser_status"] == "failed"
    assert "page 5" in versions[0]["parser_error"]

    # failed -> parsing is legal: the right file then parses
    assert _attach(client, doc, ver, _structure(pages=2)).status_code == 200
    assert _status(client, doc) == "parsed"


def test_invalid_json_fails_the_version(client: TestClient) -> None:
    doc, ver = _upload(client)
    resp = _attach(client, doc, ver, b'{"tables": "nope"}')
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_structure"
    assert _status(client, doc) == "failed"


def test_filename_mismatch_is_a_warning(client: TestClient) -> None:
    doc, ver = _upload(client)
    body = _attach(client, doc, ver, _structure(file="NASDAQ_AAPL_2025.pdf")).json()
    assert any("NASDAQ_AAPL_2025.pdf" in w for w in body["warnings"])
    assert body["version"]["parse_warnings"] == body["warnings"]


def test_unreadable_pdf_skips_page_check_with_warning(client: TestClient, make_pdf) -> None:
    body = client.post(
        "/api/v1/documents",
        files={"file": ("report.pdf", make_pdf("not-a-real-pdf"), "application/pdf")},
        data={"company_name": "Acme"},
    ).json()
    resp = _attach(client, body["document"]["id"], body["version"]["id"], _structure())
    assert resp.status_code == 200
    assert any("page count" in w for w in resp.json()["warnings"])


def test_indexed_version_cannot_be_reparsed(client: TestClient, session: Session) -> None:
    doc, ver = _upload(client)
    _attach(client, doc, ver, _structure())
    v = session.get(DocumentVersion, uuid.UUID(ver))
    set_parser_status(v, ParserStatus.INDEXING)
    set_parser_status(v, ParserStatus.INDEXED)
    session.commit()
    resp = _attach(client, doc, ver, _structure())
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "illegal_status_transition"


def test_wrong_document_or_tenant_is_404(client: TestClient) -> None:
    doc, ver = _upload(client)
    other_doc, _ = _upload(client, tag="b")
    assert _attach(client, other_doc, ver, _structure()).status_code == 404
    assert _attach(client, doc, str(uuid.uuid4()), _structure()).status_code == 404
    assert client.get(f"/api/v1/documents/{other_doc}/versions/{ver}/chunks").status_code == 404


@pytest.mark.parametrize("src", list(ParserStatus))
def test_transition_table_is_the_only_way(src: ParserStatus) -> None:
    for dst in ParserStatus:
        v = DocumentVersion(parser_status=src.value)
        if dst in PARSER_TRANSITIONS[src]:
            set_parser_status(v, dst)
            assert v.parser_status == dst.value
        else:
            with pytest.raises(IllegalParserTransition):
                set_parser_status(v, dst)
            assert v.parser_status == src.value


def test_chunk_meta_keeps_decimals_as_strings(client: TestClient, session: Session) -> None:
    doc, ver = _upload(client)
    _attach(client, doc, ver, _structure())
    table_chunk = session.scalars(
        select(DocumentChunk).where(DocumentChunk.chunk_index == 1)
    ).one()
    cell = table_chunk.meta["table"]["rows"][1]["cells"][0]
    assert cell["value"] == "307003"
    assert json.dumps(table_chunk.meta)  # plain JSON


def test_tablelens_stem_suffix_is_not_a_filename_mismatch() -> None:
    from aeids.ingestion.pipeline import _stem
    from aeids.ingestion.tablelens import tablelens_stem

    stem = tablelens_stem("NASDAQ TSLA 2025.pdf", "65dd654744c3" + "0" * 52)
    assert _stem(stem + ".pdf") == _stem("NASDAQ TSLA 2025.pdf")
    assert _stem("other_report.pdf") != _stem("NASDAQ TSLA 2025.pdf")
