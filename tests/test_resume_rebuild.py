"""T6 (completion) — state-machine resume from every intermediate state using
fakes (job re-polled not resubmitted; cached LLM output reused; upload not
duplicated), and rebuild-from-UUID reconstructing an equivalent project."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rfi_manager.models import (
    PipelineState,
    Project,
    Requirement,
    RequirementsArtifact,
    ResponseRecord,
)
from rfi_manager.persistence import cache_llm_output
from rfi_manager.pipeline import (
    ANSWERS_ARTIFACT_NAME,
    fetch_requirements_artifact,
    process_response,
    rebuild_from_platform,
)
from tests.fakes import FakeIstari, FakeLLM
from tests.test_stage2 import ANSWERS_JSON, make_setup


def start_record(istari, project, response) -> ResponseRecord:
    record = ResponseRecord(uuid=response.model_id,
                            revision=response.latest_revision_id,
                            vendor=response.name)
    project.responses.append(record)
    return record


def finish(istari, project, path, record, req_artifact, llm=None):
    return process_response(
        istari, llm or FakeLLM([ANSWERS_JSON]), project, path, record,
        req_artifact, poll_interval_s=0,
    )


def test_resume_from_job_submitted_repolls_same_job(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    # simulate a prior run that crashed right after submitting the job
    record.job_id = istari.submit_extraction_job(response.model_id)
    record.transition(PipelineState.JOB_SUBMITTED)
    submitted_before = len(istari.job_submissions)

    llm = FakeLLM([ANSWERS_JSON])
    result = finish(istari, project, path, record, req_artifact, llm)

    assert result.state is PipelineState.DONE
    assert len(istari.job_submissions) == submitted_before  # re-polled, not resubmitted
    assert len(llm.calls) == 1


def test_resume_from_job_submitted_with_dead_job_restarts_clean(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    record.job_id = "job-that-never-existed"
    record.transition(PipelineState.JOB_SUBMITTED)

    notes: list[str] = []
    result = process_response(
        istari, FakeLLM([ANSWERS_JSON]), project, path, record, req_artifact,
        poll_interval_s=0, log=notes.append, force=True,
    )
    assert result.state is PipelineState.DONE
    assert any("restarting from queued" in n for n in notes)  # FR11 log note
    assert result.job_id != "job-that-never-existed"


def test_resume_from_llm_returned_reuses_cache_never_calls_llm(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    # simulate crash after the LLM returned: cache present, state llm_returned
    record.job_id = "job-x"
    record.transition(PipelineState.JOB_SUBMITTED)
    record.transition(PipelineState.TEXT_RETRIEVED)
    record.llm_cache_path = str(cache_llm_output(path, record.uuid, ANSWERS_JSON))
    record.transition(PipelineState.LLM_RETURNED)

    llm = FakeLLM([])  # any LLM call would blow up
    result = finish(istari, project, path, record, req_artifact, llm)

    assert result.state is PipelineState.DONE
    assert llm.calls == []  # cached output reused — LLM never re-paid (§3.6b)
    assert any(c["name"] == ANSWERS_ARTIFACT_NAME for c in istari.upload_calls)


def test_resume_from_llm_returned_with_missing_cache_restarts(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    record.job_id = "job-x"
    record.transition(PipelineState.JOB_SUBMITTED)
    record.transition(PipelineState.TEXT_RETRIEVED)
    record.llm_cache_path = str(tmp_path / "vanished.txt")
    record.transition(PipelineState.LLM_RETURNED)

    notes: list[str] = []
    result = process_response(
        istari, FakeLLM([ANSWERS_JSON]), project, path, record, req_artifact,
        poll_interval_s=0, log=notes.append, force=True,
    )
    assert result.state is PipelineState.DONE
    assert any("cached LLM output missing" in n for n in notes)


def test_resume_from_validated_uploads_once(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    record.job_id = "job-x"
    record.transition(PipelineState.JOB_SUBMITTED)
    record.transition(PipelineState.TEXT_RETRIEVED)
    record.llm_cache_path = str(cache_llm_output(path, record.uuid, ANSWERS_JSON))
    record.transition(PipelineState.LLM_RETURNED)
    record.transition(PipelineState.VALIDATED)

    llm = FakeLLM([])
    result = finish(istari, project, path, record, req_artifact, llm)
    assert result.state is PipelineState.DONE
    uploads = [c for c in istari.upload_calls if c["name"] == ANSWERS_ARTIFACT_NAME]
    assert len(uploads) == 1


def test_resume_from_uploaded_links_without_reupload(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    # simulate crash after upload but before linking
    art = istari.upload_json_artifact(
        response.model_id, ANSWERS_ARTIFACT_NAME,
        {"response_uuid": response.model_id,
         "response_revision": response.latest_revision_id,
         "vendor": response.name, "schema_version": "1.0",
         "extracted_at": "t", "llm_model": "fake-llm", "answers": []},
    )
    record.answers_artifact_uuid = art.artifact_id
    record.schema_version = "1.0"
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_RETURNED, PipelineState.VALIDATED,
                  PipelineState.UPLOADED):
        record.transition(state)
    uploads_before = len(istari.upload_calls)

    result = finish(istari, project, path, record, req_artifact, FakeLLM([]))

    assert result.state is PipelineState.DONE
    assert len(istari.upload_calls) == uploads_before  # upload not duplicated
    assert (response.latest_revision_id, art.revision_id) in istari.link_calls


def test_resume_is_idempotent_on_links(tmp_path: Path):
    """Running the uploaded step twice must not duplicate links."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    finish(istari, project, path, record, req_artifact)
    links_before = list(istari.link_calls)

    # force a second pass over the uploaded->done step
    record2 = start_record(istari, project, istari.get_model_info(response.model_id))
    record2.answers_artifact_uuid = record.answers_artifact_uuid
    record2.schema_version = "1.0"
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_RETURNED, PipelineState.VALIDATED,
                  PipelineState.UPLOADED):
        record2.transition(state)
    finish(istari, project, path, record2, req_artifact, FakeLLM([]))
    assert istari.link_calls == links_before  # no duplicate edges


