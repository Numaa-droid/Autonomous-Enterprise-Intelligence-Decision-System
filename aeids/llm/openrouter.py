"""OpenRouter client for embeddings and chat completions (OpenAI-compatible API).

Retry policy (CLAUDE.md rule 8): only transient failures are retried -- timeouts, connection
errors, HTTP 408/425/429/5xx -- with exponential backoff and jitter, honouring ``Retry-After``,
at most ``llm_max_retries`` times. Authentication, payment and permission errors (401/402/403)
and bad requests (400/404/413/422) are never retried.

The API key comes from settings (environment) only and never appears in errors or logs.
Every call returns model, token usage, cost (when OpenRouter reports it), latency and attempts,
for the ``agent_runs`` audit.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import httpx

from aeids import tracing
from aeids.config import Settings

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
AUTH_STATUS = {401, 402, 403}
MAX_RETRY_AFTER = 60.0


class LLMError(Exception):
    retryable = False

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class LLMConfigError(LLMError):
    """Missing or invalid configuration (e.g. no API key)."""


class LLMAuthError(LLMError):
    """401/402/403: bad key, no credits or no permission. Never retried."""


class LLMBadRequest(LLMError):
    """The request itself is wrong (unknown model, too long, invalid parameters)."""


class LLMTransientError(LLMError):
    retryable = True


class LLMResponseError(LLMError):
    """A 200 response without the expected content (e.g. an empty reply). Transient: providers
    occasionally return one; the task is retried with backoff, bounded by its max attempts."""

    retryable = True


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: Decimal | None = None  # USD, when OpenRouter reports it

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        cost = data.get("cost")
        prompt = int(data.get("prompt_tokens") or 0)
        completion = int(data.get("completion_tokens") or 0)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(data.get("total_tokens") or prompt + completion),
            cost=Decimal(str(cost)) if cost is not None else None,
        )


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    usage: Usage
    latency_ms: int
    attempts: int
    provider: str | None = None  # which OpenRouter provider served the request


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    finish_reason: str | None
    usage: Usage
    latency_ms: int
    attempts: int
    response_id: str | None


class OpenRouterClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=settings.openrouter_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
            transport=transport,
            headers={"X-Title": "AEIDS"},
        )

    def __enter__(self) -> OpenRouterClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ---------- public API ----------

    def embed(self, texts: Sequence[str], *, model: str | None = None, provider: str | None = None,
              allow_fallbacks: bool = True) -> EmbeddingResult:
        """``provider`` pins an OpenRouter provider (e.g. "nebius"); with ``allow_fallbacks``
        False no other provider is used (the same model differs between providers, D-023)."""
        if not texts:
            raise ValueError("no texts to embed")
        if any(not t or not t.strip() for t in texts):
            raise ValueError("cannot embed an empty text")
        model = model or self._settings.embedding_model
        with tracing.span("openrouter.embeddings", "embedding", model=model, pinned_provider=provider,
                          inputs={"texts": len(texts), "first_text": texts[0][:300]}) as sp:
            result = self._embed(texts, model, provider, allow_fallbacks)
            sp.set(**_usage_attrs(result.model, result.usage, result.latency_ms, result.attempts),
                   dimension=len(result.vectors[0]), provider=result.provider)
            sp.set_output({"vectors": len(result.vectors), "dimension": len(result.vectors[0])})
            return result

    def _embed(self, texts: Sequence[str], model: str, provider: str | None = None,
               allow_fallbacks: bool = True) -> EmbeddingResult:
        payload: dict[str, Any] = {"model": model, "input": list(texts)}
        if provider:
            payload["provider"] = {"order": [provider], "allow_fallbacks": allow_fallbacks}
        body, attempts, latency = self._post("embeddings", payload)
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise LLMResponseError(
                f"embedding response has {len(data) if isinstance(data, list) else 'no'} "
                f"vectors for {len(texts)} inputs"
            )
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [d.get("embedding") for d in ordered]
        if not all(isinstance(v, list) and v for v in vectors):
            raise LLMResponseError("embedding response contains an empty vector")
        if len({len(v) for v in vectors}) != 1:
            raise LLMResponseError("embedding vectors have different lengths")
        return EmbeddingResult(
            vectors=[[float(x) for x in v] for v in vectors],
            model=body.get("model") or model,
            usage=Usage.from_json(body.get("usage")),
            latency_ms=latency,
            attempts=attempts,
            provider=body.get("provider"),
        )

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> ChatResult:
        model = model or self._settings.llm_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "usage": {"include": True},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if seed is not None:
            payload["seed"] = seed
        reasoning = self._settings.llm_reasoning
        if reasoning == "off":
            payload["reasoning"] = {"enabled": False}
        elif reasoning != "model_default":
            payload["reasoning"] = {"effort": reasoning}
        with tracing.span("openrouter.chat", "llm", model=model, temperature=temperature, reasoning=reasoning,
                          inputs={"messages": list(messages)}) as sp:
            result = self._chat_retrying(payload, model)
            sp.set(**_usage_attrs(result.model, result.usage, result.latency_ms, result.attempts),
                   prompt_tokens=result.usage.prompt_tokens,
                   completion_tokens=result.usage.completion_tokens,
                   finish_reason=result.finish_reason)
            sp.set_output({"content": result.content})
            return result

    def _chat_retrying(self, payload: dict[str, Any], model: str) -> ChatResult:
        """An empty reply (a reasoning model that returned no final text) is asked again, with the
        same bounded backoff as transport errors, before the task has to be retried."""
        attempt = 0
        while True:
            try:
                result = self._chat(payload, model)
                return result if not attempt else replace(result, attempts=result.attempts + attempt)
            except LLMResponseError as exc:
                attempt += 1
                if attempt > self._settings.llm_max_retries:
                    raise
                delay = self._delay(attempt, None)
                log.warning("OpenRouter chat: %s; asking again in %.1fs (%d)", exc.message, delay, attempt)
                self._sleep(delay)

    def _chat(self, payload: dict[str, Any], model: str) -> ChatResult:
        body, attempts, latency = self._post("chat/completions", payload)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("chat response has no choices")
        choice = choices[0]
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise LLMResponseError("chat response has no text content")
        return ChatResult(
            content=content,
            model=body.get("model") or model,
            finish_reason=choice.get("finish_reason"),
            usage=Usage.from_json(body.get("usage")),
            latency_ms=latency,
            attempts=attempts,
            response_id=body.get("id"),
        )

    # ---------- transport ----------

    def _auth_header(self) -> dict[str, str]:
        key = self._settings.openrouter_api_key
        if key is None or not key.get_secret_value().strip():
            raise LLMConfigError("AEIDS_OPENROUTER_API_KEY is not set")
        return {"Authorization": f"Bearer {key.get_secret_value().strip()}"}

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
        headers = self._auth_header()
        attempts = 0
        started = time.monotonic()
        while True:
            attempts += 1
            try:
                body = self._send(path, payload, headers)
                return body, attempts, int((time.monotonic() - started) * 1000)
            except LLMError as exc:
                if not exc.retryable or attempts > self._settings.llm_max_retries:
                    raise
                delay = self._delay(attempts, getattr(exc, "retry_after", None))
                log.warning("OpenRouter %s failed (%s); retry %d in %.1fs",
                            path, exc.message, attempts, delay)
                self._sleep(delay)

    def _send(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            resp = self._http.post(path, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise LLMTransientError(f"timeout calling OpenRouter {path}") from None
        except httpx.TransportError as exc:
            raise LLMTransientError(f"cannot reach OpenRouter: {type(exc).__name__}") from None

        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200 or (isinstance(body, dict) and "error" in body):
            raise self._error(resp, body)
        if not isinstance(body, dict):
            raise LLMTransientError(f"OpenRouter {path} returned a non-JSON body")
        return body

    def _error(self, resp: httpx.Response, body: Any) -> LLMError:
        err = body.get("error") if isinstance(body, dict) else None
        detail = err.get("message") if isinstance(err, dict) else None
        status = resp.status_code
        if status == 200 and isinstance(err, dict):  # provider error inside a 200 body
            code = err.get("code")
            status = code if isinstance(code, int) else 502
        message = self._scrub(f"OpenRouter HTTP {status}: {detail or resp.reason_phrase}")[:500]
        if status in AUTH_STATUS:
            return LLMAuthError(message, status)
        if status in TRANSIENT_STATUS:
            exc = LLMTransientError(message, status)
            exc.retry_after = _retry_after(resp)  # type: ignore[attr-defined]
            return exc
        return LLMBadRequest(message, status)

    def _scrub(self, text: str) -> str:
        key = self._settings.openrouter_api_key
        secret = key.get_secret_value().strip() if key else ""
        return text.replace(secret, "***") if secret else text

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        base = self._settings.llm_backoff_seconds * (2 ** (attempt - 1))
        delay = base * (1 + random.uniform(0, 0.25))
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER))
        return delay


def _usage_attrs(model: str, usage: Usage, latency_ms: int, attempts: int) -> dict[str, Any]:
    return {"model": model, "total_tokens": usage.total_tokens, "cost_usd": usage.cost,
            "latency_ms": latency_ms, "attempts": attempts}


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
