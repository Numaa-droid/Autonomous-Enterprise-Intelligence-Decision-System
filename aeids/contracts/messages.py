"""Schema version 1.0 of the agent protocol (ARCHITECTURE.md §3.3, §3.4).

Strict by design: unknown fields are refused, floats are refused for numbers (binary floats
would corrupt money; send strings or integers), currency amounts need an ISO currency, and a
finding needs at least one evidence reference. A model that returns something else fails
validation, which the Supervisor treats as a schema error (retried once, then final).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

Unit = Literal["currency", "percent", "ratio", "count", "shares", "per_share", "days", "other"]
Scale = Literal["units", "thousand", "million", "billion", "lakh", "crore"]
AgentStatus = Literal["completed", "needs_more_information", "blocked", "retryable_error",
                      "final_error"]
NextAction = Literal["submit_for_verification", "none", "request_dependency", "escalate"]
VerificationStatus = Literal["pending", "verified", "rejected", "needs_review"]


def _strict_decimal(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("numbers must be decimal strings or integers, not floats")
    if isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"not a decimal number: {value!r}") from None
        value = d
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("numbers must be finite")
    return value


Dec = Annotated[Decimal, BeforeValidator(_strict_decimal)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Quantity(_Model):
    """A typed number: value with unit, currency, scale and period as separate fields."""

    value: Dec
    unit: Unit
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    scale: Scale | None = None
    period: str | None = Field(default=None, max_length=40)
    evidence_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _currency_matches_unit(self) -> Quantity:
        if self.unit == "currency" and not self.currency:
            raise ValueError("a currency amount needs its ISO currency (e.g. USD)")
        if self.unit != "currency" and self.currency and self.unit != "per_share":
            raise ValueError(f"unit {self.unit!r} takes no currency")
        return self


class EvidenceProposal(_Model):
    """Evidence an agent found: a quote from a chunk. The Supervisor stores it as an
    ``evidence_records`` row only when it accepts the result, after checking the chunk belongs
    to the tenant and the quote is really in the chunk's text (no invented quotes)."""

    evidence_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    chunk_id: uuid.UUID
    page: int = Field(ge=1)
    section: str | None = Field(default=None, max_length=500)
    quote: str = Field(min_length=1, max_length=2000)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)


class EvidenceRef(_Model):
    evidence_id: uuid.UUID
    relation: Literal["supports", "contradicts", "context"] = "supports"


class Calculation(_Model):
    """A deterministic calculation (rule 2): reproducible from function, inputs and version."""

    calculation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    function: str = Field(min_length=1, max_length=100)
    formula: str = Field(min_length=1, max_length=500)
    inputs: dict[str, Quantity] = Field(min_length=1)
    output: Dec
    unit: Unit
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    scale: Scale | None = None
    calc_version: str = Field(min_length=1, max_length=20)


class Finding(_Model):
    finding_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    category: str = Field(default="metric", max_length=50)
    claim: str = Field(min_length=1, max_length=2000)
    value: Dec | None = None
    unit: Unit | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    scale: Scale | None = None
    period: str | None = Field(default=None, max_length=40)
    calculation: Calculation | None = None
    evidence: list[EvidenceRef] = Field(min_length=1)  # rule 1: no evidence, not a finding
    verification_status: VerificationStatus = "pending"

    @model_validator(mode="after")
    def _consistent(self) -> Finding:
        if self.value is not None and self.unit is None:
            raise ValueError("a finding with a value needs its unit")
        if self.calculation is not None and self.value is not None \
                and self.calculation.output != self.value:
            raise ValueError("the finding's value must equal its calculation's output")
        return self


class TaskConstraints(_Model):
    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_seconds: int = Field(default=180, ge=5, le=3600)
    requires_human_approval: bool = False


class TaskMessage(_Model):
    """What the Supervisor sends an agent: one task, the minimum context, no documents."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    workflow_id: uuid.UUID
    task_id: uuid.UUID
    parent_task_id: uuid.UUID | None = None
    rework_round: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    tenant_id: uuid.UUID
    assigned_agent: str = Field(min_length=1, max_length=50)
    objective: str = Field(min_length=1, max_length=2000)
    inputs: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[uuid.UUID] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    deadline: datetime | None = None
    trace_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(default=1, ge=1)


class VerificationDecision(_Model):
    finding_id: uuid.UUID
    decision: Literal["verified", "rejected", "needs_review"]
    reasons: list[str] = Field(default_factory=list)
    rework_instructions: str | None = Field(default=None, max_length=2000)


class AgentResult(_Model):
    """What an agent returns. It proposes a ``next_action``; only the Supervisor changes state."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    workflow_id: uuid.UUID
    task_id: uuid.UUID
    agent_name: str = Field(min_length=1, max_length=50)
    status: AgentStatus
    findings: list[Finding] = Field(default_factory=list)
    evidence: list[EvidenceProposal] = Field(default_factory=list)
    verifications: list[VerificationDecision] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)  # e.g. evidence ids for later tasks
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    next_action: NextAction = "none"
    needs: str | None = Field(default=None, max_length=1000)  # what information is missing
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    trace_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _consistent(self) -> AgentResult:
        if self.status in ("retryable_error", "final_error"):
            if not self.errors:
                raise ValueError(f"status {self.status!r} needs at least one error")
            if self.findings:
                raise ValueError("an error result carries no findings")
        if self.status == "needs_more_information" and not self.needs:
            raise ValueError("needs_more_information must say what is needed")
        if self.findings and self.next_action != "submit_for_verification":
            raise ValueError("findings must be submitted for verification")
        return self
