"""The metric knowledge base (``metrics.yaml``, D-026): loading, checking, running recipes.

The YAML says what a metric means and how it is computed; this module refuses a file whose
recipes name an unknown calc function, argument, input, step or line item, so a typo can never
reach a calculation. ``run_metric`` executes a recipe step by step with ``aeids.calc`` (the only
place numbers are computed, rule 2); constants such as 365 or 1,000,000 become stored inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aeids import calc
from aeids.calc import CalcResult, PeriodMismatch, UnitMismatch, fiscal_year
from aeids.contracts import Quantity

PATH = Path(__file__).with_name("metrics.yaml")
ANY = "any"
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")

Statement = Literal["income_statement", "balance_sheet", "cash_flow", "brsr_environment", "brsr_social", "csr"]
Category = Literal["profitability", "growth", "returns", "leverage", "liquidity", "cash_flow", "efficiency",
                   "brsr_environment", "brsr_social", "csr"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LineItem(_Strict):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=1)
    statement: Statement
    unit: Literal["currency", "count", "other", "percent"]
    sign: Literal["normal", "outflow"] = "normal"
    notes: str | None = None
    reported_ratio_of: str | None = None

    def names(self) -> list[str]:
        return [self.name, *self.aliases]


class InputSpec(_Strict):
    line_item: str
    unit: Literal["currency", "count", "other", "percent"] | None = None  # constrains "any" inputs


class PeriodRule(_Strict):
    same: list[str] | None = None
    later: str | None = None
    earlier: str | None = None

    @model_validator(mode="after")
    def _one_form(self) -> PeriodRule:
        if (self.same is None) == (self.later is None and self.earlier is None):
            raise ValueError("a period rule is either {same: [...]} or {later: x, earlier: y}")
        if self.same is None and not (self.later and self.earlier):
            raise ValueError("later and earlier go together")
        return self

    def refs(self) -> list[str]:
        return list(self.same or []) + [r for r in (self.later, self.earlier) if r]


class Step(_Strict):
    id: str | None = None
    function: str
    args: dict[str, str | list[str]]
    constants: dict[str, str] = Field(default_factory=dict)


class Metric(_Strict):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    category: Category
    unit: Literal["percent", "ratio", "days", "other", "currency", "count"]
    result_label: str | None = None
    display: Literal["default", "percentage_points"] = "default"
    formula: str = Field(min_length=1)
    standard: str | None = None
    inputs: dict[str, InputSpec] = Field(min_length=1)
    periods: list[PeriodRule] = Field(default_factory=list)
    same_line_item: bool = False
    recipe: list[Step] = Field(min_length=1, max_length=6)
    notes: str | None = None


class KnowledgeBase(_Strict):
    version: str = Field(min_length=1)
    line_items: dict[str, LineItem]
    metrics: dict[str, Metric]

    @model_validator(mode="after")
    def _references(self) -> KnowledgeBase:
        problems: list[str] = []
        for key, item in self.line_items.items():
            if not _KEY.match(key):
                problems.append(f"line item {key!r}: ids are lower_snake_case")
            if item.reported_ratio_of and item.reported_ratio_of not in self.metrics:
                problems.append(f"line item {key}: reported_ratio_of {item.reported_ratio_of!r} is not a metric")
        for key, m in self.metrics.items():
            problems += [f"metric {key}: {p}" for p in _check_metric(key, m, self.line_items)]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ---- lookups ----

    def line_item_for(self, text: str) -> tuple[str, LineItem] | None:
        """The line item whose name or an alias matches a printed label or metric name."""
        t = _norm(text)
        best: tuple[int, int, str] | None = None  # (exact?, matched length, key): longest match wins
        for key, item in self.line_items.items():
            if item.reported_ratio_of:
                continue
            for n in item.names():
                a = _norm(n)
                if len(a) < 4:
                    continue
                score = 2 if a == t else 1 if (a in t or (len(t) >= 5 and t in a)) else 0
                if score and (best is None or (score, len(a)) > best[:2]):
                    best = (score, len(a), key)
        return (best[2], self.line_items[best[2]]) if best else None


    def metric_for(self, text: str) -> tuple[str, Metric] | None:
        """The metric a title or question names (by its name or an alias), if any."""
        t = _norm(text)
        best: tuple[int, str] | None = None
        for key, metric in self.metrics.items():
            for n in [metric.name, *metric.aliases]:
                a = _norm(n)
                if len(a) >= 6 and (a == t or f" {a} " in f" {t} ") and (best is None or len(a) > best[0]):
                    best = (len(a), key)
        return (best[1], self.metrics[best[1]]) if best else None


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%()+]+", " ", s.casefold()).split())


def _check_metric(key: str, m: Metric, line_items: dict[str, LineItem]) -> list[str]:
    out: list[str] = []
    if not _KEY.match(key):
        out.append("ids are lower_snake_case")
    for name, spec in m.inputs.items():
        if spec.line_item != ANY and spec.line_item not in line_items:
            out.append(f"input {name}: unknown line item {spec.line_item!r}")
    for rule in m.periods:
        out += [f"period rule names unknown input {r!r}" for r in rule.refs() if r not in m.inputs]
    known = set(m.inputs)
    used: set[str] = set()
    for i, step in enumerate(m.recipe):
        last = i == len(m.recipe) - 1
        where = f"step {i + 1} ({step.function})"
        if step.function not in calc.FUNCTIONS:
            out.append(f"{where}: unknown calc function")
            continue
        expected = set(calc.FUNCTIONS[step.function][1])
        given = set(step.args) | set(step.constants)
        if given != expected or set(step.args) & set(step.constants):
            out.append(f"{where}: arguments {sorted(given)} must be exactly {sorted(expected)}")
        for arg, ref in step.args.items():
            if isinstance(ref, list) != (step.function in calc.LIST_INPUTS and arg == "values"):
                out.append(f"{where}: {arg} must be {'a list' if isinstance(ref, str) else 'one reference'}")
            for r in ref if isinstance(ref, list) else [ref]:
                if r not in known:
                    out.append(f"{where}: {r!r} is not an input or an earlier step")
                used.add(r)
        for arg, value in step.constants.items():
            try:
                if Decimal(value) <= 0:
                    raise InvalidOperation
            except InvalidOperation:
                out.append(f"{where}: constant {arg} must be a positive number")
        if not last:
            if not step.id or not _KEY.match(step.id):
                out.append(f"{where}: an intermediate step needs a lower_snake_case id")
            elif step.id in known:
                out.append(f"{where}: id {step.id!r} is already used")
            else:
                known.add(step.id)
    unused = set(m.inputs) - used
    if unused:
        out.append(f"inputs never used: {sorted(unused)}")
    return out


@lru_cache(maxsize=4)
def load(path: Path = PATH) -> KnowledgeBase:
    """The knowledge base, checked. Raises ``pydantic.ValidationError`` on any problem."""
    return KnowledgeBase.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- binding facts and running recipes ----------------------------------------------------------

def coerce(q: Quantity, line_item: LineItem | None, unit: str | None = None) -> Quantity:
    """A fact's quantity typed as its line item expects. A count or physical quantity read without
    a unit becomes the line item's unit; a currency line item needs a currency amount. ``unit``
    constrains an input that accepts any line item."""
    if unit is not None and line_item is None and q.unit != unit:
        raise UnitMismatch(f"this input must be a {unit} value, the value has unit {q.unit}")
    if line_item is None:
        return q
    want = line_item.unit
    if want == "currency" and q.unit != "currency":
        raise UnitMismatch(f"{line_item.name} must be a currency amount, the value has unit {q.unit}")
    if want == "percent" and q.unit != "percent":
        raise UnitMismatch(f"{line_item.name} must be a percentage, the value has unit {q.unit}")
    if want in ("count", "other"):
        if q.unit not in ("count", "other"):
            raise UnitMismatch(f"{line_item.name} must be a plain quantity, the value has unit {q.unit}")
        return q if q.unit == want else q.model_copy(update={"unit": want})
    return q


def check_periods(metric: Metric, bound: dict[str, Quantity]) -> None:
    for rule in metric.periods:
        if rule.same is not None:
            periods = {bound[r].period for r in rule.same if bound[r].period}
            if len(periods) > 1:
                raise PeriodMismatch(f"{', '.join(rule.same)} must be for the same year, got {sorted(periods)}")
        else:
            late, early = fiscal_year(bound[rule.later].period), fiscal_year(bound[rule.earlier].period)
            if late is None or early is None:
                raise PeriodMismatch(f"{rule.later} and {rule.earlier} need fiscal-year periods")
            if late <= early:
                raise PeriodMismatch(f"{rule.later} ({bound[rule.later].period}) must be after "
                                     f"{rule.earlier} ({bound[rule.earlier].period})")


@dataclass(frozen=True)
class StepRun:
    step_id: str
    result: CalcResult
    leaves: tuple[str, ...]  # the metric inputs this step depends on (for citing evidence)
    final: bool


def run_metric(metric: Metric, bound: dict[str, Quantity]) -> list[StepRun]:
    """Run a recipe on bound inputs (already coerced). Raises ``CalcError`` on any refusal."""
    if set(bound) != set(metric.inputs):
        raise calc.MissingInput(f"{metric.name}: inputs must be exactly {sorted(metric.inputs)}")
    check_periods(metric, bound)
    values: dict[str, Quantity] = dict(bound)
    leaves: dict[str, tuple[str, ...]] = {k: (k,) for k in bound}
    runs: list[StepRun] = []
    for i, step in enumerate(metric.recipe):
        args: dict[str, object] = {}
        deps: list[str] = []
        for arg, ref in step.args.items():
            refs = ref if isinstance(ref, list) else [ref]
            deps += refs
            args[arg] = [values[r] for r in refs] if isinstance(ref, list) else values[ref]
        for arg, value in step.constants.items():
            args[arg] = Quantity(value=value, unit="other")
        result = calc.run(step.function, args)
        final = i == len(metric.recipe) - 1
        sid = "result" if final else step.id
        values[sid] = result.output
        leaves[sid] = tuple(dict.fromkeys(x for d in deps for x in leaves[d]))
        runs.append(StepRun(sid, result, leaves[sid], final))
    out = runs[-1].result.output
    if out.unit != metric.unit:
        raise UnitMismatch(f"{metric.name}: the recipe returns {out.unit}, the metric is {metric.unit}")
    return runs


# --- text for prompts ---------------------------------------------------------------------------

FINANCIAL_STATEMENTS = ("income_statement", "balance_sheet", "cash_flow")
ESG_WORDS = re.compile(r"\b(esg|brsr|sustainab\w*|energy|renewable|water|emissions?|ghg|carbon|scope\s*[123]|waste|"
                       r"recycl\w*|wom[ae]n|female|gender|diversity|board|employees?|workforce|headcount|injur\w*|"
                       r"ltifr|safety|well-?being|csr|social|turnover|msme|wages?)\b", re.I)


def planning_catalog(kb: KnowledgeBase, question: str | None = None) -> str:
    """Line items (with how reports print them) and metrics (with what they need).

    With a ``question`` that mentions no ESG / BRSR topic, only the financial-statement line
    items and the metrics built from them are listed: the rest cannot answer it, and leaving it
    out halves the planning prompt (D-030). Without a question, everything is listed."""
    esg = question is None or bool(ESG_WORDS.search(question))
    keep = {k for k, i in kb.line_items.items() if esg or i.statement in FINANCIAL_STATEMENTS}
    items = [f"- {i.name} (printed as: {', '.join(i.aliases[:4])})" for k, i in kb.line_items.items()
             if not i.reported_ratio_of and k in keep]
    printed = [f"- {i.name}" for k, i in kb.line_items.items() if k in keep and (
        i.reported_ratio_of or i.statement.startswith("brsr") and i.unit == "percent")]
    metrics = []
    for m in kb.metrics.values():
        if any(s.line_item != ANY and s.line_item not in keep for s in m.inputs.values()):
            continue
        needs = sorted({_any_text(s) if s.line_item == ANY else kb.line_items[s.line_item].name
                        for s in m.inputs.values()})
        extra = " (also the previous year's balance)" if any(r.later and not m.same_line_item for r in m.periods) else ""
        metrics.append(f"- {m.name}: {m.formula}. Needs: {', '.join(needs)}{extra}")
    return ("LINE ITEMS (use these names):\n" + "\n".join(items)
            + "\n\nRATIOS BRSR PRINTS ITSELF (can be read directly):\n" + ("\n".join(dict.fromkeys(printed)) or "(none)")
            + "\n\nMETRICS:\n" + "\n".join(metrics))


def analysis_catalog(kb: KnowledgeBase, fact_texts: list[str]) -> list[tuple[str, Metric]]:
    """The metrics whose line items all appear among the facts (generic ones always)."""
    found = {hit[0] for t in fact_texts if (hit := kb.line_item_for(t))}
    out = []
    for key, m in kb.metrics.items():
        needs = {s.line_item for s in m.inputs.values()}
        if needs == {ANY} or (needs - {ANY}) <= found:
            out.append((key, m))
    return out


def _any_text(spec: InputSpec) -> str:
    return "any reported percentage" if spec.unit == "percent" else "any reported amount"


def describe(key: str, m: Metric, kb: KnowledgeBase) -> str:
    inputs = "; ".join(f"{n} = {_any_text(s) if s.line_item == ANY else kb.line_items[s.line_item].name}"
                       for n, s in m.inputs.items())
    rules = "; ".join(f"same year: {', '.join(r.same)}" if r.same else f"{r.later} later than {r.earlier}"
                      for r in m.periods)
    extra = " The two inputs must be the same line item." if m.same_line_item else ""
    return f"{key} | {m.name} | {m.formula}\n   inputs: {inputs}\n   periods: {rules or 'any'}.{extra}"
