"""Fake adapters for tests (PRD §7). No network, no credentials, ever.

FakeIstari mirrors rfi_manager.istari_adapter.IstariAdapter's public surface
(same method names, same return dataclasses) — test_adapters.py enforces the
match so the fake cannot drift from the real adapter.
"""

from __future__ import annotations

import itertools
from typing import Any

from rfi_manager.istari_adapter import (
    ArtifactInfo,
    IstariError,
    JobState,
    LinkInfo,
    ModelInfo,
)


class FakeLLM:
    """Scripted LLM: returns queued responses in order and records calls."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []  # (system, user)
        self.model = "fake-llm"

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self._responses.pop(0)


class FakeIstari:
    """In-memory Istari platform: models, extraction jobs, artifacts, links.

    Jobs complete on the next ``get_job_state`` poll by default; set
    ``auto_complete_jobs = False`` and call ``complete_job``/``fail_job`` to
    exercise polling paths.
    """

    def __init__(self) -> None:
        self._seq = itertools.count(1)
        self.models: dict[str, ModelInfo] = {}
        self.extracted_text: dict[str, str] = {}  # model_id -> text.txt content
        self.jobs: dict[str, dict[str, Any]] = {}  # job_id -> {model_id, state}
        self.artifacts: dict[str, list[tuple[ArtifactInfo, dict[str, Any]]]] = {}
        self.links: list[LinkInfo] = []
        self.auto_complete_jobs = True
        # call log for assertions (T2: correct linkage/provenance args)
        self.upload_calls: list[dict[str, Any]] = []
        self.link_calls: list[tuple[str, str]] = []

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
        self.extracted_text[model_id] = text
        self.artifacts[model_id] = []
        return info

    def complete_job(self, job_id: str) -> None:
        self.jobs[job_id]["state"] = JobState.COMPLETED

    def fail_job(self, job_id: str) -> None:
        self.jobs[job_id]["state"] = JobState.FAILED

    # ------------------------------------------------- adapter interface

    def get_model_info(self, model_id: str) -> ModelInfo:
        if model_id not in self.models:
            raise IstariError(f"cannot fetch model {model_id}: not found")
        return self.models[model_id]

    def submit_extraction_job(self, model_id: str) -> str:
        if model_id not in self.models:
            raise IstariError(f"cannot submit extraction job for {model_id}: not found")
        job_id = f"job-{next(self._seq)}"
        self.jobs[job_id] = {"model_id": model_id, "state": JobState.RUNNING}
        return job_id

    def get_job_state(self, job_id: str) -> JobState:
        if job_id not in self.jobs:
            raise IstariError(f"cannot fetch job {job_id}: not found")
        job = self.jobs[job_id]
        if job["state"] is JobState.RUNNING and self.auto_complete_jobs:
            job["state"] = JobState.COMPLETED
        return job["state"]

    def get_extracted_text(self, model_id: str) -> str:
        text = self.extracted_text.get(model_id)
        if not text:
            raise IstariError(f"model {model_id} has no text.txt artifact")
        return text

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
        artifact_id = f"artifact-{next(self._seq)}"
        info = ArtifactInfo(
            artifact_id=artifact_id, name=name, revision_id=f"{artifact_id}-rev-1"
        )
        self.artifacts[model_id].append((info, payload))
        self.upload_calls.append(
            {"model_id": model_id, "name": name, "payload": payload,
             "description": description}
        )
        return info

    def list_json_artifacts(
        self, model_id: str, name: str
    ) -> list[tuple[ArtifactInfo, dict[str, Any]]]:
        if model_id not in self.models:
            raise IstariError(f"cannot fetch model {model_id}: not found")
        matching = [(i, p) for i, p in self.artifacts[model_id] if i.name == name]
        return list(reversed(matching))  # newest first, like the real adapter

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
