"""T1 — table-driven tests for every validation rule in PRD §4."""

from __future__ import annotations

import json

import pytest

from rfi_manager.istari_adapter import (
    LLM_FUNCTION_EXTRACT_RESPONSE,
    LLM_RESPONSE_OUTPUT_ARTIFACT,
)
from rfi_manager.pipeline import (
    LLMJobConfig,
    run_llm_job_validated,
    strip_fences,
    validate_answers,
    validate_requirements,
)
from rfi_manager.models import NOT_FOUND, Requirement
from tests.fakes import FakeIstari

REQS = [
    Requirement(id="C-01", label="MOSA compliance", description="MOSA?", type="enum",
                options=["Compliant", "Partial", "Non-compliant"]),
    Requirement(id="C-02", label="Unit weight (kg)", description="Weight", type="numeric",
                unit="kg"),
    Requirement(id="C-03", label="ITAR restricted", description="ITAR?", type="boolean"),
    Requirement(id="C-04", label="Notes", description="Free text", type="text"),
]


def answers_json(overrides: dict | None = None, drop: list[str] | None = None) -> str:
    """A fully valid answers payload, with per-id overrides / dropped ids."""
    base = {
        "C-01": {"id": "C-01", "value": "Compliant", "unit": None,
                 "quote": "We are compliant.", "page": 3, "confidence": "high"},
        "C-02": {"id": "C-02", "value": 38.5, "unit": "kg",
                 "quote": "Weighs 38.5 kg.", "page": 5, "confidence": "high"},
        "C-03": {"id": "C-03", "value": True, "unit": None,
                 "quote": "ITAR applies.", "page": 7, "confidence": "medium"},
        "C-04": {"id": "C-04", "value": "See appendix", "unit": None,
                 "quote": "See appendix.", "page": 9, "confidence": "low"},
    }
    for rid, patch in (overrides or {}).items():
        base[rid].update(patch)
    for rid in drop or []:
        del base[rid]
    return json.dumps(list(base.values()))


# ---------------------------------------------------------------- fences

@pytest.mark.parametrize("wrapped", [
    "```json\n{payload}\n```",
    "```\n{payload}\n```",
    "  ```json\n{payload}\n```  ",
    "{payload}",
])
def test_strip_fences_variants(wrapped: str):
    payload = '[{"a": 1}]'
    assert json.loads(strip_fences(wrapped.format(payload=payload))) == [{"a": 1}]


def test_fenced_answers_validate():
    fenced = f"```json\n{answers_json()}\n```"
    result = validate_answers(fenced, REQS)
    assert result.ok
    assert len(result.items) == 4


# ---------------------------------------------------------------- JSON parse

def test_invalid_json_is_error():
    result = validate_answers("not json at all", REQS)
    assert not result.ok
    assert "not valid JSON" in result.errors[0]


def test_non_array_json_is_error():
    result = validate_answers('{"id": "C-01"}', REQS)
    assert not result.ok
    assert "expected a JSON array" in result.errors[0]


# ---------------------------------------------------------------- id coverage

def test_missing_id_is_error():
    result = validate_answers(answers_json(drop=["C-02"]), REQS)
    assert not result.ok
    assert any("C-02" in e and "missing" in e for e in result.errors)


def test_duplicate_id_is_error():
    data = json.loads(answers_json())
    data.append(dict(data[0]))
    result = validate_answers(json.dumps(data), REQS)
    assert not result.ok
    assert any("duplicate" in e for e in result.errors)


def test_unknown_id_is_warning_and_dropped():
    data = json.loads(answers_json())
    data.append({"id": "X-99", "value": "?", "quote": "", "page": None,
                 "confidence": "low"})
    result = validate_answers(json.dumps(data), REQS)
    assert result.ok
    assert any("unknown" in w for w in result.warnings)
    assert {a.id for a in result.items} == {"C-01", "C-02", "C-03", "C-04"}


# ---------------------------------------------------------------- type rules

def test_numeric_string_coerced_with_warning():
    result = validate_answers(answers_json({"C-02": {"value": "38.5"}}), REQS)
    assert result.ok
    assert any("coerced" in w for w in result.warnings)
    assert next(a for a in result.items if a.id == "C-02").value == 38.5


def test_numeric_unparseable_is_error():
    result = validate_answers(answers_json({"C-02": {"value": "heavy"}}), REQS)
    assert not result.ok
    assert any("does not parse as a number" in e for e in result.errors)


def test_boolean_must_be_json_bool():
    result = validate_answers(answers_json({"C-03": {"value": "yes"}}), REQS)
    assert not result.ok
    assert any("true/false" in e for e in result.errors)


