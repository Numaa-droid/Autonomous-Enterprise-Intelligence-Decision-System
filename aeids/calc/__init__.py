"""Deterministic calculation library (rule 2). See ``functions.py``."""

from aeids.calc.functions import (
    CALC_VERSION,
    FUNCTIONS,
    LIST_INPUTS,
    CalcError,
    CalcResult,
    CurrencyMismatch,
    DivisionByZero,
    MissingInput,
    PeriodMismatch,
    ScaleMismatch,
    UnitMismatch,
    absolute,
    average,
    cagr,
    days,
    difference,
    display,
    fiscal_year,
    intensity,
    margin,
    normalize_scale,
    pct_change,
    ratio,
    run,
    sum_values,
)

__all__ = [
    "CALC_VERSION", "FUNCTIONS", "LIST_INPUTS", "CalcError", "CalcResult", "CurrencyMismatch", "DivisionByZero",
    "MissingInput", "PeriodMismatch", "ScaleMismatch", "UnitMismatch", "absolute", "average", "cagr", "days",
    "difference", "display",
    "fiscal_year", "intensity", "margin", "normalize_scale", "pct_change", "ratio", "run", "sum_values",
]
