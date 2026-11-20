"""Run tracing: every operation becomes a timed span in a trace (like LangSmith), stored in
Postgres (``trace_spans``) and shown in the dashboard (D-022).

    with span("search", "retrieval", inputs={"query": q}) as s:
        ...
        s.set_output({"hits": 10})
        s.set(model="qwen/...", total_tokens=123)

Spans nest through a context variable: a span opened inside another becomes its child and
shares its ``trace_id`` (OpenTelemetry-shaped ids: 32 hex trace, 16 hex span, so traces can be
exported to Langfuse / OTel later). Work handed to a thread pool keeps its parent when it is
submitted with ``contextvars.copy_context().run``.

Tracing never breaks the work it observes: a failing sink is logged once and ignored. Inputs and
outputs are made JSON-safe and truncated; strings that look like API keys are redacted (rule 9).
Document text in inputs/outputs is untrusted data (rule 7): the dashboard shows it as text only.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging
import re
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any, Protocol

log = logging.getLogger(__name__)

MAX_STRING = 2000
MAX_ITEMS = 50
MAX_DEPTH = 5
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9_\-.]{8,})")

KINDS = ("job", "tool", "parser", "index", "embedding", "llm", "vectordb", "retrieval", "eval",
         "chain")


@dataclasses.dataclass
class Span:
    name: str
    kind: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    tenant_id: uuid.UUID | None
    started_at: datetime
    inputs: Any = None
    outputs: Any = None
    attributes: dict[str, Any] = dataclasses.field(default_factory=dict)
    status: str = "ok"
    error: str | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    _t0: float = dataclasses.field(default_factory=time.perf_counter, repr=False)

    def set_output(self, value: Any) -> None:
        self.outputs = value

    def set(self, **attributes: Any) -> None:
        """Attributes; ``model``, ``total_tokens`` and ``cost_usd`` are also columns."""
        self.attributes.update(attributes)

    def fail(self, message: str) -> None:
        self.status, self.error = "error", message


class TraceSink(Protocol):
    def write(self, span: Span) -> None: ...


_current: contextvars.ContextVar[Span | None] = contextvars.ContextVar("aeids_span", default=None)
_sink: TraceSink | None = None
_sink_failed = threading.Event()


def configure(sink: TraceSink | None) -> None:
    """Where finished spans go; None turns storing off (spans are still timed)."""
    global _sink
    _sink = sink
    _sink_failed.clear()


def current() -> Span | None:
    return _current.get()


@contextmanager
def span(name: str, kind: str = "chain", *, inputs: Any = None, tenant_id: uuid.UUID | None = None,
         trace_id: str | None = None, parent_span_id: str | None = None,
         **attributes: Any) -> Iterator[Span]:
    """``trace_id`` / ``parent_span_id`` attach a top-level span to an existing trace (e.g. a
    workflow's task runs, which happen in different processes, join the workflow's trace)."""
    parent = _current.get()
    s = Span(
        name=name[:200], kind=kind if kind in KINDS else "chain",
        trace_id=parent.trace_id if parent else (trace_id or secrets.token_hex(16)),
        span_id=secrets.token_hex(8),
        parent_span_id=parent.span_id if parent else parent_span_id,
        tenant_id=tenant_id or (parent.tenant_id if parent else None),
        started_at=datetime.now(UTC), inputs=inputs, attributes=dict(attributes),
    )
    token = _current.set(s)
    try:
        yield s
    except BaseException as exc:
        if s.status == "ok":
            message = getattr(exc, "message", None) or str(exc)
            s.fail(f"{type(exc).__name__}: {message}"[:2000])
        raise
    finally:
        _current.reset(token)
        s.duration_ms = round((time.perf_counter() - s._t0) * 1000, 2)
        s.ended_at = datetime.now(UTC)
        _emit(s)


def _emit(s: Span) -> None:
    sink = _sink
    if sink is None:
        return
    try:
        sink.write(s)
    except Exception as exc:  # noqa: BLE001 — tracing must never break the traced work
        if not _sink_failed.is_set():
            _sink_failed.set()
            log.warning("trace spans are not being stored: %s", type(exc).__name__)


# --- JSON-safe, truncated, redacted ----------------------------------------------

def safe(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _SECRET.sub("[redacted]", value)
        return text if len(text) <= MAX_STRING else text[:MAX_STRING] + f"… [{len(text)} chars]"
    if isinstance(value, (Decimal, uuid.UUID, PurePath)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return safe(value.value, depth)
    if depth >= MAX_DEPTH:
        return "…"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)
                 if not f.name.startswith("_")}
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(k): safe(v, depth + 1) for k, v in items[:MAX_ITEMS]}
        if len(items) > MAX_ITEMS:
            out["…"] = f"{len(items) - MAX_ITEMS} more"
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [safe(v, depth + 1) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"… {len(items) - MAX_ITEMS} more")
        return out
    return safe(repr(value), depth)


class SqlTraceSink:
    """Writes each finished span as one row (its own short transaction, so it never mixes
    with the traced work's session)."""

    def __init__(self, engine: Any) -> None:
        if engine.dialect.name == "sqlite":
            # SQLite has one writer: never wait long behind the traced work's own transaction
            from sqlalchemy import create_engine

            engine = create_engine(engine.url, connect_args={"timeout": 0.2})
        self.engine = engine

    def write(self, s: Span) -> None:
        from aeids.db.models import TraceSpan

        attrs = safe(s.attributes) or {}
        tokens = s.attributes.get("total_tokens")
        cost = s.attributes.get("cost_usd")
        with self.engine.begin() as conn:
            conn.execute(TraceSpan.__table__.insert().values(
                id=uuid.uuid4(), trace_id=s.trace_id, span_id=s.span_id,
                parent_span_id=s.parent_span_id, tenant_id=s.tenant_id, name=s.name,
                kind=s.kind, status=s.status, started_at=s.started_at, ended_at=s.ended_at,
                duration_ms=s.duration_ms, inputs=safe(s.inputs), outputs=safe(s.outputs),
                attributes=attrs, error=safe(s.error),
                model=str(s.attributes["model"])[:200] if s.attributes.get("model") else None,
                total_tokens=int(tokens) if tokens is not None else None,
                cost_usd=Decimal(str(cost)) if cost is not None else None,
            ))
