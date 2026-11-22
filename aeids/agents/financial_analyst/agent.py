"""Financial Analyst (M4, D-025/D-026): turns reported facts into calculated findings (rule 2).

The model only chooses. It prefers a **metric of the knowledge base** (``aeids/knowledge/
metrics.yaml``: definition, inputs, period rules and a recipe of ``aeids.calc`` steps) and says
which fact fills each input; only when no metric fits does it name a basic calc function. The
facts come from Document Intelligence (values read from normalised table cells, each with its
evidence record); the numbers come from ``aeids.calc``.

Every recipe step becomes a finding that carries its stored calculation and cites the evidence of
the inputs it depends on: intermediate steps (e.g. average equity) as ``calculation_step``, the
metric itself as ``calculation``. A request the knowledge base or the calculation module refuses
(unknown metric or fact, wrong units, periods that break the metric's rules, a zero divisor) is
dropped with a warning, never repaired by guessing.

The finding's text is built here, not by the model: its value is ``calc.display`` of the output,
and a model-written title is used only when it contains no numbers other than fiscal years.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aeids import calc, knowledge
from aeids.agents.base import AgentContext
from aeids.agents.llm_json import ChatModel, Usage, ask_json, load_prompt, untrusted
from aeids.agents.stubs import FinancialAnalystStub, _base
from aeids.config import Settings
from aeids.contracts import AgentResult, EvidenceRef, Finding, Quantity, TaskMessage

HERE = Path(__file__).parent
PROMPT_VERSION = "fa-v2"  # + "+kb-<version>" of the metric knowledge base (D-026)
_YEARS = re.compile(r"\bFY\s?\d{2,4}\b|\b(?:19|20)\d{2}\b", re.I)
LABELS = {"pct_change": "Change", "difference": "Difference", "ratio": "Ratio", "margin": "Margin",
          "cagr": "Compound annual growth", "sum_values": "Sum"}
BASIC = set(LABELS)  # functions the model may call directly; the others only run inside recipes


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MetricRequest(_Loose):
    metric: str = Field(min_length=1, max_length=60)
    inputs: dict[str, str] = Field(default_factory=dict)
    title: str = Field(default="", max_length=200)


class CalcRequest(_Loose):
    function: str = Field(min_length=1, max_length=40)
    inputs: dict[str, str | list[str]] = Field(default_factory=dict)
    title: str = Field(default="", max_length=200)


class CalcPlan(_Loose):
    metrics: list[MetricRequest] = Field(default_factory=list, max_length=40)
    calculations: list[CalcRequest] = Field(default_factory=list, max_length=40)


def clean_title(title: str) -> str | None:
    """The model's title, unless it carries a number that is not a fiscal year."""
    title = " ".join(title.split())
    return title if title and not re.search(r"\d", _YEARS.sub("", title)) else None


def _fact_line(f: dict[str, Any]) -> str:
    units = " ".join(x for x in (f.get("currency"), f.get("scale")) if x and x != "units")
    if f.get("unit") == "percent":
        units = "%"
    return (f"{f['fact_id']} | {f['company']} | {f['label']} | {f.get('period') or 'period not stated'}"
            f" | {f['printed']}{f' ({units})' if units else ''}")


