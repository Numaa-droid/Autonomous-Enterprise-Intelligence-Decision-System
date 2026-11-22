"""Verification (M5, D-027): checks every finding against its stored evidence and calculation.

Deterministic code, no model: a claim is checked against what was parsed from the report, never
against another model's opinion. Everything is read from the database (findings, their cited
evidence records, the chunks those quote, stored calculations), not from the task payload.

Per finding:

* **Evidence**: every cited record exists for the tenant, its quote is in its chunk, its page is
  inside the chunk, its document version is still current and the document active.
* **Reported value**: the cited row is located in the chunk's normalised table and the evidence
  box picks the cell; the finding's value, unit, currency, scale and period must equal the cell's,
  the evidence page must be the cell's page, and the claim must show the printed value.
* **Calculation**: replayed with ``aeids.calc`` from the stored inputs; the replayed output must
  equal the stored output and the finding's value, unit, scale and period. Each input with an
  evidence id must be cited by the finding and equal its table cell; an input without one must
  be the output of an earlier step of the same task (or an ``intensity`` constant). The claim must
  show the calculated value.

Decision: any failed check -> ``rejected`` (with reasons and rework instructions); something that
cannot be checked automatically (values read off an image, a number without a table cell, a
calculation whose function changed since it was stored) -> ``needs_review``; else ``verified``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from aeids import calc
from aeids.agents.base import AgentContext, EvidenceView, FindingView
from aeids.agents.cells import CellValue, locate, polygon_box, read_cell
from aeids.agents.stubs import _base
from aeids.contracts import AgentResult, Quantity, TaskMessage, VerificationDecision

PLAIN = {"count", "other"}  # a count read without a unit is an "other" amount (D-026)

FIX_VALUE = ("Cite the table cell that holds the value and state its value, unit, currency, scale and "
             "period exactly as the table does.")
FIX_VERSION = "Extract the value again from the current version of the report; the cited version was replaced."
FIX_QUOTE = "Quote the source text exactly and cite only evidence collected from the current reports."
FIX_CALC = ("Recompute with the calculation module from facts that match their cited cells; never change "
            "stored inputs or outputs by hand.")


def _units_match(a: str | None, b: str | None) -> bool:
    return a == b or (a in PLAIN and b in PLAIN)


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


class _Check:
    """Problems (reject), doubts (needs review), notes (why it passed) and fixes for one finding."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.doubts: list[str] = []
        self.notes: list[str] = []
        self.fixes: list[str] = []

    def fail(self, reason: str, fix: str) -> None:
        self.problems.append(reason)
        if fix not in self.fixes:
            self.fixes.append(fix)

    def decision(self, finding_id: uuid.UUID) -> VerificationDecision:
        if self.problems:
            return VerificationDecision(finding_id=finding_id, decision="rejected", reasons=self.problems[:20],
                                        rework_instructions=" ".join(self.fixes)[:2000])
        if self.doubts:
            return VerificationDecision(finding_id=finding_id, decision="needs_review", reasons=self.doubts[:20])
        return VerificationDecision(finding_id=finding_id, decision="verified", reasons=self.notes[:20] or ["checked"])


