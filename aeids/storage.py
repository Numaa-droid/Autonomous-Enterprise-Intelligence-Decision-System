"""Object storage for original files (D-005).

``LocalStore`` keeps files under a root directory with content-addressed keys
``<tenant>/<sha[:2]>/<sha>.pdf`` and URIs ``local://<key>``. Storing the same bytes twice
is a no-op. A MinIO/S3 store can implement the same ``ObjectStore`` protocol later.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Protocol

LOCAL_SCHEME = "local://"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StorageError(Exception):
    """Invalid key/URI or a storage operation that cannot be performed."""


class ObjectStore(Protocol):
    def key_for(self, tenant_id: uuid.UUID, sha256: str, suffix: str = ".pdf") -> str: ...

    def put_file(self, src: Path, key: str) -> str:
        """Move ``src`` into the store under ``key``; return the object's URI."""
        ...

    def path_for(self, uri: str) -> Path: ...

    def exists(self, uri: str) -> bool: ...

    def staging_dir(self) -> Path:
        """Directory for in-progress uploads, on the same filesystem as the store."""
        ...


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def key_for(self, tenant_id: uuid.UUID, sha256: str, suffix: str = ".pdf") -> str:
        if not _SHA256_RE.match(sha256):
            raise StorageError("checksum must be 64 lowercase hex characters")
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            raise StorageError("invalid file suffix")
        return f"{tenant_id}/{sha256[:2]}/{sha256}{suffix}"

    def staging_dir(self) -> Path:
        path = self.root / ".staging"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put_file(self, src: Path, key: str) -> str:
        dest = self._resolve_key(key)
        if dest.exists():
            # Content-addressed: same key means same bytes.
            src.unlink(missing_ok=True)
            return LOCAL_SCHEME + key
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.part")
        try:
            shutil.move(src, part)
            os.replace(part, dest)  # atomic on the same filesystem
        finally:
            part.unlink(missing_ok=True)
        return LOCAL_SCHEME + key

    def path_for(self, uri: str) -> Path:
        if not uri.startswith(LOCAL_SCHEME):
            raise StorageError("not a local:// URI")
        return self._resolve_key(uri[len(LOCAL_SCHEME) :])

    def exists(self, uri: str) -> bool:
        return self.path_for(uri).is_file()

    def _resolve_key(self, key: str) -> Path:
        if not key or key.startswith("/") or "\\" in key or ".." in key.split("/"):
            raise StorageError("invalid storage key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("storage key escapes the store root")
        return path
