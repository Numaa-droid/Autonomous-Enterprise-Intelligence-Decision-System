"""Upload, checksum dedupe and versioning through the HTTP API (MVP_PLAN.md M1).

An upload is stored as *not current*: a version becomes current only once it is indexed
(rule 6), which tests/test_indexing.py covers. Processing is off here (``jobs_mode="off"``)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeids import documents as docs
from aeids.db.models import DocumentType, DocumentVersion
from aeids.storage import LocalStore
from tests.conftest import MAX_UPLOAD_BYTES

MakePdf = Callable[[str], bytes]


def upload(client: TestClient, content: bytes, filename: str = "report.pdf", **form: str):
    data = {"company_name": "Acme Ltd", **form}
    return client.post(
        "/api/v1/documents",
        files={"file": (filename, content, "application/pdf")},
        data=data,
    )


def upload_version(client: TestClient, document_id: str, content: bytes, filename="v2.pdf"):
    return client.post(
        f"/api/v1/documents/{document_id}/versions",
        files={"file": (filename, content, "application/pdf")},
    )


def version_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DocumentVersion)) or 0


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def _make_current(session: Session, version_id: str) -> None:
    """What indexing does once a version's index is verified (tests/test_indexing.py)."""
    docs.promote_to_current(session, session.get(DocumentVersion, uuid.UUID(version_id)))
    session.commit()


