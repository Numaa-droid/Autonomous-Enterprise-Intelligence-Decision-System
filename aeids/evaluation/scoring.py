"""Scoring for the AEIDS-vs-baseline benchmark (MVP_PLAN.md M6, D-029). Pure functions, no I/O.

Both systems are scored by the same rules on what each shows its user:

* **Claims** are the numbers an answer states. For the baseline: every number in its answer text.
  For AEIDS: the values of its verified findings (calculated results and reported figures); its
  written summary only repeats those, so it is not counted twice. Years, page numbers and numbers
  copied from the question are not claims.
* **Answer accuracy.** An answerable question is correct when every expected value is among the
  claims, within its tolerance, whatever else the answer says. A value matches across scales
  ("$416.2 billion" matches 416,161 million) and on its magnitude (a "decrease of 2.9%" matches
  -2.93%). A not-found question is correct when the system says it could not find the answer
  (AEIDS: no verified figure; baseline: ``found`` is false).
* **Citation correctness** (answerable questions): a citation (file, pages) is correct when one of
  its pages is a gold page of that file, i.e. a page where a figure the answer rests on is printed.
* **Unsupported claims**: a claim is supported when the number is printed on a page the system
  cited (same matching rules), or, for AEIDS, when it is the output of a verified, stored
  calculation whose inputs are cited. Everything else is unsupported, including arithmetic the
  baseline model did itself: that is the difference the benchmark measures (rules 1 and 2).

The default tolerances: 0.5 % of the value for amounts and counts, 0.5 percentage points for
percentages (reports print growth rounded to whole percent), 0.01 for ratios, 0.005 per share.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

SCALES: dict[str, Decimal] = {"units": Decimal(1), "thousand": Decimal(10) ** 3, "million": Decimal(10) ** 6,
                              "billion": Decimal(10) ** 9, "trillion": Decimal(10) ** 12}
_SCALE_WORDS = {"thousand": "thousand", "k": "thousand", "million": "million", "mn": "million", "m": "million",
                "billion": "billion", "bn": "billion", "b": "billion", "trillion": "trillion", "tn": "trillion"}
DEFAULT_TOLERANCE = {"currency": Decimal("0.005"), "count": Decimal("0.005"), "percent": Decimal("0.5"),
                     "ratio": Decimal("0.01"), "per_share": Decimal("0.005")}
RELATIVE = {"currency", "count"}  # tolerance is a share of the value; otherwise absolute

_MENTION = re.compile(
    r"(?<![\w.,$€£₹])"
    r"(?P<open>\()?(?P<sign>[-−–])?\s?(?P<cur>US\$|\$|€|£|₹|USD\s)?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<close>\))?"
    r"(?P<pct>\s?(?:%|percent\b|per cent\b|percentage points?\b|pts?\b))?"
    r"(?:\s?(?:USD|US dollars|dollars)\b)?"
    r"(?:\s?(?P<scale>thousand|million|billion|trillion|bn|mn|tn|[kmb])\b)?", re.I)
_SKIP_BEFORE = re.compile(r"(?:\b(?:page|pages|p|pp|note|notes|item|items|part|fy|q[1-4]|form|section|"
                          r"top|rank|s)\.?\s*[-#]?\s*)$", re.I)
_SKIP_AFTER = re.compile(r"^-?[KQ]\b|^(?:st|nd|rd|th)\b")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


@dataclass(frozen=True)
class Claim:
    text: str
    value: Decimal  # as written, sign kept
    percent: bool
    scale: str | None  # an explicit scale word, else None
    calculated: bool = False  # AEIDS: output of a verified stored calculation


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def mentions(text: str, *, ignore: Iterable[Decimal] = (), calculated: bool = False) -> list[Claim]:
    """The numbers stated in ``text``, without years, page/item numbers and ``ignore`` values."""
    out = []
    ignored = {abs(x) for x in ignore}
    for m in _MENTION.finditer(text or ""):
        value = _dec(m["num"])
        if value is None:
            continue
        pct, scale = bool(m["pct"]), m["scale"]
        if scale and scale.lower() in ("k", "m", "b") and not m["cur"]:
            scale = None  # a lone k/m/b is a scale only after a currency sign ("$416b")
        bare = not pct and not scale and not m["cur"]
        if bare and _YEAR.match(m["num"]):
            continue
        if _SKIP_BEFORE.search(text[max(0, m.start() - 14):m.start()]) or _SKIP_AFTER.match(text[m.end():m.end() + 3]):
            continue
        if bare and abs(value) in ignored:
            continue
        if m["sign"] or (m["open"] and m["close"]):
            value = -value
        out.append(Claim(m.group(0).strip(), value, pct, _SCALE_WORDS[scale.lower()] if scale else None, calculated))
    return out


@dataclass(frozen=True)
class Expected:
    value: Decimal
    unit: str  # currency | count | percent | ratio | per_share
    scale: str = "units"
    tolerance: Decimal | None = None
    label: str = ""

    @property
    def tol(self) -> Decimal:
        return self.tolerance if self.tolerance is not None else DEFAULT_TOLERANCE.get(self.unit, Decimal("0.005"))

    @property
    def absolute(self) -> Decimal:
        return self.value * SCALES[self.scale] if self.unit in RELATIVE else self.value

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Expected:
        return cls(Decimal(d["value"]), d["unit"], d.get("scale") or "units",
                   Decimal(d["tolerance"]) if d.get("tolerance") is not None else None, d.get("label", ""))


def matches(claim: Claim, exp: Expected) -> bool:
    """Is ``claim`` the expected value, on its magnitude, within tolerance?"""
    if exp.unit == "percent":
        return claim.percent and abs(abs(claim.value) - abs(exp.value)) <= exp.tol
    if claim.percent:
        return False
    if exp.unit in RELATIVE:
        target = abs(exp.absolute)
        scales = [SCALES[claim.scale]] if claim.scale else [Decimal(1), SCALES[exp.scale]]
        return any(abs(abs(claim.value) * s - target) <= exp.tol * target for s in scales)
    return claim.scale is None and abs(abs(claim.value) - abs(exp.value)) <= exp.tol


def printed_on(claim: Claim, page_text: str) -> bool:
    """Is the claimed number printed on the page? Tables print amounts without a scale word, so a
    bare number on the page may be at any scale; a percentage must agree to 0.05."""
    mine = abs(claim.value) * (SCALES[claim.scale] if claim.scale else 1)
    for p in mentions(page_text):
        if claim.percent:
            if abs(abs(p.value) - abs(claim.value)) <= Decimal("0.05"):
                return True
            continue
        if p.percent or not mine:
            continue
        theirs = [abs(p.value) * SCALES[p.scale]] if p.scale else [abs(p.value) * s for s in SCALES.values()]
        if any(abs(t - mine) <= Decimal("0.005") * mine for t in theirs):
            return True
    return False


@dataclass(frozen=True)
class Citation:
    file: str
    pages: tuple[int, ...]


@dataclass
class Question:
    id: str
    type: str  # lookup | calculation | comparison | not_found
    question: str
    expected: list[Expected]
    gold: dict[str, set[int]]  # file -> pages where a supporting figure is printed

    @property
    def answerable(self) -> bool:
        return self.type != "not_found"


@dataclass
class SystemAnswer:
    system: str
    found: bool
    text: str
    claims: list[Claim]
    citations: list[Citation]
    latency_ms: float = 0.0
    tokens: int = 0
    cost_usd: Decimal | None = None
    error: str | None = None


@dataclass
class QuestionScore:
    id: str
    type: str
    system: str
    correct: bool
    abstained: bool
    matched: list[str]
    citations: int
    correct_citations: int
    claims: int
    unsupported: int
    unsupported_claims: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    tokens: int = 0
    cost_usd: Decimal | None = None
    error: str | None = None


def score(q: Question, a: SystemAnswer, page_text: Callable[[str, int], str]) -> QuestionScore:
    matched = [e.label for e in q.expected if any(matches(c, e) for c in a.claims)]
    correct = (a.found and len(matched) == len(q.expected)) if q.answerable else not a.found
    good_cites = sum(1 for c in a.citations if q.gold.get(c.file, set()) & set(c.pages)) if q.answerable else 0
    cited_texts = [page_text(c.file, p) for c in a.citations for p in c.pages]
    unsupported = [c.text for c in a.claims
                   if not c.calculated and not any(printed_on(c, t) for t in cited_texts)]
    return QuestionScore(q.id, q.type, a.system, correct, not a.found, matched,
                         len(a.citations) if q.answerable else 0, good_cites, len(a.claims), len(unsupported),
                         unsupported, a.latency_ms, a.tokens, a.cost_usd, a.error)


def _rate(n: float, d: float) -> float | None:
    return n / d if d else None


def summarise(scores: list[QuestionScore]) -> dict[str, Any]:
    """Aggregate one system's question scores."""
    answerable = [s for s in scores if s.type != "not_found"]
    nf = [s for s in scores if s.type == "not_found"]
    lat = sorted(s.latency_ms for s in scores)
    costs = [s.cost_usd for s in scores if s.cost_usd is not None]
    by_type = {}
    for t in ("lookup", "calculation", "comparison", "not_found"):
        rows = [s for s in scores if s.type == t]
        if rows:
            by_type[t] = _rate(sum(s.correct for s in rows), len(rows))
    return {
        "questions": len(scores),
        "accuracy": _rate(sum(s.correct for s in scores), len(scores)),
        "accuracy_by_type": by_type,
        "answerable_accuracy": _rate(sum(s.correct for s in answerable), len(answerable)),
        "abstained_answerable": _rate(sum(s.abstained for s in answerable), len(answerable)),
        "not_found_correct": _rate(sum(s.correct for s in nf), len(nf)),
        "citation_precision": _rate(sum(s.correct_citations for s in answerable), sum(s.citations for s in answerable)),
        "answers_with_correct_citation": _rate(sum(1 for s in answerable if s.correct_citations), len(answerable)),
        "claims": sum(s.claims for s in scores),
        "unsupported_claim_rate": _rate(sum(s.unsupported for s in scores), sum(s.claims for s in scores)),
        "latency_median_s": statistics.median(lat) / 1000 if lat else None,
        "latency_p95_s": lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] / 1000 if lat else None,
        "tokens_per_question": _rate(sum(s.tokens for s in scores), len(scores)),
        "cost_per_question_usd": float(sum(costs) / len(scores)) if costs and scores else None,
        "cost_total_usd": float(sum(costs)) if costs else None,
        "errors": sum(1 for s in scores if s.error),
    }
