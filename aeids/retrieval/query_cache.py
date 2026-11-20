"""Postgres cache of question vectors (D-023), behind ``aeids.llm.embedding.QueryCache``.

Each read and write uses its own short connection (never the caller's session). A failing cache
is never fatal: a miss is embedded again, a failed write is skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from aeids.db.models import QueryEmbedding, utcnow
from aeids.llm.embedding import pack, unpack

log = logging.getLogger(__name__)


class SqlQueryCache:
    def __init__(self, engine: Any) -> None:
        if engine.dialect.name == "sqlite":  # one writer: never wait long behind other work
            engine = create_engine(engine.url, connect_args={"timeout": 0.5})
        self.engine = engine

    def get(self, key: str) -> list[float] | None:
        try:
            with self.engine.connect() as conn:
                data = conn.scalar(select(QueryEmbedding.vector).where(QueryEmbedding.key == key))
        except SQLAlchemyError as exc:
            log.warning("query vector cache unavailable: %s", type(exc).__name__)
            return None
        return unpack(data) if data else None

    def put(self, key: str, model: str, provider: str | None, vector: Sequence[float]) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(QueryEmbedding).values(
                    key=key, model=model[:200], provider=(provider or None), dimension=len(vector),
                    vector=pack(vector), created_at=utcnow()))
        except IntegrityError:
            pass  # another request cached the same question first
        except SQLAlchemyError as exc:
            log.warning("query vector not cached: %s", type(exc).__name__)
