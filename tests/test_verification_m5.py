"""M5: verification replays calculations and checks every cited value; bounded rework; human review.

"Done when" (MVP_PLAN.md M5): all injected faults are caught -> the ``test_fault_*`` tests. A fault
is injected either by a faulty agent (a wrapper that alters a real agent's result: what a wrong
model would produce) or by changing stored rows between extraction and verification (what an
agent cannot produce through the contract, e.g. a wrong page or a superseded version). Each test
checks the rejection reason, then that bounded rework recovers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from aeids import calc
from aeids.contracts import EvidenceRef
from aeids.db.models import AgentTask, Document, DocumentChunk, EvidenceRecord, FindingRecord, VerificationResult
from aeids.orchestration import runner
from aeids.orchestration import supervisor as sup
from tests.factories import pdf_bytes
from tests.test_agents_m4 import Env, FakeLLM, env  # noqa: F401  (env is a fixture)

QUESTION = "Compare Apple's revenue and operating margin FY2023–FY2025"


class Faulty:
    """Wraps a real agent; ``mutate`` alters its result on the first ``times`` runs with findings."""

    def __init__(self, agent, mutate: Callable, times: int = 1) -> None:
        self.agent, self.mutate, self.times, self.calls = agent, mutate, times, 0
        for k in ("name", "version", "model", "prompt_version"):
            setattr(self, k, getattr(agent, k))

    def run(self, task, ctx):
        r = self.agent.run(task, ctx)
        if r.findings and self.calls < self.times:
            self.calls += 1
            return self.mutate(r)
        return r


def _swap(env: Env, name: str, mutate: Callable, times: int = 1) -> Faulty:
    reg = env.state.registry
    faulty = Faulty(reg.get(name), mutate, times)
    reg._agents[name] = faulty
    return faulty


def _edit(r, pick: Callable, **changes):
    """The result with one finding changed (the first that ``pick`` accepts)."""
    findings = list(r.findings)
    i = next(i for i, f in enumerate(findings) if pick(f))
    findings[i] = findings[i].model_copy(update=changes)
    return r.model_copy(update={"findings": findings})


def revenue25(f) -> bool:
    return f.category == "reported_value" and "Total net sales" in f.claim and f.period == "FY2025"


def margin25(f) -> bool:
    return f.category == "calculation" and f.calculation.function == "margin" and f.period == "FY2025"


def _wf(env: Env, wf_id: str) -> dict:
    wf = env.client.get(f"/api/v1/workflows/{wf_id}").json()
    tasks = env.client.get(f"/api/v1/workflows/{wf_id}/tasks").json()
    wf["tasks"] = tasks
    wf["by_key"] = {}
    for t in tasks:  # the latest rework round wins
        if t["plan_key"] not in wf["by_key"] or t["rework_round"] >= wf["by_key"][t["plan_key"]]["rework_round"]:
            wf["by_key"][t["plan_key"]] = t
    wf["findings"] = env.client.get(f"/api/v1/workflows/{wf_id}/findings").json()
    return wf


def _ask(env: Env, query: str = QUESTION) -> dict:
    resp = env.client.post("/api/v1/workflows", json={"query": query})
    assert resp.status_code == 202, resp.text
    return _wf(env, resp.json()["id"])


def _rejections(session, needle: str) -> list[str]:
    return [r for v in session.scalars(select(VerificationResult).where(VerificationResult.decision == "rejected"))
            for r in v.reasons if needle in r]


def _recovered(wf: dict, key: str) -> None:
    rounds = [(t["rework_round"], t["status"]) for t in wf["tasks"] if t["plan_key"] == key]
    assert rounds == [(0, "REJECTED"), (1, "VERIFIED")], rounds
    assert wf["status"] == "COMPLETED" and not wf["report"]["limitations"], wf["report"]["limitations"]


# ---------------- the clean run verifies every check ----------------

def test_clean_run_is_verified_with_reasons(env: Env, session) -> None:
    wf = _ask(env)
    assert wf["status"] == "COMPLETED" and len(wf["report"]["verified_findings"]) == 11
    reasons = [r for v in session.scalars(select(VerificationResult)) for r in v.reasons]
    assert any("416,161 matches the cited cell (currency USD million FY2025" in r for r in reasons)
    assert any(r.startswith("margin replayed from its 2 stored input(s): 31.97%") for r in reasons)


# ---------------- faults in reported values (Document Intelligence) ----------------

@pytest.mark.parametrize("changes,reason", [
    ({"value": Decimal("416162")}, "wrong number: 416162 claimed, the cited cell holds 416161"),
    ({"unit": "other", "currency": None}, "currency mismatch: None claimed, the table states USD"),
    ({"unit": "percent", "currency": None, "scale": None}, "unit mismatch: percent claimed, the table states currency"),
    ({"scale": "billion"}, "scale mismatch: billion claimed, the table states million"),
    ({"period": "FY2023"}, "wrong period: FY2023 claimed, the cell is for FY2025"),
    ({"currency": "EUR"}, "currency mismatch: EUR claimed, the table states USD"),
])
def test_fault_in_a_reported_value_is_caught_and_reworked(env: Env, session, changes: dict, reason: str) -> None:
    _swap(env, "document_intelligence", lambda r: _edit(r, revenue25, **changes))
    wf = _ask(env)
    assert _rejections(session, reason), reason
    _recovered(wf, "extract")
    revenue = next(f for f in wf["findings"] if f["verification_status"] == "verified" and "Total net sales" in f["claim"]
                   and f["period"] == "FY2025" and f["category"] == "reported_value")
    assert revenue["value"] == "416161"


def test_fault_wrong_citation_is_caught(env: Env, session) -> None:
    def swap_evidence(r):  # cite operating income's cell for the revenue value
        other = next(f for f in r.findings if "Operating income" in f.claim and f.period == "FY2025")
        return _edit(r, revenue25, evidence=[EvidenceRef(evidence_id=other.evidence[0].evidence_id)])
    _swap(env, "document_intelligence", swap_evidence)
    wf = _ask(env)
    assert _rejections(session, "wrong number: 416161 claimed, the cited cell holds 133050")
    _recovered(wf, "extract")


def test_fault_claim_text_that_shows_another_number(env: Env, session) -> None:
    _swap(env, "document_intelligence", lambda r: _edit(r, revenue25, claim="Apple reports total net sales of 500,000."))
    wf = _ask(env)
    assert _rejections(session, "does not show the cited value \"$ 416,161\"")
    _recovered(wf, "extract")


# ---------------- faults in calculations (Financial Analyst) ----------------

def test_fault_tampered_calculation_output(env: Env, session) -> None:
    def mutate(r):
        f = next(x for x in r.findings if margin25(x))
        out = f.calculation.output + 1
        return _edit(r, margin25, value=out, calculation=f.calculation.model_copy(update={"output": out}))
    _swap(env, "financial_analyst", mutate)
    wf = _ask(env)
    assert _rejections(session, "replaying margin gives")
    _recovered(wf, "analyse")


def test_fault_consistent_lie_about_an_input(env: Env, session) -> None:
    """The agent changes an input and recomputes: the replay agrees, the cited cell does not."""
    def mutate(r):
        f = next(x for x in r.findings if margin25(x))
        inputs = dict(f.calculation.inputs)
        inputs["part"] = inputs["part"].model_copy(update={"value": Decimal("150000")})
        out = calc.run("margin", inputs).output
        claim = f.claim.replace(calc.display(calc.run("margin", dict(f.calculation.inputs)).output), calc.display(out))
        return _edit(r, margin25, claim=claim, value=out.value,
                     calculation=f.calculation.model_copy(update={"inputs": inputs, "output": out.value}))
    _swap(env, "financial_analyst", mutate)
    wf = _ask(env)
    assert _rejections(session, "input part: wrong number: 150000 claimed, the cited cell holds 133050")
    _recovered(wf, "analyse")


def test_fault_uncited_input_and_wrong_claim_number(env: Env, session) -> None:
    def mutate(r):
        f = next(x for x in r.findings if margin25(x))
        r = _edit(r, margin25, evidence=f.evidence[:1])  # drops the citation of one input
        return _edit(r, lambda x: x.category == "calculation" and x.period == "FY2024"
                     and x.calculation.function == "margin", claim="Apple operating margin, FY2024: 40.00%")
    _swap(env, "financial_analyst", mutate)
    wf = _ask(env)
    assert _rejections(session, "is not cited by the finding")
    assert _rejections(session, "does not show the calculated value 31.51%")
    _recovered(wf, "analyse")


# ---------------- faults in stored rows (between extraction and verification) ----------------

def _run_extract_only(env: Env, query: str = QUESTION) -> uuid.UUID:
    env.state.settings = env.state.settings.model_copy(update={"jobs_mode": "off"})
    wf_id = uuid.UUID(env.client.post("/api/v1/workflows", json={"query": query}).json()["id"])
    env.state.settings = env.state.settings.model_copy(update={"jobs_mode": "inline"})
    with env.sessions() as s:
        queued = sup.tick(s, wf_id, settings=env.state.settings).queued
    for t in queued:
        runner.run_task(env.sessions, t, registry=env.state.registry, settings=env.state.settings,
                        index=env.state.vector_index, embedder=env.state.embedder)
    return wf_id


def _drive(env: Env, wf_id: uuid.UUID) -> dict:
    runner.drive(env.sessions, wf_id, registry=env.state.registry, settings=env.state.settings,
                 index=env.state.vector_index, embedder=env.state.embedder)
    return _wf(env, str(wf_id))


def _strip_table(env: Env) -> None:
    with env.sessions() as s:  # the table structure is gone: values can no longer be checked against cells
        s.execute(update(DocumentChunk).where(DocumentChunk.text.contains("Total net sales")).values(meta={}))
        s.commit()


def test_fault_wrong_page_is_caught(env: Env, session) -> None:
    wf_id = _run_extract_only(env)
    with env.sessions() as s:  # the table chunk now spans pages 1-2, but its cells are on page 2
        ev = s.scalars(select(EvidenceRecord).where(EvidenceRecord.quote.startswith("Total net sales"))).first()
        s.execute(update(DocumentChunk).where(DocumentChunk.id == ev.chunk_id).values(page_start=1))
        s.execute(update(EvidenceRecord).where(EvidenceRecord.id == ev.id).values(page=1))
        s.commit()
    wf = _drive(env, wf_id)
    assert _rejections(session, "wrong page: the evidence cites p. 1, the cell is on p. 2")
    _recovered(wf, "extract")


def test_fault_superseded_version_is_caught_and_rework_uses_the_new_one(env: Env, session) -> None:
    wf_id = _run_extract_only(env)
    with env.sessions() as s:
        doc = s.scalars(select(Document)).first()
    resp = env.client.post(f"/api/v1/documents/{doc.id}/versions",
                           files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "v2"), "application/pdf")})
    assert resp.status_code == 201 and resp.json()["version"]["is_current"], resp.text
    v2 = resp.json()["version"]["id"]
    wf = _drive(env, wf_id)
    assert _rejections(session, "cites a superseded version of NASDAQ_AAPL_2025.pdf")
    _recovered(wf, "extract")
    new = [f for f in wf["findings"] if f["verification_status"] == "verified" and f["category"] == "reported_value"]
    assert new and all(str(e["version_id"]) == v2 for f in new for e in f["evidence"])


def test_fault_quote_not_in_its_source(env: Env, session) -> None:
    wf_id = _run_extract_only(env)
    with env.sessions() as s:
        s.execute(update(EvidenceRecord).where(EvidenceRecord.quote.startswith("Total net sales"))
                  .values(quote="Total net sales | $ 999,999"))
        s.commit()
    _drive(env, wf_id)
    assert _rejections(session, "the quote is not in its source chunk")


# ---------------- bounded rework: drop what stays wrong, keep what is right ----------------

def test_a_persistent_fault_is_dropped_after_two_rework_rounds(env: Env) -> None:
    def op23(f) -> bool:
        return f.category == "reported_value" and "Operating income" in f.claim and f.period == "FY2023"
    _swap(env, "document_intelligence", lambda r: _edit(r, op23, value=Decimal("1")), times=99)
    wf = _ask(env)
    extract = [(t["rework_round"], t["status"]) for t in wf["tasks"] if t["plan_key"] == "extract"]
    assert extract == [(0, "REJECTED"), (1, "REJECTED"), (2, "VERIFIED")]  # at most 2 rework rounds
    assert wf["status"] == "COMPLETED"
    assert any(lim.startswith("Dropped after verification: Apple Inc. reports Operating income of 114,301")
               for lim in wf["report"]["limitations"])
    # the analyst never received the rejected fact: no FY2023 margin, the rest is there
    margins = {f["period"] for f in wf["report"]["verified_findings"]
               if (f.get("calculation") or {}).get("function") == "margin"}
    assert margins == {"FY2024", "FY2025"}


# ---------------- human review ----------------

def test_unverifiable_values_go_to_a_person_who_can_approve(env: Env) -> None:
    wf_id = _run_extract_only(env)
    _strip_table(env)
    wf = _drive(env, wf_id)
    assert wf["status"] == "NEEDS_HUMAN_REVIEW" and wf["by_key"]["extract"]["status"] == "NEEDS_HUMAN_REVIEW"
    assert {f["verification_status"] for f in wf["findings"]} == {"needs_review"}
    for key in ("extract", "analyse"):  # approve the extraction, then the calculations built on it
        wf = _wf(env, str(wf_id))
        if wf["status"] == "COMPLETED":
            break
        resp = env.client.post(f"/api/v1/workflows/{wf_id}/reviews",
                               json={"task_id": wf["by_key"][key]["id"], "decision": "approve", "reviewer": "ana",
                                     "note": "checked against p. 2"})
        assert resp.status_code == 200, resp.text
    wf = _wf(env, str(wf_id))
    assert wf["status"] == "COMPLETED" and len(wf["report"]["verified_findings"]) == 11
    transitions = [tr for t in wf["tasks"] for tr in t["transitions"] if tr["actor"] == "reviewer:ana"]
    assert transitions and transitions[0]["reason"] == "approve by ana: checked against p. 2"


def test_a_person_can_reject(env: Env) -> None:
    wf_id = _run_extract_only(env)
    _strip_table(env)
    wf = _drive(env, wf_id)
    resp = env.client.post(f"/api/v1/workflows/{wf_id}/reviews",
                           json={"task_id": wf["by_key"]["extract"]["id"], "decision": "reject", "note": "wrong table"})
    assert resp.status_code == 200, resp.text
    wf = _wf(env, str(wf_id))
    assert wf["status"] == "COMPLETED" and wf["report"]["verified_findings"] == []
    assert wf["by_key"]["extract"]["status"] == "FAILED_FINAL" and wf["by_key"]["analyse"]["status"] == "FAILED_FINAL"


def test_rerun_after_the_missing_report_is_uploaded(env: Env) -> None:
    state = {"tesla": False}
    env.llm.plan = lambda user: {"companies": [{"named": "Tesla", "match": "Tesla, Inc." if state["tesla"] else None}],
                                 "metrics": FakeLLM.default_plan(user)["metrics"],
                                 "periods": ["FY2023", "FY2024", "FY2025"]}
    wf = _ask(env, "Compare Tesla's revenue and operating margin FY2023–FY2025")
    assert wf["status"] == "NEEDS_HUMAN_REVIEW" and "Tesla" in wf["by_key"]["extract"]["last_error"]
    up = env.client.post("/api/v1/documents", data={"company_name": "Tesla, Inc.", "fiscal_year": "2025"},
                         files={"file": ("NASDAQ_TSLA_2025.pdf", pdf_bytes(2, "tsla"), "application/pdf")})
    assert up.status_code == 201, up.text
    state["tesla"] = True
    resp = env.client.post(f"/api/v1/workflows/{wf['id']}/reviews",
                           json={"task_id": wf["by_key"]["extract"]["id"], "decision": "rerun"})
    assert resp.status_code == 200 and resp.json()["status"] == "COMPLETED", resp.text
    wf = _wf(env, wf["id"])
    found = wf["report"]["verified_findings"]
    assert found and {e["file"] for f in found for e in f["evidence"]} == {"NASDAQ_TSLA_2025.pdf"}
    assert all("Tesla" in f["claim"] for f in found if f["category"] == "reported_value")


def test_only_waiting_tasks_can_be_reviewed(env: Env) -> None:
    wf = _ask(env)
    extract = wf["by_key"]["extract"]
    resp = env.client.post(f"/api/v1/workflows/{wf['id']}/reviews", json={"task_id": extract["id"], "decision": "approve"})
    assert resp.status_code == 409 and "only a task waiting for review" in resp.text
    resp = env.client.post(f"/api/v1/workflows/{wf['id']}/reviews", json={"task_id": extract["id"], "decision": "maybe"})
    assert resp.status_code == 422
    resp = env.client.post(f"/api/v1/workflows/{uuid.uuid4()}/reviews",
                           json={"task_id": extract["id"], "decision": "approve"})
    assert resp.status_code == 404


def test_rerun_is_refused_for_a_task_with_findings(env: Env) -> None:
    wf_id = _run_extract_only(env)
    _strip_table(env)
    wf = _drive(env, wf_id)
    resp = env.client.post(f"/api/v1/workflows/{wf_id}/reviews",
                           json={"task_id": wf["by_key"]["extract"]["id"], "decision": "rerun"})
    assert resp.status_code == 409 and "duplicate" in resp.text


def test_a_clean_run_needs_no_person(env: Env, session) -> None:
    wf = _ask(env)
    assert {f.verification_status for f in session.scalars(select(FindingRecord))} == {"verified"}
    assert session.scalar(select(AgentTask).where(AgentTask.status == "NEEDS_HUMAN_REVIEW")) is None
    assert wf["status"] == "COMPLETED"
