"""Reports (M7, D-031): a workflow's verified answer as a checked, versioned document.

``build`` turns a completed workflow's verified findings (``workflow.report``, built by the
Supervisor) into a :class:`ReportDoc`: the answer (calculated results, reported figures, cited
passages), numbered citations that resolve to ``evidence_records``, every calculation with its
typed inputs, limitations, review status and how the report was made (models, prompt versions,
calc version, tokens, cost, trace id). ``check`` re-reads the database and refuses a report that
says more than was verified:

* every line is a verified finding of this workflow, and every verified finding is in the report;
* every line cites evidence, and every citation is an evidence record of the tenant, on the
  stated version and page, linked to a finding of the report;
* every calculation output equals the stored one; every number in the summary is a checked value.

``render`` writes Markdown in which document text (quotes, labels, file names) is escaped, so a
report never turns document content into links, images or HTML (rule 7). ``publish`` stores a
checked report as a :class:`~aeids.db.models.Report` row; a failed check raises
:class:`ReportCheckFailed` and nothing is stored.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aeids.db.models import (
    AgentRun,
    AgentTask,
    AuditEvent,
    CalculationRecord,
    Company,
    Document,
    DocumentVersion,
    EvidenceRecord,
    FindingEvidence,
    FindingRecord,
    Report,
    Workflow,
    utcnow,
)
from aeids.documents import DocumentError
from aeids.reporting.answer import arrange, check_numbers, template_summary

REPORT_SCHEMA_VERSION = "report/1.0"
RENDERER_VERSION = "md-v1"
NOTICE = "Decision support only: not a legal, financial or compliance determination."


class ReportNotReady(DocumentError):
    code = "report_not_ready"
    http_status = 409


class ReportRefused(DocumentError):
    """A report requested for an older workflow failed its check, so none is served."""

    code = "report_check_failed"
    http_status = 422


class ReportCheckFailed(Exception):
    """The report would say something that is not verified; it is not stored."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# --- the document ---------------------------------------------------------------------------

class Citation(BaseModel):
    n: int = Field(ge=1)
    evidence_id: str
    company: str | None = None
    file: str
    version_id: str
    version_number: int
    version_current: bool
    page: int = Field(ge=1)
    section: str | None = None
    quote: str
    bbox: list[Any] | None = None


class Line(BaseModel):
    finding_id: str
    company: str | None = None
    label: str
    value: str | None = None
    units: str | None = None
    period: str | None = None
    formula: str | None = None
    quote: str | None = None  # cited passages only
    citations: list[int] = Field(min_length=1)


class CalcInput(BaseModel):
    name: str
    value: str
    unit: str | None = None
    currency: str | None = None
    scale: str | None = None
    period: str | None = None
    citation: int | None = None  # None: an earlier step's output or a constant


class CalcDetail(BaseModel):
    finding_id: str
    label: str
    function: str
    formula: str
    output: str
    shown: str
    calc_version: str
    inputs: list[CalcInput]


class ReviewEntry(BaseModel):
    task: str
    decision: str
    reviewer: str
    note: str = ""
    at: str


class Review(BaseModel):
    mode: Literal["automatic", "human"]
    verified: int
    dropped: int
    not_reviewed: int
    rework_rounds: int
    entries: list[ReviewEntry] = Field(default_factory=list)


class Generation(BaseModel):
    models: list[str]
    prompt_versions: dict[str, list[str]]
    agent_versions: dict[str, str]
    calc_versions: list[str]
    total_tokens: int
    cost_usd: str
    trace_id: str
    renderer: str = RENDERER_VERSION


class ReportDoc(BaseModel):
    schema_version: Literal["report/1.0"] = REPORT_SCHEMA_VERSION
    workflow_id: str
    query: str
    generated_at: str
    status: Literal["complete", "partial", "nothing_verified"]
    summary: str | None = None
    summary_source: str | None = None  # ai | template
    results: list[Line] = Field(default_factory=list)
    reported: list[Line] = Field(default_factory=list)
    passages: list[Line] = Field(default_factory=list)
    calculations: list[CalcDetail] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    review: Review
    generation: Generation
    notice: str = NOTICE

    def lines(self) -> list[Line]:
        return self.results + self.reported + self.passages


# --- build ----------------------------------------------------------------------------------

