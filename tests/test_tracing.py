"""Run tracing (aeids/tracing.py): nesting, errors, threads, safety, and stored traces."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.db.models import TraceSpan
from aeids.llm.embedding import OpenRouterEmbedder
from aeids.llm.openrouter import OpenRouterClient
from tests.factories import pdf_bytes
from tests.test_openrouter import _settings
from tests.test_tablelens import FakeTableLens, _client


class Memory:
    def __init__(self) -> None:
        self.spans: list[tracing.Span] = []

    def write(self, span: tracing.Span) -> None:
        self.spans.append(span)


@pytest.fixture
def mem() -> Memory:
    sink = Memory()
    tracing.configure(sink)
    return sink


def test_spans_nest_into_one_trace(mem: Memory) -> None:
    with tracing.span("outer", "job", inputs={"a": 1}) as outer:
        with tracing.span("inner", "tool") as inner:
            inner.set(total_tokens=5)
        outer.set_output("done")
    inner_s, outer_s = mem.spans  # children finish first
    assert inner_s.trace_id == outer_s.trace_id and len(outer_s.trace_id) == 32
    assert inner_s.parent_span_id == outer_s.span_id and outer_s.parent_span_id is None
    assert outer_s.duration_ms >= inner_s.duration_ms >= 0
    assert outer_s.outputs == "done" and inner_s.attributes == {"total_tokens": 5}
    with tracing.span("next"):
        pass
    assert mem.spans[-1].trace_id != outer_s.trace_id  # a new root starts a new trace


def test_errors_are_recorded_and_raised(mem: Memory) -> None:
    with pytest.raises(ValueError):
        with tracing.span("parent"):
            with tracing.span("child"):
                raise ValueError("boom")
    child, parent = mem.spans
    assert child.status == parent.status == "error"
    assert child.error == "ValueError: boom"


def test_a_failing_sink_never_breaks_the_work() -> None:
    class Broken:
        def write(self, span):
            raise RuntimeError("database down")

    tracing.configure(Broken())
    with tracing.span("work") as s:
        s.set_output(42)
    assert s.outputs == 42


def test_parallel_embedding_batches_keep_their_parent(mem: Memory) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(texts))]
        return httpx.Response(200, json={"data": data, "model": "m",
                                         "usage": {"total_tokens": len(texts), "cost": 0.001}})

    client = OpenRouterClient(_settings(), transport=httpx.MockTransport(answer),
                              sleep=lambda _s: None)
    emb = OpenRouterEmbedder(client, model="m", batch_size=2, concurrency=3)
    with tracing.span("embed_chunks") as parent:
        emb.embed_documents([f"text {i}" for i in range(6)])
    calls = [s for s in mem.spans if s.kind == "embedding"]
    assert len(calls) == 3
    assert {s.parent_span_id for s in calls} == {parent.span_id}
    assert {s.trace_id for s in calls} == {parent.trace_id}
    assert sum(s.attributes["total_tokens"] for s in calls) == 6
    assert calls[0].attributes["cost_usd"] == Decimal("0.001") and calls[0].attributes["model"] == "m"


def test_safe_truncates_converts_and_redacts() -> None:
    out = tracing.safe({"key": "Bearer sk-or-v1-abcdefghijklmnop", "n": Decimal("1.5"),
                        "id": uuid.UUID(int=1), "long": "x" * 5000, "list": list(range(100))})
    assert "sk-or" not in json.dumps(out) and out["key"] == "[redacted]"
    assert out["n"] == "1.5" and out["id"] == str(uuid.UUID(int=1))
    assert len(out["long"]) < 2100 and out["long"].endswith("[5000 chars]")
    assert len(out["list"]) == 51


def test_processing_an_upload_is_traced(client: TestClient, session: Session) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(FakeTableLens())
    resp = client.post("/api/v1/documents", data={"company_name": "Apple Inc."},
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "t"), "application/pdf")})
    assert resp.json()["version"]["parser_status"] == "indexed"
    spans = list(session.scalars(select(TraceSpan)))
    root = [s for s in spans if s.parent_span_id is None]
    assert [r.name for r in root] == ["process_version"]
    names = {s.name for s in spans}
    assert {"parse_version", "tablelens", "normalise_and_chunk", "index_version", "embed_chunks",
            "qdrant.replace_version", "qdrant.set_current"} <= names
    assert {s.trace_id for s in spans} == {root[0].trace_id}
    assert root[0].outputs["status"] == "indexed" and root[0].tenant_id is not None
