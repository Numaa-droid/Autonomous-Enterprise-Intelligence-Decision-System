"""Embedders: turn chunk text and questions into vectors.

``OpenRouterEmbedder`` is the real one (qwen/qwen3-embedding-8b, D-002). Qwen3 embedding models
are trained with an instruction on the *query* side only, so questions are sent as
``Instruct: <task>\\nQuery: <question>`` and documents as plain text.

``HashEmbedder`` is deterministic and offline, for tests only: it does not understand meaning.

Repeatability (D-023): the provider is pinned (``embedding_provider``) and question vectors go
through a ``QueryCache``, so the same question always gets exactly the same vector. Vectors are
rounded to float32 (what Qdrant stores) before they are returned or cached, so a fresh vector
and its cached copy are identical.
"""

from __future__ import annotations

import contextvars
import hashlib
import math
import re
import struct
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from aeids.config import Settings
from aeids.llm.openrouter import OpenRouterClient, Usage


def to_float32(vector: Sequence[float]) -> list[float]:
    return list(struct.unpack(f"<{len(vector)}f", struct.pack(f"<{len(vector)}f", *vector)))


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(data: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(data) // 4}f", data))


def query_key(model: str, provider: str | None, text: str) -> str:
    return hashlib.sha256(f"{model}\x1f{provider or ''}\x1f{text}".encode()).hexdigest()


class QueryCache(Protocol):
    def get(self, key: str) -> list[float] | None: ...

    def put(self, key: str, model: str, provider: str | None, vector: Sequence[float]) -> None: ...


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OpenRouterEmbedder:
    def __init__(
        self,
        client: OpenRouterClient,
        *,
        model: str,
        batch_size: int = 32,
        concurrency: int = 1,
        query_instruction: str | None = None,
        provider: str | None = None,
        allow_fallbacks: bool = True,
        cache: QueryCache | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.provider = provider
        self.allow_fallbacks = allow_fallbacks
        self.cache = cache
        self.cache_hits = 0
        self.batch_size = batch_size
        self.concurrency = max(1, concurrency)
        self.query_instruction = query_instruction
        self.dimension: int | None = None
        self.usage = Usage()
        self._lock = threading.Lock()  # batches run in parallel threads
        self._memo: dict[str, list[float]] = {}  # query vectors of this process (bounded)

    @classmethod
    def from_settings(cls, settings: Settings, client: OpenRouterClient | None = None,
                      cache: QueryCache | None = None):
        return cls(
            client or OpenRouterClient(settings),
            model=settings.embedding_model,
            batch_size=settings.embedding_batch_size,
            concurrency=settings.embedding_concurrency,
            query_instruction=settings.embedding_query_instruction,
            provider=settings.embedding_provider or None,
            allow_fallbacks=settings.embedding_allow_fallbacks,
            cache=cache if settings.query_embedding_cache else None,
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors in the order of ``texts``; up to ``concurrency`` batches in flight."""
        batches = [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        if self.concurrency == 1 or len(batches) <= 1:
            results = [self._embed(b) for b in batches]
        else:
            with ThreadPoolExecutor(max_workers=min(self.concurrency, len(batches))) as pool:
                # each batch runs in a copy of this context, so its trace span has our parent
                futures = [pool.submit(contextvars.copy_context().run, self._embed, b)
                           for b in batches]
                results = [f.result() for f in futures]  # keeps order; first error raises
        return [v for batch in results for v in batch]

    def _query(self, text: str) -> tuple[str, str]:
        query = f"Instruct: {self.query_instruction}\nQuery: {text}" if self.query_instruction \
            else text
        return query, query_key(self.model, self.provider, query)

    def _known(self, key: str) -> list[float] | None:
        vector = self._memo.get(key)
        if vector is None and self.cache is not None:
            vector = self.cache.get(key)
        if vector is not None:
            self.cache_hits += 1
        return vector

    def _remember(self, key: str, vector: list[float]) -> None:
        if len(self._memo) >= 512:
            self._memo.pop(next(iter(self._memo)))
        self._memo[key] = vector
        if self.cache is not None:
            self.cache.put(key, self.model, self.provider, vector)

    def embed_query(self, text: str) -> list[float]:
        query, key = self._query(text)
        vector = self._known(key)
        if vector is None:
            vector = self._embed([query])[0]
            self._remember(key, vector)
        return vector

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Several query vectors with one request for those not known yet (an agent's searches)."""
        wanted = [self._query(t) for t in texts]
        found = {key: v for _, key in wanted if (v := self._known(key)) is not None}
        missing = list(dict.fromkeys((q, key) for q, key in wanted if key not in found))
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start:start + self.batch_size]
            for (_, key), vector in zip(batch, self._embed([q for q, _ in batch]), strict=True):
                self._remember(key, vector)
                found[key] = vector
        return [found[key] for _, key in wanted]

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = self.client.embed(texts, model=self.model, provider=self.provider,
                                   allow_fallbacks=self.allow_fallbacks)
        dim = len(result.vectors[0])
        with self._lock:
            if self.dimension is None:
                self.dimension = dim
            elif dim != self.dimension:
                raise ValueError(f"embedding size changed from {self.dimension} to {dim}")
            u = self.usage
            self.usage = Usage(
                prompt_tokens=u.prompt_tokens + result.usage.prompt_tokens,
                total_tokens=u.total_tokens + result.usage.total_tokens,
                cost=(u.cost or 0) + result.usage.cost if result.usage.cost is not None else u.cost,
            )
        return [to_float32(v) for v in result.vectors]


class HashEmbedder:
    """Deterministic bag-of-words hashing vectors. Offline tests only."""

    def __init__(self, dimension: int = 256) -> None:
        self.model = f"test/hash-{dimension}"
        self.dimension = dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dimension
        for token in re.findall(r"[a-z0-9]+", text.casefold()):
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            v[h % self.dimension] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]
