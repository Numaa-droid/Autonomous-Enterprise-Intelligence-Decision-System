"""The calculation module (rule 2): hand-computed cases, scale conversions, refusals, properties."""

from __future__ import annotations

import random
import uuid
from decimal import Decimal

import pytest

from aeids import calc
from aeids.calc import (
    CalcError,
    CurrencyMismatch,
    DivisionByZero,
    MissingInput,
    PeriodMismatch,
    ScaleMismatch,
    UnitMismatch,
)
from aeids.contracts import EvidenceRef, Finding, Quantity


def inr(value: str, scale: str | None = "crore", period: str | None = "FY2025") -> Quantity:
    return Quantity(value=value, unit="currency", currency="INR", scale=scale, period=period)


def usd(value: str, scale: str | None = "million", period: str | None = "FY2025") -> Quantity:
    return Quantity(value=value, unit="currency", currency="USD", scale=scale, period=period)


# ---------------- hand-computed cases ----------------

def test_pct_change() -> None:
    r = calc.pct_change(inr("4500"), inr("4000", period="FY2024"))
    assert r.output.value == Decimal("12.5") and r.output.unit == "percent"
    assert r.output.period == "FY2024 to FY2025" and r.function == "pct_change"
    assert r.inputs["current"].value == Decimal("4500") and r.calc_version == calc.CALC_VERSION
    # a fall, and a negative base (a loss narrowing from -200 to -50 is +75 %)
    assert calc.pct_change(inr("3000"), inr("4000", period="FY2024")).output.value == Decimal("-25")
    assert calc.pct_change(inr("-50"), inr("-200", period="FY2024")).output.value == Decimal("75")


def test_margin_ratio_difference() -> None:
    assert calc.margin(usd("123216"), usd("416161")).output.value.quantize(Decimal("0.0001")) == Decimal("29.6078")
    assert calc.ratio(usd("1"), usd("4")).output.value == Decimal("0.25")
    r = calc.difference(usd("416161"), usd("391035", period="FY2024"))
    assert r.output.value == Decimal("25126") and r.output.scale == "million" and r.output.period is None


def test_cagr() -> None:
    r = calc.cagr(inr("121", period="FY2025"), inr("100", period="FY2023"))
    assert r.output.value.quantize(Decimal("0.000001")) == Decimal("10.000000")
    assert "1 / 2" in r.formula
    assert calc.cagr(inr("200", period="2025"), inr("100", period="2024")).output.value == Decimal("100")


def test_sum_values() -> None:
    r = calc.sum_values([usd("10"), usd("2.5"), usd("0.5", scale="billion")])
    assert r.output.value == Decimal("512.5") and r.output.scale == "million"
    assert list(r.inputs) == ["v1", "v2", "v3"]


def test_full_precision_is_stored_and_rounding_is_only_for_display() -> None:
    r = calc.ratio(usd("1"), usd("3"))
    assert str(r.output.value) == "0.3333333333333333333333333333"
    assert calc.display(r.output) == "0.3333"
    assert calc.display(calc.margin(usd("1"), usd("3")).output) == "33.33%"
    assert calc.display(usd("416161")) == "USD 416,161 million"


# ---------------- scales ----------------

@pytest.mark.parametrize("value,src,dst,expected", [
    ("1", "crore", "lakh", "100"),
    ("250", "lakh", "crore", "2.5"),
    ("1", "crore", "million", "10"),
    ("45", "million", "crore", "4.5"),
    ("2", "billion", "million", "2000"),
    ("1500", "thousand", "million", "1.5"),
    ("3", "units", "thousand", "0.003"),
])
def test_scale_conversions(value: str, src: str, dst: str, expected: str) -> None:
    out = calc.normalize_scale(inr(value, scale=src), dst)
    assert out.value == Decimal(expected) and out.scale == dst and out.currency == "INR"


def test_mixed_scales_are_aligned_before_combining() -> None:
    # 4,500 crore vs 40,000 million = 4,000 crore -> 12.5 %
    r = calc.pct_change(inr("4500"), inr("40000", scale="million", period="FY2024"))
    assert r.output.value == Decimal("12.5")
    assert r.inputs["previous"].scale == "million"  # inputs are stored as given


