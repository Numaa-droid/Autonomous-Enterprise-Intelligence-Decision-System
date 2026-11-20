"""Typed, versioned messages between the Supervisor and agents (rule 3, ARCHITECTURE.md §3).

Agents never talk to each other: the Supervisor sends a ``TaskMessage`` and receives an
``AgentResult``; both are validated (unknown fields are refused) before anything is persisted.
Numbers are ``Decimal`` and serialise as strings; unit, currency, scale and period are separate
fields (rule: never fused). A ``Finding`` without evidence is not a finding (rule 1).
"""

from aeids.contracts.messages import (
    SCHEMA_VERSION,
    AgentResult,
    AgentStatus,
    Calculation,
    EvidenceProposal,
    EvidenceRef,
    Finding,
    NextAction,
    Quantity,
    TaskConstraints,
    TaskMessage,
    VerificationDecision,
)

__all__ = [
    "SCHEMA_VERSION",
    "AgentResult",
    "AgentStatus",
    "Calculation",
    "EvidenceProposal",
    "EvidenceRef",
    "Finding",
    "NextAction",
    "Quantity",
    "TaskConstraints",
    "TaskMessage",
    "VerificationDecision",
]