def test_upload_creates_document_and_pending_version(
    client: TestClient, make_pdf: MakePdf, storage_root: Path
) -> None:
    content = make_pdf("v1")
    resp = upload(client, content, document_type="annual_report", fiscal_year="2025")

    assert resp.status_code == 201
    body = resp.json()
    assert body["duplicate"] is False
    doc, version = body["document"], body["version"]
    assert doc["company_name"] == "Acme Ltd"
    assert doc["document_type"] == "annual_report"
    assert doc["fiscal_year"] == 2025
    assert doc["current_version"] is None  # not current until indexed
    assert version["version_number"] == 1
    assert version["is_current"] is False
    assert version["parser_status"] == "pending"
    assert version["size_bytes"] == len(content)

    sha = hashlib.sha256(content).hexdigest()
    assert version["checksum"] == sha
    stored = list(storage_root.rglob(f"{sha}.pdf"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == content
    assert stored[0].parent.name == sha[:2]
    assert "storage_uri" not in version  # internal location is not exposed


def test_duplicate_upload_returns_existing_version(
    client: TestClient, make_pdf: MakePdf, session: Session
) -> None:
    content = make_pdf("same")
    first = upload(client, content).json()

    resp = upload(client, content, filename="renamed.pdf")

    assert resp.status_code == 200
    body = resp.json()
    assert body["duplicate"] is True
    assert body["version"]["id"] == first["version"]["id"]
    assert body["document"]["id"] == first["document"]["id"]
    assert version_count(session) == 1


def test_replacement_upload_keeps_the_old_version_current(
    client: TestClient, make_pdf: MakePdf, session: Session
) -> None:
    """Two versions of one PDF; the old one stays current until the new one is indexed."""
    v1 = upload(client, make_pdf("fy25-draft")).json()
    doc_id = v1["document"]["id"]
    _make_current(session, v1["version"]["id"])

    resp = upload_version(client, doc_id, make_pdf("fy25-final"))
    assert resp.status_code == 201
    v2 = resp.json()["version"]
    assert v2["version_number"] == 2
    assert v2["is_current"] is False

    versions = client.get(f"/api/v1/documents/{doc_id}/versions").json()
    assert [v["version_number"] for v in versions] == [1, 2]
    assert [v["is_current"] for v in versions] == [True, False]
    assert client.get(f"/api/v1/documents/{doc_id}").json()["current_version"]["id"] == \
        v1["version"]["id"]


def test_reuploading_an_old_version_returns_it_and_keeps_current(
    client: TestClient, make_pdf: MakePdf, session: Session
) -> None:
    v1 = upload(client, make_pdf("a")).json()
    doc_id = v1["document"]["id"]
    v2 = upload_version(client, doc_id, make_pdf("b")).json()["version"]
    _make_current(session, v2["id"])

    resp = upload_version(client, doc_id, make_pdf("a"))

    assert resp.status_code == 200
    assert resp.json()["duplicate"] is True
    assert resp.json()["version"]["id"] == v1["version"]["id"]
    versions = client.get(f"/api/v1/documents/{doc_id}/versions").json()
    assert len(versions) == 2
    assert [v["id"] for v in versions if v["is_current"]] == [v2["id"]]


def test_version_numbers_keep_increasing(client: TestClient, make_pdf: MakePdf) -> None:
    doc_id = upload(client, make_pdf("1")).json()["document"]["id"]
    for i in range(2, 5):
        assert upload_version(client, doc_id, make_pdf(str(i))).json()["version"][
            "version_number"
        ] == i
    versions = client.get(f"/api/v1/documents/{doc_id}/versions").json()
    assert [v["version_number"] for v in versions] == [1, 2, 3, 4]
    assert not any(v["is_current"] for v in versions)  # none indexed yet


def test_list_documents(client: TestClient, make_pdf: MakePdf) -> None:
    upload(client, make_pdf("x"), company_name="Alpha")
    upload(client, make_pdf("y"), company_name="Beta", document_type="brsr")
    listed = client.get("/api/v1/documents").json()
    assert sorted(d["company_name"] for d in listed) == ["Alpha", "Beta"]
    assert all(d["current_version"] is None for d in listed)  # none indexed yet


def test_same_company_name_reuses_company(client: TestClient, make_pdf: MakePdf) -> None:
    a = upload(client, make_pdf("1"), company_name="Acme   Ltd ").json()["document"]
    b = upload(client, make_pdf("2"), company_name="Acme Ltd").json()["document"]
    assert a["company_id"] == b["company_id"]
    assert a["id"] != b["id"]


def test_rejects_non_pdf(client: TestClient, storage_root: Path) -> None:
    resp = upload(client, b"PK\x03\x04 not a pdf", filename="report.pdf")
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file_type"
    assert list((storage_root / ".staging").iterdir()) == []


def test_rejects_empty_file(client: TestClient) -> None:
    resp = upload(client, b"")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_upload"


def test_rejects_too_large(client: TestClient, storage_root: Path, session: Session) -> None:
    resp = upload(client, b"%PDF-" + b"0" * MAX_UPLOAD_BYTES)
    assert resp.status_code == 413
    assert list((storage_root / ".staging").iterdir()) == []
    assert version_count(session) == 0


def test_rejects_blank_company_and_bad_type(client: TestClient, make_pdf: MakePdf) -> None:
    assert upload(client, make_pdf("c"), company_name="   ").status_code == 400
    assert upload(client, make_pdf("d"), document_type="lab_report").status_code == 422
    assert upload(client, make_pdf("e"), fiscal_year="1800").status_code == 422


def test_filename_is_sanitized(client: TestClient, make_pdf: MakePdf) -> None:
    # Control characters are covered in test_documents_service (the client encodes them).
    body = upload(client, make_pdf("f"), filename="../../etc/passwd.pdf").json()
    assert body["version"]["filename"] == "passwd.pdf"


def test_unknown_document_is_404(client: TestClient, make_pdf: MakePdf) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/documents/{missing}").status_code == 404
    assert client.get(f"/api/v1/documents/{missing}/versions").status_code == 404
    resp = upload_version(client, str(missing), make_pdf("g"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "document_not_found"


def test_other_tenants_documents_are_invisible(
    client: TestClient, make_pdf: MakePdf, session: Session, store: LocalStore
) -> None:
    upload(client, make_pdf("mine"))  # creates the default tenant
    other = docs.get_or_create_tenant(session, "other-tenant")
    session.commit()
    staged = docs.stage_upload(
        _bytes_io(make_pdf("theirs")), "theirs.pdf", store.staging_dir(), MAX_UPLOAD_BYTES
    )
    theirs = docs.create_document(
        session,
        store,
        other.id,
        staged,
        company_name="Other Co",
        document_type=DocumentType.ANNUAL_REPORT,
        fiscal_year=None,
    ).document

    assert client.get(f"/api/v1/documents/{theirs.id}").status_code == 404
    assert client.get(f"/api/v1/documents/{theirs.id}/versions").status_code == 404
    assert upload_version(client, str(theirs.id), make_pdf("h")).status_code == 404
    assert [d["company_name"] for d in client.get("/api/v1/documents").json()] == ["Acme Ltd"]

    # The same bytes in another tenant are a new document, not a duplicate.
    resp = upload(client, make_pdf("theirs"))
    assert resp.status_code == 201
    assert resp.json()["document"]["id"] != str(theirs.id)


def _bytes_io(data: bytes):
    import io

    return io.BytesIO(data)
