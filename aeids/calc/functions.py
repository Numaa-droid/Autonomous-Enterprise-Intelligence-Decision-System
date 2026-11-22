"""Deterministic financial calculations (rule 2, MVP_PLAN.md §4).

Pure functions: no I/O, no model calls. Inputs are ``Quantity`` objects (value with unit,
currency, scale and period as separate fields); every function returns a ``CalcResult`` that
holds the function name, a readable formula, the inputs exactly as given, the full-precision
output and ``CALC_VERSION``. Rounding happens only when a number is shown (:func:`display`).

A function refuses (raises a ``CalcError`` subclass) rather than guess: different currencies,
units that cannot be combined, a currency amount whose scale is unknown next to one whose scale
is known, periods that do not fit the calculation, a zero divisor, a missing input.
"""

from __future__ import annotations

import re
from decimal import Decimal, localcontext
from typing import Any

from pydantic import BaseModel, ConfigDict

from aeids.contracts import Calculation, Quantity

CALC_VERSION = "1.1"  # 1.1: average, absolute, intensity, days (1.0 functions unchanged)
PRECISION = 28  # significant digits for division and roots
SCALE_FACTORS: dict[str, Decimal] = {
    "units": Decimal(1),
    "thousand": Decimal(10) ** 3,
    "lakh": Decimal(10) ** 5,
    "million": Decimal(10) ** 6,
    "crore": Decimal(10) ** 7,
    "billion": Decimal(10) ** 9,
}
SCALABLE_UNITS = {"currency", "count", "shares", "other"}  # units a scale word applies to
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


class CalcError(ValueError):
    code = "calc_error"


class MissingInput(CalcError):
    code = "missing_input"


class UnitMismatch(CalcError):
    code = "unit_mismatch"


class CurrencyMismatch(CalcError):
    code = "currency_mismatch"


class ScaleMismatch(CalcError):
    code = "scale_mismatch"


class PeriodMismatch(CalcError):
    code = "period_mismatch"


class DivisionByZero(CalcError):
    code = "division_by_zero"


class CalcResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    function: str
    formula: str
    inputs: dict[str, Quantity]
    output: Quantity
    calc_version: str = CALC_VERSION

    def to_contract(self) -> Calculation:
        """The ``Calculation`` stored with a finding (``calculations`` table)."""
        return Calculation(function=self.function, formula=self.formula, inputs=self.inputs,
                           output=self.output.value, unit=self.output.unit,
                           currency=self.output.currency, scale=self.output.scale,
                           calc_version=self.calc_version)


# --- helpers ----------------------------------------------------------------------------------

def _need(**named: Quantity | None) -> None:
    missing = [k for k, v in named.items() if v is None]
    if missing:
        raise MissingInput(f"missing input: {', '.join(missing)}")


def _scale_of(q: Quantity) -> str | None:
    return q.scale if q.unit in SCALABLE_UNITS else None


def fiscal_year(period: str | None) -> int | None:
    """2025 from "FY2025", "2025" or "FY 2025"; None when the period names no single year."""
    if not period:
        return None
    years = {int(m) for m in _YEAR.findall(period)}
    return years.pop() if len(years) == 1 else None


def normalize_scale(q: Quantity, to: str) -> Quantity:
    """The same amount expressed in another scale (e.g. 4,500 crore -> 45,000 million)."""
    _need(q=q)
    if to not in SCALE_FACTORS:
        raise ScaleMismatch(f"unknown scale {to!r}")
    if q.unit not in SCALABLE_UNITS:
        raise ScaleMismatch(f"a {q.unit} value has no scale")
    if q.scale is None:
        raise ScaleMismatch("the value's scale is not stated, so it cannot be converted")
    if q.scale == to:
        return q
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = q.value * SCALE_FACTORS[q.scale] / SCALE_FACTORS[to]
    return q.model_copy(update={"value": value, "scale": to})


def _same_kind(a: Quantity, b: Quantity, what: str) -> None:
    if a.unit != b.unit:
        raise UnitMismatch(f"{what}: cannot combine {a.unit} with {b.unit}")
    if (a.currency or None) != (b.currency or None):
        raise CurrencyMismatch(f"{what}: cannot combine {a.currency} with {b.currency}")