class _Citations:
    """Numbers evidence records in order of first use."""

    def __init__(self, session: Session, tenant_id: uuid.UUID) -> None:
        self.session, self.tenant_id = session, tenant_id
        self.by_id: dict[str, Citation] = {}

    def cite(self, evidence_id: str) -> int:
        if evidence_id in self.by_id:
            return self.by_id[evidence_id].n
        row = self.session.execute(
            select(EvidenceRecord, DocumentVersion, Document.filename, Company.name)
            .join(DocumentVersion, DocumentVersion.id == EvidenceRecord.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Company, Company.id == Document.company_id)
            .where(EvidenceRecord.id == uuid.UUID(evidence_id), EvidenceRecord.tenant_id == self.tenant_id)).first()
        if row is None:  # check() reports it; numbering stays stable
            c = Citation(n=len(self.by_id) + 1, evidence_id=evidence_id, file="(missing)", version_id="",
                         version_number=0, version_current=False, page=1, quote="")
        else:
            ev, version, filename, company = row
            c = Citation(n=len(self.by_id) + 1, evidence_id=evidence_id, company=company,
                         file=version.filename or filename, version_id=str(version.id),
                         version_number=version.version_number, version_current=bool(version.is_current),
                         page=ev.page, section=ev.section, quote=ev.quote[:600], bbox=ev.bbox)
        self.by_id[evidence_id] = c
        return c.n


def _status(lines: list[Line], limitations: list[str]) -> str:
    if not lines:
        return "nothing_verified"
    return "partial" if limitations else "complete"


def _review(session: Session, wf: Workflow, verified: int, limitations: list[str]) -> Review:
    entries = [ReviewEntry(task=str(e.details.get("task", "")), decision=str(e.details.get("decision", "")),
                           reviewer=e.actor.removeprefix("reviewer:"), note=str(e.details.get("note") or ""),
                           at=e.created_at.isoformat())
               for e in session.scalars(select(AuditEvent).where(AuditEvent.resource == f"workflow:{wf.id}",
                                                                 AuditEvent.action == "workflow.review")
                                        .order_by(AuditEvent.created_at))]
    rounds = session.scalar(select(func.max(AgentTask.rework_round)).where(AgentTask.workflow_id == wf.id)) or 0
    return Review(mode="human" if entries else "automatic", verified=verified,
                  dropped=sum(lim.startswith("Dropped after verification") for lim in limitations),
                  not_reviewed=sum(lim.startswith("Not reviewed") for lim in limitations),
                  rework_rounds=rounds, entries=entries)


def _generation(session: Session, wf: Workflow, calc_versions: set[str]) -> Generation:
    runs = session.execute(select(AgentRun).join(AgentTask, AgentTask.id == AgentRun.task_id)
                           .where(AgentTask.workflow_id == wf.id).order_by(AgentRun.started_at)).scalars().all()
    prompts: dict[str, list[str]] = {}
    agents: dict[str, str] = {}
    for r in runs:
        agents[r.agent_name] = r.agent_version
        if r.prompt_version and r.prompt_version not in prompts.setdefault(r.agent_name, []):
            prompts[r.agent_name].append(r.prompt_version)
    cost = sum((r.cost_usd or Decimal(0) for r in runs), Decimal(0))
    return Generation(models=sorted({r.model for r in runs if r.model}),
                      prompt_versions={k: v for k, v in prompts.items() if v}, agent_versions=agents,
                      calc_versions=sorted(calc_versions), total_tokens=sum(r.total_tokens or 0 for r in runs),
                      cost_usd=f"{cost.quantize(Decimal('0.000001'))}", trace_id=wf.trace_id)


def _inputs(calc: dict[str, Any], cites: _Citations) -> list[CalcInput]:
    out = []
    for name, q in (calc.get("inputs") or {}).items():
        items = q if isinstance(q, list) else [q]
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            ev = item.get("evidence_id")
            out.append(CalcInput(name=f"{name}[{i + 1}]" if isinstance(q, list) else name,
                                 value=str(item.get("value")), unit=item.get("unit"), currency=item.get("currency"),
                                 scale=item.get("scale"), period=item.get("period"),
                                 citation=cites.cite(ev) if ev else None))
    return out


