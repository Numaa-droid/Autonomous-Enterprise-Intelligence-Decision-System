"""D-037: a hosted Qdrant gets its API key from settings; a local one gets none; the key is never printed."""

from __future__ import annotations

from aeids.config import Settings
from aeids.retrieval import vector_index
from aeids.retrieval.vector_index import VectorIndex


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(vector_index, "QdrantClient", FakeClient)
    return seen


def test_api_key_is_sent_when_set(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    s = Settings(_env_file=None, qdrant_url="https://x.cloud.qdrant.io:6333", qdrant_api_key="k-123")
    VectorIndex.from_settings(s)
    assert seen["url"] == "https://x.cloud.qdrant.io:6333"
    assert seen["api_key"] == "k-123"


def test_no_api_key_for_local_qdrant(monkeypatch) -> None:
    seen = _capture(monkeypatch)
    VectorIndex.from_settings(Settings(_env_file=None, qdrant_api_key=None))
    assert seen["api_key"] is None
    VectorIndex.from_settings(Settings(_env_file=None, qdrant_api_key=""))
    assert seen["api_key"] is None


def test_api_key_is_not_printed() -> None:
    s = Settings(_env_file=None, qdrant_api_key="k-secret-123")
    assert "k-secret-123" not in repr(s) and "k-secret-123" not in str(s)
