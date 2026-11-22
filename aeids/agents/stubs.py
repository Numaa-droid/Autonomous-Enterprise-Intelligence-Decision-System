"""Release 0 / M3 agents: no LLM yet (M4 replaces Document Intelligence and Financial Analyst).

* ``document_intelligence``: real hybrid search over the workflow's scope; proposes the top
  passages as evidence (exact quotes from the chunk text). No numbers are extracted yet.
* ``financial_analyst``: turns the evidence from its dependency into one summary finding that
  cites it. No calculation yet (the calc module arrives in M4).
* ``verification``: a real structural check of every finding: each cited evidence record
  exists for this tenant and its quote is really in the chunk it came from. Anything else is
  rejected with rework instructions.

``AEIDS_STUB_AGENT_DELAY_SECONDS`` slows the first two down, to watch a workflow move in the
console or to test a worker being killed mid-task.
"""

from __future__ import annotations

import re
import time
import uuid

from aeids.agents.base import AgentContext, Registry
from aeids.contracts import (
    AgentResult,
    EvidenceProposal,
    EvidenceRef,
    Finding,
    TaskMessage,
    VerificationDecision,
)
from aeids.retrieval.search import SearchFilters

_HEADER = re.compile(r"^Section: [^\n]*\n+")


def excerpt(chunk_text: str, limit: int = 600) -> str:
    """A quote that is literally part of the chunk text (the section header line dropped)."""
    body = _HEADER.sub("", chunk_text, count=1) or chunk_text
    return body[:limit].strip() or chunk_text[:limit]


def _filters(scope: dict) -> SearchFilters:
    return SearchFilters(
        company_ids=tuple(uuid.UUID(x) for x in scope.get("company_ids", [])),
        document_ids=tuple(uuid.UUID(x) for x in scope.get("document_ids", [])),
        fiscal_years=tuple(int(x) for x in scope.get("fiscal_years", [])),
    )


def _base(task: TaskMessage, agent: str) -> dict:
    return {"workflow_id": task.workflow_id, "task_id": task.task_id, "agent_name": agent,
            "trace_id": task.trace_id}


class DocumentIntelligenceStub:
    name = "document_intelligence"
    version = "0.1-stub"
    model = None
    prompt_version = None

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        if ctx.settings.stub_agent_delay_seconds:
            time.sleep(ctx.settings.stub_agent_delay_seconds)
        query = str(task.inputs.get("query") or task.objective)
        top_k = int(task.inputs.get("top_k", 5))
        hits = ctx.search(query, top_k=top_k, filters=_filters(task.inputs.get("scope", {})))
        if not hits:
            return AgentResult(**_base(task, self.name), status="needs_more_information",
                               needs="no indexed document in the workflow's scope matches the question")
        evidence = [EvidenceProposal(chunk_id=h.chunk_id, page=h.page_start, section=h.section,
                                     quote=excerpt(h.text)) for h in hits]
        return AgentResult(
            **_base(task, self.name), status="completed", evidence=evidence,
            outputs={"evidence_ids": [str(e.evidence_id) for e in evidence],
                     "sources": [f"{h.company_name} · {h.filename} p.{h.page_start}" for h in hits]},
            warnings=["stub agent: passages found, numbers not extracted yet (M4)"],
            metrics={"hits": len(hits)},
        )


class FinancialAnalystStub:
    name = "financial_analyst"
    version = "0.1-stub"
    model = None
    prompt_version = None

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        if ctx.settings.stub_agent_delay_seconds:
            time.sleep(ctx.settings.stub_agent_delay_seconds)
        evidence_ids = [uuid.UUID(x) for x in task.inputs.get("evidence_ids", [])]
        if not evidence_ids:
            return AgentResult(**_base(task, self.name), status="needs_more_information",
                               needs="no evidence was collected for this question")
        sources = task.inputs.get("sources", [])
        claim = (f"{len(evidence_ids)} passages address the question "
                 f"\"{task.inputs.get('query', task.objective)}\""
                 + (f" (top source: {sources[0]})" if sources else ""))
        finding = Finding(category="evidence_summary", claim=claim,
                          evidence=[EvidenceRef(evidence_id=e) for e in evidence_ids[:5]])
        return AgentResult(**_base(task, self.name), status="completed", findings=[finding],
                           next_action="submit_for_verification",
                           warnings=["stub agent: no calculation yet (M4)"])


class VerificationStub:
    name = "verification"
    version = "0.1"
    model = None
    prompt_version = None

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        decisions = []
        for f in task.inputs.get("findings", []):
            ids = [uuid.UUID(x) for x in f.get("evidence_ids", [])]
            found = ctx.evidence(ids)
            reasons = []
            missing = [str(i) for i in ids if i not in found]
            if not ids:
                reasons.append("the finding cites no evidence")
            if missing:
                reasons.append(f"cited evidence not found: {', '.join(missing)}")
            for ev in found.values():
                if not ev.chunk_text or ev.quote not in ev.chunk_text:
                    reasons.append(f"evidence {ev.evidence_id}: the quote is not in its source chunk")
            decisions.append(VerificationDecision(
                finding_id=uuid.UUID(f["finding_id"]),
                decision="rejected" if reasons else "verified",
                reasons=reasons or [f"{len(found)} cited passage(s) exist and quote their source exactly"],
                rework_instructions=("Cite only evidence records collected for this workflow; "
                                     "quote the source text exactly.") if reasons else None,
            ))
        return AgentResult(**_base(task, self.name), status="completed", verifications=decisions,
                           metrics={"checked": len(decisions)})


def default_registry() -> Registry:
    """The stand-ins for Document Intelligence and the Financial Analyst, with the real (M5)
    verifier: it needs no model, so every mode checks findings the same way."""
    from aeids.agents.verification import VerificationAgent

    return Registry([DocumentIntelligenceStub(), FinancialAnalystStub(), VerificationAgent()])
