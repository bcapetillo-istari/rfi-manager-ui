"""T2 (stage 1) — end-to-end with fakes: extraction + LLM job produce
requirements; commit uploads the artifact with correct linkage args."""

from __future__ import annotations

import json

import pytest

from rfi_manager.istari_adapter import (
    LLM_FUNCTION_EXTRACT_RFI,
    CredentialSelection,
)
from rfi_manager.models import Requirement
from rfi_manager.pipeline import (
    REQUIREMENTS_ARTIFACT_NAME,
    LLMJobConfig,
    PipelineError,
    commit_requirements,
    next_schema_version,
    run_stage1_extraction,
    wait_for_job,
)
from tests.fakes import FakeIstari

REQS_JSON = json.dumps([
    {"id": "C-01", "label": "MOSA", "description": "MOSA compliance", "type": "enum",
     "unit": None, "options": ["Compliant", "Partial"], "required": True},
    {"id": "C-02", "label": "Weight (kg)", "description": "Unit weight", "type": "numeric",
     "unit": "kg", "options": None, "required": False},
])


def make_config(istari: FakeIstari) -> LLMJobConfig:
    return LLMJobConfig(
        credentials=istari.default_credentials(), provider="claude",
        model="claude-opus-5",
    )


def test_stage1_extraction_end_to_end():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="RFI DOCUMENT TEXT")
    istari.queue_llm_output(REQS_JSON)
    events: list[tuple[str, str]] = []

    result = run_stage1_extraction(
        istari, make_config(istari), rfi.model_id, poll_interval_s=0,
        progress=lambda s, d: events.append((s, d)),
    )

    assert [r.id for r in result.requirements] == ["C-01", "C-02"]
    assert result.rfi_revision_id == rfi.latest_revision_id
    assert result.llm_model == "claude:claude-opus-5"

    # the LLM job is attached to the extracted-text ARTIFACT — not the RFI
    # model — so the platform stages text.txt, not the source PDF (never
    # raw text either way: only revision references travel in parameters)
    [llm_call] = istari.llm_calls
    assert llm_call["function"] == LLM_FUNCTION_EXTRACT_RFI
    text_artifact = istari.find_artifact(rfi.model_id, "text.txt")
    assert llm_call["model_id"] == text_artifact.artifact_id
    assert llm_call["parameters"]["source_resource_id"] == text_artifact.artifact_id
    assert llm_call["parameters"]["source_revision_id"] == text_artifact.revision_id
    assert llm_call["parameters"]["origin_resource_id"] == rfi.model_id
    assert llm_call["parameters"]["provider"] == "claude"
    assert "RFI DOCUMENT TEXT" not in json.dumps(llm_call["parameters"])

    # credentials bound by reference — no key material in parameters
    assert llm_call["credentials"].llm_credential_id.startswith("cred-")

    # progress states in PRD §3.2 order
    states = [s for s, _ in events]
    assert states.index("queued") < states.index("extracting") < states.index("llm") \
        < states.index("validating")


def test_stage1_unknown_revision_fails():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="T")
    with pytest.raises(PipelineError, match="revision"):
        run_stage1_extraction(istari, make_config(istari), rfi.model_id,
                              revision_id="nope", poll_interval_s=0)


def test_stage1_invalid_llm_after_retry_fails():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="T")
    istari.queue_llm_output("garbage")
    istari.queue_llm_output("more garbage")
    with pytest.raises(PipelineError, match="failed validation"):
        run_stage1_extraction(istari, make_config(istari), rfi.model_id,
                              poll_interval_s=0)
    assert len(istari.llm_calls) == 2  # retried exactly once


def test_wait_for_job_failure_raises():
    istari = FakeIstari()
    istari.auto_complete_jobs = False
    rfi = istari.add_model("rfi.pdf", text="T")
    job_id = istari.submit_extraction_job(rfi.model_id)
    istari.fail_job(job_id)
    with pytest.raises(PipelineError, match="failed"):
        wait_for_job(istari, job_id, poll_interval_s=0)


def test_wait_for_job_timeout():
    istari = FakeIstari()
    istari.auto_complete_jobs = False
    rfi = istari.add_model("rfi.pdf", text="T")
    job_id = istari.submit_extraction_job(rfi.model_id)
    with pytest.raises(PipelineError, match="after"):
        wait_for_job(istari, job_id, poll_interval_s=0, timeout_s=-1)


def test_commit_uploads_artifact_with_correct_linkage():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="T")
    requirements = [
        Requirement(id="C-01", label="MOSA", description="d", type="enum",
                    options=["Compliant"]),
    ]

    artifact, info = commit_requirements(
        istari, rfi=rfi, rfi_revision_id=rfi.latest_revision_id,
        requirements=requirements, schema_version="1.0",
        llm_model="claude:claude-opus-5",
    )

    # upload went to the RFI model under the discoverable type-tag name
    [call] = istari.upload_calls
    assert call["model_id"] == rfi.model_id
    assert call["name"] == REQUIREMENTS_ARTIFACT_NAME
    payload = call["payload"]
    assert payload["rfi_uuid"] == rfi.model_id
    assert payload["rfi_revision"] == rfi.latest_revision_id
    assert payload["schema_version"] == "1.0"
    assert payload["llm_model"] == "claude:claude-opus-5"
    # prompts live module-side: the producing function is the version stamp
    assert payload["prompt_version"] == LLM_FUNCTION_EXTRACT_RFI
    assert payload["requirements"][0]["id"] == "C-01"
    assert payload["generated_at"]  # iso8601 timestamp present

    # linked source RFI revision -> artifact revision
    assert istari.link_calls == [(rfi.latest_revision_id, info.revision_id)]

    # discoverable for rebuild (PRD §3.6c)
    found = istari.list_json_artifacts(rfi.model_id, REQUIREMENTS_ARTIFACT_NAME)
    assert found[0][1] == artifact.to_dict()


@pytest.mark.parametrize("current,expected", [
    (None, "1.0"),
    ("1.0", "1.1"),
    ("1.9", "1.10"),
    ("2", "2.1"),
])
def test_next_schema_version(current, expected):
    assert next_schema_version(current) == expected
