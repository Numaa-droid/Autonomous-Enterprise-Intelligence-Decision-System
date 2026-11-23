"""D-030: fewer and smaller model calls, same evidence rules.

* Document Intelligence: code picks the table cell when the match is clear; the model is asked
  only about what is left, and the planning prompt lists ESG / BRSR entries only when asked.
* Financial Analyst: no model call for a plain lookup. Report writer: a template sentence for an
  answer of one or two reported figures.
* Chat requests turn hidden reasoning off by default.
"""

from __future__ import annotations

import json

from aeids import knowledge
from aeids.agents.financial_analyst.agent import needs_calculation
from tests.test_agents_m4 import Env, _ask, env  # noqa: F401  (env is a fixture)
from tests.test_openrouter import Fake, _chat_body, _client

EXTRACT = "point at the cells"


def _prompts(e: Env, marker: str) -> list[str]:
    return [u for s, u in e.llm.prompts if marker in s]


def test_code_picks_clear_cells_without_the_model(env: Env) -> None:
    wf = _ask(env, "Compare Apple's revenue and operating margin FY2023–FY2025")
    assert wf["status"] == "COMPLETED"
    assert _prompts(env, EXTRACT) == []  # no table-reading call at all
    facts = wf["tasks_by_key"]["extract"]["output"]["facts"]
    assert len(facts) == 6 and {f["picked_by"] for f in facts} == {"code"}
    assert {(f["label"], f["period"], f["printed"]) for f in facts} >= {
        ("Total net sales", "FY2025", "$ 416,161"), ("Operating income", "FY2025", "133,050")}
    reported = [f for f in wf["findings"] if f["category"] == "reported_value"]
    assert len(reported) == 6 and all(f["verification_status"] == "verified" for f in reported)
    assert any(f["category"] == "calculation" for f in wf["findings"])  # the analyst still calculated


def test_the_model_only_sees_what_code_could_not_pick(env: Env) -> None:
    env.llm.plan = lambda user: {"companies": [{"named": "Apple", "match": "Apple Inc."}],
                                 "metrics": [{"name": "Total revenue", "search_query": "total net sales"},
                                             {"name": "Free cash flow", "search_query": "free cash flow"}],
                                 "periods": ["FY2025"]}
    env.llm.picks = lambda user: {"facts": [], "not_found": ["Free cash flow"]}
    wf = _ask(env, "What were Apple's revenue and free cash flow in FY2025?")
    extract = _prompts(env, EXTRACT)
    assert len(extract) == 1
    metrics = extract[0].split("METRICS:", 1)[1].split("PERIODS:", 1)[0]
    assert "Free cash flow" in metrics and "Total revenue" not in metrics
    assert [f["claim"].split(" of ")[1].split(" (")[0] for f in wf["findings"] if f["value"]] == ["$ 416,161"]
    assert "Not found in the reports: Free cash flow" in wf["report"]["limitations"]


def test_an_ambiguous_printed_name_is_left_to_the_model() -> None:
    from aeids.agents.document_intelligence.resolver import CellResolver

    r = CellResolver(knowledge.load())
    _, revenue = r.names("Total revenue")
    _, operating = r.names("Operating income")
    assert "income from operations" not in revenue | operating  # printed for both: never code's call
    assert {"total net sales", "revenues"} <= revenue and "operating income" in operating


def test_a_line_item_the_question_names_is_searched_even_if_the_planner_drops_it(env: Env) -> None:
    from aeids.agents.document_intelligence.resolver import CellResolver

    r = CellResolver(knowledge.load())
    assert r.named_in("What was Tesla's net income in 2024?") == ["Net income"]
    assert r.named_in("Total revenues and operating income of Tesla") == ["Total revenue", "Operating income"]
    assert r.named_in("What are Apple's main supply chain risks?") == []
    env.llm.plan = lambda user: {"companies": [{"named": "Apple", "match": "Apple Inc."}], "metrics": [], "periods": []}
    wf = _ask(env, "What were Apple's total net sales in 2025?")
    assert wf["status"] == "COMPLETED"
    assert [f["claim"].split(" of ")[1].split(" (")[0] for f in wf["findings"] if f["value"]] == ["$ 416,161"]


def test_a_lookup_skips_the_analyst_and_the_writer_models(env: Env) -> None:
    env.llm.plan = lambda user: {"companies": [{"named": "Apple", "match": "Apple Inc."}],
                                 "metrics": [{"name": "Total revenue", "search_query": "total net sales"}],
                                 "periods": ["FY2025"]}
    wf = _ask(env, "What were Apple's total net sales in fiscal 2025?")
    assert wf["status"] == "COMPLETED"
    assert len(env.llm.prompts) == 1  # only planning: cells by code, no analyst, template summary
    assert wf["report"]["summary_source"] == "template" and "416,161" in wf["report"]["summary"]
    assert wf["tasks_by_key"]["analyse"]["output"]["skipped"] == "no calculation asked for"


def test_which_questions_need_a_calculation() -> None:
    kb = knowledge.load()
    assert not needs_calculation("What were Apple's total net sales in fiscal 2025?", kb)
    assert not needs_calculation("How much did Microsoft spend on research and development in FY2025?", kb)
    for q in ("What was Apple's operating margin in FY2025?", "Revenue growth from 2024 to 2025?",
              "Compare Apple's and Microsoft's net income", "Which spent more on R&D?",
              "What was Tesla's return on equity?", "Current ratio at June 30, 2025?", "EBITDA in FY2025"):
        assert needs_calculation(q, kb), q


def test_the_planning_catalog_lists_esg_entries_only_when_asked() -> None:
    kb = knowledge.load()
    full = knowledge.planning_catalog(kb)
    financial = knowledge.planning_catalog(kb, "What was NVIDIA's operating margin in fiscal 2026?")
    esg = knowledge.planning_catalog(kb, "What was Infosys's energy intensity in FY2025?")
    assert len(financial) < 0.7 * len(full) and esg == full
    assert "Operating margin" in financial and "Total revenue" in financial and "Return on equity" in financial
    assert "Energy intensity per rupee of turnover" not in financial and "Total Scope 1 emissions" not in financial
    assert "(none)" in financial  # the BRSR printed-ratios section is there, empty


def test_chat_turns_reasoning_off_by_default() -> None:
    fake = Fake((200, _chat_body()), (200, _chat_body()))
    _client(fake).chat([{"role": "user", "content": "hi"}])
    assert json.loads(fake.requests[0].content)["reasoning"] == {"enabled": False}
    _client(fake, llm_reasoning="low").chat([{"role": "user", "content": "hi"}])
    assert json.loads(fake.requests[1].content)["reasoning"] == {"effort": "low"}
    fake2 = Fake((200, _chat_body()))
    _client(fake2, llm_reasoning="model_default").chat([{"role": "user", "content": "hi"}])
    assert "reasoning" not in json.loads(fake2.requests[0].content)
