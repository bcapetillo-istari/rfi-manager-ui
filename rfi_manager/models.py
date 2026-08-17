"""Data contracts (PRD §4) and the response pipeline state machine (PRD §3.6b).

These shapes are frozen: changing a key name or structure requires updating
the PRD first (see CLAUDE.md architecture rules).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Requirement.type per PRD §4
REQUIREMENT_TYPES = ("boolean", "numeric", "enum", "text")

# Answer.confidence per PRD §4
CONFIDENCE_LEVELS = ("high", "medium", "low", "none")

NOT_FOUND = "NOT_FOUND"


class PipelineState(str, Enum):
    """Explicit per-response state machine (PRD §3.6b).

    queued -> job_submitted -> text_retrieved -> llm_job_submitted
           -> llm_returned -> validated -> uploaded -> done | failed

    llm_returned means the LLM job's raw-output artifact exists on the
    platform — that artifact IS the post-LLM checkpoint (no local cache).
    """

    QUEUED = "queued"
    JOB_SUBMITTED = "job_submitted"
    TEXT_RETRIEVED = "text_retrieved"
    LLM_JOB_SUBMITTED = "llm_job_submitted"
    LLM_RETURNED = "llm_returned"
    VALIDATED = "validated"
    UPLOADED = "uploaded"
    DONE = "done"
    FAILED = "failed"


# States from which a crashed/interrupted response can be resumed (FR11).
RESUMABLE_STATES = frozenset(
    {
        PipelineState.QUEUED,
        PipelineState.JOB_SUBMITTED,
        PipelineState.TEXT_RETRIEVED,
        PipelineState.LLM_JOB_SUBMITTED,
        PipelineState.LLM_RETURNED,
        PipelineState.VALIDATED,
        PipelineState.UPLOADED,
    }
)

# Legal transitions; anything else is a programming error.
_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.QUEUED: frozenset({PipelineState.JOB_SUBMITTED, PipelineState.FAILED}),
    PipelineState.JOB_SUBMITTED: frozenset({PipelineState.TEXT_RETRIEVED, PipelineState.FAILED}),
    PipelineState.TEXT_RETRIEVED: frozenset({PipelineState.LLM_JOB_SUBMITTED, PipelineState.FAILED}),
    PipelineState.LLM_JOB_SUBMITTED: frozenset({PipelineState.LLM_RETURNED, PipelineState.FAILED}),
    # llm_returned -> llm_job_submitted is the retry-once resubmission (§4)
    PipelineState.LLM_RETURNED: frozenset(
        {PipelineState.VALIDATED, PipelineState.LLM_JOB_SUBMITTED, PipelineState.FAILED}
    ),
    PipelineState.VALIDATED: frozenset({PipelineState.UPLOADED, PipelineState.FAILED}),
    PipelineState.UPLOADED: frozenset({PipelineState.DONE, PipelineState.FAILED}),
    PipelineState.DONE: frozenset(),
    # A failed response may be retried from scratch (FR4).
    PipelineState.FAILED: frozenset({PipelineState.QUEUED}),
}


def can_transition(current: PipelineState, new: PipelineState) -> bool:
    """Return True if ``current -> new`` is a legal state-machine edge."""
    return new in _TRANSITIONS[current]


@dataclass
class Requirement:
    """One extracted RFI requirement (PRD §4, Stage 1 output)."""

    id: str
    label: str
    description: str
    type: str  # one of REQUIREMENT_TYPES
    unit: str | None = None  # numeric only
    options: list[str] | None = None  # enum only
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "type": self.type,
            "unit": self.unit,
            "options": self.options,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Requirement:
        return cls(
            id=d["id"],
            label=d["label"],
            description=d["description"],
            type=d["type"],
            unit=d.get("unit"),
            options=d.get("options"),
            required=bool(d.get("required", False)),
        )


@dataclass
class RequirementsArtifact:
    """Requirements JSON as uploaded to Istari — the schema of record (PRD §4)."""

    rfi_uuid: str
    rfi_revision: str
    schema_version: str
    generated_at: str  # iso8601
    llm_model: str
    requirements: list[Requirement]
    prompt_version: str = ""  # FR9: prompt template version stamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "rfi_uuid": self.rfi_uuid,
            "rfi_revision": self.rfi_revision,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "llm_model": self.llm_model,
            "prompt_version": self.prompt_version,
            "requirements": [r.to_dict() for r in self.requirements],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RequirementsArtifact:
        return cls(
            rfi_uuid=d["rfi_uuid"],
            rfi_revision=d["rfi_revision"],
            schema_version=d["schema_version"],
            generated_at=d["generated_at"],
            llm_model=d["llm_model"],
            prompt_version=d.get("prompt_version", ""),
            requirements=[Requirement.from_dict(r) for r in d["requirements"]],
        )


@dataclass
class Answer:
    """One requirement answer extracted from a response PDF (PRD §4, Stage 2)."""

    id: str
    value: Any  # bool | int | float | str | "NOT_FOUND"
    unit: str | None = None
    quote: str = ""  # may be "" when NOT_FOUND
    page: int | None = None
    confidence: str = "none"  # one of CONFIDENCE_LEVELS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "value": self.value,
            "unit": self.unit,
            "quote": self.quote,
            "page": self.page,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Answer:
        return cls(
            id=d["id"],
            value=d["value"],
            unit=d.get("unit"),
            quote=d.get("quote", ""),
            page=d.get("page"),
            confidence=d.get("confidence", "none"),
        )


@dataclass
class AnswersArtifact:
    """Answers JSON as uploaded to Istari, with full provenance (PRD §4)."""

    response_uuid: str
    response_revision: str
    vendor: str
    schema_version: str  # matches the requirements artifact it answers
    extracted_at: str  # iso8601
    llm_model: str
    answers: list[Answer]
    prompt_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_uuid": self.response_uuid,
            "response_revision": self.response_revision,
            "vendor": self.vendor,
            "schema_version": self.schema_version,
            "extracted_at": self.extracted_at,
            "llm_model": self.llm_model,
            "prompt_version": self.prompt_version,
            "answers": [a.to_dict() for a in self.answers],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnswersArtifact:
        return cls(
            response_uuid=d["response_uuid"],
            response_revision=d["response_revision"],
            vendor=d["vendor"],
            schema_version=d["schema_version"],
            extracted_at=d["extracted_at"],
            llm_model=d["llm_model"],
            prompt_version=d.get("prompt_version", ""),
            answers=[Answer.from_dict(a) for a in d["answers"]],
        )


@dataclass
class ResponseRecord:
    """Per-response entry in the project file (PRD §3.6a) — pointers only.

    Table content is never stored here; it is re-fetched from Istari.
    ``llm_input_model_id`` is the standalone Model the extracted text was
    staged as (LLM jobs can only stage a genuine Model's own revision as
    input — verified live 2026-08-14) — needed on resume to re-poll the job
    and read its output from the right resource. ``llm_job_id`` is the
    post-LLM checkpoint evidence: a restart re-polls the job and reads its
    raw-output artifact from the platform (PRD §3.6b — never re-pay for a
    crash between LLM return and upload). ``llm_attempts`` persists the §4
    retry-once counter so a crash cannot cause extra retries.
    """

    uuid: str
    revision: str | None = None
    state: PipelineState = PipelineState.QUEUED
    job_id: str | None = None
    llm_input_model_id: str | None = None
    llm_job_id: str | None = None
    llm_attempts: int = 0
    answers_artifact_uuid: str | None = None
    schema_version: str | None = None
    vendor: str | None = None
    error: str | None = None

    def transition(self, new: PipelineState, *, error: str | None = None) -> None:
        """Move to ``new``, enforcing legal state-machine edges."""
        if not can_transition(self.state, new):
            raise ValueError(f"illegal transition {self.state.value} -> {new.value}")
        self.state = new
        self.error = error if new is PipelineState.FAILED else None
        if new is PipelineState.QUEUED:  # retry from failed: drop stale evidence
            self.job_id = None
            self.llm_input_model_id = None
            self.llm_job_id = None
            self.llm_attempts = 0
            self.answers_artifact_uuid = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "revision": self.revision,
            "state": self.state.value,
            "job_id": self.job_id,
            "llm_input_model_id": self.llm_input_model_id,
            "llm_job_id": self.llm_job_id,
            "llm_attempts": self.llm_attempts,
            "answers_artifact_uuid": self.answers_artifact_uuid,
            "schema_version": self.schema_version,
            "vendor": self.vendor,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResponseRecord:
        return cls(
            uuid=d["uuid"],
            revision=d.get("revision"),
            state=PipelineState(d.get("state", "queued")),
            job_id=d.get("job_id"),
            llm_input_model_id=d.get("llm_input_model_id"),
            llm_job_id=d.get("llm_job_id"),
            llm_attempts=int(d.get("llm_attempts", 0)),
            answers_artifact_uuid=d.get("answers_artifact_uuid"),
            schema_version=d.get("schema_version"),
            vendor=d.get("vendor"),
            error=d.get("error"),
        )


@dataclass
class Project:
    """The ``.rfiproj`` contents (PRD §3.6a): an index/pointer cache only.

    The platform is the source of truth; everything here is recoverable from
    Istari via link traversal (PRD §3.6c).
    """

    FORMAT_VERSION = 1

    rfi_uuid: str
    rfi_revision: str | None = None
    requirements_artifact_uuid: str | None = None
    # extra pointer beyond PRD §3.6a's minimum: the artifact's revision id,
    # needed as the traversal root for rebuild links (§3.6c). Recoverable
    # from the platform, so still cache-only.
    requirements_artifact_revision: str | None = None
    schema_version: str | None = None
    responses: list[ResponseRecord] = field(default_factory=list)

    def response_for(self, uuid: str) -> ResponseRecord | None:
        for r in self.responses:
            if r.uuid == uuid:
                return r
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "rfi_uuid": self.rfi_uuid,
            "rfi_revision": self.rfi_revision,
            "requirements_artifact_uuid": self.requirements_artifact_uuid,
            "requirements_artifact_revision": self.requirements_artifact_revision,
            "schema_version": self.schema_version,
            "responses": [r.to_dict() for r in self.responses],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Project:
        version = d.get("format_version")
        if version != cls.FORMAT_VERSION:
            raise ValueError(f"unsupported project file format_version: {version!r}")
        return cls(
            rfi_uuid=d["rfi_uuid"],
            rfi_revision=d.get("rfi_revision"),
            requirements_artifact_uuid=d.get("requirements_artifact_uuid"),
            requirements_artifact_revision=d.get("requirements_artifact_revision"),
            schema_version=d.get("schema_version"),
            responses=[ResponseRecord.from_dict(r) for r in d.get("responses", [])],
        )
