"""The metric knowledge base (metrics.yaml, D-026): it loads, every recipe runs, bad files are refused."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from aeids.calc import PeriodMismatch, UnitMismatch
from aeids.contracts import Quantity
from aeids.knowledge import ANY, KnowledgeBase, coerce, load, run_metric
from aeids.knowledge.metrics import PATH, check_periods

KB = load()
RAW = yaml.safe_load(PATH.read_text(encoding="utf-8"))


def q(value: str, unit: str = "currency", period: str | None = "FY2025", currency: str | None = None,
      scale: str | None = None) -> Quantity:
    if unit == "currency":
        currency, scale = currency or "INR", scale or "crore"
    return Quantity(value=value, unit=unit, currency=currency, scale=scale, period=period)


def synthetic(key: str) -> dict[str, Quantity]:
    """Plausible inputs for a metric: right units, periods that satisfy its rules."""
    m = KB.metrics[key]
    earlier = {r.earlier for r in m.periods if r.earlier}
    out = {}
    for i, (name, spec) in enumerate(m.inputs.items()):
        unit = (spec.unit or "currency") if spec.line_item == ANY else KB.line_items[spec.line_item].unit
        out[name] = q(str(1000 - 10 * i), unit, "FY2024" if name in earlier else "FY2025")
    return out


def test_it_loads_with_financial_and_brsr_metrics() -> None:
    assert KB.version == "1.1" and len(KB.line_items) >= 45 and len(KB.metrics) >= 35
    categories = {m.category for m in KB.metrics.values()}
    assert {"profitability", "returns", "leverage", "brsr_environment", "brsr_social", "csr"} <= categories
    brsr = [m for m in KB.metrics.values() if m.category.startswith("brsr")]
    assert len(brsr) >= 10 and all(m.standard for m in brsr if m.recipe[-1].function == "intensity")
    # every BRSR ratio that reports print themselves points at the metric that recomputes it
    printed = {i.reported_ratio_of for i in KB.line_items.values() if i.reported_ratio_of}
    assert {"energy_intensity_turnover", "water_intensity_turnover", "emission_intensity_turnover",
            "waste_intensity_turnover", "ltifr", "wellbeing_cost_share"} <= printed


@pytest.mark.parametrize("key", sorted(KB.metrics))
def test_every_recipe_runs_and_returns_its_unit(key: str) -> None:
    runs = run_metric(KB.metrics[key], synthetic(key))
    assert runs[-1].final and runs[-1].result.output.unit == KB.metrics[key].unit
    assert set(runs[-1].leaves) == set(KB.metrics[key].inputs)  # the result cites every input


# ---------------- hand-computed ----------------

def test_roe_on_average_equity() -> None:
    usd = dict(unit="currency", currency="USD", scale="million")
    runs = run_metric(KB.metrics["roe"], {"net_income": q("112010", **usd), "equity_end": q("73733", **usd),
                                          "equity_start": q("56950", period="FY2024", **usd)})
    assert [r.step_id for r in runs] == ["avg_equity", "result"]
    assert runs[0].result.output.value == Decimal("65341.5")
    assert runs[-1].result.output.value.quantize(Decimal("0.01")) == Decimal("171.42")


def test_free_cash_flow_takes_capex_printed_negative() -> None:
    usd = dict(unit="currency", currency="USD", scale="million")
    runs = run_metric(KB.metrics["free_cash_flow"], {"operating_cash_flow": q("111482", **usd),
                                                     "capex": q("-12715", **usd)})
    assert runs[-1].result.output.value == Decimal("98767") and runs[0].result.function == "absolute"


def test_brsr_emission_intensity_per_rupee() -> None:
    # (30,000 + 70,000 tCO2e) / ₹ 10,000 crore = 0.000001 t per rupee
    runs = run_metric(KB.metrics["emission_intensity_turnover"], {
        "scope1": q("30000", "other"), "scope2": q("70000", "other"), "revenue": q("10000")})
    assert runs[-1].result.output.value == Decimal("0.000001") and runs[-1].result.inputs["per"].value == 1


def test_ltifr_and_women_share() -> None:
    ltifr = run_metric(KB.metrics["ltifr"], {"injuries": q("3", "count"), "hours": q("12000000", "count")})
    assert ltifr[-1].result.output.value == Decimal("0.25")
    female = coerce(q("4200", "other"), KB.line_items["employees_female"])  # read without a unit
    total = coerce(q("21000", "other"), KB.line_items["employees_total"])
    share = run_metric(KB.metrics["women_employee_share"], {"female": female, "total": total})
    assert female.unit == "count" and share[-1].result.output.value == Decimal("20")


def test_ind_as_operating_margin_derives_ebit() -> None:
    # PBT 900 + finance costs (printed -60) - other income 40 = EBIT 920 on revenue 4,600 -> 20 %
    runs = run_metric(KB.metrics["operating_margin_ind_as"], {
        "profit_before_tax": q("900"), "finance_costs": q("-60"), "other_income": q("40"), "revenue": q("4600")})
    assert [r.step_id for r in runs] == ["interest", "pbit", "ebit", "result"]
    assert runs[2].result.output.value == Decimal("920") and runs[-1].result.output.value == Decimal("20")


def test_inventory_days() -> None:
    runs = run_metric(KB.metrics["inventory_days"], {"inventories_end": q("400"),
                                                      "inventories_start": q("200", period="FY2024"),
                                                      "cost_of_revenue": q("3650")})
    assert runs[-1].result.output.value == Decimal("30") and runs[-1].result.output.unit == "days"


# ---------------- refusals ----------------

def test_period_rules_are_enforced() -> None:
    bad = {"net_income": q("1"), "equity_end": q("2", period="FY2024"), "equity_start": q("2", period="FY2025")}
    with pytest.raises(PeriodMismatch):
        check_periods(KB.metrics["roe"], bad)
    with pytest.raises(PeriodMismatch, match="same year"):
        run_metric(KB.metrics["operating_margin"], {"operating_income": q("1"), "revenue": q("2", period="FY2024")})
    with pytest.raises(PeriodMismatch, match="after"):
        run_metric(KB.metrics["yoy_growth"], {"current": q("1", period="FY2024"), "previous": q("2")})


def test_inputs_must_match_and_units_must_fit() -> None:
    with pytest.raises(Exception, match="inputs must be exactly"):
        run_metric(KB.metrics["operating_margin"], {"operating_income": q("1")})
    with pytest.raises(UnitMismatch, match="currency amount"):
        coerce(q("5", "other"), KB.line_items["revenue"])
    with pytest.raises(UnitMismatch):
        coerce(q("5"), KB.line_items["energy_total"])


def _broken(edit) -> dict:
    raw = copy.deepcopy(RAW)
    edit(raw)
    return raw


@pytest.mark.parametrize("edit,message", [
    (lambda r: r["metrics"]["gross_margin"]["recipe"][0].update(function="guess"), "unknown calc function"),
    (lambda r: r["metrics"]["gross_margin"]["recipe"][0]["args"].pop("whole"), "must be exactly"),
    (lambda r: r["metrics"]["gross_margin"]["recipe"][0]["args"].update(whole="nowhere"), "not an input or an earlier step"),
    (lambda r: r["metrics"]["gross_margin"]["inputs"].update(extra={"line_item": "revenue"}), "never used"),
    (lambda r: r["metrics"]["gross_margin"]["inputs"]["revenue"].update(line_item="sales"), "unknown line item"),
    (lambda r: r["metrics"]["roe"]["recipe"][0].pop("id"), "needs a lower_snake_case id"),
    (lambda r: r["metrics"]["roe"]["recipe"].reverse(), "earlier step"),
    (lambda r: r["metrics"]["ltifr"]["recipe"][0]["constants"].update(per="-1"), "positive"),
    (lambda r: r["metrics"]["gross_margin"]["periods"].append({"same": ["ghost"]}), "unknown input"),
    (lambda r: r["line_items"]["ltifr_reported"].update(reported_ratio_of="nope"), "not a metric"),
    (lambda r: r["metrics"]["roe"]["recipe"][0]["args"].update(values="equity_end"), "must be a list"),
    (lambda r: r["metrics"]["gross_margin"].update(colour="red"), "Extra inputs"),
])
def test_a_broken_file_is_refused(edit, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        KnowledgeBase.model_validate(_broken(edit))


def test_labels_map_to_line_items() -> None:
    cases = {"Net sales: Total net sales": "revenue", "Revenue from operations": "revenue",
             "Total shareholders' equity": "total_equity", "Profit for the year": "net_income",
             "Total energy consumed (A+B+C+D+E+F)": "energy_total", "Gross margin": "gross_profit",
             "Payments for acquisition of property, plant and equipment": "capital_expenditure"}
    for label, key in cases.items():
        assert KB.line_item_for(label)[0] == key, label
    assert KB.line_item_for("Weather in Mumbai") is None


def test_point_change_takes_percentages_only() -> None:
    m = KB.metrics["point_change"]
    pct = dict(unit="percent", currency=None, scale=None)
    runs = run_metric(m, {"current": coerce(q("71.1", **pct), None, "percent"),
                          "previous": coerce(q("75.0", period="FY2024", **pct), None, "percent")})
    assert runs[-1].result.output.value == Decimal("-3.9") and m.display == "percentage_points"
    with pytest.raises(UnitMismatch, match="percent value"):
        coerce(q("5"), None, "percent")


# ---------------- earnings per share (Release 0 review, KB 1.1) ----------------

@pytest.mark.parametrize("label, key", [
    ("Earnings per share: Diluted", "diluted_eps"),               # Apple, Microsoft (heading: row)
    ("Earnings per share: Basic", "basic_eps"),
    ("Net income per share: Diluted", "diluted_eps"),             # NVIDIA
    ("Net income per share: Basic (1)", "basic_eps"),
    ("Diluted net income per share (Note 12)", "diluted_eps"),    # Alphabet
    ("Net income per diluted share", "diluted_eps"),
    ("Diluted earnings per share", "diluted_eps"),
    ("Basic earnings per equity share", "basic_eps"),             # Ind AS
    ("Net income", "net_income"),
    ("Net income attributable to common stockholders", "net_income"),
])
def test_per_share_labels_resolve_to_eps_and_only_there(label: str, key: str) -> None:
    assert KB.line_item_for(label)[0] == key


@pytest.mark.parametrize("label", [
    "Shares used in computing earnings per share: Diluted",       # a share count, not EPS
    "Weighted-average shares outstanding: Diluted",
    "Net income per share of common stock attributable to common stockholders: Diluted",  # no safe match
])
def test_share_counts_and_unknown_per_share_labels_are_never_guessed(label: str) -> None:
    assert KB.line_item_for(label) is None


def test_a_question_naming_eps_is_searched_even_if_the_planner_drops_it() -> None:
    from aeids.agents.document_intelligence.resolver import CellResolver

    r = CellResolver(KB)
    assert r.named_in("What were Apple's diluted earnings per share for fiscal 2025?") == ["Diluted earnings per share"]
    assert r.named_in("Microsoft basic EPS in fiscal 2025") == ["Basic earnings per share"]
    assert r.named_in("How many diluted shares did Apple have?") == []
    from aeids.knowledge import planning_catalog

    assert "Diluted earnings per share" in planning_catalog(KB, "What was NVIDIA's diluted EPS?")
