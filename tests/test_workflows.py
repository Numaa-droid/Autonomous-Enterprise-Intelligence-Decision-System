"""M3: contracts, state machines, Supervisor, runner and workflow API (stub agents).

"Done when" (MVP_PLAN.md M3): a stub workflow survives a killed worker and finishes without
duplicate work -> test_killed_worker_is_recovered_without_duplicate_work.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeids.agents.base import Registry
from aeids.agents.stubs import DocumentIntelligenceStub, FinancialAnalystStub, VerificationStub
from aeids.contracts import AgentResult, Calculation, EvidenceRef, Finding, Quantity, VerificationDecision
from aeids.db.models import AgentTask, AuditEvent, EvidenceRecord, FindingRecord, TaskStatus, TaskTransition, utcnow
from aeids.llm.openrouter import LLMTransientError
from aeids.orchestration import runner
from aeids.orchestration import supervisor as sup
from aeids.orchestration.states import TASK_TRANSITIONS, IllegalTransition, move_task
from tests.factories import pdf_bytes
from tests.test_search import report
from tests.test_tablelens import FakeTableLens, _client


# ---------------- contracts ----------------

def _ids():
    return {"workflow_id": uuid.uuid4(), "task_id": uuid.uuid4(), "agent_name": "x", "trace_id": "t"}


def test_numbers_are_decimal_strings_never_floats() -> None:
    q = Quantity(value="416161", unit="currency", currency="USD", scale="million", period="FY2025")
    assert q.value == Decimal("416161") and q.model_dump(mode="json")["value"] == "416161"
    with pytest.raises(ValidationError, match="not floats"):
        Quantity(value=1.5, unit="ratio")
    with pytest.raises(ValidationError, match="ISO currency"):
        Quantity(value="1", unit="currency")
    with pytest.raises(ValidationError):
        Quantity(value="1", unit="ratio", colour="red")  # unknown fields refused


def test_finding_needs_evidence_and_consistent_calculation() -> None:
    ev = [EvidenceRef(evidence_id=uuid.uuid4())]
    with pytest.raises(ValidationError):
        Finding(claim="Revenue grew", evidence=[])  # rule 1
    with pytest.raises(ValidationError, match="needs its unit"):
        Finding(claim="x", value="1", evidence=ev)
    calc = Calculation(function="pct_change", formula="(c - p) / p * 100", output="12.5", unit="percent",
                       calc_version="1", inputs={"current": Quantity(value="900", unit="count")})
    with pytest.raises(ValidationError, match="calculation's output"):
        Finding(claim="x", value="13", unit="percent", calculation=calc, evidence=ev)
    assert Finding(claim="x", value="12.5", unit="percent", calculation=calc, evidence=ev)


def test_agent_result_rules() -> None:
    with pytest.raises(ValidationError, match="at least one error"):
        AgentResult(**_ids(), status="final_error")
    with pytest.raises(ValidationError, match="what is needed"):
        AgentResult(**_ids(), status="needs_more_information")
    f = Finding(claim="x", evidence=[EvidenceRef(evidence_id=uuid.uuid4())])
    with pytest.raises(ValidationError, match="submitted for verification"):
        AgentResult(**_ids(), status="completed", findings=[f])


# ---------------- state machines ----------------

@pytest.mark.parametrize("src", list(TaskStatus))
def test_only_listed_task_transitions_are_legal(src: TaskStatus) -> None:
    for dst in TaskStatus:
        task = AgentTask(status=src.value, plan_key="k")
        calls: list = []
        fake = type("S", (), {"add": lambda self, o: calls.append(o)})()
        if dst in TASK_TRANSITIONS[src]:
            move_task(fake, task, dst, reason="r", actor="a")
            assert task.status == dst.value and isinstance(calls[0], TaskTransition)
        else:
            with pytest.raises(IllegalTransition):
                move_task(fake, task, dst, reason="r", actor="a")
            assert task.status == src.value and calls == []  # nothing written


# ---------------- workflows ----------------

class Counting:
    """Wraps an agent and counts how often it really runs."""

    def __init__(self, agent, fail_with=None, times=1):
        self.agent, self.calls, self.fail_with, self.times = agent, 0, fail_with, times
        for k in ("name", "version", "model", "prompt_version"):
            setattr(self, k, getattr(agent, k))

    def run(self, task, ctx):
        self.calls += 1
        if self.fail_with is not None and self.calls <= self.times:
            raise self.fail_with
        return self.agent.run(task, ctx)


class Env:
    pass


@pytest.fixture
def env(client: TestClient) -> Env:
    """An indexed Apple report in the default tenant, and the pieces to run workflows."""
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline", "task_retry_backoff_seconds": 0})
    state.tablelens = _client(FakeTableLens(report()))
    body = client.post("/api/v1/documents", data={"company_name": "Apple Inc.", "fiscal_year": "2025"},
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "w"), "application/pdf")}).json()
    assert body["version"]["parser_status"] == "indexed"
    e = Env()
    e.client, e.state, e.sessions = client, state, state.sessionmaker
    e.di, e.fa, e.ver = Counting(DocumentIntelligenceStub()), Counting(FinancialAnalystStub()), Counting(VerificationStub())
    state.registry = Registry([e.di, e.fa, e.ver])
    return e


def _create(e: Env, query: str = "What supply chain risks does Apple face?", mode: str | None = None) -> dict:
    if mode:
        e.state.settings = e.state.settings.model_copy(update={"jobs_mode": mode})
    resp = e.client.post("/api/v1/workflows", json={"query": query})
    assert resp.status_code == 202, resp.text
    return resp.json()


def _tasks(e: Env, wf_id: str) -> dict:
    return {t["plan_key"]: t for t in e.client.get(f"/api/v1/workflows/{wf_id}/tasks").json()}


def _drive(e: Env, wf_id: str, **kw) -> str:
    return runner.drive(e.sessions, uuid.UUID(wf_id), registry=e.state.registry, settings=e.state.settings,
                        index=e.state.vector_index, embedder=e.state.embedder, **kw)


def _run(e: Env, task_id, **kw) -> str:
    return runner.run_task(e.sessions, task_id, registry=e.state.registry, settings=e.state.settings,
                           index=e.state.vector_index, embedder=e.state.embedder, **kw)


def test_workflow_plans_runs_verifies_and_reports(env: Env) -> None:
    wf = _create(env)
    assert wf["status"] == "COMPLETED" and wf["processing"] == "COMPLETED"
    tasks = _tasks(env, wf["id"])
    assert set(tasks) == {"extract", "analyse", "verify:analyse"}
    assert all(t["status"] == "VERIFIED" and t["attempts"] == 1 for t in tasks.values())
    assert tasks["analyse"]["depends_on"] == [str(tasks["extract"]["id"])]
    assert [tr["to"] for tr in tasks["analyse"]["transitions"]] == \
        ["PLANNED", "QUEUED", "RUNNING", "SUCCEEDED", "VERIFYING", "VERIFIED"]
    assert [tr["to"] for tr in tasks["extract"]["transitions"]][-2:] == ["SUCCEEDED", "VERIFIED"]  # no findings
    findings = env.client.get(f"/api/v1/workflows/{wf['id']}/findings").json()
    assert len(findings) == 1 and findings[0]["verification_status"] == "verified"
    ev = findings[0]["evidence"]
    assert ev and all(e["file"] == "NASDAQ_AAPL_2025.pdf" and e["page"] >= 1 and e["quote"] for e in ev)
    full = env.client.get(f"/api/v1/workflows/{wf['id']}").json()
    assert full["report"]["verified_findings"][0]["evidence"] and full["report"]["limitations"] == []
    assert [s["key"] for s in full["plan"]] == ["extract", "analyse"]
    assert env.di.calls == env.fa.calls == env.ver.calls == 1


def test_evidence_quotes_are_exactly_in_their_chunks(env: Env, session: Session) -> None:
    from aeids.db.models import DocumentChunk

    _create(env)
    evidence = list(session.scalars(select(EvidenceRecord)))
    assert evidence and all(ev.quote in session.get(DocumentChunk, ev.chunk_id).text for ev in evidence)


def test_killed_worker_is_recovered_without_duplicate_work(env: Env, session: Session) -> None:
    """M3 done-when: a worker dies holding the 'analyse' task; once its lease expires the task is
    re-queued, runs once more, and the workflow finishes. 'extract' (already VERIFIED) never runs
    again, and nothing is stored twice."""
    wf = _create(env, mode="off")
    wid, s = uuid.UUID(wf["id"]), env.state.settings
    with env.sessions() as db:
        (extract_id,) = sup.tick(db, wid, settings=s).queued
    assert _run(env, extract_id) == "done"
    with env.sessions() as db:
        (analyse_id,) = sup.tick(db, wid, settings=s).queued
    assert _run(env, analyse_id, worker="w-dead", crash_after_claim=True) == "crashed"
    with env.sessions() as db:  # before the lease expires, nobody takes it over
        assert sup.tick(db, wid, settings=s).queued == []
        assert db.get(AgentTask, analyse_id).status == "RUNNING"
    later = utcnow() + timedelta(seconds=s.task_timeout_seconds + 1)
    assert _drive(env, wf["id"], clock=lambda: later) == "COMPLETED"

    tasks = _tasks(env, wf["id"])
    assert tasks["analyse"]["attempts"] == 2  # the lost attempt counts
    assert any(tr["actor"] == "lease-recovery" and tr["to"] == "QUEUED" for tr in tasks["analyse"]["transitions"])
    assert env.di.calls == 1  # VERIFIED work is never redone
    assert env.fa.calls == 1  # the crashed attempt never reached the agent; the re-run ran once
    assert session.scalar(select(func.count()).select_from(FindingRecord)) == 1
    assert session.scalar(select(func.count()).select_from(EvidenceRecord)) == 3  # stored once
    keys = [t["idempotency_key"] for t in tasks.values()]
    assert len(keys) == len(set(keys))


def test_a_stale_worker_cannot_write_after_losing_its_lease(env: Env) -> None:
    wf = _create(env, mode="off")
    wid, s = uuid.UUID(wf["id"]), env.state.settings
    with env.sessions() as db:
        (extract_id,) = sup.tick(db, wid, settings=s).queued
    later = utcnow() + timedelta(seconds=s.task_timeout_seconds + 1)
    with env.sessions() as db:
        assert runner.claim(db, extract_id, "w-old", utcnow()) is not None
        sup.tick(db, wid, settings=s, now=later)  # lease expired -> QUEUED
    with env.sessions() as db:
        assert runner.claim(db, extract_id, "w-new", later) is not None
        assert runner.claim(db, extract_id, "w-third", later) is None  # one claim at a time
    stale = AgentResult(workflow_id=wid, task_id=extract_id, agent_name="document_intelligence",
                        status="completed", trace_id="t")
    with env.sessions() as db:
        t = db.get(AgentTask, extract_id)
        assert sup.accept_result(db, t, stale, sup.RunInfo(worker="w-old", agent_version="x"), settings=s) is False
        db.commit()
    with env.sessions() as db:
        assert db.get(AgentTask, extract_id).status == "RUNNING"  # still w-new's


def test_transient_errors_are_retried(env: Env) -> None:
    env.fa.fail_with = LLMTransientError("OpenRouter HTTP 503")
    wf = _create(env)
    assert wf["status"] == "COMPLETED"
    t = _tasks(env, wf["id"])["analyse"]
    assert t["attempts"] == 2 and t["status"] == "VERIFIED"
    assert [r["status"] for r in t["runs"]] == ["transient_error", "completed"]


def test_permission_errors_are_never_retried(env: Env) -> None:
    env.fa.fail_with = PermissionError("tenant mismatch")
    wf = _create(env)
    t = _tasks(env, wf["id"])["analyse"]
    assert t["status"] == "FAILED_FINAL" and t["attempts"] == 1
    assert wf["status"] == "COMPLETED"  # partial results are limitations, not a crash
    report_ = env.client.get(f"/api/v1/workflows/{wf['id']}").json()["report"]
    assert report_["verified_findings"] == []
    assert report_["limitations"][0].startswith("analyse (financial_analyst) ended FAILED_FINAL")


def test_schema_errors_are_retried_once(env: Env) -> None:
    class Bad:
        name, version, model, prompt_version = "financial_analyst", "bad", None, None
        calls = 0

        def run(self, task, ctx):
            Bad.calls += 1
            return AgentResult(workflow_id=task.workflow_id, task_id=uuid.uuid4(),  # another task
                               agent_name=self.name, status="completed", trace_id=task.trace_id)

    env.state.registry = Registry([env.di, Bad(), env.ver])
    wf = _create(env)
    t = _tasks(env, wf["id"])["analyse"]
    assert t["status"] == "FAILED_FINAL" and Bad.calls == 2
    assert [r["status"] for r in t["runs"]] == ["schema_error", "schema_error"]


def test_invented_quotes_are_refused(env: Env, session: Session) -> None:
    from aeids.contracts import EvidenceProposal

    class Inventor(DocumentIntelligenceStub):
        def run(self, task, ctx):
            r = super().run(task, ctx)
            fake = [EvidenceProposal(chunk_id=e.chunk_id, page=e.page, quote="Revenue tripled.") for e in r.evidence]
            return r.model_copy(update={"evidence": fake})

    env.state.registry = Registry([Inventor(), env.fa, env.ver])
    wf = _create(env)
    tasks = _tasks(env, wf["id"])
    assert tasks["extract"]["status"] == "FAILED_FINAL"
    assert "quote is not in the chunk" in tasks["extract"]["runs"][0]["error"]
    assert session.scalar(select(func.count()).select_from(EvidenceRecord)) == 0
    assert tasks["analyse"]["status"] == "FAILED_FINAL"  # its dependency failed for good


def test_rejected_findings_get_one_rework_round(env: Env, session: Session) -> None:
    class StrictOnce(VerificationStub):
        calls = 0

        def run(self, task, ctx):
            StrictOnce.calls += 1
            r = super().run(task, ctx)
            if StrictOnce.calls == 1:
                return r.model_copy(update={"verifications": [
                    VerificationDecision(finding_id=v.finding_id, decision="rejected", reasons=["wrong period"],
                                         rework_instructions="Use FY2025 figures.") for v in r.verifications]})
            return r

    env.state.registry = Registry([env.di, env.fa, StrictOnce()])
    wf = _create(env)
    assert wf["status"] == "COMPLETED"
    analyse = [t for t in env.client.get(f"/api/v1/workflows/{wf['id']}/tasks").json() if t["plan_key"] == "analyse"]
    assert [(t["rework_round"], t["status"]) for t in analyse] == [(0, "REJECTED"), (1, "VERIFIED")]
    assert analyse[1]["parent_task_id"] == analyse[0]["id"]
    assert sorted(f.verification_status for f in session.scalars(select(FindingRecord))) == ["rejected", "verified"]
    report_ = env.client.get(f"/api/v1/workflows/{wf['id']}").json()["report"]
    assert len(report_["verified_findings"]) == 1 and report_["limitations"] == []  # superseded


def test_rejection_without_rounds_left_is_a_limitation(env: Env) -> None:
    env.state.settings = env.state.settings.model_copy(update={"workflow_max_rework_rounds": 0})

    class Reject(VerificationStub):
        def run(self, task, ctx):
            r = super().run(task, ctx)
            return r.model_copy(update={"verifications": [
                VerificationDecision(finding_id=v.finding_id, decision="rejected", reasons=["no"])
                for v in r.verifications]})

    env.state.registry = Registry([env.di, env.fa, Reject()])
    wf = _create(env)
    assert wf["status"] == "COMPLETED"
    report_ = env.client.get(f"/api/v1/workflows/{wf['id']}").json()["report"]
    assert report_["verified_findings"] == [] and "REJECTED" in report_["limitations"][0]


def test_nothing_to_search_needs_a_person(env: Env) -> None:
    class Empty(DocumentIntelligenceStub):
        def run(self, task, ctx):
            return AgentResult(workflow_id=task.workflow_id, task_id=task.task_id, agent_name=self.name,
                               status="needs_more_information", needs="no documents in scope",
                               trace_id=task.trace_id)

    env.state.registry = Registry([Empty(), env.fa, env.ver])
    wf = _create(env)
    assert wf["status"] == "NEEDS_HUMAN_REVIEW"
    t = _tasks(env, wf["id"])
    assert t["extract"]["status"] == "NEEDS_HUMAN_REVIEW" and t["analyse"]["status"] == "PLANNED"


def test_ticks_are_idempotent(env: Env, session: Session) -> None:
    wf = _create(env)
    with env.sessions() as db:
        for _ in range(3):
            assert sup.tick(db, uuid.UUID(wf["id"]), settings=env.state.settings).queued == []
    assert session.scalar(select(func.count()).select_from(AgentTask)) == 3


def test_every_change_is_persisted(env: Env, session: Session) -> None:
    _create(env)
    events = [e.details["to"] for e in session.scalars(select(AuditEvent).where(
        AuditEvent.action == "workflow.transition").order_by(AuditEvent.created_at))]
    assert events == ["PLANNING", "RUNNING", "REPORTING", "COMPLETED"]
    assert session.scalar(select(func.count()).select_from(TaskTransition)) >= 15


def test_queue_mode_enqueues_a_tick(env: Env, monkeypatch) -> None:
    from aeids.orchestration import jobs

    calls = []
    monkeypatch.setattr(jobs, "enqueue_tick", lambda settings, wid, **kw: calls.append(wid))
    wf = _create(env, mode="queue")
    assert wf["processing"] == "queued" and wf["status"] == "RUNNING" and len(calls) == 1


def test_cancel_and_not_found(env: Env) -> None:
    wf = _create(env, mode="off")
    assert env.client.post(f"/api/v1/workflows/{wf['id']}/cancel").json()["status"] == "CANCELLED"
    assert {t["status"] for t in _tasks(env, wf["id"]).values()} == {"CANCELLED"}
    assert env.client.post(f"/api/v1/workflows/{wf['id']}/cancel").status_code == 409
    assert env.client.get(f"/api/v1/workflows/{uuid.uuid4()}").status_code == 404
    assert len(env.client.get("/api/v1/workflows").json()) == 1


def test_trace_joins_the_workflow(env: Env, session: Session) -> None:
    from aeids.db.models import TraceSpan

    wf = _create(env)
    spans = list(session.scalars(select(TraceSpan).where(TraceSpan.trace_id == wf["trace_id"])))
    assert {"workflow", "task:extract", "task:analyse", "task:verify:analyse", "search"} <= {s.name for s in spans}
    root = next(s for s in spans if s.name == "workflow")
    assert root.parent_span_id is None and root.duration_ms is not None
    assert all(s.parent_span_id == root.span_id for s in spans if s.name.startswith("task:"))
