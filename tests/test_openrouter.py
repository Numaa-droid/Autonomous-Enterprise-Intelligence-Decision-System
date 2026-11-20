"""OpenRouter client and embedder, against a fake HTTP transport (no network, no key needed)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from aeids.config import Settings
from aeids.llm.embedding import HashEmbedder, OpenRouterEmbedder
from aeids.llm.openrouter import (
    LLMAuthError,
    LLMBadRequest,
    LLMConfigError,
    LLMResponseError,
    LLMTransientError,
    OpenRouterClient,
)

KEY = "sk-or-test-SECRET-123"


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, openrouter_api_key=KEY, llm_max_retries=3,
                    llm_backoff_seconds=1.0, **kw)


class Fake:
    """A transport that answers with queued responses and records requests."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, *headers = item
        return httpx.Response(status, json=body, headers=headers[0] if headers else None)


def _client(fake: Fake, sleeps: list[float] | None = None, **kw) -> OpenRouterClient:
    return OpenRouterClient(_settings(**kw), transport=httpx.MockTransport(fake),
                            sleep=(sleeps.append if sleeps is not None else lambda _s: None))


def _emb_body(n: int, dim: int = 4) -> dict:
    data = [{"index": i, "embedding": [float(i)] * dim, "object": "embedding"}
            for i in reversed(range(n))]  # out of order on purpose
    return {"data": data, "model": "qwen/qwen3-embedding-8b",
            "usage": {"prompt_tokens": 7 * n, "total_tokens": 7 * n, "cost": 0.0000007}}


def _chat_body(content: str = '{"status": "ok"}') -> dict:
    return {"id": "gen-1", "model": "qwen/qwen3.8-flash",
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
                      "cost": 0.00001}}


def test_embed_request_and_result() -> None:
    fake = Fake((200, _emb_body(3)))
    result = _client(fake).embed(["a", "b", "c"])
    req = fake.requests[0]
    assert req.url == "https://openrouter.ai/api/v1/embeddings"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(req.content) == {"model": "qwen/qwen3-embedding-8b",
                                       "input": ["a", "b", "c"]}
    assert [v[0] for v in result.vectors] == [0.0, 1.0, 2.0]  # sorted by index
    assert result.usage.total_tokens == 21
    assert result.usage.cost == Decimal("7E-7")
    assert result.attempts == 1


def test_chat_request_and_result() -> None:
    fake = Fake((200, _chat_body()))
    result = _client(fake).chat([{"role": "user", "content": "hi"}], max_tokens=10,
                                response_format={"type": "json_object"})
    payload = json.loads(fake.requests[0].content)
    assert payload["model"] == "qwen/qwen3.8-flash"
    assert payload["temperature"] == 0.0
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["usage"] == {"include": True}
    assert result.content == '{"status": "ok"}'
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (20, 5)
    assert result.response_id == "gen-1" and result.finish_reason == "stop"


def test_transient_errors_are_retried_with_backoff() -> None:
    sleeps: list[float] = []
    fake = Fake((429, {"error": {"message": "rate limited"}}, {"retry-after": "5"}),
                httpx.ConnectError("boom"),
                (503, {"error": {"message": "overloaded"}}),
                (200, _emb_body(1)))
    result = _client(fake, sleeps).embed(["x"])
    assert result.attempts == 4
    assert len(sleeps) == 3
    assert sleeps[0] >= 5  # Retry-After honoured
    assert 2.0 <= sleeps[1] <= 2.5 and 4.0 <= sleeps[2] <= 5.0  # exponential


def test_retries_are_bounded() -> None:
    fake = Fake(*[(502, {"error": {"message": "bad gateway"}})] * 4)
    with pytest.raises(LLMTransientError):
        _client(fake).embed(["x"])
    assert len(fake.requests) == 4  # 1 + llm_max_retries


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_errors_are_never_retried(status: int) -> None:
    fake = Fake((status, {"error": {"message": f"invalid key {KEY}"}}))
    with pytest.raises(LLMAuthError) as info:
        _client(fake).chat([{"role": "user", "content": "hi"}])
    assert len(fake.requests) == 1
    assert KEY not in info.value.message and "***" in info.value.message


