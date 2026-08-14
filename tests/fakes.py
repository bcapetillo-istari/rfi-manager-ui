"""Fake Istari adapter for tests (PRD §7). No network, no credentials, ever.

FakeIstari mirrors rfi_manager.istari_adapter.IstariAdapter's public surface
(same method names, same return dataclasses) — test_adapters.py enforces the
match so the fake cannot drift from the real adapter.

LLM calls are platform jobs (docs/LLM_Call_Flow.md): script the fake's LLM
outputs with ``queue_llm_output(raw)``; each completing LLM job consumes one
queued output and materializes it as an ``llm_output.json`` artifact on the
job's model — exactly what the real @istari_utils:rfi_manager functions do.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

from rfi_manager.istari_adapter import (
    EXTRACT_TEXT_ARTIFACT,
    LLM_OUTPUT_ARTIFACT,
    ArtifactInfo,
    CredentialInfo,
    CredentialSelection,
    IstariError,
    JobState,
    LinkInfo,
    ModelInfo,
)


class FakeIstari:
    """In-memory Istari platform: models, jobs (extraction + LLM), artifacts,
    links, and stored credentials.

    Jobs complete on the next ``get_job_state`` poll by default; set
    ``auto_complete_jobs = False`` and call ``complete_job``/``fail_job`` to
    exercise polling paths.
    """

    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self.models: dict[str, ModelInfo] = {}
        self.jobs: dict[str, dict[str, Any]] = {}  # id -> {model_id, state, kind}
        self.artifacts: dict[str, list[tuple[ArtifactInfo, Any]]] = {}
        self.links: list[LinkInfo] = []
        self.credentials: list[CredentialInfo] = []
        self.revision_owner: dict[str, str] = {}  # revision_id -> resource id
        self.auto_complete_jobs = True
        self.llm_outputs: list[str] = []  # FIFO consumed by completing LLM jobs
        self.suppress_llm_artifacts = False  # simulate a module writing no output
        self.pending_text: dict[str, str] = {}  # model_id -> text.txt-to-be
        # call logs for assertions (T2/T3: linkage, provenance, contract args)
        self.upload_calls: list[dict[str, Any]] = []
        self.link_calls: list[tuple[str, str]] = []
        self.job_submissions: list[str] = []  # extraction jobs, by model id
        self.llm_calls: list[dict[str, Any]] = []  # submitted LLM jobs

    # ------------------------------------------------------- test helpers

    def add_model(self, name: str, text: str = "", revisions: int = 1) -> ModelInfo:
        model_id = f"model-{next(self._seq)}"
        rev_ids = tuple(f"{model_id}-rev-{i + 1}" for i in range(revisions))
        info = ModelInfo(
            model_id=model_id,
            name=name,
            file_id=f"{model_id}-file",
            latest_revision_id=rev_ids[-1],
            revision_ids=rev_ids,
        )
        self.models[model_id] = info
        self.artifacts[model_id] = []
        for rev_id in rev_ids:
            self.revision_owner[rev_id] = model_id
        if text:
            # like the real platform, text.txt only exists after an
            # extraction job completes (see _complete)
            self.pending_text[model_id] = text
        return info

    def materialize_text(self, model_id: str) -> None:
        """Test shortcut: run an extraction job to completion so text.txt
        exists (for tests that skip the pipeline's extraction step)."""
        job_id = self.submit_extraction_job(model_id)
        self._complete(job_id)

    def add_credential(self, name: str, auth_type: str = "token") -> CredentialInfo:
        cred = CredentialInfo(
            credential_id=f"cred-{next(self._seq)}",
            name=name,
            auth_type=auth_type,
            account_identity=f"{name}@example",
        )
        self.credentials.append(cred)
        return cred

    def default_credentials(self) -> CredentialSelection:
        """One Istari + one LLM credential, created on first use."""
        if not self.credentials:
            self.add_credential("istari-pat", auth_type="istari")
            self.add_credential("llm-key", auth_type="llm")
        return CredentialSelection(
            llm_credential_id=self.credentials[1].credential_id,
            istari_credential_id=self.credentials[0].credential_id,
        )

    def queue_llm_output(self, raw: str) -> None:
        self.llm_outputs.append(raw)

    def complete_job(self, job_id: str) -> None:
        self._complete(job_id)

    def fail_job(self, job_id: str) -> None:
        self.jobs[job_id]["state"] = JobState.FAILED

    def _add_artifact(self, model_id: str, name: str, payload: Any) -> ArtifactInfo:
        artifact_id = f"artifact-{next(self._seq)}"
        info = ArtifactInfo(
            artifact_id=artifact_id, name=name, revision_id=f"{artifact_id}-rev-1"
        )
        self.revision_owner[info.revision_id] = artifact_id
        self.artifacts.setdefault(model_id, []).append((info, payload))
        # An artifact is itself an addressable resource — a job can attach to
        # it (LLM jobs attach to the text.txt artifact, not the parent model)
        # and further artifacts (e.g. llm_output.json) can be uploaded onto it.
        self.artifacts.setdefault(artifact_id, [])
        return info

    def _resource_exists(self, resource_id: str) -> bool:
        """A resource is any model OR artifact id jobs/reads can target."""
        return resource_id in self.artifacts

    def _complete(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job["state"] = JobState.COMPLETED
        if job.get("output_written"):
            return
        if job["kind"] == "extract":
            text = self.pending_text.get(job["model_id"])
            if text is not None:
                self._add_artifact(job["model_id"], EXTRACT_TEXT_ARTIFACT, text)
            job["output_written"] = True
        elif job["kind"] == "llm":
            if self.suppress_llm_artifacts:
                return  # deployed module wrote nothing / wrong artifact name
            if not self.llm_outputs:
                raise AssertionError("FakeIstari ran out of queued LLM outputs")
            raw = self.llm_outputs.pop(0)
            self._add_artifact(job["model_id"], LLM_OUTPUT_ARTIFACT, raw)
            job["output_written"] = True

    # ------------------------------------------------- adapter interface

    def check_connection(self) -> str:
        return "fake-user@example"

    def get_model_info(self, model_id: str) -> ModelInfo:
        if model_id not in self.models:
            raise IstariError(f"cannot fetch model {model_id}: not found")
        return self.models[model_id]

    def model_id_for_revision(self, revision_id: str) -> str:
        if revision_id not in self.revision_owner:
            raise IstariError(f"cannot resolve revision {revision_id}: not found")
        return self.revision_owner[revision_id]

    def submit_extraction_job(self, model_id: str) -> str:
        if model_id not in self.models:
            raise IstariError(f"cannot submit extraction job for {model_id}: not found")
        job_id = f"job-{next(self._seq)}"
        self.jobs[job_id] = {"model_id": model_id, "state": JobState.RUNNING,
                             "kind": "extract"}
        self.job_submissions.append(model_id)
        return job_id

    def submit_llm_job(
        self,
        model_id: str,
        function: str,
        parameters: dict[str, Any],
        credentials: CredentialSelection,
    ) -> str:
        if not self._resource_exists(model_id):
            raise IstariError(f"cannot submit LLM job for {model_id}: not found")
        job_id = f"llmjob-{next(self._seq)}"
        self.jobs[job_id] = {"model_id": model_id, "state": JobState.RUNNING,
                             "kind": "llm"}
        self.llm_calls.append(
            {"job_id": job_id, "model_id": model_id, "function": function,
             "parameters": parameters, "credentials": credentials}
        )
        return job_id

    def list_credentials(self) -> list[CredentialInfo]:
        return list(self.credentials)

    def get_job_state(self, job_id: str) -> JobState:
        if job_id not in self.jobs:
            raise IstariError(f"cannot fetch job {job_id}: not found")
        job = self.jobs[job_id]
        if job["state"] is JobState.RUNNING and self.auto_complete_jobs:
            self._complete(job_id)
        return job["state"]

    def read_text_artifact(self, model_id: str, name: str) -> str:
        if not self._resource_exists(model_id):
            raise IstariError(f"cannot fetch model {model_id}: not found")
        for info, payload in reversed(self.artifacts[model_id]):
            if info.name == name:
                return payload if isinstance(payload, str) else json.dumps(payload)
        raise IstariError(f"model {model_id} has no {name} artifact — did the job complete?")

    def find_artifact(self, model_id: str, name: str) -> ArtifactInfo | None:
        if not self._resource_exists(model_id):
            raise IstariError(f"cannot fetch model {model_id}: not found")
        for info, _payload in reversed(self.artifacts[model_id]):
            if info.name == name:
                return info
        return None

    def get_extracted_text(self, model_id: str) -> str:
        return self.read_text_artifact(model_id, EXTRACT_TEXT_ARTIFACT)

    def upload_json_artifact(
        self,
        model_id: str,
        name: str,
        payload: dict[str, Any],
        *,
        description: str | None = None,
    ) -> ArtifactInfo:
        if model_id not in self.models:
            raise IstariError(f"cannot upload artifact {name}: model not found")
        info = self._add_artifact(model_id, name, payload)
        self.upload_calls.append(
            {"model_id": model_id, "name": name, "payload": payload,
             "description": description}
        )
        return info

    def list_json_artifacts(
        self, model_id: str, name: str
    ) -> list[tuple[ArtifactInfo, Any]]:
        """Parity with the real adapter: returns whatever parses as JSON
        (object, array, ...); unparseable artifacts are skipped, not raised."""
        if model_id not in self.models:
            raise IstariError(f"cannot fetch model {model_id}: not found")
        results: list[tuple[ArtifactInfo, Any]] = []
        for info, payload in reversed(self.artifacts[model_id]):
            if info.name != name:
                continue
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    continue
            results.append((info, payload))
        return results

    def create_link(self, source_revision_id: str, produced_revision_id: str) -> LinkInfo:
        link = LinkInfo(
            link_id=f"link-{next(self._seq)}",
            type_name="produces",
            left_revision_id=source_revision_id,
            right_revision_id=produced_revision_id,
        )
        self.links.append(link)
        self.link_calls.append((source_revision_id, produced_revision_id))
        return link

    def list_links(self, revision_id: str) -> list[LinkInfo]:
        return [
            link for link in self.links
            if revision_id in (link.left_revision_id, link.right_revision_id)
        ]