def test_scale_refusals() -> None:
    with pytest.raises(ScaleMismatch, match="not stated"):
        calc.normalize_scale(inr("1", scale=None), "crore")
    with pytest.raises(ScaleMismatch, match="unknown scale"):
        calc.normalize_scale(inr("1"), "dozen")
    with pytest.raises(ScaleMismatch, match="does not"):
        calc.margin(usd("1", scale=None), usd("4"))
    # both unstated: the same printed basis, allowed
    assert calc.margin(usd("1", scale=None), usd("4", scale=None)).output.value == Decimal("25")


# ---------------- refusals ----------------

def test_currency_and_unit_mismatches() -> None:
    with pytest.raises(CurrencyMismatch):
        calc.pct_change(inr("1"), usd("1", period="FY2024"))
    with pytest.raises(UnitMismatch):
        calc.ratio(usd("1"), Quantity(value="3", unit="count", period="FY2025"))
    pct = Quantity(value="10", unit="percent", period="FY2025")
    with pytest.raises(UnitMismatch, match="ambiguous"):
        calc.pct_change(pct, pct.model_copy(update={"period": "FY2024"}))
    with pytest.raises(UnitMismatch):
        calc.margin(pct, pct)


def test_period_rules() -> None:
    with pytest.raises(PeriodMismatch, match="same period"):
        calc.margin(usd("1"), usd("4", period="FY2024"))
    with pytest.raises(PeriodMismatch, match="both values are for"):
        calc.pct_change(usd("2"), usd("1"))
    with pytest.raises(PeriodMismatch, match="fiscal-year"):
        calc.cagr(usd("2", period=None), usd("1", period="FY2023"))
    with pytest.raises(PeriodMismatch, match="must be after"):
        calc.cagr(usd("2", period="FY2023"), usd("1", period="FY2025"))
    with pytest.raises(PeriodMismatch):
        calc.sum_values([usd("1"), usd("2", period="FY2024")])


def test_division_by_zero_and_missing_inputs() -> None:
    with pytest.raises(DivisionByZero):
        calc.pct_change(usd("5"), usd("0", period="FY2024"))
    with pytest.raises(DivisionByZero):
        calc.margin(usd("5"), usd("0"))
    with pytest.raises(MissingInput):
        calc.ratio(usd("1"), None)  # type: ignore[arg-type]
    with pytest.raises(MissingInput):
        calc.sum_values([])
    with pytest.raises(CalcError, match="positive"):
        calc.cagr(usd("-1", period="FY2025"), usd("1", period="FY2024"))


def test_run_by_name() -> None:
    assert calc.run("margin", {"part": usd("1"), "whole": usd("4")}).output.value == Decimal("25")
    assert calc.run("sum_values", {"values": [usd("1"), usd("2")]}).output.value == Decimal("3")
    with pytest.raises(CalcError, match="unknown function"):
        calc.run("guess", {})
    with pytest.raises(CalcError, match="unknown inputs"):
        calc.run("margin", {"part": usd("1"), "whole": usd("4"), "extra": usd("1")})
    with pytest.raises(MissingInput):
        calc.run("margin", {"part": usd("1")})


def test_result_becomes_a_valid_stored_calculation() -> None:
    r = calc.pct_change(inr("4500"), inr("4000", period="FY2024"))
    c = r.to_contract()
    assert c.output == Decimal("12.5") and c.unit == "percent" and c.calc_version == calc.CALC_VERSION
    f = Finding(claim="Revenue grew", value=c.output, unit="percent", calculation=c,
                evidence=[EvidenceRef(evidence_id=uuid.uuid4())])
    assert f.model_dump(mode="json")["calculation"]["inputs"]["current"]["value"] == "4500"
    assert calc.run(c.function, dict(c.inputs)).output.value == c.output  # reproducible from storage


# ---------------- properties (seeded, repeatable) ----------------

def _rand_values(n: int = 200) -> list[Decimal]:
    rng = random.Random(20261122)
    return [Decimal(rng.randint(1, 10**9)) / Decimal(10 ** rng.randint(0, 4)) for _ in range(n)]


