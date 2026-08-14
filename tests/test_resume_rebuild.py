"""T6 (completion) — state-machine resume from every intermediate state using
fakes (jobs re-polled not resubmitted; the platform raw-output artifact reused
instead of re-paying the LLM; upload not duplicated), and rebuild-from-UUID
reconstructing an equivalent project."""

from __future__ import annotations

from pathlib import Path

import pytest

from rfi_manager.istari_adapter import LLM_OUTPUT_ARTIFACT
from rfi_manager.models import (
    PipelineState,
    Project,
    RequirementsArtifact,
    ResponseRecord,
)
from rfi_manager.pipeline import (
    ANSWERS_ARTIFACT_NAME,
    fetch_requirements_artifact,
    process_response,
    rebuild_from_platform,
)
from tests.fakes import FakeIstari
from tests.test_stage2 import ANSWERS_JSON, llm_config, make_setup


def start_record(istari, project, response) -> ResponseRecord:
    record = ResponseRecord(uuid=response.model_id,
                            revision=response.latest_revision_id,
                            vendor=response.name)
    project.responses.append(record)
    return record


def finish(istari, project, path, record, req_artifact, outputs=None):
    for raw in outputs if outputs is not None else [ANSWERS_JSON]:
        istari.queue_llm_output(raw)
    return process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0,
    )


def test_resume_from_job_submitted_repolls_same_job(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    # simulate a prior run that crashed right after submitting the extract job
    record.job_id = istari.submit_extraction_job(response.model_id)
    record.transition(PipelineState.JOB_SUBMITTED)
    submitted_before = len(istari.job_submissions)

    result = finish(istari, project, path, record, req_artifact)

    assert result.state is PipelineState.DONE
    assert len(istari.job_submissions) == submitted_before  # re-polled, not resubmitted


def test_resume_from_job_submitted_with_dead_job_restarts_clean(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    record.job_id = "job-that-never-existed"
    record.transition(PipelineState.JOB_SUBMITTED)

    notes: list[str] = []
    istari.queue_llm_output(ANSWERS_JSON)
    result = process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0, log=notes.append, force=True,
    )
    assert result.state is PipelineState.DONE
    assert any("restarting from queued" in n for n in notes)  # FR11 log note
    assert result.job_id != "job-that-never-existed"


def test_resume_from_llm_job_submitted_repolls_same_llm_job(tmp_path: Path):
    """Crash right after submitting the LLM job: resume re-polls the same job
    and never submits another one (§3.6b)."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    istari.queue_llm_output(ANSWERS_JSON)
    record.job_id = "job-x"
    record.llm_job_id = istari.submit_llm_job(
        response.model_id, "@istari_utils:extract_response_requirements", {},
        llm_config(istari).credentials,
    )
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED):
        record.transition(state)
    llm_jobs_before = len(istari.llm_calls)

    result = finish(istari, project, path, record, req_artifact, outputs=[])

    assert result.state is PipelineState.DONE
    assert len(istari.llm_calls) == llm_jobs_before  # re-polled, not resubmitted


def test_resume_from_llm_job_submitted_with_dead_job_restarts(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    record.job_id = "job-x"
    record.llm_job_id = "llmjob-that-never-existed"
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED):
        record.transition(state)

    notes: list[str] = []
    istari.queue_llm_output(ANSWERS_JSON)
    result = process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0, log=notes.append, force=True,
    )
    assert result.state is PipelineState.DONE
    assert any("restarting from queued" in n for n in notes)


def test_resume_from_llm_returned_reuses_platform_artifact(tmp_path: Path):
    """Crash after the LLM job completed: the raw-output artifact on the
    platform is the checkpoint — no new LLM job is submitted (§3.6b)."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    istari._add_artifact(response.model_id, LLM_OUTPUT_ARTIFACT, ANSWERS_JSON)
    record.job_id = "job-x"
    record.llm_job_id = "llmjob-x"
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED):
        record.transition(state)

    result = finish(istari, project, path, record, req_artifact, outputs=[])

    assert result.state is PipelineState.DONE
    assert istari.llm_calls == []  # LLM never re-paid
    assert any(c["name"] == ANSWERS_ARTIFACT_NAME for c in istari.upload_calls)


def test_resume_from_llm_returned_with_missing_artifact_restarts(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    record.job_id = "job-x"
    record.llm_job_id = "llmjob-x"
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED):
        record.transition(state)
    # no llm_output.json artifact exists -> unusable checkpoint evidence

    notes: list[str] = []
    istari.queue_llm_output(ANSWERS_JSON)
    result = process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0, log=notes.append, force=True,
    )
    assert result.state is PipelineState.DONE
    assert any("LLM output artifact unusable" in n for n in notes)


