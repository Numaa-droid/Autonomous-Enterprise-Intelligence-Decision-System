"""Service-level checks: DB invariants, staging and filename handling."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aeids import documents as docs
from aeids.db.models import DocumentType, DocumentVersion
from aeids.storage import LocalStore


def _create(session: Session, store: LocalStore, content: bytes) -> docs.UploadOutcome:
    tenant = docs.get_or_create_tenant(session, "t1")
    staged = docs.stage_upload(io.BytesIO(content), "r.pdf", store.staging_dir(), 10_000)
    return docs.create_document(
        session,
        store,
        tenant.id,
        staged,
        company_name="Acme",
        document_type=DocumentType.ANNUAL_REPORT,
        fiscal_year=2024,
    )


def test_database_rejects_two_current_versions(
    session: Session, store: LocalStore, make_pdf: Callable[[str], bytes]
) -> None:
    v1 = _create(session, store, make_pdf("1")).version
    docs.promote_to_current(session, v1)
    session.add(
        DocumentVersion(
            tenant_id=v1.tenant_id,
            document_id=v1.document_id,
            version_number=2,
            checksum="b" * 64,
            filename="x.pdf",
            size_bytes=1,
            storage_uri="local://x",
            is_current=True,
            parser_status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_database_rejects_unknown_parser_status(
    session: Session, store: LocalStore, make_pdf: Callable[[str], bytes]
) -> None:
    v1 = _create(session, store, make_pdf("1")).version
    v1.parser_status = "done-ish"
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_promote_to_current_switches_pointer(
    session: Session, store: LocalStore, make_pdf: Callable[[str], bytes]
) -> None:
    first = _create(session, store, make_pdf("1"))
    staged = docs.stage_upload(io.BytesIO(make_pdf("2")), "r2.pdf", store.staging_dir(), 10_000)
    second = docs.add_version(
        session, store, first.document.tenant_id, first.document.id, staged
    ).version

    docs.promote_to_current(session, first.version)  # e.g. a rollback to v1
    session.commit()

    session.refresh(first.version)
    session.refresh(second)
    assert first.version.is_current is True
    assert second.is_current is False


def test_stage_upload_hashes_and_cleans_up(tmp_path: Path) -> None:
    staged = docs.stage_upload(io.BytesIO(b"%PDF-1.7 hello"), "a.pdf", tmp_path, 100)
    assert staged.size_bytes == 14
    assert staged.path.read_bytes() == b"%PDF-1.7 hello"
    assert len(staged.sha256) == 64

    with pytest.raises(docs.UnsupportedFileType):
        docs.stage_upload(io.BytesIO(b"%PD"), "b.pdf", tmp_path, 100)
    with pytest.raises(docs.UploadTooLarge):
        docs.stage_upload(io.BytesIO(b"%PDF-" + b"x" * 200), "c.pdf", tmp_path, 100)
    assert sorted(tmp_path.iterdir()) == [staged.path]


def test_stage_upload_reads_magic_across_small_chunks(tmp_path: Path) -> None:
    class Trickle(io.RawIOBase):
        def __init__(self, data: bytes) -> None:
            self.data = data

        def read(self, _n: int = -1) -> bytes:
            out, self.data = self.data[:2], self.data[2:]
            return out

    staged = docs.stage_upload(Trickle(b"%PDF-1.4 ok"), "t.pdf", tmp_path, 100)
    assert staged.size_bytes == 11


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\annual.pdf", "annual.pdf"),
        ("  spaced   name .pdf ", "spaced name .pdf"),
        ("", "document.pdf"),
        (None, "document.pdf"),
        ("..", "document.pdf"),
        ("a\x00b\x1f.pdf", "ab.pdf"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert docs.sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length() -> None:
    name = docs.sanitize_filename("x" * 400 + ".pdf")
    assert len(name) == 255
    assert name.endswith(".pdf")
