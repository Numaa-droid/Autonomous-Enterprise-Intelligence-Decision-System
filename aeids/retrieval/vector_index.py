"""Qdrant vector index of document chunks (D-007).

One collection per embedding model and size (``aeids_chunks__<model>__<dim>``), so switching
models never mixes vectors. One point per chunk; the point id is the chunk id. The payload
(ARCHITECTURE.md §4.1) carries tenant, company, document, version, type, fiscal year, pages,
section, kind and ``is_current``.

Rule 10: every query here takes a ``tenant_id`` and filters on it; there is no unfiltered
search. ``is_current`` in the payload is a pre-filter only: callers re-check the current
version in Postgres, the source of truth (rule 6).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

UPSERT_BATCH = 128


class VectorIndexError(Exception):
    """Qdrant unreachable or refused a request (retryable: the service may come back)."""

    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def collection_name(model: str, dimension: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-")
    return f"aeids_chunks__{slug}__{dimension}"


@dataclass(frozen=True)
class ChunkPoint:
    chunk_id: uuid.UUID
    vector: Sequence[float]
    payload: dict[str, Any]


@dataclass(frozen=True)
class VectorHit:
    chunk_id: uuid.UUID
    score: float


# fields that get a payload index (filtered on); tenant_id is the tenant key
_KEYWORD_FIELDS = ("company_id", "document_id", "document_version_id", "document_type")


class VectorIndex:
    def __init__(self, client: QdrantClient) -> None:
        self.client = client
        self._ready: set[str] = set()

    @classmethod
    def from_url(cls, url: str) -> VectorIndex:
        return cls(QdrantClient(url=url, timeout=30))

    def close(self) -> None:
        self.client.close()

    # --- writing ---------------------------------------------------------------

    def ensure_collection(self, name: str, dimension: int) -> None:
        if name in self._ready:
            return
        with _errors("create collection"):
            if not self.client.collection_exists(name):
                self.client.create_collection(
                    name,
                    vectors_config=models.VectorParams(size=dimension,
                                                       distance=models.Distance.COSINE),
                )
                self.client.create_payload_index(
                    name, "tenant_id",
                    field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD,
                                                           is_tenant=True),
                )
                for field in _KEYWORD_FIELDS:
                    self.client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
                self.client.create_payload_index(name, "is_current", models.PayloadSchemaType.BOOL)
                self.client.create_payload_index(name, "financial_year",
                                                 models.PayloadSchemaType.INTEGER)
        self._ready.add(name)

    def replace_version(self, name: str, tenant_id: uuid.UUID, version_id: uuid.UUID,
                        points: Sequence[ChunkPoint]) -> int:
        """All points of a version, replacing any earlier ones (a re-index never duplicates).
        Returns the number of points the index then holds for the version."""
        self.delete_version(name, tenant_id, version_id)
        self.upsert(name, points)
        return self.count_version(name, tenant_id, version_id)

    def upsert(self, name: str, points: Sequence[ChunkPoint]) -> None:
        """Write points; an existing point with the same id (chunk id) is overwritten in place,
        so re-embedding a version never leaves a gap in search."""
        with _errors("upsert"):
            for start in range(0, len(points), UPSERT_BATCH):
                self.client.upsert(name, wait=True, points=[
                    models.PointStruct(id=str(p.chunk_id), vector=list(p.vector), payload=p.payload)
                    for p in points[start : start + UPSERT_BATCH]
                ])

    def delete_version(self, name: str, tenant_id: uuid.UUID, version_id: uuid.UUID) -> None:
        if not self._exists(name):
            return
        with _errors("delete"):
            self.client.delete(name, wait=True, points_selector=models.FilterSelector(
                filter=_filter(tenant_id, document_version_id=version_id)))

    def count_version(self, name: str, tenant_id: uuid.UUID, version_id: uuid.UUID) -> int:
        if not self._exists(name):
            return 0
        with _errors("count"):
            return self.client.count(name, exact=True, count_filter=_filter(
                tenant_id, document_version_id=version_id)).count

    def set_current(self, name: str, tenant_id: uuid.UUID, document_id: uuid.UUID,
                    current_version_id: uuid.UUID, *, others: bool = True) -> None:
        """Mark ``current_version_id``'s points current; with ``others``, the document's other
        versions not current. Called in two steps around the Postgres switch (new on, switch,
        old off) so that search never loses the document in between."""
        if not self._exists(name):
            return
        with _errors("set payload"):
            self.client.set_payload(name, {"is_current": True}, wait=True, points=_filter(
                tenant_id, document_version_id=current_version_id))
            if others:
                self.client.set_payload(name, {"is_current": False}, wait=True, points=models.Filter(
                    must=_filter(tenant_id, document_id=document_id).must,
                    must_not=[_match("document_version_id", current_version_id)],
                ))

    # --- reading ---------------------------------------------------------------

    def search(
        self,
        name: str,
        tenant_id: uuid.UUID,
        vector: Sequence[float],
        *,
        limit: int,
        current_only: bool = True,
        company_ids: Sequence[uuid.UUID] = (),
        document_ids: Sequence[uuid.UUID] = (),
        fiscal_years: Sequence[int] = (),
    ) -> list[VectorHit]:
        if not self._exists(name):
            return []
        flt = _filter(tenant_id)
        if current_only:
            flt.must.append(models.FieldCondition(key="is_current",
                                                  match=models.MatchValue(value=True)))
        for key, values in (("company_id", company_ids), ("document_id", document_ids)):
            if values:
                flt.must.append(models.FieldCondition(
                    key=key, match=models.MatchAny(any=[str(v) for v in values])))
        if fiscal_years:
            flt.must.append(models.FieldCondition(
                key="financial_year", match=models.MatchAny(any=list(fiscal_years))))
        with _errors("search"):
            result = self.client.query_points(name, query=list(vector), query_filter=flt,
                                              limit=limit, with_payload=False)
        return [VectorHit(uuid.UUID(str(p.id)), float(p.score)) for p in result.points]

    def _exists(self, name: str) -> bool:
        if name in self._ready:
            return True
        with _errors("read collection"):
            return self.client.collection_exists(name)


def _match(key: str, value: object) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=str(value)))


def _filter(tenant_id: uuid.UUID, **equals: object) -> models.Filter:
    must: list[Any] = [_match("tenant_id", tenant_id)]
    must += [_match(k, v) for k, v in equals.items()]
    return models.Filter(must=must)


class _errors:
    """Turn qdrant-client transport / HTTP errors into VectorIndexError (no payload echoed)."""

    def __init__(self, action: str) -> None:
        self.action = action

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc is None:
            return False
        if isinstance(exc, UnexpectedResponse):
            raise VectorIndexError(f"Qdrant {self.action} failed: HTTP {exc.status_code}") from None
        if isinstance(exc, (ResponseHandlingException, ConnectionError, OSError, TimeoutError)):
            raise VectorIndexError(f"Qdrant not reachable ({self.action}): "
                                   f"{type(exc).__name__}") from None
        return False