def build(session: Session, wf: Workflow) -> ReportDoc:
    report = wf.report or {}
    findings = report.get("verified_findings", [])
    limitations = list(report.get("limitations", []))
    cites = _Citations(session, wf.tenant_id)
    results: list[Line] = []
    reported: list[Line] = []
    passages: list[Line] = []
    calcs: list[CalcDetail] = []
    for f in findings:
        if f.get("category") == "calculation_step":
            continue  # an intermediate step: shown inside its result's calculation
        one = arrange([f])
        nums = [cites.cite(e["evidence_id"]) for e in f.get("evidence", [])]
        if one["results"]:
            r = one["results"][0]
            calc = f.get("calculation") or {}
            inputs = _inputs(calc, cites)
            cited = sorted(set(nums + [i.citation for i in inputs if i.citation]))
            results.append(Line(finding_id=f["finding_id"], label=r["label"], value=r["value"], period=r["period"],
                                formula=r["formula"], citations=cited))
            if calc:
                calcs.append(CalcDetail(finding_id=f["finding_id"], label=r["label"], function=calc["function"],
                                        formula=calc["formula"], output=calc["output"], shown=r["value"],
                                        calc_version=calc["calc_version"], inputs=inputs))
        elif one["reported"]:
            r = one["reported"][0]
            reported.append(Line(finding_id=f["finding_id"], company=r["company"], label=r["label"],
                                 value=r["value"], units=r["units"] or None, period=r["period"], citations=nums))
        else:
            for e, n in zip(f.get("evidence", []), nums, strict=True):
                passages.append(Line(finding_id=f["finding_id"], label=e.get("section") or "Passage",
                                     quote=e.get("quote", "")[:400], citations=[n]))
    lines = results + reported + passages
    return ReportDoc(workflow_id=str(wf.id), query=wf.query, generated_at=utcnow().isoformat(),
                     status=_status(lines, limitations), summary=report.get("summary"),
                     summary_source=report.get("summary_source"), results=results, reported=reported,
                     passages=passages, calculations=calcs,
                     citations=sorted(cites.by_id.values(), key=lambda c: c.n), limitations=limitations,
                     review=_review(session, wf, len(lines), limitations),
                     generation=_generation(session, wf, {c.calc_version for c in calcs}),
                     notice=report.get("notice") or NOTICE)


# --- check ----------------------------------------------------------------------------------

def check(session: Session, wf: Workflow, doc: ReportDoc) -> list[str]:
    """What the report says that is not verified (empty: the report may be stored)."""
    problems: list[str] = []
    ReportDoc.model_validate(doc.model_dump())  # the schema, round-tripped
    lines = doc.lines()
    ids = {ln.finding_id for ln in lines}
    db = {str(f.id): f for f in session.scalars(select(FindingRecord).where(FindingRecord.workflow_id == wf.id))}
    for fid in sorted(ids):
        f = db.get(fid)
        if f is None:
            problems.append(f"finding {fid} is not a finding of this workflow")
        elif f.verification_status != "verified":
            problems.append(f"finding {fid} is {f.verification_status}, not verified")
    expected = {f["finding_id"] for f in (wf.report or {}).get("verified_findings", [])
                if f.get("category") != "calculation_step"}
    for fid in sorted(expected - ids):
        problems.append(f"verified finding {fid} is missing from the report")

    by_n = {c.n: c for c in doc.citations}
    if sorted(by_n) != list(range(1, len(by_n) + 1)):
        problems.append("citation numbers are not 1..n")
    for ln in lines:
        for n in ln.citations:
            if n not in by_n:
                problems.append(f"finding {ln.finding_id} cites [{n}], which is not in the sources")
    valid = [uuid.UUID(i) for i in ids if i in db]
    linked = {str(e) for e in session.scalars(select(FindingEvidence.evidence_record_id).where(
        FindingEvidence.finding_id.in_(valid)))} if valid else set()
    input_refs = {i.citation for c in doc.calculations for i in c.inputs if i.citation}
    for c in doc.citations:
        ev = session.scalar(select(EvidenceRecord).where(EvidenceRecord.id == uuid.UUID(c.evidence_id),
                                                         EvidenceRecord.tenant_id == wf.tenant_id))
        if ev is None:
            problems.append(f"source [{c.n}] is not an evidence record of this tenant")
            continue
        if str(ev.version_id) != c.version_id or ev.page != c.page:
            problems.append(f"source [{c.n}] does not match its evidence record (version or page)")
        if c.evidence_id not in linked and c.n not in input_refs:
            problems.append(f"source [{c.n}] supports no finding in the report")

    for calc in doc.calculations:
        stored = session.scalar(select(CalculationRecord).where(
            CalculationRecord.finding_id == uuid.UUID(calc.finding_id)))
        if stored is None or stored.output != calc.output or stored.formula != calc.formula:
            problems.append(f"calculation of finding {calc.finding_id} differs from the stored one")

    if doc.summary:
        arranged = arrange((wf.report or {}).get("verified_findings", []))
        if doc.summary_source == "template":  # written by code: must be exactly what code writes from these findings
            if doc.summary != template_summary(arranged):
                problems.append("the template summary is not the one code writes from the verified findings")
        else:  # written by AI: every number in it must be a checked value (D-028)
            bad = check_numbers(doc.summary, arranged, wf.query)
            if bad:
                problems.append(f"the summary states unchecked numbers: {', '.join(bad)}")
    return problems