# ------------------------------------------------------------------ rebuild

def test_rebuild_from_uuid_reconstructs_equivalent_project(tmp_path: Path):
    istari, project, req_artifact, response_a, path = make_setup(tmp_path)
    response_b = istari.add_model("beta-response.pdf", text="BETA TEXT")

    for response in (response_a, response_b):
        record = ResponseRecord(uuid=response.model_id)
        project.responses.append(record)
        process_response(
            istari, FakeLLM([ANSWERS_JSON]), project, path, record,
            req_artifact, poll_interval_s=0,
        )
        assert record.state is PipelineState.DONE

    notes: list[str] = []
    rebuilt, rebuilt_reqs = rebuild_from_platform(
        istari, project.rfi_uuid, log=notes.append
    )

    assert rebuilt.rfi_uuid == project.rfi_uuid
    assert rebuilt.requirements_artifact_uuid == project.requirements_artifact_uuid
    assert rebuilt.schema_version == project.schema_version
    assert rebuilt_reqs.to_dict() == req_artifact.to_dict()

    by_uuid = {r.uuid: r for r in rebuilt.responses}
    assert set(by_uuid) == {response_a.model_id, response_b.model_id}
    for original in project.responses:
        recovered = by_uuid[original.uuid]
        assert recovered.state is PipelineState.DONE
        assert recovered.answers_artifact_uuid == original.answers_artifact_uuid
        assert recovered.revision == original.revision
        assert recovered.vendor == original.vendor


def test_rebuild_picks_highest_schema_version(tmp_path: Path):
    istari, project, req_artifact, _response, path = make_setup(tmp_path)
    # commit a second, newer schema (FR3 re-run happened)
    newer = RequirementsArtifact(
        rfi_uuid=project.rfi_uuid, rfi_revision=project.rfi_revision,
        schema_version="1.1", generated_at="2026-08-14T12:00:00+00:00",
        llm_model="fake-llm", prompt_version="A1-B1",
        requirements=req_artifact.requirements,
    )
    istari.upload_json_artifact(project.rfi_uuid, "rfi-requirements.json",
                                newer.to_dict())
    notes: list[str] = []
    rebuilt, rebuilt_reqs = rebuild_from_platform(istari, project.rfi_uuid,
                                                  log=notes.append)
    assert rebuilt.schema_version == "1.1"
    assert rebuilt_reqs.schema_version == "1.1"
    assert any("2 requirements artifacts" in n for n in notes)  # user shown choice (FR12)


def test_fetch_requirements_artifact_roundtrip(tmp_path: Path):
    istari, project, req_artifact, _response, path = make_setup(tmp_path)
    fetched = fetch_requirements_artifact(istari, project)
    assert fetched.to_dict() == req_artifact.to_dict()