def test_enum_value_not_in_options_is_error():
    result = validate_answers(answers_json({"C-01": {"value": "Sort of"}}), REQS)
    assert not result.ok
    assert any("not in options" in e for e in result.errors)


def test_enum_case_insensitive_match_normalized():
    result = validate_answers(answers_json({"C-01": {"value": "compliant"}}), REQS)
    assert result.ok
    assert next(a for a in result.items if a.id == "C-01").value == "Compliant"


def test_bool_rejected_for_numeric():
    result = validate_answers(answers_json({"C-02": {"value": True}}), REQS)
    assert not result.ok


def test_text_must_be_string():
    result = validate_answers(answers_json({"C-04": {"value": 42}}), REQS)
    assert not result.ok


# ---------------------------------------------------------------- NOT_FOUND

def test_not_found_allowed_anywhere_with_confidence_none():
    result = validate_answers(
        answers_json({"C-02": {"value": NOT_FOUND, "quote": "", "page": None,
                               "confidence": "none"}}),
        REQS,
    )
    assert result.ok
    a = next(a for a in result.items if a.id == "C-02")
    assert a.value == NOT_FOUND
    assert a.confidence == "none"


def test_not_found_with_wrong_confidence_normalized_with_warning():
    result = validate_answers(
        answers_json({"C-01": {"value": NOT_FOUND, "confidence": "high"}}), REQS
    )
    assert result.ok
    assert any("normalized" in w for w in result.warnings)
    assert next(a for a in result.items if a.id == "C-01").confidence == "none"


def test_invalid_confidence_is_error():
    result = validate_answers(answers_json({"C-03": {"confidence": "sure"}}), REQS)
    assert not result.ok


# ------------------------------------------------------- requirements (Prompt A)

def test_valid_requirements_parse():
    raw = json.dumps([r.to_dict() for r in REQS])
    result = validate_requirements(raw)
    assert result.ok
    assert [r.id for r in result.items] == ["C-01", "C-02", "C-03", "C-04"]


def test_requirement_duplicate_id_is_error():
    raw = json.dumps([REQS[0].to_dict(), REQS[0].to_dict()])
    result = validate_requirements(raw)
    assert not result.ok
    assert any("duplicate" in e for e in result.errors)


def test_requirement_invalid_type_is_error():
    bad = REQS[0].to_dict() | {"type": "date"}
    result = validate_requirements(json.dumps([bad]))
    assert not result.ok


def test_enum_without_options_is_error():
    bad = REQS[0].to_dict() | {"options": None}
    result = validate_requirements(json.dumps([bad]))
    assert not result.ok
    assert any("no options" in e for e in result.errors)


def test_numeric_without_unit_is_warning():
    ok = REQS[1].to_dict() | {"unit": None}
    result = validate_requirements(json.dumps([ok]))
    assert result.ok
    assert any("no unit" in w for w in result.warnings)


def test_long_label_is_warning():
    ok = REQS[3].to_dict() | {"label": "a very long label with many words"}
    result = validate_requirements(json.dumps([ok]))
    assert result.ok
    assert any("longer than 4 words" in w for w in result.warnings)


# ------------------------------------------- retry-once (LLM jobs, PRD §4)

def run_job(istari, model_id, config=None):
    istari.materialize_text(model_id)  # LLM jobs reference the text.txt revision
    return run_llm_job_validated(
        istari, model_id, LLM_FUNCTION_EXTRACT_RESPONSE,
        config or LLMJobConfig(credentials=istari.default_credentials()),
        lambda t: validate_answers(t, REQS),
        output_artifact=LLM_RESPONSE_OUTPUT_ARTIFACT,
        poll_interval_s=0,
    )


def test_retry_once_on_invalid_then_valid():
    istari = FakeIstari()
    model = istari.add_model("resp.pdf", text="T")
    istari.queue_llm_output("totally broken")
    istari.queue_llm_output(answers_json())

    result, raw = run_job(istari, model.model_id)

    assert result.ok
    assert raw == answers_json()
    assert len(istari.llm_calls) == 2
    # the retry job carries the error list as the validation_errors parameter
    assert "validation_errors" not in istari.llm_calls[0]["parameters"]
    retry_errors = istari.llm_calls[1]["parameters"]["validation_errors"]
    assert any("not valid JSON" in e for e in retry_errors)


