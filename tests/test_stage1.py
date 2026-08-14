"""T2 (stage 1) — end-to-end with fakes: extraction produces requirements;
commit uploads the artifact with correct linkage args. Plus FR3 helpers."""

from __future__ import annotations

import json

import pytest

from rfi_manager.models import Requirement
from rfi_manager.pipeline import (
    REQUIREMENTS_ARTIFACT_NAME,
    PipelineError,
    commit_requirements,
    next_schema_version,
    run_stage1_extraction,
    wait_for_job,
)
from tests.fakes import FakeIstari, FakeLLM

REQS_JSON = json.dumps([
    {"id": "C-01", "label": "MOSA", "description": "MOSA compliance", "type": "enum",
     "unit": None, "options": ["Compliant", "Partial"], "required": True},
    {"id": "C-02", "label": "Weight (kg)", "description": "Unit weight", "type": "numeric",
     "unit": "kg", "options": None, "required": False},
])


def test_stage1_extraction_end_to_end():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="RFI DOCUMENT TEXT")
    llm = FakeLLM([REQS_JSON])
    events: list[tuple[str, str]] = []

    result = run_stage1_extraction(
        istari, llm, rfi.model_id, poll_interval_s=0, progress=lambda s, d: events.append((s, d)),
    )

    assert [r.id for r in result.requirements] == ["C-01", "C-02"]
    assert result.rfi_revision_id == rfi.latest_revision_id
    # the RFI text reached Prompt A
    assert "RFI DOCUMENT TEXT" in llm.calls[0][1]
    # progress states in PRD §3.2 order
    states = [s for s, _ in events]
    assert states.index("queued") < states.index("extracting") < states.index("llm") \
        < states.index("validating")


def test_stage1_unknown_revision_fails():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="T")
    with pytest.raises(PipelineError, match="revision"):
        run_stage1_extraction(istari, FakeLLM([]), rfi.model_id,
                              revision_id="nope", poll_interval_s=0)


def test_stage1_invalid_llm_after_retry_fails():
    istari = FakeIstari()
    rfi = istari.add_model("rfi.pdf", text="T")
    llm = FakeLLM(["garbage", "more garbage"])
    with pytest.raises(PipelineError, match="failed validation"):
        run_stage1_extraction(istari, llm, rfi.model_id, poll_interval_s=0)
    assert len(llm.calls) == 2


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
        requirements=requirements, schema_version="1.0", llm_model="fake-llm",
    )

    # upload went to the RFI model under the discoverable type-tag name
    [call] = istari.upload_calls
    assert call["model_id"] == rfi.model_id
    assert call["name"] == REQUIREMENTS_ARTIFACT_NAME
    payload = call["payload"]
    assert payload["rfi_uuid"] == rfi.model_id
    assert payload["rfi_revision"] == rfi.latest_revision_id
    assert payload["schema_version"] == "1.0"
    assert payload["llm_model"] == "fake-llm"
    assert payload["prompt_version"]
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
