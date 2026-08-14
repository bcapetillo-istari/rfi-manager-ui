"""T4 — models/serialization round-trip and state-machine rules."""

from __future__ import annotations

import pytest

from rfi_manager.models import (
    Answer,
    AnswersArtifact,
    PipelineState,
    Project,
    Requirement,
    RequirementsArtifact,
    ResponseRecord,
    can_transition,
)


def make_requirements_artifact() -> RequirementsArtifact:
    return RequirementsArtifact(
        rfi_uuid="rfi-123",
        rfi_revision="rev-1",
        schema_version="1.0",
        generated_at="2026-08-14T10:00:00+00:00",
        llm_model="fake-llm",
        prompt_version="A1-B1",
        requirements=[
            Requirement(id="C-01", label="MOSA", description="MOSA?", type="enum",
                        options=["Compliant", "Partial"], required=True),
            Requirement(id="C-02", label="Weight (kg)", description="Weight",
                        type="numeric", unit="kg"),
        ],
    )


def make_answers_artifact() -> AnswersArtifact:
    return AnswersArtifact(
        response_uuid="resp-9",
        response_revision="rev-2",
        vendor="Acme Aerospace",
        schema_version="1.0",
        extracted_at="2026-08-14T11:00:00+00:00",
        llm_model="fake-llm",
        prompt_version="A1-B1",
        answers=[
            Answer(id="C-01", value="Compliant", quote="We comply.", page=3,
                   confidence="high"),
            Answer(id="C-02", value="NOT_FOUND", confidence="none"),
        ],
    )


def test_requirements_artifact_round_trip():
    art = make_requirements_artifact()
    assert RequirementsArtifact.from_dict(art.to_dict()) == art


def test_answers_artifact_round_trip():
    art = make_answers_artifact()
    assert AnswersArtifact.from_dict(art.to_dict()) == art


def test_project_round_trip():
    project = Project(
        rfi_uuid="rfi-123",
        rfi_revision="rev-1",
        requirements_artifact_uuid="art-55",
        schema_version="1.0",
        responses=[
            ResponseRecord(uuid="resp-9", revision="rev-2",
                           state=PipelineState.LLM_RETURNED, job_id="job-7",
                           llm_cache_path="/tmp/x.txt", vendor="Acme"),
        ],
    )
    assert Project.from_dict(project.to_dict()) == project


def test_project_rejects_unknown_format_version():
    d = Project(rfi_uuid="rfi-123").to_dict()
    d["format_version"] = 999
    with pytest.raises(ValueError, match="format_version"):
        Project.from_dict(d)


def test_data_contract_keys_frozen():
    """PRD §4 key names are frozen — a rename here is a spec violation."""
    req_keys = set(make_requirements_artifact().requirements[0].to_dict())
    assert req_keys == {"id", "label", "description", "type", "unit", "options", "required"}
    ans_keys = set(make_answers_artifact().answers[0].to_dict())
    assert ans_keys == {"id", "value", "unit", "quote", "page", "confidence"}
    wrapper = set(make_answers_artifact().to_dict())
    assert {"response_uuid", "response_revision", "vendor", "schema_version",
            "extracted_at", "llm_model", "answers"} <= wrapper


# ---------------------------------------------------------------- state machine

def test_happy_path_transitions():
    record = ResponseRecord(uuid="r1")
    for state in [PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_RETURNED, PipelineState.VALIDATED,
                  PipelineState.UPLOADED, PipelineState.DONE]:
        record.transition(state)
    assert record.state is PipelineState.DONE


def test_illegal_transition_raises():
    record = ResponseRecord(uuid="r1")
    with pytest.raises(ValueError, match="illegal transition"):
        record.transition(PipelineState.UPLOADED)  # queued -> uploaded skips steps


def test_any_active_state_may_fail():
    for state in [PipelineState.QUEUED, PipelineState.JOB_SUBMITTED,
                  PipelineState.TEXT_RETRIEVED, PipelineState.LLM_RETURNED,
                  PipelineState.VALIDATED, PipelineState.UPLOADED]:
        assert can_transition(state, PipelineState.FAILED)
    assert not can_transition(PipelineState.DONE, PipelineState.FAILED)


def test_failed_records_reason_and_retry_clears_evidence():
    record = ResponseRecord(uuid="r1", job_id="job-7", llm_cache_path="/tmp/x.txt")
    record.transition(PipelineState.JOB_SUBMITTED)
    record.transition(PipelineState.FAILED, error="job vanished")
    assert record.error == "job vanished"
    record.transition(PipelineState.QUEUED)  # retry (FR4)
    assert record.state is PipelineState.QUEUED
    assert record.error is None
    assert record.job_id is None
    assert record.llm_cache_path is None