def _align(a: Quantity, b: Quantity, what: str) -> tuple[Quantity, Quantity]:
    """Bring b to a's scale. Both unknown is fine (same printed basis); one unknown is not."""
    sa, sb = _scale_of(a), _scale_of(b)
    if sa == sb:
        return a, b
    if sa is None or sb is None:
        raise ScaleMismatch(f"{what}: one value states its scale ({sa or sb}) and the other does not")
    return a, normalize_scale(b, sa)


def _same_period(a: Quantity, b: Quantity, what: str) -> None:
    if a.period and b.period and a.period != b.period:
        raise PeriodMismatch(f"{what}: both values must be for the same period ({a.period} vs {b.period})")


def _div(x: Decimal, y: Decimal, what: str) -> Decimal:
    if y == 0:
        raise DivisionByZero(f"{what}: division by zero")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return x / y


def _result(function: str, formula: str, inputs: dict[str, Quantity], output: Quantity) -> CalcResult:
    return CalcResult(function=function, formula=formula, inputs=inputs, output=output)


# --- functions --------------------------------------------------------------------------------

def difference(a: Quantity, b: Quantity) -> CalcResult:
    """a − b, in a's unit, currency and scale. Any periods (e.g. FY2025 − FY2024)."""
    _need(a=a, b=b)
    _same_kind(a, b, "difference")
    a2, b2 = _align(a, b, "difference")
    out = Quantity(value=a2.value - b2.value, unit=a.unit, currency=a.currency, scale=_scale_of(a),
                   period=a.period if a.period == b.period else None)
    return _result("difference", "a - b", {"a": a, "b": b}, out)


def pct_change(current: Quantity, previous: Quantity) -> CalcResult:
    """(current − previous) / |previous| × 100, in percent. The periods must differ."""
    _need(current=current, previous=previous)
    _same_kind(current, previous, "pct_change")
    if current.unit in ("percent", "ratio"):
        raise UnitMismatch("pct_change of a percentage or ratio is ambiguous; use difference")
    if current.period and previous.period and current.period == previous.period:
        raise PeriodMismatch(f"pct_change: both values are for {current.period}")
    c, p = _align(current, previous, "pct_change")
    value = _div(c.value - p.value, abs(p.value), "pct_change") * 100
    period = f"{previous.period} to {current.period}" if current.period and previous.period else None
    return _result("pct_change", "(current - previous) / |previous| * 100",
                   {"current": current, "previous": previous},
                   Quantity(value=value, unit="percent", period=period))


def ratio(numerator: Quantity, denominator: Quantity) -> CalcResult:
    """numerator / denominator, for the same period and the same unit (and currency)."""
    _need(numerator=numerator, denominator=denominator)
    _same_kind(numerator, denominator, "ratio")
    _same_period(numerator, denominator, "ratio")
    n, d = _align(numerator, denominator, "ratio")
    value = _div(n.value, d.value, "ratio")
    return _result("ratio", "numerator / denominator", {"numerator": numerator, "denominator": denominator},
                   Quantity(value=value, unit="ratio", period=numerator.period or denominator.period))


def margin(part: Quantity, whole: Quantity) -> CalcResult:
    """part / whole × 100, in percent (e.g. operating income / revenue). Same period."""
    _need(part=part, whole=whole)
    _same_kind(part, whole, "margin")
    if part.unit in ("percent", "ratio"):
        raise UnitMismatch("margin needs two amounts, not percentages or ratios")
    _same_period(part, whole, "margin")
    p, w = _align(part, whole, "margin")
    value = _div(p.value, w.value, "margin") * 100
    return _result("margin", "part / whole * 100", {"part": part, "whole": whole},
                   Quantity(value=value, unit="percent", period=part.period or whole.period))


