"""grading.py: all five categories, both directions, T=O and T=none tiers,
unit conversion, the deterministic-wins precedence rule, epsilon at band
edges, and backcompat with requirements missing T/O fields."""

from __future__ import annotations

import pytest

from rfi_manager.grading import grade_cell
from rfi_manager.models import NOT_FOUND, Requirement
from rfi_manager.pipeline import ComparisonCell


def numeric_req(threshold=200, objective=1500, direction="at_least", unit="km", **kw):
    return Requirement(
        id="R-1", label="Range", description="d", type="numeric", unit=unit,
        threshold=threshold, objective=objective, direction=direction,
        gradeable=True, **kw,
    )


def cell(value, unit=None, **kw):
    return ComparisonCell(value=value, unit=unit, **kw)


# ------------------------------------------------------------- numeric bands

@pytest.mark.parametrize("value,expected", [
    (199, "BELOW_THRESHOLD"),
    (200, "MEETS_THRESHOLD"),  # inclusive lower edge
    (1499, "MEETS_THRESHOLD"),
    (1500, "MEETS_OBJECTIVE"),  # inclusive objective edge
    (99999, "MEETS_OBJECTIVE"),  # beyond objective grades the same
])
def test_at_least_bands(value, expected):
    record = grade_cell(numeric_req(), cell(value, unit="km"))
    assert record.grade == expected
    assert record.grade_source == "deterministic"


@pytest.mark.parametrize("value,expected", [
    (7, "BELOW_THRESHOLD"),  # lead time: T=6 months, O=3 months
    (6, "MEETS_THRESHOLD"),
    (4, "MEETS_THRESHOLD"),
    (3, "MEETS_OBJECTIVE"),
    (1, "MEETS_OBJECTIVE"),
])
def test_at_most_bands(value, expected):
    req = numeric_req(threshold=6, objective=3, direction="at_most", unit="month")
    assert grade_cell(req, cell(value, unit="month")).grade == expected


def test_t_equals_o_collapses_to_objective():
    req = numeric_req(threshold=50, objective=50, unit="mbps")
    assert grade_cell(req, cell(50, unit="mbps")).grade == "MEETS_OBJECTIVE"
    assert grade_cell(req, cell(49, unit="mbps")).grade == "BELOW_THRESHOLD"


def test_t_none_grades_against_objective_only():
    req = numeric_req(threshold=None, objective=100)
    assert grade_cell(req, cell(150, unit="km")).grade == "MEETS_OBJECTIVE"
    # found but below O with no T: MEETS_THRESHOLD, never BELOW_THRESHOLD
    assert grade_cell(req, cell(50, unit="km")).grade == "MEETS_THRESHOLD"


def test_unit_conversion_applied():
    # 200 km threshold; answer of 120 nmi = 222.24 km -> meets threshold
    record = grade_cell(numeric_req(), cell(120, unit="nmi"))
    assert record.grade == "MEETS_THRESHOLD"
    assert record.converted_value == pytest.approx(222.24)
    assert record.converted_unit == "km"
    assert record.original_value == 120
    assert record.original_unit == "nmi"


def test_epsilon_absorbs_float_noise():
    # a conversion chain that lands a hair under the threshold must still pass
    value = 200.0 - 200.0 * 1e-12
    assert grade_cell(numeric_req(), cell(value, unit="km")).grade == "MEETS_THRESHOLD"


def test_unsupported_conversion_not_gradeable():
    record = grade_cell(numeric_req(), cell(38.5, unit="kg"))
    assert record.grade == "NOT_GRADEABLE"
    assert record.grade_reason == "unsupported_conversion"


def test_numeric_string_value_coerced():
    assert grade_cell(numeric_req(), cell("1,650", unit="km")).grade == "MEETS_OBJECTIVE"


def test_text_blob_value_not_gradeable():
    record = grade_cell(numeric_req(), cell("12,000-18,000 ft blah", unit="km"))
    assert record.grade == "NOT_GRADEABLE"
    assert record.grade_reason == "value_not_numeric"


# ---------------------------------------------------------- other categories

def test_not_found():
    assert grade_cell(numeric_req(), cell(NOT_FOUND)).grade == "NOT_FOUND"
    assert grade_cell(numeric_req(), cell(None)).grade == "NOT_FOUND"


def test_informational_not_gradeable():
    req = numeric_req()
    req = Requirement(**{**req.to_dict(), "gradeable": False})
    record = grade_cell(req, cell(500, unit="km"))
    assert record.grade == "NOT_GRADEABLE"
    assert record.grade_reason == "informational"


def test_missing_to_fields_backcompat():
    """Old artifacts (pre-T/O schema) parse with gradeable=False and grade
    NOT_GRADEABLE — never crash."""
    old = Requirement.from_dict(
        {"id": "R-1", "label": "Range", "description": "d", "type": "numeric",
         "unit": "km"}
    )
    assert old.gradeable is False
    assert grade_cell(old, cell(500, unit="km")).grade == "NOT_GRADEABLE"


# ------------------------------------------------------------------- boolean

def test_boolean_grading():
    req = Requirement(id="KSA-1", label="IFF", description="d", type="boolean",
                      gradeable=True, threshold=True, objective=True)
    assert grade_cell(req, cell(True)).grade == "MEETS_OBJECTIVE"
    assert grade_cell(req, cell(False)).grade == "BELOW_THRESHOLD"


# ---------------------------------------------------------------------- enum

def enum_req(**kw):
    return Requirement(
        id="C-01", label="MOSA", description="d", type="enum",
        options=["Non-Compliant", "Partial", "Compliant"],
        threshold_option="Partial", objective_option="Compliant",
        gradeable=True, **kw,
    )


@pytest.mark.parametrize("value,expected", [
    ("Non-Compliant", "BELOW_THRESHOLD"),
    ("Partial", "MEETS_THRESHOLD"),
    ("Compliant", "MEETS_OBJECTIVE"),
])
def test_enum_grading(value, expected):
    assert grade_cell(enum_req(), cell(value)).grade == expected


def test_enum_without_tiers_not_gradeable():
    req = Requirement(id="C-01", label="MOSA", description="d", type="enum",
                      options=["A", "B"], gradeable=True)
    record = grade_cell(req, cell("A"))
    assert record.grade == "NOT_GRADEABLE"
    assert record.grade_reason == "enum_tiers_missing"


# ---------------------------------------------------- precedence: text + LLM

def text_req(**kw):
    return Requirement(id="1.9", label="Comms", description="d", type="text",
                       gradeable=True, **kw)


def test_text_uses_llm_grade():
    record = grade_cell(
        text_req(),
        cell("Secure BLOS with FMV", llm_grade="MEETS_OBJECTIVE",
             llm_grade_rationale="BLOS SATCOM with FMV meets the O tier."),
    )
    assert record.grade == "MEETS_OBJECTIVE"
    assert record.grade_source == "llm"
    assert record.llm_grade_rationale == "BLOS SATCOM with FMV meets the O tier."


def test_text_without_llm_grade_not_gradeable():
    record = grade_cell(text_req(), cell("some prose"))
    assert record.grade == "NOT_GRADEABLE"
    assert record.grade_reason == "qualitative_ungraded"


def test_deterministic_wins_over_llm_grade():
    """Precedence rule: an llm_grade on a numeric cell is ignored — the
    deterministic computation always wins for numeric/enum/boolean."""
    record = grade_cell(
        numeric_req(), cell(199, unit="km", llm_grade="MEETS_OBJECTIVE")
    )
    assert record.grade == "BELOW_THRESHOLD"
    assert record.grade_source == "deterministic"