class FinancialAnalystAgent:
    name = "financial_analyst"
    version = "1.1"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: ChatModel, settings: Settings) -> None:
        self._llm = llm
        self.model = settings.llm_model
        self._kb = knowledge.load()
        self.prompt_version = f"{PROMPT_VERSION}+kb-{self._kb.version}"
        self._prompt = load_prompt(HERE / "prompt_v2.md")
        self._summary = FinancialAnalystStub()

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        usage = Usage()
        facts = {f["fact_id"]: f for f in task.inputs.get("facts") or []}
        if not facts:
            if task.inputs.get("not_found"):
                return AgentResult(**_base(task, self.name), status="completed",
                                   warnings=["no reported value was found, so nothing was calculated"],
                                   outputs={"not_found": task.inputs["not_found"]})
            summary = self._summary.run(task, ctx)  # qualitative question: cite the passages found
            return summary.model_copy(update={
                "agent_name": self.name,
                "warnings": ["the question asks for no numbers: the passages found are cited"]})

        query = str(task.inputs.get("query") or task.objective)
        catalog = knowledge.analysis_catalog(self._kb, [t for f in facts.values()
                                                        for t in (f["label"], f.get("metric", ""))])
        plan = ask_json(self._llm, CalcPlan, self._prompt, self._message(query, facts, catalog, task), usage,
                        model=self.model, max_tokens=3000)
        quantities = {fid: Quantity(value=f["value"], unit=f["unit"], currency=f.get("currency"),
                                    scale=f.get("scale"), period=f.get("period"),
                                    evidence_id=uuid.UUID(f["evidence_id"]))
                      for fid, f in facts.items()}
        findings: list[Finding] = []
        warnings: list[str] = []
        done: set[tuple] = set()
        used_metrics: list[str] = []
        for req in plan.metrics:
            produced = self._metric(req, facts, quantities, warnings, done)
            if produced:
                findings += produced
                used_metrics.append(req.metric)
        for req in plan.calculations:
            finding = self._calculate(req, facts, quantities, warnings, done)
            if finding is not None:
                findings.append(finding)
        outputs = {"calculations": sum(1 for f in findings if f.category == "calculation"),
                   "kb_metrics": list(dict.fromkeys(used_metrics)), "kb_version": self._kb.version}
        if not findings:
            warnings.append("no calculation was needed or possible; the reported values answer the question")
            return AgentResult(**_base(task, self.name), status="completed", warnings=warnings,
                               outputs=outputs, metrics=usage.metrics())
        return AgentResult(**_base(task, self.name), status="completed", findings=findings,
                           next_action="submit_for_verification", warnings=warnings, outputs=outputs,
                           metrics={**usage.metrics(), "calculations": outputs["calculations"]})

    def _message(self, query: str, facts: dict[str, dict[str, Any]], catalog: list[tuple[str, knowledge.Metric]],
                 task: TaskMessage) -> str:
        lines = "\n".join(_fact_line(f) for f in facts.values())
        metrics = "\n".join(knowledge.describe(k, m, self._kb) for k, m in catalog) or "(none fits these facts)"
        notes = []
        if task.inputs.get("rework_instructions"):
            notes.append(f"A reviewer rejected the previous answer: {task.inputs['rework_instructions']}")
        if task.inputs.get("previous_error"):
            notes.append(f"Your previous reply was invalid: {str(task.inputs['previous_error'])[:500]}")
        return (f"QUESTION:\n{query}\n\nFACTS (id | company | line item | period | value):\n"
                + untrusted("facts", lines)
                + f"\n\nMETRICS (knowledge base {self._kb.version}; id | name | formula):\n{metrics}"
                + ("\n\nNOTES:\n" + "\n".join(notes) if notes else ""))

    # ---- knowledge-base metrics ----

    def _metric(self, req: MetricRequest, facts: dict[str, dict[str, Any]], quantities: dict[str, Quantity],
                warnings: list[str], done: set[tuple]) -> list[Finding]:
        key = req.metric.strip()
        metric = self._kb.metrics.get(key)
        if metric is None:
            warnings.append(f"unknown metric {key!r}; ignored")
            return []
        if set(req.inputs) != set(metric.inputs):
            warnings.append(f"{key}: inputs must be exactly {sorted(metric.inputs)}, got {sorted(req.inputs)}; ignored")
            return []
        unknown = [f for f in req.inputs.values() if f not in quantities]
        if unknown:
            warnings.append(f"{key}: unknown facts {unknown}; ignored")
            return []
        if len({facts[f]["company"] for f in req.inputs.values()}) > 1:
            warnings.append(f"{key} of {sorted(req.inputs.values())}: combines different companies; ignored")
            return []
        if metric.same_line_item and len({facts[f]["label"].casefold() for f in req.inputs.values()}) > 1:
            warnings.append(f"{key}: its inputs must be the same line item; ignored")
            return []
        for name, fid in req.inputs.items():  # each fact must be the line item the metric names
            want = metric.inputs[name].line_item
            if want == knowledge.ANY:
                continue
            got = self._kb.line_item_for(facts[fid]["label"])
            if got is None or got[0] != want:
                expected = self._kb.line_items[want].name
                warnings.append(f"{key}: {fid} is \"{facts[fid]['label']}\", not a known name of {expected}; "
                                f"ignored (add the label to the knowledge base if it is)")
                return []
        signature = ("metric", key, tuple(sorted(req.inputs.items())))
        if signature in done:
            return []
        try:
            bound = {}
            for name, fid in req.inputs.items():
                spec = metric.inputs[name]
                bound[name] = knowledge.coerce(quantities[fid], None if spec.line_item == knowledge.ANY
                                               else self._kb.line_items[spec.line_item], spec.unit)
            runs = knowledge.run_metric(metric, bound)
        except calc.CalcError as exc:
            warnings.append(f"{key} of {sorted(req.inputs.values())}: refused ({exc})")
            return []
        done.add(signature)
        company = facts[next(iter(req.inputs.values()))]["company"]
        final = runs[-1].result.output
        title = clean_title(req.title) or f"{metric.name} ({company}{', ' + final.period if final.period else ''})"
        inputs_text = "; ".join(f"{name} = {facts[fid]['label']}, {facts[fid].get('period') or 'period not stated'}: "
                                f"{facts[fid]['printed']}" for name, fid in req.inputs.items())
        out = []
        for run in runs:
            o = run.result.output
            shown = calc.display(o) + (f" {metric.result_label}" if run.final and metric.result_label else "")
            if run.final and metric.display == "percentage_points":
                shown = calc.display_points(o)
            if run.final:
                claim = f"{title}: {shown} [{metric.formula}; {inputs_text}]"
            else:
                claim = f"{metric.name}, step {run.step_id} ({company}): {shown} [{run.result.formula}]"
            refs = [EvidenceRef(evidence_id=uuid.UUID(e)) for e in
                    dict.fromkeys(facts[req.inputs[leaf]]["evidence_id"] for leaf in run.leaves)]
            out.append(Finding(category="calculation" if run.final else "calculation_step", claim=claim[:2000],
                               value=o.value, unit=o.unit, currency=o.currency, scale=o.scale, period=o.period,
                               calculation=run.result.to_contract(), evidence=refs))
        return out

    # ---- basic functions (when no metric fits) ----

    def _calculate(self, req: CalcRequest, facts: dict[str, dict[str, Any]], quantities: dict[str, Quantity],
                   warnings: list[str], done: set[tuple]) -> Finding | None:
        fn = req.function.strip()
        if fn not in BASIC:
            warnings.append(f"unknown function {fn!r}; ignored")
            return None
        named = self._kb.metric_for(req.title) if req.title else None
        if named is not None and any(s.line_item != knowledge.ANY for s in named[1].inputs.values()):
            # a metric with defined line items is computed only with its recipe and line items
            warnings.append(f"\"{req.title}\" is the knowledge-base metric {named[0]}; a basic {fn} for it is "
                            "refused (it must be computed with its recipe and line items)")
            return None
        ids = {k: (v if isinstance(v, list) else [v]) for k, v in req.inputs.items()}
        used = [i for v in ids.values() for i in v]
        unknown = [i for i in used if i not in quantities]
        if unknown or not used:
            warnings.append(f"{fn}: unknown or missing facts {unknown}; ignored")
            return None
        if fn != "ratio" and len({facts[i]["company"] for i in used}) > 1:
            warnings.append(f"{fn} of {used}: combines different companies; ignored")
            return None
        key = (fn, tuple(sorted((k, tuple(v)) for k, v in ids.items())))
        if key in done:
            return None
        args = {k: ([quantities[i] for i in v] if k == "values" else quantities[v[0]]) for k, v in ids.items()}
        try:
            result = calc.run(fn, args)
        except calc.CalcError as exc:
            warnings.append(f"{fn} of {used}: refused by the calculation module ({exc})")
            return None
        done.add(key)
        out = result.output
        companies = ", ".join(dict.fromkeys(facts[i]["company"] for i in used))
        title = clean_title(req.title) or (
            f"{LABELS[fn]} of " + " and ".join(dict.fromkeys(facts[i]["label"] for i in used))
            + f" ({companies}{', ' + out.period if out.period else ''})")
        inputs_text = "; ".join(
            f"{name} = {facts[i]['label']}, {facts[i].get('period') or 'period not stated'}: {facts[i]['printed']}"
            for name, v in ids.items() for i in v)
        claim = f"{title}: {calc.display(out)} [{result.formula}; {inputs_text}]"
        refs = [EvidenceRef(evidence_id=uuid.UUID(e)) for e in dict.fromkeys(facts[i]["evidence_id"] for i in used)]
        return Finding(category="calculation", claim=claim[:2000], value=out.value, unit=out.unit,
                       currency=out.currency, scale=out.scale, period=out.period,
                       calculation=result.to_contract(), evidence=refs)