def cagr(end: Quantity, start: Quantity) -> CalcResult:
    """((end / start) ^ (1 / years) − 1) × 100, years from the two fiscal-year periods."""
    _need(end=end, start=start)
    _same_kind(end, start, "cagr")
    if end.unit in ("percent", "ratio"):
        raise UnitMismatch("cagr needs two amounts, not percentages or ratios")
    y_end, y_start = fiscal_year(end.period), fiscal_year(start.period)
    if y_end is None or y_start is None:
        raise PeriodMismatch("cagr: both values need a fiscal-year period (e.g. FY2025)")
    years = y_end - y_start
    if years <= 0:
        raise PeriodMismatch(f"cagr: the end period ({end.period}) must be after the start ({start.period})")
    e, s = _align(end, start, "cagr")
    if s.value <= 0 or e.value <= 0:
        raise CalcError("cagr: both values must be positive")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        growth = (e.value / s.value) ** (Decimal(1) / Decimal(years))
        value = (growth - 1) * 100
    return _result("cagr", f"((end / start) ^ (1 / {years}) - 1) * 100", {"end": end, "start": start},
                   Quantity(value=value, unit="percent", period=f"{start.period} to {end.period}"))


def sum_values(values: list[Quantity]) -> CalcResult:
    """v1 + v2 + …, same unit, currency and period; scales aligned to the first value's."""
    if not values or any(v is None for v in values):
        raise MissingInput("sum_values needs at least one value")
    first = values[0]
    aligned = [first]
    for v in values[1:]:
        _same_kind(first, v, "sum_values")
        _same_period(first, v, "sum_values")
        aligned.append(_align(first, v, "sum_values")[1])
    total = sum((v.value for v in aligned), Decimal(0))
    names = {f"v{i + 1}": v for i, v in enumerate(values)}
    return _result("sum_values", " + ".join(names), names,
                   Quantity(value=total, unit=first.unit, currency=first.currency,
                            scale=_scale_of(first), period=first.period))


def average(values: list[Quantity]) -> CalcResult:
    """(v1 + … + vn) / n, same unit and currency; the periods may differ (e.g. opening and
    closing equity). The result carries the latest fiscal year, the year it is used for."""
    if not values or any(v is None for v in values):
        raise MissingInput("average needs at least one value")
    first = values[0]
    aligned = [first]
    for v in values[1:]:
        _same_kind(first, v, "average")
        aligned.append(_align(first, v, "average")[1])
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = sum((v.value for v in aligned), Decimal(0)) / len(aligned)
    years = [fiscal_year(v.period) for v in values]
    period = None if None in years else next(v.period for v in values if fiscal_year(v.period) == max(years))
    names = {f"v{i + 1}": v for i, v in enumerate(values)}
    return _result("average", f"({' + '.join(names)}) / {len(names)}", names,
                   Quantity(value=value, unit=first.unit, currency=first.currency,
                            scale=_scale_of(first), period=period))


def absolute(a: Quantity) -> CalcResult:
    """|a|: for outflows printed as negatives (capital expenditure, dividends paid)."""
    _need(a=a)
    return _result("absolute", "|a|", {"a": a}, a.model_copy(update={"value": abs(a.value),
                                                                      "evidence_id": None}))


def _in_units(q: Quantity, what: str) -> Decimal:
    """The plain amount (scale applied). A currency amount must state its scale."""
    if q.unit in SCALABLE_UNITS and q.scale:
        return q.value * SCALE_FACTORS[q.scale]
    if q.unit == "currency" and q.scale is None:
        raise ScaleMismatch(f"{what}: the currency amount's scale is not stated")
    return q.value


def intensity(amount: Quantity, base: Quantity, per: Quantity) -> CalcResult:
    """amount / base × per, both in plain units (e.g. energy in GJ per rupee of turnover, with
    per = 1; injuries per million hours worked, with per = 1,000,000). The units may differ;
    two currency amounts must share a currency. Same period."""
    _need(amount=amount, base=base, per=per)
    if amount.unit == "currency" and base.unit == "currency" and amount.currency != base.currency:
        raise CurrencyMismatch(f"intensity: cannot combine {amount.currency} with {base.currency}")
    if amount.unit in ("percent", "ratio") or base.unit in ("percent", "ratio"):
        raise UnitMismatch("intensity needs two amounts, not percentages or ratios")
    if per.unit not in ("count", "other") or per.value <= 0:
        raise CalcError("intensity: per must be a positive plain number")
    _same_period(amount, base, "intensity")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = _div(_in_units(amount, "intensity"), _in_units(base, "intensity"), "intensity") * per.value
    return _result("intensity", "amount / base * per (amounts in plain units)",
                   {"amount": amount, "base": base, "per": per},
                   Quantity(value=value, unit="other", period=amount.period or base.period))


