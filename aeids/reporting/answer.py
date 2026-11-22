"""The answer to a question, built from verified findings only (D-028).

* ``verified_findings``: a workflow's verified findings with their evidence and calculation (the
  report's content; also what the report writer reads).
* ``arrange``: those findings as an answer: calculated results, the reported figures they rest
  on, cited passages.
* ``check_numbers``: the guard on a written summary: every number in it must be a checked value
  (as displayed in the answer) or a year. ``template_summary`` is the fallback when it is not.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aeids.db.models import CalculationRecord, DocumentVersion, EvidenceRecord, FindingEvidence, FindingRecord

_REPORTED = re.compile(r"^(?P<company>.+?) reports (?P<label>.+) of (?P<value>[^()]+?)(?: \((?P<units>[^()]*)\))? for "
                       r"(?P<period>FY\d{4}|an unstated period) \((?P<source>[^()]*p\. \d+)\)\.$")
_NUMBER = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?")
_YEAR = re.compile(r"^(19|20)\d{2}$")


def verified_findings(session: Session, workflow_id: uuid.UUID) -> list[dict[str, Any]]:
    findings = []
    for f in session.scalars(select(FindingRecord).where(FindingRecord.workflow_id == workflow_id,
                                                         FindingRecord.verification_status == "verified")
                             .order_by(FindingRecord.created_at)):
        ev = session.execute(
            select(EvidenceRecord, DocumentVersion.filename)
            .join(FindingEvidence, FindingEvidence.evidence_record_id == EvidenceRecord.id)
            .join(DocumentVersion, DocumentVersion.id == EvidenceRecord.version_id)
            .where(FindingEvidence.finding_id == f.id)).all()
        calc = session.scalar(select(CalculationRecord).where(CalculationRecord.finding_id == f.id))
        findings.append({"finding_id": str(f.id), "category": f.category, "claim": f.claim, "value": f.value,
                         "unit": f.unit, "currency": f.currency, "scale": f.scale, "period": f.period,
                         "calculation": None if calc is None else {
                             "function": calc.function, "formula": calc.formula, "inputs": calc.inputs,
                             "output": calc.output, "calc_version": calc.calc_version},
                         "evidence": [{"evidence_id": str(e.id), "file": name, "page": e.page,
                                       "section": e.section, "quote": e.quote[:400]} for e, name in ev]})
    return findings


def arrange(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculated results first, then the reported figures, then cited passages."""
    results, reported, passages = [], [], []
    for f in findings:
        category, claim = f.get("category"), f.get("claim", "")
        source = ", ".join(dict.fromkeys(f"{e['file']} p. {e['page']}" for e in f.get("evidence", [])))
        if category == "calculation" or (category is None and f.get("calculation")):
            head = claim.split(" [", 1)[0]
            label, _, shown = head.rpartition(": ")
            formula = claim.split(" [", 1)[1].split(";", 1)[0] if " [" in claim else (f.get("calculation") or {}).get("formula")
            results.append({"label": label or head, "value": shown or head, "period": f.get("period"),
                            "formula": formula, "source": source})
        elif category == "calculation_step":
            continue  # shown with its finding in the audit view
        elif f.get("value") is not None:
            m = _REPORTED.match(claim)
            units = " ".join(x for x in (f.get("currency"), f.get("scale")) if x and x != "units")
            reported.append({"company": m["company"] if m else None,
                             "label": m["label"].rpartition(": ")[2] if m else claim,
                             "value": m["value"].strip() if m else f["value"],
                             "units": (m["units"] if m and m["units"] else None)
                             or ("%" if f.get("unit") == "percent" else units),
                             "period": f.get("period"), "source": source})
        else:
            passages.extend({"source": f"{e['file']} p. {e['page']}", "section": e.get("section"),
                             "quote": e.get("quote", "")[:400]} for e in f.get("evidence", []))
    return {"results": results, "reported": reported, "passages": passages}


def counts(arranged: dict[str, Any]) -> str:
    parts = []
    for key, word in (("results", "calculated result"), ("reported", "reported figure"), ("passages", "cited passage")):
        n = len(arranged[key])
        if n:
            parts.append(f"{n} {word}{'s' if n != 1 else ''}")
    return ("Verified: " + ", ".join(parts) + ".") if parts else "Nothing could be verified for this question."


# --- the guard on a written summary ---------------------------------------------------------

def _num(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "").replace("−", "-"))
    except InvalidOperation:
        return None


def allowed_numbers(arranged: dict[str, Any], query: str = "") -> set[Decimal]:
    """The numbers a summary may contain: the checked values as displayed, and years."""
    texts = [r["value"] for r in arranged["results"]] + [r["value"] for r in arranged["reported"]]
    texts += [r.get("period") or "" for r in arranged["results"] + arranged["reported"]] + [query]
    out = set()
    for t in texts:
        for tok in _NUMBER.findall(t or ""):
            n = _num(tok)
            if n is not None:
                out |= {n, abs(n)}
    return out


def check_numbers(text: str, arranged: dict[str, Any], query: str = "") -> list[str]:
    """The numbers in ``text`` that are neither a checked value nor a year."""
    allowed = allowed_numbers(arranged, query)
    bad = []
    for tok in _NUMBER.findall(text):
        n = _num(tok)
        if n is None or _YEAR.match(tok.replace(",", "")) or n in allowed or abs(n) in allowed:
            continue
        bad.append(tok)
    return bad


def template_summary(arranged: dict[str, Any]) -> str:
    """A plain summary that only restates the checked values (the fallback)."""
    if arranged["results"]:
        return " ".join(f"{r['label']}: {r['value']}." for r in arranged["results"][:6])
    if arranged["reported"]:
        return " ".join(f"{r['company'] + ' reported ' if r['company'] else ''}{r['label']} of {r['value']}"
                        f"{' ' + r['units'] if r['units'] else ''} for {r['period'] or 'the period shown'}."
                        for r in arranged["reported"][:6])
    if arranged["passages"]:
        n = len(arranged["passages"])
        return f"The reports address this in {n} passage{'s' if n != 1 else ''}, quoted below."
    return ""