def test_crash_after_first_retry_never_retries_again(tmp_path: Path):
    """llm_attempts is persisted: a crash between the retry job and its
    validation must not grant extra retries (§3.6b)."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    istari._add_artifact(response.model_id, LLM_OUTPUT_ARTIFACT, "still invalid")
    record.job_id = "job-x"
    record.llm_job_id = "llmjob-retry"
    record.llm_attempts = 2  # the one retry already happened before the crash
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED):
        record.transition(state)

    result = finish(istari, project, path, record, req_artifact, outputs=[])

    assert result.state is PipelineState.FAILED
    assert istari.llm_calls == []  # no third attempt


def test_resume_from_validated_uploads_once(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)
    istari._add_artifact(response.model_id, LLM_OUTPUT_ARTIFACT, ANSWERS_JSON)
    record.job_id = "job-x"
    record.llm_job_id = "llmjob-x"
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED,
                  PipelineState.VALIDATED):
        record.transition(state)

    result = finish(istari, project, path, record, req_artifact, outputs=[])
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
         "extracted_at": "t", "llm_model": "m", "answers": []},
    )
    record.answers_artifact_uuid = art.artifact_id
    record.schema_version = "1.0"
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED,
                  PipelineState.VALIDATED, PipelineState.UPLOADED):
        record.transition(state)
    uploads_before = len(istari.upload_calls)

    result = finish(istari, project, path, record, req_artifact, outputs=[])

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
    record2.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED,
                  PipelineState.VALIDATED, PipelineState.UPLOADED):
        record2.transition(state)
    finish(istari, project, path, record2, req_artifact, outputs=[])
    assert istari.link_calls == links_before  # no duplicate edges


def test_persistent_missing_llm_output_fails_instead_of_looping(tmp_path: Path):
    """A module that never writes llm_output.json (e.g. wrong artifact name in
    the deployed manifest) must end FAILED after one clean restart — not loop
    forever resubmitting paid jobs."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    istari.suppress_llm_artifacts = True
    record = start_record(istari, project, response)

    result = process_response(
        istari, llm_config(istari), project, path, record, req_artifact,
        poll_interval_s=0, force=True,
    )

    assert result.state is PipelineState.FAILED
    assert "unusable" in result.error
    # one original run + one clean restart — bounded spend
    assert len(istari.llm_calls) == 2
    assert len(istari.job_submissions) == 2


def test_crash_between_upload_and_persist_adopts_instead_of_reupload(tmp_path: Path):
    """Crash after upload_json_artifact succeeded but before the uploaded
    state was persisted: resume must adopt the existing artifact, not upload
    a duplicate (§3.6b/T6)."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = start_record(istari, project, response)

    # platform state: raw output + answers artifact both exist...
    istari._add_artifact(response.model_id, LLM_OUTPUT_ARTIFACT, ANSWERS_JSON)
    istari.upload_json_artifact(
        response.model_id, ANSWERS_ARTIFACT_NAME,
        {"response_uuid": response.model_id,
         "response_revision": response.latest_revision_id,
         "vendor": response.name, "schema_version": "1.0",
         "extracted_at": "t", "llm_model": "m", "answers": []},
    )
    # ...but the record still says validated (crash before save_project)
    record.job_id = "job-x"
    record.llm_job_id = "llmjob-x"
    record.llm_attempts = 1
    for state in (PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                  PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED,
                  PipelineState.VALIDATED):
        record.transition(state)
    uploads_before = len(istari.upload_calls)

    result = finish(istari, project, path, record, req_artifact, outputs=[])

    assert result.state is PipelineState.DONE
    assert len(istari.upload_calls) == uploads_before  # adopted, not re-uploaded
    assert result.answers_artifact_uuid is not None


# ------------------------------------------------------------------ rebuild

def test_rebuild_from_uuid_reconstructs_equivalent_project(tmp_path: Path):
    istari, project, req_artifact, response_a, path = make_setup(tmp_path)
    response_b = istari.add_model("beta-response.pdf", text="BETA TEXT")

    for response in (response_a, response_b):
        record = ResponseRecord(uuid=response.model_id)
        project.responses.append(record)
        istari.queue_llm_output(ANSWERS_JSON)
        process_response(
            istari, llm_config(istari), project, path, record,
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
        llm_model="m", prompt_version="x",
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


def test_rebuild_keeps_stale_schema_responses(tmp_path: Path):
    """FR3/FR12: responses ingested under an earlier schema survive a rebuild,
    stamped with their actual schema_version so they render stale (FR6)."""
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = ResponseRecord(uuid=response.model_id)
    project.responses.append(record)
    istari.queue_llm_output(ANSWERS_JSON)
    process_response(istari, llm_config(istari), project, path, record,
                     req_artifact, poll_interval_s=0)
    assert record.state is PipelineState.DONE  # ingested under schema 1.0

    # FR3 re-run commits a newer schema
    newer = RequirementsArtifact(
        rfi_uuid=project.rfi_uuid, rfi_revision=project.rfi_revision,
        schema_version="1.1", generated_at="t2", llm_model="m",
        prompt_version="x", requirements=req_artifact.requirements,
    )
    newer_info = istari.upload_json_artifact(
        project.rfi_uuid, "rfi-requirements.json", newer.to_dict()
    )
    istari.create_link(istari.get_model_info(project.rfi_uuid).latest_revision_id,
                       newer_info.revision_id)

    rebuilt, rebuilt_reqs = rebuild_from_platform(istari, project.rfi_uuid)

    assert rebuilt.schema_version == "1.1"  # highest schema chosen
    [recovered] = rebuilt.responses  # the old-schema response survived
    assert recovered.uuid == response.model_id
    assert recovered.schema_version == "1.0"  # stamped stale-able


def test_rebuild_dedups_duplicate_discovery_links(tmp_path: Path):
    istari, project, req_artifact, response, path = make_setup(tmp_path)
    record = ResponseRecord(uuid=response.model_id)
    project.responses.append(record)
    istari.queue_llm_output(ANSWERS_JSON)
    process_response(istari, llm_config(istari), project, path, record,
                     req_artifact, poll_interval_s=0)
    # a second machine raced the link-exists check and made a duplicate edge
    istari.create_link(project.requirements_artifact_revision, record.revision)

    rebuilt, _reqs = rebuild_from_platform(istari, project.rfi_uuid)
    assert len(rebuilt.responses) == 1


def test_fetch_requirements_artifact_roundtrip(tmp_path: Path):
    istari, project, req_artifact, _response, path = make_setup(tmp_path)
    fetched = fetch_requirements_artifact(istari, project)
    assert fetched.to_dict() == req_artifact.to_dict()