def days(amount: Quantity, base: Quantity) -> CalcResult:
    """amount / base × 365, in days (e.g. average inventory / cost of sales). Same unit."""
    _need(amount=amount, base=base)
    _same_kind(amount, base, "days")
    a, b = _align(amount, base, "days")
    if amount.period and base.period and amount.period != base.period:
        raise PeriodMismatch(f"days: both values must be for the same period ({amount.period} vs {base.period})")
    value = _div(a.value, b.value, "days") * 365
    return _result("days", "amount / base * 365", {"amount": amount, "base": base},
                   Quantity(value=value, unit="days", period=amount.period or base.period))


# The functions an agent may ask for, with the names of their inputs.
FUNCTIONS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "pct_change": (pct_change, ("current", "previous")),
    "difference": (difference, ("a", "b")),
    "ratio": (ratio, ("numerator", "denominator")),
    "margin": (margin, ("part", "whole")),
    "cagr": (cagr, ("end", "start")),
    "sum_values": (sum_values, ("values",)),
    "average": (average, ("values",)),
    "absolute": (absolute, ("a",)),
    "intensity": (intensity, ("amount", "base", "per")),
    "days": (days, ("amount", "base")),
}
LIST_INPUTS = {"sum_values", "average"}  # functions whose one input is a list of values


def run(function: str, inputs: dict[str, Any]) -> CalcResult:
    """Call a function by name with named inputs (``values`` is a list for sum_values/average,
    or the stored ``v1``, ``v2``, … names, so a stored calculation replays as it was run)."""
    if function in LIST_INPUTS and "values" not in inputs and inputs and \
            all(re.fullmatch(r"v\d+", k) for k in inputs):
        inputs = {"values": [inputs[k] for k in sorted(inputs, key=lambda k: int(k[1:]))]}
    if function not in FUNCTIONS:
        raise CalcError(f"unknown function {function!r}; known: {', '.join(FUNCTIONS)}")
    fn, names = FUNCTIONS[function]
    unknown = set(inputs) - set(names)
    if unknown:
        raise CalcError(f"{function}: unknown inputs {sorted(unknown)}; expected {list(names)}")
    missing = [n for n in names if inputs.get(n) is None]
    if missing:
        raise MissingInput(f"{function}: missing {', '.join(missing)}")
    if function in LIST_INPUTS:
        return fn(list(inputs["values"]))
    return fn(*(inputs[n] for n in names))


# --- presentation -----------------------------------------------------------------------------

def display(q: Quantity) -> str:
    """A rounded, readable form of a value. Only for text; stored values keep full precision."""
    if q.unit == "percent":
        return f"{q.value.quantize(Decimal('0.01')):,}%"
    if q.value != 0 and abs(q.value) < 1 and q.unit in ("ratio", "other", "count"):
        return f"{q.value:.4g}"  # small intensities (e.g. GJ per rupee) keep 4 significant digits
    if q.unit == "ratio":
        return f"{q.value.quantize(Decimal('0.0001')):,}"
    if q.unit == "days":
        return f"{q.value.quantize(Decimal('0.1')):,} days"
    places = Decimal(1) if q.value == q.value.to_integral_value() else Decimal("0.01")
    text = f"{q.value.quantize(places):,}"
    if q.unit == "currency":
        text = f"{q.currency} {text}"
    if q.scale and q.scale != "units":
        text += f" {q.scale}"
    return text


def display_points(q: Quantity) -> str:
    """A change between two percentages, in percentage points (e.g. "-3.90 percentage points")."""
    return f"{q.value.quantize(Decimal('0.01')):+,} percentage points"
