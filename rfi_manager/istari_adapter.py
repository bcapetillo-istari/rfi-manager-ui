"""Istari platform adapter. ALL Istari SDK calls live here (CLAUDE.md rule).

Written against ``istari-digital-client`` 11.2.0, following the usage patterns
in the internal reference repos (istari-digital-examples, model_diff_ui):
``Configuration``/``Client`` auth, ``add_job`` with the ``@istari:extract`` /
``open_pdf`` function for PDF extraction, ``get_job`` polling, ``model
.artifacts`` + ``read_text``/``read_json`` for artifact content, path-based
``add_artifact`` uploads, and the v3 ``V3Client`` revision-relationship API
for links (PRD §3.6c).

Assumptions noted from SDK introspection (flagged in PROGRESS.md):
- Artifacts cannot be listed per job; we match by artifact name and iterate
  ``reversed(model.artifacts)`` for the most recent (known SDK gap).
- Links are between *revisions*, not models: we link artifact revisions to
  source file revisions via ``create_revision_relationship``.
- The relationship type id is resolved by name from
  ``list_revision_relationship_types`` at first use.

The pipeline and UI depend only on this module's small dataclasses and
methods — never on SDK types.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config import IstariConfig


class IstariError(Exception):
    """Raised when a platform operation fails. Self-redacting: wrapped SDK
    exception text can carry a credential, so scrub it in the constructor —
    this message reaches logs, dialogs, and (via ResponseRecord.error) disk."""

    def __init__(self, message: object = "") -> None:
        from .redaction import redact

        super().__init__(redact(str(message)))


class JobState(str, Enum):
    """Adapter-level job status (mapped from SDK JobStatusName)."""

    RUNNING = "running"  # Created/Pending/Claimed/Validating/Running/Uploading
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelInfo:
    """Pointer info for a platform model/file."""

    model_id: str
    name: str
    file_id: str
    latest_revision_id: str
    revision_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactInfo:
    """Pointer info for an uploaded/discovered artifact."""

    artifact_id: str
    name: str
    revision_id: str


@dataclass(frozen=True)
class LinkInfo:
    """One revision-relationship edge (PRD §3.6c traversal)."""

    link_id: str
    type_name: str
    left_revision_id: str
    right_revision_id: str


@dataclass(frozen=True)
class BranchInfo:
    """One branch of an Istari System (docs/SYSTEM_SELECTION.md)."""

    name: str  # SnapshotTag.tag
    snapshot_id: str


# SnapshotRevisionSearchItem.resource_type value for models. NOTE (verified
# live 2026-08-27): the branch-revisions search endpoint returns CAPITALIZED
# values ('Model', 'Artifact'), unlike the v2 ResourceType enum's lowercase
# ('model', 'artifact', ...) — match case-insensitively.
_RESOURCE_TYPE_MODEL = "model"

# Display-name prefix for the standalone Models the pipeline stages as LLM
# job inputs (register_text_model). They are genuine Models and get tracked
# on system branches (verified live 2026-08-27), so the selection pickers
# must screen them out — they are pipeline plumbing, not user files.
STAGED_TEXT_MODEL_PREFIX = "extracted-text-"


@dataclass(frozen=True)
class SystemFileInfo:
    """One file tracked by a system branch, as shown in the selection pickers.

    ``resource_id`` is the model id the whole pipeline runs on;
    ``revision_id`` is the revision pinned by the branch snapshot (stronger
    provenance than "latest" — the FR5 idempotency key cannot drift
    mid-batch)."""

    resource_id: str
    revision_id: str
    name: str
    resource_type: str | None = None

    @property
    def is_model(self) -> bool:
        """True when the tracked resource is a Model — the only resource type
        the pipeline can run on. Case-insensitive: the live branch-listing
        endpoint says 'Model' while the v2 enum says 'model'. ``None`` (field
        unpopulated) counts as a model, fail-open."""
        return (
            self.resource_type is None
            or self.resource_type.lower() == _RESOURCE_TYPE_MODEL
        )

    @property
    def is_selectable(self) -> bool:
        """What the selection pickers show: Models, excluding the pipeline's
        own staged-text plumbing models (verified live: both get tracked on
        system branches alongside the user's actual files)."""
        return self.is_model and not self.name.startswith(STAGED_TEXT_MODEL_PREFIX)


@dataclass(frozen=True)
class CredentialInfo:
    """A Linked Account (stored credential) selectable in the UI."""

    credential_id: str
    name: str
    auth_type: str | None = None
    account_identity: str | None = None


@dataclass(frozen=True)
class CredentialSelection:
    """Credential bound to LLM jobs (by reference, never by value). The
    deployed @istari_utils:rfi_manager functions declare exactly one
    auth_info input, llm_auth — verified live: binding an istari_auth input
    returns 400 "Credential Binding Mismatch" since no such input exists."""

    llm_credential_id: str


# The Istari extraction function produces text.txt among its artifacts
# (verified against the extract-pdf reference notebook).
_EXTRACT_FUNCTION = "@istari:extract"
_EXTRACT_TOOL = "open_pdf"
_EXTRACT_TOOL_VERSION = "1.0.0"
_EXTRACT_OS = "Windows 10"
EXTRACT_TEXT_ARTIFACT = "text.txt"
_EXTRACT_TEXT_ARTIFACT = EXTRACT_TEXT_ARTIFACT  # back-compat alias

# ---------------------------------------------------------------------------
# @istari_utils:rfi_manager module contract — fetched LIVE from the deployed
# function_schema on 2026-08-14 (module_version 1.1.0) via get_function /
# get_function_schema. This is verified ground truth, not a guess:
#
# extract_rfi_requirements inputs:
#   input_model (user_model, @extension:txt/pdf/docx/xlsx) — required
#   provider, model, validation_errors, rfi_uuid, rfi_rev — optional parameters
#   llm_auth (auth_info, @token:llm) — required
# extract_response_requirements inputs: same, plus
#   requirements_json (parameter) — required
#   response_uuid, response_rev instead of rfi_uuid/rfi_rev
# Both outputs: stdout, stderr, working_directory (file/file/directory,
# all uploaded as artifacts). Verified live against a real completed job
# (2026-08-17): cl_module flattens every file it produces into individual
# same-named artifacts on the model — there is no nested "working_directory"
# artifact to browse, and nothing is literally named "stdout"/"stderr".
# cl_module's own wrapper log lands as istari-module-stdout.txt /
# istari-module-stderr.txt; the inner script's own stdout/stderr (just a
# "Done — ..." print) land as stdout.log / stderr.log; the script's actual
# JSON result is a separate flat artifact named by its --output default:
# requirements_raw.json (Stage 1) / answers_raw.json (Stage 2).
#
# input_model MUST be a genuine Model's own revision — NOT an artifact.
# Verified live: attaching a job directly to an artifact resource ID produces
# "Could not load file" in Job Details and the job hangs (add_job's own
# docstring says "Add a new job to a model"; the SDK has a private
# _create_model_job method — jobs are model-scoped by design). Attaching to
# the RFI/response model itself stages that model's own latest revision
# (the PDF), never an artifact hanging off it. The only way to get the
# extracted text staged as input_model is to register it as its own
# standalone Model first (register_text_model), then attach the job there.
# ---------------------------------------------------------------------------
LLM_FUNCTION_EXTRACT_RFI = "@istari_utils:extract_rfi_requirements"
LLM_FUNCTION_EXTRACT_RESPONSE = "@istari_utils:extract_response_requirements"
LLM_TOOL_NAME: str | None = "llm"
LLM_TOOL_VERSION: str | None = "1.0"
LLM_OS: str | None = (
    None  # "Ubuntu 24.04" per the manifest; left unset (untested as an OS enum value)
)
LLM_RFI_OUTPUT_ARTIFACT = "requirements_raw.json"
LLM_RESPONSE_OUTPUT_ARTIFACT = "answers_raw.json"
LLM_STDERR_ARTIFACT = "stderr.log"
# auth_info input name in the module manifest (verified: no istari_auth input
# exists — binding one returns 400 "Credential Binding Mismatch")
LLM_AUTH_INPUT = "llm_auth"

# Relationship type used to link uploaded artifacts to their source revision.
_LINK_TYPE_NAME = "produces"

_RUNNING_STATUSES = {
    "Created",
    "Pending",
    "Claimed",
    "Validating",
    "Running",
    "Uploading",
}


class IstariAdapter:
    """Real adapter over the official Istari Python client."""

    def __init__(self, config: IstariConfig) -> None:
        """Build ``Configuration(registry_url=..., registry_auth_token=...,
        http_request_timeout_secs=..., retry_*=...)`` — request timeout and
        retry counts from config (PRD §3.3) — and instantiate both ``Client``
        (v2 surface) and ``V3Client`` (needed for revision relationships,
        which are not on ``Client``)."""
        from istari_digital_client import Client, Configuration, V3Client

        sdk_config = Configuration(
            registry_url=config.base_url,
            registry_auth_token=config.token,
            http_request_timeout_secs=int(config.request_timeout_s),
            retry_enabled=config.retries > 0,
            retry_max_attempts=max(config.retries, 1),
        )
        self._client = Client(config=sdk_config)
        self._v3 = V3Client(sdk_config)
        self._link_type_id: str | None = None

    # --------------------------------------------------------- connection

    def check_connection(self) -> str:
        """``client.get_current_user()`` -> display name/email. Validates the
        registry URL + PAT the user typed into the connection bar."""
        try:
            user = self._client.get_current_user()
        except Exception as e:
            raise IstariError(f"cannot connect to registry: {e}") from e
        return (
            getattr(user, "display_name", None)
            or getattr(user, "email", None)
            or "connected"
        )

    # ------------------------------------------------------------- models

    def get_model_info(self, model_id: str) -> ModelInfo:
        """``client.get_model(model_id)`` -> pointer info; raises IstariError
        if the model does not exist or has no revisions."""
        try:
            model = self._client.get_model(model_id)
        except Exception as e:  # SDK raises assorted ApiException types
            raise IstariError(f"cannot fetch model {model_id}: {e}") from e
        revisions = model.file.revisions or []
        if not revisions:
            raise IstariError(f"model {model_id} has no revisions")
        return ModelInfo(
            model_id=model_id,
            name=model.display_name or revisions[0].name,
            file_id=model.file.id,
            latest_revision_id=revisions[-1].id,
            revision_ids=tuple(r.id for r in revisions),
        )

    def model_id_for_revision(self, revision_id: str) -> str:
        """``client.get_revision(revision_id)`` -> ``client.get_file(file_id)``
        -> ``.resource_id`` (the owning model id). Pattern from model_diff_ui's
        ``model_id_from_rev_id``; used by rebuild-from-platform traversal
        (PRD §3.6c) to resolve linked response file revisions to models."""
        try:
            revision = self._client.get_revision(revision_id)
            file = self._client.get_file(revision.file_id)
        except Exception as e:
            raise IstariError(f"cannot resolve revision {revision_id}: {e}") from e
        resource_id = getattr(file, "resource_id", None)
        if not resource_id:
            raise IstariError(f"revision {revision_id} has no owning resource")
        return resource_id

    def register_text_model(
        self, text: str, *, display_name: str, source_revision_id: str | None = None
    ) -> ModelInfo:
        """``client.add_model(path=<temp .txt file>, ...)`` — registers
        ``text`` as a standalone Model. LLM jobs stage a Model's own revision
        as their ``input_model``; artifacts and a model's own non-text
        revision (e.g. the source PDF) cannot be staged that way (verified
        live 2026-08-14). ``source_revision_id`` (the extracted-text
        artifact's revision, when known) is recorded as a source for
        provenance."""
        from istari_digital_client import NewSource

        sources = (
            [
                NewSource(
                    revision_id=source_revision_id,
                    relationship_identifier="extracted_text",
                )
            ]
            if source_revision_id
            else None
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extracted_text.txt"
            path.write_text(text, encoding="utf-8")
            try:
                model = self._client.add_model(
                    path=str(path),
                    display_name=display_name,
                    description="Extracted text staged as an LLM function job input.",
                    sources=sources,
                )
            except Exception as e:
                raise IstariError(f"cannot register text model: {e}") from e
        revisions = model.file.revisions or []
        if not revisions:
            raise IstariError("registered text model has no revisions")
        return ModelInfo(
            model_id=model.id,
            name=display_name,
            file_id=model.file.id,
            latest_revision_id=revisions[-1].id,
            revision_ids=tuple(r.id for r in revisions),
        )

    # --------------------------------------------------------------- jobs

    # ------------------------------------------------------------- systems

    def list_system_branches(self, system_id: str) -> list[BranchInfo]:
        """``client.list_tags(system_id=...)`` (paginated) -> BranchInfo per
        SnapshotTag, INCLUDING the baseline (docs/SYSTEM_SELECTION.md). Feeds
        the Stage 1 branch dropdown. ``System.list_branches()`` is NOT used
        because it excludes the baseline tag, and users legitimately keep
        files there. User branches keep API order; the baseline sorts last so
        the dropdown defaults to a user branch when one exists."""
        tags = []
        try:
            page_num = 1
            while True:
                page = self._client.list_tags(system_id=system_id, page=page_num)
                tags.extend(page.items)
                if page_num * page.size >= page.total:
                    break
                page_num += 1
        except Exception as e:
            raise IstariError(f"cannot list branches of system {system_id}: {e}") from e
        ordered = [t for t in tags if not t.is_baseline] + [
            t for t in tags if t.is_baseline
        ]
        return [BranchInfo(name=t.tag, snapshot_id=t.snapshot_id) for t in ordered]

    def list_system_files(
        self, system_id: str, branch_name: str
    ) -> list[SystemFileInfo]:
        """``client.get_system(system_id)`` -> ``System.get_branch(branch_name)``
        -> ``System.list_branch_revisions(branch)`` -> SystemFileInfo per
        tracked file (docs/SYSTEM_SELECTION.md). Internally paginated by the
        SDK — 100+ files is fine. Entries without a resource or revision id
        are skipped (nothing the pipeline could run on)."""
        try:
            system = self._client.get_system(system_id)
            branch = system.get_branch(branch_name)
            revisions = system.list_branch_revisions(branch)
        except Exception as e:
            raise IstariError(
                f"cannot list files of system {system_id} branch {branch_name!r}: {e}"
            ) from e
        files = []
        for item in revisions:
            resource_id = getattr(item, "resource_id", None)
            revision_id = getattr(item, "revision_id", None)
            if not resource_id or not revision_id:
                continue
            files.append(
                SystemFileInfo(
                    resource_id=resource_id,
                    revision_id=revision_id,
                    name=(
                        getattr(item, "display_name", None)
                        or getattr(item, "name", None)
                        or resource_id
                    ),
                    resource_type=getattr(item, "resource_type", None),
                )
            )
        return files

    def read_revision_bytes(self, revision_id: str) -> bytes:
        """``client.get_revision(revision_id).read_bytes()`` — FileRevision
        mixes in the legacy SDK's ``Readable`` capability, so this reads the
        content straight from storage with no presigned-URL round trip.
        Used for the DO_CUSTOM_EXTRACTION path (pdf_extraction.py): the raw
        PDF bytes for local extraction, bypassing Istari's own
        @istari:extract job entirely."""
        try:
            revision = self._client.get_revision(revision_id)
            return revision.read_bytes()
        except Exception as e:
            raise IstariError(f"cannot read revision {revision_id}: {e}") from e

    def submit_extraction_job(self, model_id: str) -> str:
        """``client.add_job(model_id, function="@istari:extract",
        tool_name="open_pdf", ...)`` -> job id. Runs Istari's PDF
        data-extraction function on the model's latest revision."""
        try:
            job = self._client.add_job(
                model_id,
                function=_EXTRACT_FUNCTION,
                tool_name=_EXTRACT_TOOL,
                tool_version=_EXTRACT_TOOL_VERSION,
                operating_system=_EXTRACT_OS,
                parameters={},
            )
        except Exception as e:
            raise IstariError(
                f"cannot submit extraction job for {model_id}: {e}"
            ) from e
        return job.id

    def submit_llm_job(
        self,
        model_id: str,
        function: str,
        parameters: dict[str, Any],
        credentials: CredentialSelection,
    ) -> str:
        """``client.add_job(model_id, function, parameters=...,
        auth_bindings=[NewCredentialBinding(...)])`` -> job id.

        Submits one of the @istari_utils:rfi_manager LLM functions. The
        selected Linked Account is bound by credential id — no key material
        ever enters parameters (PRD §3.3/§3.4). The deployed functions
        declare exactly one auth_info input, llm_auth.

        Every input in the deployed function_schema is declared
        ``@mime_type:plain/text`` — the module's own scripts json.loads()
        their own parameters (verified live 2026-08-17: passing
        requirements_json as a real list, not a JSON string, crashed the
        agent's env-var setup with "str expected, not list"). JSON-encode
        any non-string value here so callers can pass native Python values.
        """
        from istari_digital_client import NewCredentialBinding

        parameters = {
            k: (v if isinstance(v, str) else json.dumps(v))
            for k, v in parameters.items()
        }
        auth_bindings = [
            NewCredentialBinding(
                input_name=LLM_AUTH_INPUT,
                credential_id=credentials.llm_credential_id,
            ),
        ]
        try:
            job = self._client.add_job(
                model_id,
                function=function,
                tool_name=LLM_TOOL_NAME,
                tool_version=LLM_TOOL_VERSION,
                operating_system=LLM_OS,
                parameters=parameters,
                auth_bindings=auth_bindings,
            )
        except Exception as e:
            raise IstariError(
                f"cannot submit LLM job ({function}) for {model_id}: {e}"
            ) from e
        return job.id

    def list_credentials(self) -> list[CredentialInfo]:
        """``client.list_credentials()`` -> Linked Accounts the user can bind
        to LLM jobs (feeds the UI credential pickers)."""
        try:
            stored = self._client.list_credentials()
        except Exception as e:
            raise IstariError(f"cannot list credentials: {e}") from e
        return [
            CredentialInfo(
                credential_id=c.id,
                name=c.name or c.id,
                auth_type=getattr(c, "auth_type", None),
                account_identity=getattr(c, "account_identity", None),
            )
            for c in stored or []
        ]

    def get_job_state(self, job_id: str) -> JobState:
        """``client.get_job(job_id)`` -> mapped JobState. Restart-safe: the
        pipeline re-polls a persisted job id instead of resubmitting (§3.6b).
        A job id the platform no longer knows raises IstariError, which the
        resume logic treats as unusable checkpoint evidence (FR11)."""
        try:
            job = self._client.get_job(job_id)
        except Exception as e:
            raise IstariError(f"cannot fetch job {job_id}: {e}") from e
        status = job.status.name.value  # e.g. "Running"
        if status in _RUNNING_STATUSES:
            return JobState.RUNNING
        if status == "Completed":
            return JobState.COMPLETED
        if status == "Failed":
            return JobState.FAILED
        if status == "Canceled":
            return JobState.CANCELED
        return JobState.UNKNOWN

    # ---------------------------------------------------------- artifacts

    def _iter_artifacts(self, model_id: str):
        try:
            model = self._client.get_model(model_id)
        except Exception as e:
            raise IstariError(f"cannot fetch model {model_id}: {e}") from e
        return model.artifacts or []

    def read_text_artifact(self, model_id: str, name: str) -> str:
        """Read the newest artifact named ``name`` via ``artifact.read_text()``.
        Iterates ``reversed(model.artifacts)`` so re-runs yield the most
        recent output (artifacts cannot be listed per job — known SDK gap)."""
        for artifact in reversed(list(self._iter_artifacts(model_id))):
            if _artifact_name(artifact) == name:
                try:
                    return artifact.read_text()
                except Exception as e:
                    raise IstariError(f"cannot read {name}: {e}") from e
        raise IstariError(
            f"model {model_id} has no {name} artifact — did the job complete?"
        )

    def find_artifact(self, model_id: str, name: str) -> ArtifactInfo | None:
        """Newest artifact named ``name`` as pointer info (or None). Used to
        locate the extracted-text revision an LLM job should reference."""
        for artifact in reversed(list(self._iter_artifacts(model_id))):
            if _artifact_name(artifact) != name:
                continue
            revisions = artifact.file.revisions or []
            if revisions:
                return ArtifactInfo(
                    artifact_id=artifact.id, name=name, revision_id=revisions[-1].id
                )
        return None

    def get_extracted_text(self, model_id: str) -> str:
        """The extraction function's ``text.txt`` content (newest)."""
        return self.read_text_artifact(model_id, EXTRACT_TEXT_ARTIFACT)

    def upload_json_artifact(
        self,
        model_id: str,
        name: str,
        payload: dict[str, Any],
        *,
        description: str | None = None,
    ) -> ArtifactInfo:
        """``client.add_artifact(model_id, path, ...)`` — the SDK is
        path-based only, so the JSON is serialized to a temp file named
        ``name`` first. ``name`` must carry the discoverability type tag
        (\"rfi-requirements\"/\"rfi-answers\", PRD §3.6c)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            try:
                artifact = self._client.add_artifact(
                    model_id,
                    path=str(path),
                    display_name=name,
                    description=description,
                )
            except Exception as e:
                raise IstariError(f"cannot upload artifact {name}: {e}") from e
        revisions = artifact.file.revisions or []
        if not revisions:
            raise IstariError(f"uploaded artifact {name} has no revisions")
        return ArtifactInfo(
            artifact_id=artifact.id, name=name, revision_id=revisions[-1].id
        )

    def upload_text_artifact(
        self,
        model_id: str,
        name: str,
        text: str,
        *,
        source_revision_id: str | None = None,
        description: str | None = None,
    ) -> ArtifactInfo:
        """``client.add_artifact(model_id, path=<temp file>, sources=[...])``
        — like ``upload_json_artifact`` but writes ``text`` verbatim (no JSON
        encoding). Used to record locally-extracted text (pdf_extraction.py,
        DO_CUSTOM_EXTRACTION) as a ``text.txt`` artifact on the source model
        — the same artifact name/shape Istari's own @istari:extract job
        produces — so a model's provenance looks the same regardless of
        which extraction path ran. ``source_revision_id`` (the source PDF's
        revision, when known) is recorded as a source for provenance."""
        from istari_digital_client import NewSource

        sources = (
            [
                NewSource(
                    revision_id=source_revision_id,
                    relationship_identifier="extracted_from",
                )
            ]
            if source_revision_id
            else None
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(text, encoding="utf-8")
            try:
                artifact = self._client.add_artifact(
                    model_id,
                    path=str(path),
                    sources=sources,
                    display_name=name,
                    description=description,
                )
            except Exception as e:
                raise IstariError(f"cannot upload artifact {name}: {e}") from e
        revisions = artifact.file.revisions or []
        if not revisions:
            raise IstariError(f"uploaded artifact {name} has no revisions")
        return ArtifactInfo(
            artifact_id=artifact.id, name=name, revision_id=revisions[-1].id
        )

    def list_json_artifacts(
        self, model_id: str, name: str
    ) -> list[tuple[ArtifactInfo, dict[str, Any]]]:
        """All artifacts on ``model_id`` whose name matches ``name``, newest
        first, with parsed JSON content (``artifact.read_json()``). Used by
        rebuild-from-platform (FR12) and idempotency checks (FR5)."""
        results: list[tuple[ArtifactInfo, dict[str, Any]]] = []
        for artifact in reversed(list(self._iter_artifacts(model_id))):
            if _artifact_name(artifact) != name:
                continue
            revisions = artifact.file.revisions or []
            if not revisions:
                continue
            try:
                payload = artifact.read_json()
            except Exception:
                continue  # unreadable/non-JSON artifact: skip, not fatal
            results.append(
                (
                    ArtifactInfo(
                        artifact_id=artifact.id, name=name, revision_id=revisions[-1].id
                    ),
                    payload,
                )
            )
        return results

    # -------------------------------------------------------------- links

    def _resolve_link_type_id(self) -> str:
        """``v3.list_revision_relationship_types()`` -> id of the
        \"produces\" relationship type (cached). Raises IstariError if the
        platform defines no such type."""
        if self._link_type_id is not None:
            return self._link_type_id
        try:
            page = self._v3.list_revision_relationship_types()
        except Exception as e:
            raise IstariError(f"cannot list relationship types: {e}") from e
        for t in page.items or []:
            if t.name == _LINK_TYPE_NAME:
                self._link_type_id = t.id
                return t.id
        available = [t.name for t in page.items or []]
        raise IstariError(
            f"platform defines no '{_LINK_TYPE_NAME}' relationship type "
            f"(available: {available})"
        )

    def create_link(
        self, source_revision_id: str, produced_revision_id: str
    ) -> LinkInfo:
        """``v3.create_revision_relationship(NewRevisionRelationshipDto)``:
        left = the source revision (RFI/response file revision), right = the
        produced revision (our uploaded artifact revision).

        The client's retry policy (``retries`` in config) resends timed-out
        requests without regard to idempotency — verified live 2026-08-17: a
        create-link call can succeed server-side while the client sees a
        read timeout, retry, and get back an "Integrity Conflict" 500 for
        the duplicate. Treat that specific conflict as success by looking up
        the edge the first attempt already created, instead of failing a
        commit whose data is actually fine on the platform."""
        from istari_digital_client import ApiException
        from istari_digital_client.legacy.v3.models.new_revision_relationship_dto import (
            NewRevisionRelationshipDto,
        )

        dto = NewRevisionRelationshipDto(
            relationship_type_id=self._resolve_link_type_id(),
            left_revision_id=source_revision_id,
            right_revision_id=produced_revision_id,
        )
        try:
            rel = self._v3.create_revision_relationship(dto)
        except ApiException as e:
            if e.status == 500 and e.body and "Integrity Conflict" in e.body:
                for link in self.list_links(source_revision_id):
                    if (
                        link.left_revision_id == source_revision_id
                        and link.right_revision_id == produced_revision_id
                    ):
                        return link
            raise IstariError(f"cannot create link: {e}") from e
        except Exception as e:
            raise IstariError(f"cannot create link: {e}") from e
        return LinkInfo(
            link_id=rel.id,
            type_name=rel.relationship_type_name,
            left_revision_id=rel.left_revision.file_revision_id,
            right_revision_id=rel.right_revision.file_revision_id,
        )

    def list_links(self, revision_id: str) -> list[LinkInfo]:
        """``v3.list_revision_relationships(revision_id)`` -> all edges
        touching ``revision_id`` (cursor-paginated, size<=100). Used for
        rebuild-from-platform traversal (PRD §3.6c)."""
        links: list[LinkInfo] = []
        cursor = None
        while True:
            try:
                page = self._v3.list_revision_relationships(
                    revision_id, cursor=cursor, size=100
                )
            except Exception as e:
                raise IstariError(f"cannot list links for {revision_id}: {e}") from e
            for rel in page.items or []:
                links.append(
                    LinkInfo(
                        link_id=rel.id,
                        type_name=rel.relationship_type_name,
                        left_revision_id=rel.left_revision.file_revision_id,
                        right_revision_id=rel.right_revision.file_revision_id,
                    )
                )
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return links


def _artifact_name(artifact: Any) -> str | None:
    """Artifact display name, defensively (reference repos disagree on the
    access path: ``artifact.name`` vs ``artifact.file.revisions[0].name``)."""
    name = getattr(artifact, "name", None)
    if name:
        return name
    file = getattr(artifact, "file", None)
    if file is not None and file.revisions:
        return file.revisions[0].name
    return None
