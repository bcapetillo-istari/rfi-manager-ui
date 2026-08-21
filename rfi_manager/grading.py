"""Deterministic T/O grading (docs/T-O_COMPLIANCE.md). Pure: no Qt, no SDK.

Grades are DERIVED, computed at export/report-build time from the on-screen
comparison data (pipeline.ComparisonCell — which carries what the committed
answer said, including any LLM text grade) plus the requirement's T/O fields.
The answers artifacts are never mutated. Numeric/enum/boolean grade
deterministically here; text types carry the LLM grade produced by the
Stage 2 job. Precedence rule: where deterministic code can compute, it always
wins — the LLM grade is consulted ONLY for text types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import units
from .models import GRADE_CATEGORIES, Requirement
from .pipeline import ComparisonCell

# Relative epsilon at band edges: absorbs float noise from unit conversion
# (199.99999999999997 km must not fail a T=200km threshold).
_EPSILON = 1e-9


@dataclass(frozen=True)
class GradeRecord:
    """One graded (requirement, response) cell — everything the validation
    report needs to render and audit a grade."""

    requirement_id: str
    grade: str  # one of GRADE_CATEGORIES
    grade_source: str  # "deterministic" | "llm"
    grade_reason: str | None = None  # set for NOT_GRADEABLE
    original_value: Any = None
    original_unit: str | None = None
    converted_value: float | None = None
    converted_unit: str | None = None
    threshold: Any = None
    objective: Any = None
    direction: str | None = None
    llm_grade_rationale: str | None = None


def _not_gradeable(req: Requirement, cell: ComparisonCell, reason: str) -> GradeRecord:
    return GradeRecord(
        requirement_id=req.id,
        grade="NOT_GRADEABLE",
        grade_source="deterministic",
        grade_reason=reason,
        original_value=cell.value,
        original_unit=cell.unit,
        threshold=req.threshold,
        objective=req.objective,
        direction=req.direction,
    )


def _passes(value: float, bound: float, direction: str) -> bool:
    """Direction-aware comparison with a relative epsilon at the band edge."""
    tolerance = _EPSILON * max(abs(value), abs(bound), 1.0)
    if direction == "at_least":
        return value >= bound - tolerance
    return value <= bound + tolerance  # at_most


def _numeric_grade(value: float, req: Requirement) -> str:
    """Band the value: fails T -> BELOW_THRESHOLD, passes T but not O ->
    MEETS_THRESHOLD, passes O -> MEETS_OBJECTIVE (direction-aware). T=O
    collapses the MEETS_THRESHOLD band — passing values go straight to
    MEETS_OBJECTIVE. With T absent (T=none tiers), any found value is at
    least MEETS_THRESHOLD."""
    direction = req.direction or "at_least"
    if req.objective is not None and _passes(value, float(req.objective), direction):
        return "MEETS_OBJECTIVE"
    if req.threshold is None:
        return "MEETS_THRESHOLD"  # T=none: found but below O
    if _passes(value, float(req.threshold), direction):
        return "MEETS_THRESHOLD"
    return "BELOW_THRESHOLD"


def _coerce_number(value: Any) -> float | None:
    """Real extracted values arrive as int/float or numeric strings ("212.98",
    "1,150"). Embedded-text blobs and ranges are NOT parsed — deterministic
    grading refuses to guess (docs/T-O_COMPLIANCE.md: ranges NOT_GRADEABLE)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _grade_numeric(req: Requirement, cell: ComparisonCell) -> GradeRecord:
    number = _coerce_number(cell.value)
    if number is None:
        return _not_gradeable(req, cell, "value_not_numeric")

    converted = number
    converted_unit = cell.unit
    if req.unit and cell.unit and cell.unit != req.unit:
        try:
            converted = units.convert(number, cell.unit, req.unit)
            converted_unit = req.unit
        except units.UnsupportedConversion:
            return _not_gradeable(req, cell, "unsupported_conversion")
    elif req.unit:
        # cell.unit is None or already matches: validate_answers inherits
        # req.unit when the LLM omits it, so treat the value as req-unit'd
        converted_unit = req.unit

    return GradeRecord(
        requirement_id=req.id,
        grade=_numeric_grade(converted, req),
        grade_source="deterministic",
        original_value=cell.value,
        original_unit=cell.unit,
        converted_value=converted,
        converted_unit=converted_unit,
        threshold=req.threshold,
        objective=req.objective,
        direction=req.direction,
    )