class VerificationAgent:
    name = "verification"
    version = "2.0"
    model = None
    prompt_version = None

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        ids = [uuid.UUID(f["finding_id"]) for f in task.inputs.get("findings", [])]
        findings = ctx.findings(ids)
        cited = list(dict.fromkeys(e for f in findings.values() for e in f.evidence_ids))
        evidence = ctx.evidence(cited)
        steps: dict[uuid.UUID, dict[uuid.UUID, FindingView]] = {}
        decisions = []
        for fid in ids:
            f = findings.get(fid)
            check = _Check()
            if f is None:
                check.fail("the finding does not exist for this tenant", FIX_QUOTE)
            else:
                if f.task_id not in steps:
                    steps[f.task_id] = ctx.task_findings(f.task_id)
                self._check(f, evidence, steps[f.task_id], check)
            decisions.append(check.decision(fid))
        counts = {d: sum(1 for x in decisions if x.decision == d) for d in ("verified", "rejected", "needs_review")}
        return AgentResult(**_base(task, self.name), status="completed", verifications=decisions,
                           metrics={"checked": len(decisions), **counts})

    # ---- one finding ----

    def _check(self, f: FindingView, evidence: dict[uuid.UUID, EvidenceView],
               steps: dict[uuid.UUID, FindingView], check: _Check) -> None:
        if not f.evidence_ids:
            check.fail("the finding cites no evidence", FIX_QUOTE)
        for eid in f.evidence_ids:
            self._evidence(eid, evidence.get(eid), check)
        if f.calculation is not None:
            self._calculation(f, evidence, steps, check)
        elif f.category == "reported_value" or f.value is not None:
            self._reported(f, [evidence[e] for e in f.evidence_ids if e in evidence], check)
        elif not check.problems:
            check.notes.append(f"{len(f.evidence_ids)} cited passage(s) exist, are current and quote their source")

    def _evidence(self, eid: uuid.UUID, ev: EvidenceView | None, check: _Check) -> None:
        if ev is None:
            check.fail(f"cited evidence {eid} does not exist for this tenant", FIX_QUOTE)
            return
        if not ev.chunk_text or ev.quote not in ev.chunk_text:
            check.fail(f"evidence {eid}: the quote is not in its source chunk", FIX_QUOTE)
        if ev.chunk_pages and not ev.chunk_pages[0] <= ev.page <= ev.chunk_pages[1]:
            check.fail(f"evidence {eid}: page {ev.page} is outside its chunk "
                       f"(pages {ev.chunk_pages[0]}-{ev.chunk_pages[1]})", FIX_VALUE)
        if not ev.version_is_current:
            check.fail(f"evidence {eid} cites a superseded version of {ev.filename}", FIX_VERSION)
        if not ev.document_active:
            check.fail(f"evidence {eid} cites an archived document", FIX_VERSION)
        meta = ev.chunk_meta or {}
        if meta.get("image_read") or meta.get("kind") == "chart":
            check.doubts.append(f"evidence {eid}: the values were read off a chart image, not the PDF text")

    def _cell(self, ev: EvidenceView, check: _Check, what: str, value: Decimal | None) -> CellValue | None:
        """The typed table cell an evidence record points at, or None (with the reason recorded)."""
        table = (ev.chunk_meta or {}).get("table")
        if not isinstance(table, dict):
            check.doubts.append(f"{what}: the cited passage is not a table, so the value cannot be checked "
                                "against a cell")
            return None
        loc = locate(table, ev.quote, polygon_box(ev.bbox))
        if loc.row is None:
            check.fail(f"{what}: the quote is not a row of the cited table", FIX_VALUE)
            return None
        cell = loc.cell
        if cell is None and ev.bbox is not None:
            check.fail(f"{what}: the evidence box marks no cell of the cited row", FIX_VALUE)
            return None
        if cell is None:  # no box: the cell in the row with the claimed value
            cell = next((c for c in loc.candidates if (cv := read_cell(c, table)) and cv.value == value), None)
            if cell is None:
                printed = ", ".join(str(c.get("text")) for c in loc.candidates)
                check.fail(f"{what}: wrong number: {value} is not in the cited row (it shows {printed})", FIX_VALUE)
                return None
        cv = read_cell(cell, table)
        if cv is None:
            check.fail(f"{what}: the cited cell holds no number", FIX_VALUE)
        return cv

    def _compare(self, what: str, claimed: dict[str, Any], cv: CellValue, ev: EvidenceView, check: _Check) -> bool:
        before = len(check.problems)
        if claimed["value"] != cv.value:
            check.fail(f"{what}: wrong number: {claimed['value']} claimed, the cited cell holds {cv.value} "
                       f"(printed \"{cv.text}\")", FIX_VALUE)
        if cv.page is not None and ev.page != cv.page:
            check.fail(f"{what}: wrong page: the evidence cites p. {ev.page}, the cell is on p. {cv.page}", FIX_VALUE)
        if not _units_match(claimed["unit"], cv.unit):
            check.fail(f"{what}: unit mismatch: {claimed['unit']} claimed, the table states {cv.unit}", FIX_VALUE)
        if (claimed["currency"] or None) != (cv.currency or None):
            check.fail(f"{what}: currency mismatch: {claimed['currency']} claimed, the table states {cv.currency}",
                       FIX_VALUE)
        if (claimed["scale"] or None) != (cv.scale or None):
            check.fail(f"{what}: scale mismatch: {claimed['scale']} claimed, the table states {cv.scale}", FIX_VALUE)
        if (claimed["period"] or None) != (cv.period or None):
            check.fail(f"{what}: wrong period: {claimed['period']} claimed, the cell is for {cv.period}", FIX_VALUE)
        return len(check.problems) == before

    def _reported(self, f: FindingView, cited: list[EvidenceView], check: _Check) -> None:
        value = _dec(f.value)
        if value is None:
            check.fail("the finding has no valid value", FIX_VALUE)
            return
        if not cited:
            return  # already rejected for missing evidence
        ev = cited[0]
        cv = self._cell(ev, check, "value", value)
        if cv is None:
            return
        claimed = {"value": value, "unit": f.unit, "currency": f.currency, "scale": f.scale, "period": f.period}
        if self._compare("value", claimed, cv, ev, check):
            if cv.text and cv.text not in f.claim:
                check.fail(f"the claim's text does not show the cited value \"{cv.text}\"", FIX_VALUE)
            else:
                check.notes.append(" ".join(f"{cv.text} matches the cited cell ({cv.unit} {cv.currency or ''} "
                                            f"{cv.scale or ''} {cv.period or ''}, {ev.filename} p. {ev.page})".split()))

    def _calculation(self, f: FindingView, evidence: dict[uuid.UUID, EvidenceView],
                     steps: dict[uuid.UUID, FindingView], check: _Check) -> None:
        c = f.calculation
        try:
            inputs = {k: Quantity.model_validate(v) for k, v in c.inputs.items()}
        except ValidationError as exc:
            check.fail(f"the stored inputs are not valid quantities: {exc.errors()[:1]}", FIX_CALC)
            return
        try:
            replay = calc.run(c.function, dict(inputs))
        except calc.CalcError as exc:
            check.fail(f"replaying {c.function} is refused: {exc}", FIX_CALC)
            return
        out, stored, value = replay.output, _dec(c.output), _dec(f.value)
        if stored != out.value:
            msg = f"replaying {c.function} gives {out.value}, the stored output is {c.output}"
            if c.calc_version != calc.CALC_VERSION:
                check.doubts.append(f"{msg} (stored with calc {c.calc_version}, now {calc.CALC_VERSION})")
            else:
                check.fail(msg, FIX_CALC)
        if value != stored:
            check.fail(f"the finding's value {f.value} differs from its calculation's output {c.output}", FIX_CALC)
        for field, mine, theirs in (("unit", f.unit, out.unit), ("currency", f.currency, out.currency),
                                    ("scale", f.scale, out.scale), ("period", f.period, out.period)):
            if (mine or None) != (theirs or None):
                check.fail(f"{field} mismatch: the finding says {mine}, its calculation gives {theirs}", FIX_CALC)
        if not any(s in f.claim for s in (calc.display(out), calc.display_points(out))):
            check.fail(f"the claim's text does not show the calculated value {calc.display(out)}", FIX_CALC)
        for name, q in inputs.items():
            self._input(f, name, q, c.function, evidence, steps, check)
        if not check.problems and not check.doubts:
            check.notes.append(f"{c.function} replayed from its {len(inputs)} stored input(s): {calc.display(out)}; "
                               "every input matches its cited cell or an earlier step")

    def _input(self, f: FindingView, name: str, q: Quantity, function: str,
               evidence: dict[uuid.UUID, EvidenceView], steps: dict[uuid.UUID, FindingView], check: _Check) -> None:
        what = f"input {name}"
        if q.evidence_id is not None:
            if q.evidence_id not in f.evidence_ids:
                check.fail(f"{what}: its evidence {q.evidence_id} is not cited by the finding", FIX_CALC)
                return
            ev = evidence.get(q.evidence_id)
            if ev is None:
                return  # already rejected as missing evidence
            cv = self._cell(ev, check, what, q.value)
            if cv is not None:
                self._compare(what, {"value": q.value, "unit": q.unit, "currency": q.currency, "scale": q.scale,
                                     "period": q.period}, cv, ev, check)
            return
        if function == "intensity" and name == "per":
            return  # a constant of the metric's recipe, stored with the inputs
        for s in steps.values():
            sc = s.calculation
            if s.finding_id != f.finding_id and sc is not None and s.category == "calculation_step" \
                    and _dec(sc.output) == q.value and _units_match(sc.unit, q.unit) \
                    and (sc.scale or None) == (q.scale or None) and (sc.currency or None) == (q.currency or None):
                return
        check.fail(f"{what}: {q.value} has no evidence and is not the output of an earlier step", FIX_CALC)