def test_properties() -> None:
    vals = _rand_values()
    for a, b in zip(vals, reversed(vals)):
        qa, qb = usd(str(a)), usd(str(b), period="FY2024")
        assert calc.pct_change(qa, qa.model_copy(update={"period": "FY2024"})).output.value == 0
        assert calc.difference(qa, qa).output.value == 0
        assert calc.ratio(qa, qa).output.value == 1
        assert calc.margin(qa, qa).output.value == 100
        assert calc.difference(qa, qb).output.value + b == a  # exact
        back = calc.normalize_scale(calc.normalize_scale(qa, "crore"), "million")
        assert abs(back.value - a) <= Decimal("1e-18") * max(abs(a), 1)
        # pct = (ratio - 1) * 100 for positive bases
        pct = calc.pct_change(qa, qb).output.value
        rat = calc.ratio(qa, qb.model_copy(update={"period": "FY2025"})).output.value
        assert abs(pct - (rat - 1) * 100) <= Decimal("1e-15") * max(abs(pct), 1)


def test_same_inputs_same_output() -> None:
    a, b = usd("416161"), usd("391035", period="FY2024")
    assert calc.pct_change(a, b) == calc.pct_change(a, b)


# ---------------- 1.1: average, absolute, intensity, days ----------------

def plain(value: str, period: str | None = "FY2025", unit: str = "other", scale: str | None = None) -> Quantity:
    return Quantity(value=value, unit=unit, scale=scale, period=period)


def test_average_takes_the_latest_period() -> None:
    r = calc.average([usd("73733"), usd("56950", period="FY2024")])
    assert r.output.value == Decimal("65341.5") and r.output.period == "FY2025" and r.formula == "(v1 + v2) / 2"
    # usable with a same-period value of the latest year: ROE on average equity
    roe = calc.margin(usd("112010"), r.output)
    assert roe.output.value.quantize(Decimal("0.01")) == Decimal("171.42")
    assert calc.average([usd("1", period=None), usd("3")]).output.period is None
    with pytest.raises(CurrencyMismatch):
        calc.average([usd("1"), inr("1")])


def test_absolute_for_outflows_printed_negative() -> None:
    capex = usd("-12715")
    r = calc.absolute(capex)
    assert r.output.value == Decimal("12715") and r.output.currency == "USD" and r.output.scale == "million"
    fcf = calc.difference(usd("111482"), r.output)
    assert fcf.output.value == Decimal("98767")


def test_intensity_per_rupee_and_per_million_hours() -> None:
    # 2,50,000 GJ over revenue of ₹ 5,000 crore = 0.000005 GJ per rupee (BRSR "per rupee of turnover")
    r = calc.intensity(plain("250000"), inr("5000"), plain("1"))
    assert r.output.value == Decimal("0.000005") and r.output.unit == "other"
    assert calc.display(r.output) == "0.000005"
    # LTIFR: 3 lost-time injuries over 12,000,000 hours, per million hours = 0.25
    ltifr = calc.intensity(plain("3"), plain("12000000"), plain("1000000"))
    assert ltifr.output.value == Decimal("0.25")
    assert set(ltifr.inputs) == {"amount", "base", "per"}  # the constant is stored with the inputs


def test_intensity_refusals() -> None:
    with pytest.raises(ScaleMismatch, match="scale is not stated"):
        calc.intensity(plain("1"), inr("5000", scale=None), plain("1"))
    with pytest.raises(PeriodMismatch):
        calc.intensity(plain("1"), inr("5000", period="FY2024"), plain("1"))
    with pytest.raises(CalcError, match="positive"):
        calc.intensity(plain("1"), inr("5000"), plain("0"))
    with pytest.raises(DivisionByZero):
        calc.intensity(plain("1"), inr("0"), plain("1"))
    with pytest.raises(CurrencyMismatch):
        calc.intensity(usd("1"), inr("1"), plain("1"))


def test_days() -> None:
    r = calc.days(usd("7000"), usd("220960"))
    assert r.output.unit == "days" and calc.display(r.output) == "11.6 days"
    with pytest.raises(PeriodMismatch):
        calc.days(usd("1"), usd("2", period="FY2024"))


def test_list_functions_replay_from_their_stored_input_names() -> None:
    for fn, vals in (("sum_values", [usd("1"), usd("2")]), ("average", [usd("1"), usd("3", period="FY2024")])):
        stored = calc.run(fn, {"values": vals}).to_contract()
        assert calc.run(stored.function, dict(stored.inputs)).output.value == stored.output