# --- render ---------------------------------------------------------------------------------

_MD = re.compile(r"([\\`*_\[\]<>|])")
STATUS_TEXT = {
    "complete": "Complete: every part of the question was answered from verified evidence.",
    "partial": "Partial: some of the question could not be answered from verified evidence (see Limitations).",
    "nothing_verified": "Nothing verified: no part of the question could be answered from verified evidence.",
}


def _md(text: Any) -> str:
    """Document text as inert Markdown: one line, and no character that starts a link, image,
    emphasis, code, table cell or HTML tag."""
    return _MD.sub(r"\\\1", " ".join(str(text or "").split()))


def _refs(nums: list[int]) -> str:
    return " ".join(f"\\[{n}\\]" for n in nums)


def _units(i: CalcInput) -> str:
    if i.unit == "percent":
        return "%"
    return " ".join(x for x in (i.currency, i.scale if i.scale and i.scale != "units" else None) if x)


def _when(iso: str) -> str:
    return iso[:16].replace("T", " ") + " UTC"


def render(doc: ReportDoc) -> str:
    out: list[str] = [f"# {_md(doc.query)}", "", f"> {_md(doc.notice)}", "",
                      f"**Status:** {STATUS_TEXT[doc.status]}  ",
                      f"Generated {_when(doc.generated_at)} · `{doc.schema_version}` · workflow `{doc.workflow_id}`",
                      ""]
    if doc.summary:
        source = ("Written by AI from the verified findings below; every number in it was checked."
                  if doc.summary_source == "ai" else "Restates the verified findings below.")
        out += ["## Summary", "", _md(doc.summary), "", f"*{source}*", ""]
    if doc.results:
        out += ["## Calculated results", "", "| Result | Value | Period | How it was calculated | Sources |",
                "|---|---:|---|---|---|"]
        out += [f"| {_md(r.label)} | {_md(r.value)} | {_md(r.period)} | {_md(r.formula)} | {_refs(r.citations)} |"
                for r in doc.results]
        out.append("")
    if doc.reported:
        out += ["## Figures used, from the reports" if doc.results else "## Figures from the reports", "",
                "| Company | Figure | Value | Units | Period | Source |", "|---|---|---:|---|---|---|"]
        out += [f"| {_md(r.company)} | {_md(r.label)} | {_md(r.value)} | {_md(r.units)} | {_md(r.period)} | "
                f"{_refs(r.citations)} |" for r in doc.reported]
        out.append("")
    if doc.passages:
        out += ["## What the reports say", ""]
        for p in doc.passages:
            out += [f"> {_md(p.quote)} {_refs(p.citations)}", ">", f"> — {_md(p.label)}", ""]
    if not doc.lines():
        out += ["## Answer", "", "Nothing could be verified for this question. See Limitations.", ""]
    if doc.calculations:
        out += ["## Calculations in detail", "",
                "Each value below was computed by the calculation module from the inputs shown, never by the AI.", ""]
        for i, c in enumerate(doc.calculations, 1):
            out.append(f"{i}. **{_md(c.label)}** = {_md(c.shown)} · `{_md(c.function)}`: {_md(c.formula)} "
                       f"· calc {_md(c.calc_version)}")
            for x in c.inputs:
                value = " ".join(p for p in (_md(x.value), _md(_units(x))) if p)
                period = f", {_md(x.period)}" if x.period else ""
                src = _refs([x.citation]) if x.citation else "(an earlier step or a constant)"
                out.append(f"   - {_md(x.name)} = {value}{period} {src}")
        out.append("")
    out += ["## Limitations", ""]
    out += [f"- {_md(lim)}" for lim in doc.limitations] or ["- None: every requested value was found and verified."]
    out.append("")
    rv = doc.review
    plural = "s" if rv.verified != 1 else ""
    out += ["## Review", "",
            f"- Checked automatically by the Verification agent: {rv.verified} verified finding{plural} in this "
            f"report, {rv.dropped} dropped after verification, {rv.not_reviewed} not reviewed, "
            f"{rv.rework_rounds} rework round{'s' if rv.rework_rounds != 1 else ''}."]
    out += [f"- Human review: {_md(e.reviewer)} chose **{_md(e.decision)}** for task `{_md(e.task)}` on "
            f"{_when(e.at)}{': ' + _md(e.note) if e.note else ''}" for e in rv.entries]
    if not rv.entries:
        out.append("- No human review was needed.")
    out.append("")
    if doc.citations:
        out += ["## Sources", ""]
        for c in doc.citations:
            where = f"{_md(c.file)}{' (' + _md(c.company) + ')' if c.company else ''}, page {c.page}"
            version = f"version {c.version_number}{'' if c.version_current else ', since replaced'}"
            section = f", section “{_md(c.section)}”" if c.section else ""
            out.append(f"{c.n}. {where}{section}, {version}. “{_md(c.quote[:300])}” · evidence `{c.evidence_id}`")
        out.append("")
    g = doc.generation
    prompts = "; ".join(f"{_md(a)} {', '.join(_md(p) for p in v)}" for a, v in g.prompt_versions.items())
    out += ["## How this report was made", "",
            f"- Models: {', '.join(_md(m) for m in g.models) or 'none'}",
            f"- Prompts: {prompts or 'none'}",
            f"- Calculation module: {', '.join(g.calc_versions) or 'not used'}",
            f"- Tokens: {g.total_tokens:,} · cost USD {g.cost_usd} · trace `{g.trace_id}` · renderer {g.renderer}", ""]
    return "\n".join(out)