def _grade_boolean(req: Requirement, cell: ComparisonCell) -> GradeRecord:
    """The "(T=O)" must-statement class: true -> MEETS_OBJECTIVE,
    false -> BELOW_THRESHOLD."""
    if not isinstance(cell.value, bool):
        return _not_gradeable(req, cell, "value_not_boolean")
    return GradeRecord(
        requirement_id=req.id,
        grade="MEETS_OBJECTIVE" if cell.value else "BELOW_THRESHOLD",
        grade_source="deterministic",
        original_value=cell.value,
        threshold=req.threshold,
        objective=req.objective,
        direction=req.direction,
    )


def _grade_enum(req: Requirement, cell: ComparisonCell) -> GradeRecord:
    """Deterministic only when the schema maps T and O onto the options list
    (threshold_option/objective_option). Grading is by option index relative
    to the two tier positions — the tier positions themselves define which
    way "better" runs, so no assumption about options being pre-sorted."""
    if req.threshold_option is None and req.objective_option is None:
        return _not_gradeable(req, cell, "enum_tiers_missing")
    if not isinstance(cell.value, str) or not req.options:
        return _not_gradeable(req, cell, "value_not_enum")
    if cell.value not in req.options:
        return _not_gradeable(req, cell, "value_not_in_options")

    options = req.options
    value_idx = options.index(cell.value)
    obj_idx = (
        options.index(req.objective_option) if req.objective_option in options else None
    )
    thr_idx = (
        options.index(req.threshold_option) if req.threshold_option in options else None
    )
    if obj_idx is None and thr_idx is None:
        return _not_gradeable(req, cell, "enum_tiers_not_in_options")

    if obj_idx is not None and value_idx == obj_idx:
        grade = "MEETS_OBJECTIVE"
    elif obj_idx is not None and thr_idx is not None:
        low, high = sorted((thr_idx, obj_idx))
        if low <= value_idx <= high:
            grade = "MEETS_THRESHOLD"
        elif (value_idx > high) == (obj_idx > thr_idx):
            grade = "MEETS_OBJECTIVE"  # beyond the objective tier
        else:
            grade = "BELOW_THRESHOLD"
    elif thr_idx is not None and value_idx == thr_idx:
        grade = "MEETS_THRESHOLD"
    else:
        grade = "BELOW_THRESHOLD"

    return GradeRecord(
        requirement_id=req.id,
        grade=grade,
        grade_source="deterministic",
        original_value=cell.value,
        threshold=req.threshold_option,
        objective=req.objective_option,
        direction=req.direction,
    )


def grade_cell(req: Requirement, cell: ComparisonCell) -> GradeRecord:
    """Grade one comparison cell per the T/O spec.

    Precedence (docs/T-O_COMPLIANCE.md): deterministic always wins for
    numeric/enum/boolean — cell.llm_grade is consulted ONLY for text types,
    even if the LLM emitted one elsewhere."""
    if cell.is_not_found:
        return GradeRecord(
            requirement_id=req.id,
            grade="NOT_FOUND",
            grade_source="deterministic",
            original_value=cell.value,
            threshold=req.threshold,
            objective=req.objective,
            direction=req.direction,
        )
    if not req.gradeable:
        return _not_gradeable(req, cell, "informational")

    if req.type == "numeric":
        if req.threshold is None and req.objective is None:
            return _not_gradeable(req, cell, "to_values_missing")
        return _grade_numeric(req, cell)
    if req.type == "boolean":
        return _grade_boolean(req, cell)
    if req.type == "enum":
        return _grade_enum(req, cell)

    # text: the LLM's grade from the Stage 2 job, if it produced a valid one
    if cell.llm_grade in GRADE_CATEGORIES:
        return GradeRecord(
            requirement_id=req.id,
            grade=cell.llm_grade,
            grade_source="llm",
            original_value=cell.value,
            original_unit=cell.unit,
            threshold=req.threshold,
            objective=req.objective,
            direction=req.direction,
            llm_grade_rationale=cell.llm_grade_rationale,
        )
    return _not_gradeable(req, cell, "qualitative_ungraded")