def test_retry_once_still_invalid_stays_failed():
    istari = FakeIstari()
    model = istari.add_model("resp.pdf", text="T")
    istari.queue_llm_output("broken")
    istari.queue_llm_output("still broken")
    result, _raw = run_job(istari, model.model_id)
    assert not result.ok
    assert len(istari.llm_calls) == 2  # exactly one retry, never more


def test_no_retry_when_first_response_valid():
    istari = FakeIstari()
    model = istari.add_model("resp.pdf", text="T")
    istari.queue_llm_output(answers_json())
    result, _raw = run_job(istari, model.model_id)
    assert result.ok
    assert len(istari.llm_calls) == 1


# ------------------------------------------------- T/O fields (PRD §4, T-O_VALIDATION)

def test_to_fields_parse_and_roundtrip():
    raw = json.dumps([{
        "id": "1.1", "label": "Range", "description": "Range req", "type": "numeric",
        "unit": "km", "threshold": 200, "objective": 1500,
        "direction": "at_least", "gradeable": True,
        "to_raw": "T=200km/O=1500km",
    }])
    result = validate_requirements(raw)
    assert result.ok
    [req] = result.items
    assert (req.threshold, req.objective) == (200, 1500)
    assert req.direction == "at_least"
    assert req.gradeable is True
    assert req.to_raw == "T=200km/O=1500km"


def test_to_fields_missing_is_legal_backcompat():
    raw = json.dumps([r.to_dict() for r in REQS])
    result = validate_requirements(raw)
    assert result.ok
    assert all(r.gradeable is False for r in result.items)


def test_direction_contradicting_ordering_degrades():
    raw = json.dumps([{
        "id": "1.1", "label": "Range", "description": "d", "type": "numeric",
        "unit": "km", "threshold": 200, "objective": 1500,
        "direction": "at_most", "gradeable": True,
    }])
    result = validate_requirements(raw)
    assert result.ok  # degrades to a warning, never sinks the artifact
    assert any("contradicts T/O ordering" in w for w in result.warnings)
    assert result.items[0].gradeable is False


def test_invalid_direction_degrades():
    raw = json.dumps([{
        "id": "1.1", "label": "Range", "description": "d", "type": "numeric",
        "unit": "km", "threshold": 200, "objective": 1500,
        "direction": "upward", "gradeable": True,
    }])
    result = validate_requirements(raw)
    assert result.ok
    assert result.items[0].direction is None
    assert result.items[0].gradeable is False


def test_gradeable_numeric_without_to_degrades():
    raw = json.dumps([{
        "id": "1.1", "label": "Range", "description": "d", "type": "numeric",
        "unit": "km", "gradeable": True,
    }])
    result = validate_requirements(raw)
    assert result.ok
    assert result.items[0].gradeable is False


def test_enum_tier_options_must_be_members():
    raw = json.dumps([{
        "id": "C-01", "label": "MOSA", "description": "d", "type": "enum",
        "options": ["Compliant", "Partial"], "gradeable": True,
        "threshold_option": "Partial", "objective_option": "Gold",
    }])
    result = validate_requirements(raw)
    assert result.ok
    [req] = result.items
    assert req.threshold_option == "Partial"
    assert req.objective_option is None
    assert req.gradeable is False  # degraded by the bad objective_option


def test_llm_grade_accepted_on_text():
    raw = answers_json(overrides={"C-04": {
        "llm_grade": "MEETS_THRESHOLD",
        "llm_grade_rationale": "Meets the essential tier per the quote.",
    }})
    result = validate_answers(raw, REQS)
    assert result.ok
    answer = next(a for a in result.items if a.id == "C-04")
    assert answer.llm_grade == "MEETS_THRESHOLD"
    assert answer.llm_grade_rationale == "Meets the essential tier per the quote."


def test_llm_grade_invalid_vocabulary_dropped():
    raw = answers_json(overrides={"C-04": {"llm_grade": "AMAZING"}})
    result = validate_answers(raw, REQS)
    assert result.ok
    answer = next(a for a in result.items if a.id == "C-04")
    assert answer.llm_grade is None
    assert any("invalid llm_grade" in w for w in result.warnings)


def test_llm_grade_on_non_text_dropped():
    """Precedence rule: deterministic grading wins for numeric — an llm_grade
    there is dropped with a warning at validation time."""
    raw = answers_json(overrides={"C-02": {"llm_grade": "MEETS_OBJECTIVE"}})
    result = validate_answers(raw, REQS)
    assert result.ok
    answer = next(a for a in result.items if a.id == "C-02")
    assert answer.llm_grade is None
    assert any("deterministic grading takes precedence" in w for w in result.warnings)