# --- store ----------------------------------------------------------------------------------

def latest(session: Session, workflow_id: uuid.UUID) -> Report | None:
    return session.scalar(select(Report).where(Report.workflow_id == workflow_id)
                          .order_by(Report.version.desc()).limit(1))


def publish(session: Session, wf: Workflow) -> Report:
    """Build, check, render and store the next version of a workflow's report."""
    doc = build(session, wf)
    problems = check(session, wf, doc)
    if problems:
        raise ReportCheckFailed(problems)
    markdown = render(doc)
    prev = latest(session, wf.id)
    row = Report(tenant_id=wf.tenant_id, workflow_id=wf.id, version=(prev.version + 1) if prev else 1,
                 schema_version=doc.schema_version, status=doc.status, content=doc.model_dump(mode="json"),
                 markdown=markdown, sha256=hashlib.sha256(markdown.encode()).hexdigest())
    session.add(row)
    session.flush()
    return row


def ensure(session: Session, wf: Workflow) -> Report:
    """The latest report; a completed workflow from before M7 gets one on first request."""
    row = latest(session, wf.id)
    if row is not None:
        return row
    if wf.status != "COMPLETED" or not wf.report:
        raise ReportNotReady(f"the workflow is {wf.status}; a report is made when it completes")
    try:
        row = publish(session, wf)
    except ReportCheckFailed as e:
        session.rollback()
        raise ReportRefused(f"the report failed its check and was not stored: {e}") from e
    session.commit()
    return row


def view(row: Report) -> dict[str, Any]:
    return {"report_id": str(row.id), "workflow_id": str(row.workflow_id), "version": row.version,
            "schema_version": row.schema_version, "status": row.status, "sha256": row.sha256,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            "download": f"/api/v1/workflows/{row.workflow_id}/report.md", "content": row.content}
