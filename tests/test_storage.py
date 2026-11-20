"""LocalStore: content-addressed keys, idempotent puts, no path escapes."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from aeids.storage import LocalStore, StorageError

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _staged(store: LocalStore, data: bytes) -> Path:
    path = store.staging_dir() / f"{uuid.uuid4().hex}.upload"
    path.write_bytes(data)
    return path


def test_key_layout(store: LocalStore) -> None:
    sha = hashlib.sha256(b"x").hexdigest()
    assert store.key_for(TENANT, sha) == f"{TENANT}/{sha[:2]}/{sha}.pdf"


@pytest.mark.parametrize("bad", ["", "abc", "G" * 64, "A" * 64])
def test_key_rejects_bad_checksum(store: LocalStore, bad: str) -> None:
    with pytest.raises(StorageError):
        store.key_for(TENANT, bad)


def test_put_moves_file_and_is_idempotent(store: LocalStore) -> None:
    data = b"%PDF-1.4 content"
    sha = hashlib.sha256(data).hexdigest()
    key = store.key_for(TENANT, sha)

    src = _staged(store, data)
    uri = store.put_file(src, key)
    assert uri == f"local://{key}"
    assert not src.exists()
    assert store.path_for(uri).read_bytes() == data

    src2 = _staged(store, data)
    assert store.put_file(src2, key) == uri
    assert not src2.exists()
    assert store.exists(uri)
    assert not list(store.path_for(uri).parent.glob("*.part"))


@pytest.mark.parametrize(
    "uri",
    ["local://../outside.pdf", "local:///etc/passwd", "local://a/../../b", "s3://bucket/key", ""],
)
def test_path_for_rejects_escapes(store: LocalStore, uri: str) -> None:
    with pytest.raises(StorageError):
        store.path_for(uri)