def test_bad_request_is_not_retried() -> None:
    fake = Fake((400, {"error": {"message": "unknown model"}}))
    with pytest.raises(LLMBadRequest, match="unknown model"):
        _client(fake).embed(["x"])
    assert len(fake.requests) == 1


def test_provider_error_inside_200_is_classified() -> None:
    fake = Fake((200, {"error": {"code": 502, "message": "upstream down"}}),
                (200, _chat_body()))
    assert _client(fake).chat([{"role": "user", "content": "hi"}]).attempts == 2


def test_malformed_responses() -> None:
    with pytest.raises(LLMResponseError):
        _client(Fake((200, {"data": [], "model": "m"}))).embed(["x"])
    with pytest.raises(LLMResponseError):
        _client(Fake((200, {"choices": []}))).chat([{"role": "user", "content": "hi"}])


def test_missing_key_fails_before_any_request() -> None:
    fake = Fake()
    client = OpenRouterClient(Settings(_env_file=None), transport=httpx.MockTransport(fake))
    with pytest.raises(LLMConfigError, match="AEIDS_OPENROUTER_API_KEY"):
        client.embed(["x"])
    assert fake.requests == []


def test_key_is_not_in_settings_repr() -> None:
    assert KEY not in repr(_settings())


def test_empty_text_is_refused() -> None:
    with pytest.raises(ValueError):
        _client(Fake()).embed(["ok", "  "])


def test_embedder_batches_and_adds_query_instruction() -> None:
    fake = Fake((200, _emb_body(2)), (200, _emb_body(1)), (200, _emb_body(1)))
    emb = OpenRouterEmbedder(_client(fake), model="qwen/qwen3-embedding-8b", batch_size=2,
                             query_instruction="Find the passages")
    vectors = emb.embed_documents(["a", "b", "c"])
    assert len(vectors) == 3 and emb.dimension == 4
    assert [json.loads(r.content)["input"] for r in fake.requests] == [["a", "b"], ["c"]]
    emb.embed_query("revenue 2025")
    assert json.loads(fake.requests[-1].content)["input"] == [
        "Instruct: Find the passages\nQuery: revenue 2025"]
    assert emb.usage.total_tokens == 28


def test_embedder_rejects_changing_dimension() -> None:
    fake = Fake((200, _emb_body(1, dim=4)), (200, _emb_body(1, dim=8)))
    emb = OpenRouterEmbedder(_client(fake), model="m")
    emb.embed_documents(["a"])
    with pytest.raises(ValueError, match="changed"):
        emb.embed_documents(["b"])


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    h = HashEmbedder(64)
    a, b = h.embed_documents(["net sales 2025", "net sales 2025"])
    assert a == b and abs(sum(x * x for x in a) - 1) < 1e-9
    assert h.embed_query("net sales") != h.embed_query("cash flow")


def test_embedder_parallel_batches_keep_order_and_sum_usage() -> None:
    """Batches in parallel threads: vectors come back in input order, usage adds up."""
    import threading
    import time as _time

    seen_threads: set[int] = set()

    def answer(request: httpx.Request) -> httpx.Response:
        seen_threads.add(threading.get_ident())
        texts = json.loads(request.content)["input"]
        _time.sleep(0.02 * (3 - int(texts[0]) % 3))  # later batches finish first
        data = [{"index": i, "embedding": [float(t), 0.0], "object": "embedding"}
                for i, t in enumerate(texts)]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": len(texts),
                                                                 "total_tokens": len(texts)}})

    client = OpenRouterClient(_settings(), transport=httpx.MockTransport(answer),
                              sleep=lambda _s: None)
    emb = OpenRouterEmbedder(client, model="m", batch_size=2, concurrency=4)
    texts = [str(i) for i in range(9)]
    vectors = emb.embed_documents(texts)
    assert [v[0] for v in vectors] == [float(i) for i in range(9)]
    assert emb.usage.total_tokens == 9 and emb.dimension == 2
    assert len(seen_threads) > 1
