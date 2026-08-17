"""T2 (stage 2) — answers artifact with correct provenance; idempotency skip;
force re-extract. T3 — the extract_response_requirements job contract:
requirements_json coverage, prompt-version stamp, credentials by reference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rfi_manager.istari_adapter import LLM_FUNCTION_EXTRACT_RESPONSE
from rfi_manager.models import (
    PipelineState,
    Project,
    Requirement,
    RequirementsArtifact,
    ResponseRecord,
)
from rfi_manager.persistence import load_project
from rfi_manager.pipeline import (
    ANSWERS_ARTIFACT_NAME,
    LLMJobConfig,
    fetch_answers_artifact,
    process_response,
    response_prompt_version,
    retry_response,
)
from tests.fakes import FakeIstari

REQS = [
    Requirement(id="C-01", label="MOSA", description="MOSA compliance", type="enum",
                options=["Compliant", "Partial"]),
    Requirement(id="C-02", label="Weight (kg)", description="Unit weight",
                type="numeric", unit="kg"),
]

ANSWERS_JSON = json.dumps([
    {"id": "C-01", "value": "Compliant", "unit": None, "quote": "We comply.",
     "page": 2, "confidence": "high"},
    {"id": "C-02", "value": 38.5, "unit": "kg", "quote": "38.5 kg.",
     "page": 4, "confidence": "medium"},
])


def make_setup(tmp_path: Path):
    """Fake platform with a committed requirements artifact + one response."""
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="RFI TEXT")
    req_artifact = RequirementsArtifact(
        rfi_uuid=rfi.model_id, rfi_revision=rfi.latest_revision_id,
        schema_version="1.0", generated_at="2026-08-14T10:00:00+00:00",
        llm_model="claude:claude-opus-5", prompt_version="x", requirements=REQS,
    )
    info = istari.upload_json_artifact(
        rfi.model_id, "rfi-requirements.json", req_artifact.to_dict()
    )
    istari.create_link(rfi.latest_revision_id, info.revision_id)
    project = Project(
        rfi_uuid=rfi.model_id, rfi_revision=rfi.latest_revision_id,
        requirements_artifact_uuid=info.artifact_id,
        requirements_artifact_revision=info.revision_id,
        schema_version="1.0",
    )
    response = istari.add_model("acme-response.pdf", text="RESPONSE TEXT")
    return istari, project, req_artifact, response, tmp_path / "p.rfiproj"


def llm_config(istari: FakeIstari) -> LLMJobConfig:
    return LLMJobConfig(credentials=istari.default_credentials(),
                        provider="claude", model="claude-opus-5")


def run_one(istari, project, req_artifact, response, path, outputs=None, **kwargs):
    record = ResponseRecord(uuid=response.model_id)
    project.responses.append(record)
    for raw in outputs if outputs is not None else [ANSWERS_JSON]:
        istari.queue_llm_output(raw)
    return process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0, **kwargs,
    )


def test_stage2_end_to_end_provenance(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = run_one(istari, project, req_artifact, response, path)

    assert record.state is PipelineState.DONE
    assert record.vendor == "acme-response.pdf"
    assert record.schema_version == "1.0"
    assert record.llm_attempts == 1

    # T3: LLM job contract — attached to a standalone Model registered from
    # the extracted text (verified live: jobs can only stage a genuine
    # model's own revision), carries response identity + every committed
    # requirement id, binds credentials by id
    [register_call] = istari.register_text_model_calls
    text_artifact = istari.find_artifact(response.model_id, "text.txt")
    assert register_call["source_revision_id"] == text_artifact.revision_id

    [llm_call] = istari.llm_calls
    assert llm_call["function"] == LLM_FUNCTION_EXTRACT_RESPONSE
    assert llm_call["model_id"] == register_call["model_id"]
    params = llm_call["parameters"]
    assert params["response_uuid"] == response.model_id
    assert params["response_rev"] == response.latest_revision_id
    req_ids = [r["id"] for r in params["requirements_json"]]
    assert req_ids == ["C-01", "C-02"]
    assert params["requirements_json"][0]["options"] == ["Compliant", "Partial"]
    assert params["requirements_json"][1]["unit"] == "kg"
    assert "RESPONSE TEXT" not in json.dumps(params)  # text by reference only
    assert llm_call["credentials"].llm_credential_id.startswith("cred-")

    # answers artifact carries full provenance (PRD §4)
    upload = next(c for c in istari.upload_calls if c["name"] == ANSWERS_ARTIFACT_NAME)
    payload = upload["payload"]
    assert upload["model_id"] == response.model_id
    assert payload["response_uuid"] == response.model_id
    assert payload["response_revision"] == response.latest_revision_id
    assert payload["vendor"] == "acme-response.pdf"
    assert payload["schema_version"] == "1.0"
    assert payload["prompt_version"] == response_prompt_version("1.0")
    assert LLM_FUNCTION_EXTRACT_RESPONSE in payload["prompt_version"]
    assert payload["llm_model"] == "claude:claude-opus-5"
    assert [a["id"] for a in payload["answers"]] == ["C-01", "C-02"]

    # links: provenance (response rev -> answers rev) and discovery
    # (requirements artifact rev -> response rev)
    answers_rev = next(
        i.revision_id for i, _ in istari.artifacts[response.model_id]
        if i.artifact_id == record.answers_artifact_uuid
    )
    assert (response.latest_revision_id, answers_rev) in istari.link_calls
    assert (project.requirements_artifact_revision,
            response.latest_revision_id) in istari.link_calls

    # project persisted; table re-fetch path works
    assert load_project(path).response_for(response.model_id).state is PipelineState.DONE
    art = fetch_answers_artifact(istari, record)
    assert art.answers[0].value == "Compliant"


def test_prompt_version_stamp_changes_with_schema():
    assert response_prompt_version("1.0") != response_prompt_version("1.1")


def test_stage2_idempotency_skip_and_force(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    run_one(istari, project, req_artifact, response, path)
    uploads_before = len(istari.upload_calls)
    llm_before = len(istari.llm_calls)

    # second run for same (revision, schema): skipped, artifact loaded (FR5)
    record2 = run_one(istari, project, req_artifact, response, path, outputs=[])
    assert record2.state is PipelineState.DONE
    assert record2.answers_artifact_uuid is not None
    assert len(istari.upload_calls) == uploads_before  # nothing re-uploaded
    assert len(istari.llm_calls) == llm_before  # no LLM job submitted

    # force re-extract bypasses the check (FR5)
    record3 = run_one(istari, project, req_artifact, response, path, force=True)
    assert record3.state is PipelineState.DONE
    assert len(istari.llm_calls) == llm_before + 1


def test_stage2_invalid_llm_marks_failed_with_reason(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = run_one(istari, project, req_artifact, response, path,
                     outputs=["junk", "junk again"])
    assert record.state is PipelineState.FAILED
    assert "failed validation" in record.error
    assert record.llm_attempts == 2  # retried exactly once, persisted
    assert len(istari.llm_calls) == 2
    # the retry job carried the validation errors
    assert "validation_errors" in istari.llm_calls[1]["parameters"]
    # never uploaded (PRD §4)
    assert not any(c["name"] == ANSWERS_ARTIFACT_NAME for c in istari.upload_calls)


def test_stage2_job_failure_marks_failed_then_retry(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    istari.auto_complete_jobs = False

    record = ResponseRecord(uuid=response.model_id)
    project.responses.append(record)

    # drive to job_submitted, then fail the extraction job out-of-band
    import threading

    def fail_soon():
        while not istari.jobs:
            pass
        istari.fail_job(next(iter(istari.jobs)))

    t = threading.Thread(target=fail_soon)
    t.start()
    result = process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0,
    )
    t.join()
    assert result.state is PipelineState.FAILED
    assert "failed" in result.error

    # FR4 retry action: back to queued with evidence cleared, then succeeds
    istari.auto_complete_jobs = True
    retry_response(record, project, path)
    assert record.state is PipelineState.QUEUED
    assert record.job_id is None and record.llm_job_id is None
    istari.queue_llm_output(ANSWERS_JSON)
    result = process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0, force=True,
    )
    assert result.state is PipelineState.DONE
