"""Adapter conformance: FakeIstari must expose the same public surface as the
real IstariAdapter so tests written against the fake stay valid, and the fake
must behave sanely. No network — the real adapter is only inspected."""

from __future__ import annotations

import inspect

import pytest

from rfi_manager.istari_adapter import (
    LLM_FUNCTION_EXTRACT_RFI,
    LLM_RFI_OUTPUT_ARTIFACT,
    IstariAdapter,
    IstariError,
    JobState,
)
from tests.fakes import FakeIstari

ADAPTER_METHODS = [
    "check_connection",
    "get_model_info",
    "model_id_for_revision",
    "register_text_model",
    "submit_extraction_job",
    "submit_llm_job",
    "list_credentials",
    "get_job_state",
    "get_extracted_text",
    "read_text_artifact",
    "find_artifact",
    "upload_json_artifact",
    "list_json_artifacts",
    "create_link",
    "list_links",
]


@pytest.mark.parametrize("method", ADAPTER_METHODS)
def test_fake_matches_real_adapter_signature(method: str):
    real = inspect.signature(getattr(IstariAdapter, method))
    fake = inspect.signature(getattr(FakeIstari, method))
    assert list(real.parameters) == list(fake.parameters), method


def test_fake_extraction_flow():
    fake = FakeIstari()
    model = fake.add_model("rfi.pdf", text="RFI TEXT")
    job_id = fake.submit_extraction_job(model.model_id)
    assert fake.get_job_state(job_id) is JobState.COMPLETED  # auto-complete
    assert fake.get_extracted_text(model.model_id) == "RFI TEXT"
    assert fake.find_artifact(model.model_id, "text.txt") is not None


def test_fake_llm_job_flow():
    fake = FakeIstari()
    model = fake.add_model("rfi.pdf", text="T")
    creds = fake.default_credentials()
    fake.queue_llm_output('[{"id": "C-01"}]')
    job_id = fake.submit_llm_job(
        model.model_id, LLM_FUNCTION_EXTRACT_RFI, {"provider": "claude"}, creds
    )
    assert fake.get_job_state(job_id) is JobState.COMPLETED
    assert fake.read_text_artifact(model.model_id, LLM_RFI_OUTPUT_ARTIFACT) == '[{"id": "C-01"}]'
    [call] = fake.llm_calls
    assert call["function"] == LLM_FUNCTION_EXTRACT_RFI
    assert call["credentials"] is creds


def test_fake_job_polling_without_autocomplete():
    fake = FakeIstari()
    fake.auto_complete_jobs = False
    model = fake.add_model("resp.pdf", text="X")
    job_id = fake.submit_extraction_job(model.model_id)
    assert fake.get_job_state(job_id) is JobState.RUNNING
    fake.fail_job(job_id)
    assert fake.get_job_state(job_id) is JobState.FAILED


def test_fake_unknown_ids_raise():
    fake = FakeIstari()
    with pytest.raises(IstariError):
        fake.get_model_info("nope")
    with pytest.raises(IstariError):
        fake.get_job_state("nope")


def test_fake_artifact_upload_link_and_discovery():
    fake = FakeIstari()
    rfi = fake.add_model("rfi.pdf", text="T")
    art = fake.upload_json_artifact(
        rfi.model_id, "rfi-requirements.json", {"schema_version": "1.0"}
    )
    fake.create_link(rfi.latest_revision_id, art.revision_id)

    found = fake.list_json_artifacts(rfi.model_id, "rfi-requirements.json")
    assert found[0][1] == {"schema_version": "1.0"}
    links = fake.list_links(rfi.latest_revision_id)
    assert links[0].right_revision_id == art.revision_id
    assert fake.list_links(art.revision_id) == links


def test_fake_newest_artifact_first():
    fake = FakeIstari()
    rfi = fake.add_model("rfi.pdf")
    fake.upload_json_artifact(rfi.model_id, "rfi-requirements.json", {"schema_version": "1.0"})
    fake.upload_json_artifact(rfi.model_id, "rfi-requirements.json", {"schema_version": "1.1"})
    found = fake.list_json_artifacts(rfi.model_id, "rfi-requirements.json")
    assert [p["schema_version"] for _, p in found] == ["1.1", "1.0"]
