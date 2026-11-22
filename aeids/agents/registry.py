"""Which agents a worker runs (D-025).

``agent_mode``: ``llm`` registers the M4 agents (Document Intelligence and Financial Analyst use
the chat model through ``aeids/llm/openrouter.py``), ``stub`` the M3 stand-ins (no model calls),
``auto`` the LLM agents when an OpenRouter key is configured and the stand-ins otherwise.
Verification is the deterministic M5 verifier in every mode (no model calls).
"""

from __future__ import annotations

import logging

from aeids.agents.base import Registry
from aeids.agents.llm_json import ChatModel
from aeids.agents.stubs import default_registry
from aeids.config import Settings

log = logging.getLogger(__name__)


def uses_llm(settings: Settings) -> bool:
    key = settings.openrouter_api_key
    has_key = key is not None and bool(key.get_secret_value().strip())
    return settings.agent_mode == "llm" or (settings.agent_mode == "auto" and has_key)


def build_registry(settings: Settings, llm: ChatModel | None = None) -> Registry:
    if llm is None and not uses_llm(settings):
        if settings.agent_mode == "auto":
            log.info("no OpenRouter key: workflows run the stand-in agents (AEIDS_AGENT_MODE=stub)")
        return default_registry()
    from aeids.agents.document_intelligence import DocumentIntelligenceAgent
    from aeids.agents.financial_analyst import FinancialAnalystAgent
    from aeids.agents.report_writer import ReportWriterAgent
    from aeids.agents.verification import VerificationAgent

    closers = []
    if llm is None:
        from aeids.llm.openrouter import OpenRouterClient

        client = OpenRouterClient(settings)
        llm, closers = client, [client.close]
    return Registry([DocumentIntelligenceAgent(llm, settings), FinancialAnalystAgent(llm, settings),
                     VerificationAgent(), ReportWriterAgent(llm, settings)], closers=closers)
