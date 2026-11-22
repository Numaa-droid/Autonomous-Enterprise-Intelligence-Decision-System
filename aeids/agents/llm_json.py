"""Asking a chat model for JSON that must validate against a Pydantic model.

* Document text goes inside ``<document …>`` blocks and the system prompt says it is data, never
  instructions (rule 7). A closing tag inside the text is defused so it cannot end its block.
* The reply is parsed with ``model_validate_json``: anything that is not valid JSON of the
  expected shape raises ``pydantic.ValidationError``, which the runner reports as a schema error
  (retried once with the error in the task's context, then final; rule 8).
* ``Usage`` adds up tokens and cost over an agent's calls for ``agent_runs``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from aeids.llm.openrouter import ChatResult

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class ChatModel(Protocol):
    def chat(self, messages: list[dict[str, Any]], *, model: str | None = None, temperature: float = 0.0,
             max_tokens: int | None = None, response_format: dict[str, Any] | None = None,
             seed: int | None = None) -> ChatResult: ...


@dataclass
class Usage:
    calls: int = 0
    total_tokens: int = 0
    cost: Decimal | None = None
    models: list[str] = field(default_factory=list)

    def add(self, r: ChatResult) -> None:
        self.calls += 1
        self.total_tokens += r.usage.total_tokens
        if r.usage.cost is not None:
            self.cost = (self.cost or Decimal(0)) + r.usage.cost
        if r.model not in self.models:
            self.models.append(r.model)

    def metrics(self) -> dict[str, int | str]:
        out: dict[str, int | str] = {"llm_calls": self.calls, "total_tokens": self.total_tokens}
        if self.cost is not None:
            out["cost_usd"] = str(self.cost)
        if self.models:
            out["llm_model"] = ",".join(self.models)
        return out


def load_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def untrusted(tag_id: str, text: str, **attrs: Any) -> str:
    """A block of document text for a prompt. The text cannot close its own block."""
    safe = re.sub(r"</\s*document", "</ document", text, flags=re.I)
    meta = "".join(f' {k}="{str(v).replace(chr(34), chr(39))}"' for k, v in attrs.items() if v is not None)
    return f'<document id="{tag_id}"{meta}>\n{safe}\n</document>'


def extract_json(content: str) -> str:
    """The JSON object in a reply: drops reasoning blocks and code fences, and text around it."""
    text = _FENCE.sub("", _THINK.sub("", content)).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


def ask_json(llm: ChatModel, schema: type[M], system: str, user: str, usage: Usage, *,
             model: str | None = None, max_tokens: int = 2000) -> M:
    """One chat call whose reply must be a JSON object valid for ``schema``."""
    result = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                      model=model, temperature=0.0, max_tokens=max_tokens,
                      response_format={"type": "json_object"}, seed=7)
    usage.add(result)
    return schema.model_validate_json(extract_json(result.content))
