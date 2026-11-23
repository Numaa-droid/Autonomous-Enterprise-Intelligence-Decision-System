"""Single-agent RAG baseline (MVP_PLAN.md M6, D-029): what AEIDS is measured against.

One retrieval and one model call: the question goes to the same hybrid search the agents use
(current versions of this tenant only), the top passages go to the same chat model with one
prompt (``prompt_v1.md``), and the model answers in JSON with the ids of the passages it used.
There is no planner, no calculation module and no verification: the model reads the figures and
does any arithmetic itself. That is the point of the comparison.

It keeps the rules that are about safety rather than about the AEIDS design: passages are
untrusted data inside ``<document>`` blocks (rule 7), the reply must validate against a schema and
is asked again at most once (rule 8), and the run is traced with its prompt version, model,
tokens, cost and latency. Cited ids that were not among the passages are dropped and counted.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.agents.llm_json import ChatModel, Usage, ask_json, load_prompt, untrusted
from aeids.llm.embedding import Embedder
from aeids.llm.openrouter import LLMError
from aeids.retrieval.search import SearchFilters, SearchHit, search
from aeids.retrieval.vector_index import VectorIndex

HERE = Path(__file__).parent
PROMPT_VERSION = "rag-v1"
DEFAULT_TOP_K = 8
MAX_SOURCE_CHARS = 2500  # per passage; a chunk is normally shorter


class Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    found: bool
    answer: str = Field(min_length=1, max_length=3000)
    citations: list[str] = Field(default_factory=list, max_length=50)


@dataclass(frozen=True)
class Source:
    source_id: str  # "S1", as the model sees it
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    company: str
    filename: str
    page_start: int
    page_end: int
    section: str | None


@dataclass
class BaselineAnswer:
    question: str
    found: bool
    answer: str
    citations: list[Source]
    sources: list[Source]
    unknown_citations: list[str] = field(default_factory=list)  # cited ids that were not given
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    trace_id: str | None = None
    latency_ms: float = 0.0
    llm_calls: int = 0
    total_tokens: int = 0
    cost_usd: Decimal | None = None
    error: str | None = None


def _source(i: int, h: SearchHit) -> Source:
    return Source(f"S{i}", h.chunk_id, h.document_id, h.version_id, h.company_name, h.filename,
                  h.page_start, h.page_end, h.section)


def sources_text(hits: list[SearchHit]) -> str:
    if not hits:
        return "(no passages were found)"
    return "\n\n".join(
        untrusted(f"S{i}", h.text[:MAX_SOURCE_CHARS], company=h.company_name, file=h.filename,
                  pages=f"{h.page_start}-{h.page_end}" if h.page_end != h.page_start else h.page_start,
                  section=h.section)
        for i, h in enumerate(hits, start=1))


def user_prompt(question: str, hits: list[SearchHit]) -> str:
    return f"QUESTION: {question}\n\nSOURCES:\n{sources_text(hits)}"


def answer(session: Session, index: VectorIndex | None, embedder: Embedder | None, llm: ChatModel,
           tenant_id: uuid.UUID, question: str, *, top_k: int = DEFAULT_TOP_K, model: str | None = None,
           filters: SearchFilters = SearchFilters()) -> BaselineAnswer:
    """Answer ``question`` from the tenant's current reports with one prompt."""
    t0 = time.perf_counter()
    usage = Usage()
    system = load_prompt(HERE / "prompt_v1.md")
    with tracing.span("baseline_rag", "chain", tenant_id=tenant_id, prompt_version=PROMPT_VERSION,
                      inputs={"question": question, "top_k": top_k}) as sp:
        hits = search(session, index, embedder, tenant_id, question, mode="hybrid", top_k=top_k,
                      filters=filters).hits
        sources = [_source(i, h) for i, h in enumerate(hits, start=1)]
        user = user_prompt(question, hits)
        reply: Reply | None = None
        error = None
        try:
            try:
                reply = ask_json(llm, Reply, system, user, usage, model=model, max_tokens=1500)
            except ValidationError as exc:  # schema failures are asked again once (rule 8)
                retry = (f"{user}\n\nYour previous reply was not valid ({str(exc)[:400]}). "
                         "Return only the JSON object described in the instructions.")
                reply = ask_json(llm, Reply, system, retry, usage, model=model, max_tokens=1500)
        except ValidationError as exc:
            error = f"invalid reply twice: {str(exc)[:300]}"
        except LLMError as exc:
            error = f"{type(exc).__name__}: {exc.message[:300]}"
        if error:
            sp.fail(error)

        by_id = {s.source_id: s for s in sources}
        cited, unknown = [], []
        for c in dict.fromkeys(x.strip().strip("[]").upper() for x in (reply.citations if reply else [])):
            (cited.append(by_id[c]) if c in by_id else unknown.append(c))
        out = BaselineAnswer(
            question=question, found=bool(reply and reply.found), answer=reply.answer if reply else "",
            citations=cited if reply and reply.found else [], sources=sources, unknown_citations=unknown,
            model=",".join(usage.models) or None, trace_id=sp.trace_id,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1), llm_calls=usage.calls,
            total_tokens=usage.total_tokens, cost_usd=usage.cost, error=error)
        sp.set(model=out.model, llm_calls=usage.calls, unknown_citations=len(unknown))
        sp.set_output({"found": out.found, "answer": out.answer,
                       "citations": [f"{c.source_id} {c.filename} p.{c.page_start}" for c in out.citations]})
    return out
