"""Report writer (D-028): a plain-English summary of the verified answer, for non-specialists.

Runs last, after every other task is verified, and reads only verified findings. The model writes
2-4 sentences; code then checks every number in them against the checked values as the answer
displays them (``aeids.reporting.answer.check_numbers``). A summary with any other number is
sent back once with the offending numbers named; if it fails again, a template summary that only
restates the checked values is used instead. The summary is not a finding: it adds no fact, so
it is not verified as one; it is labelled in the answer as written by AI from the checked figures.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aeids.agents.base import AgentContext
from aeids.agents.llm_json import ChatModel, Usage, ask_json, load_prompt, untrusted
from aeids.agents.stubs import _base
from aeids.config import Settings
from aeids.contracts import AgentResult, TaskMessage
from aeids.reporting.answer import arrange, check_numbers, template_summary

HERE = Path(__file__).parent
PROMPT_VERSION = "rw-v1"


class Summary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(min_length=1, max_length=1200)


def facts_text(arranged: dict) -> str:
    lines = [f"RESULT | {r['label']} | {r['value']} | {r.get('formula') or ''}" for r in arranged["results"]]
    lines += [f"FIGURE | {r['company'] or ''} {r['label']} | {r['period'] or 'period not stated'} | "
              f"{r['value']}{' ' + r['units'] if r['units'] else ''}" for r in arranged["reported"]]
    lines += [f"QUOTE | {p['source']} | {p['quote'][:300]}" for p in arranged["passages"][:6]]
    return "\n".join(lines)


class TemplateWriter:
    """No model: the summary restates the checked values (stub mode, and the fallback)."""

    name = "report_writer"
    version = "1.0"
    model = None
    prompt_version = None

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        arranged = arrange(ctx.verified_findings(task.workflow_id))
        text = template_summary(arranged)
        return AgentResult(**_base(task, self.name), status="completed",
                           outputs={"summary": text or None, "summary_source": "template" if text else None})


class ReportWriterAgent:
    name = "report_writer"
    version = "1.0"
    prompt_version = PROMPT_VERSION

    def __init__(self, llm: ChatModel, settings: Settings) -> None:
        self._llm = llm
        self.model = settings.llm_model
        self._prompt = load_prompt(HERE / "prompt_v1.md")

    def run(self, task: TaskMessage, ctx: AgentContext) -> AgentResult:
        usage = Usage()
        arranged = arrange(ctx.verified_findings(task.workflow_id))
        if not (arranged["results"] or arranged["reported"] or arranged["passages"]):
            return AgentResult(**_base(task, self.name), status="completed", outputs={"summary": None},
                               warnings=["nothing verified to summarise"])
        query = str(task.inputs.get("query") or task.objective)
        message = f"QUESTION:\n{query}\n\nCHECKED FACTS:\n" + untrusted("facts", facts_text(arranged))
        warnings: list[str] = []
        for attempt in range(2):
            try:
                text = " ".join(ask_json(self._llm, Summary, self._prompt, message, usage,
                                         model=self.model, max_tokens=2000).summary.split())  # room for reasoning tokens
            except ValidationError as exc:
                warnings.append(f"summary attempt {attempt + 1}: invalid reply ({exc.errors()[:1]})")
                continue
            bad = check_numbers(text, arranged, query)
            if not bad:
                return AgentResult(**_base(task, self.name), status="completed", warnings=warnings,
                                   outputs={"summary": text, "summary_source": "ai"}, metrics=usage.metrics())
            warnings.append(f"summary attempt {attempt + 1} withheld: numbers not among the checked values {bad}")
            message += (f"\n\nNOTE: your last summary contained numbers that are not in CHECKED FACTS: {bad}. "
                        "Use only numbers copied exactly from CHECKED FACTS.")
        return AgentResult(**_base(task, self.name), status="completed", warnings=warnings,
                           outputs={"summary": template_summary(arranged) or None, "summary_source": "template"},
                           metrics=usage.metrics())
