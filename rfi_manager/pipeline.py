"""Orchestration and validation (PRD §4, §3.2). Never imports Qt.

All LLM output passes through the validators here before anything is uploaded:
an artifact that failed validation is never uploaded (PRD §4).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .istari_adapter import ArtifactInfo, IstariError, JobState, ModelInfo
from .llm_adapter import LLMError
from .models import (
    CONFIDENCE_LEVELS,
    NOT_FOUND,
    REQUIREMENT_TYPES,
    Answer,
    Requirement,
    RequirementsArtifact,
)
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, prompt_a, with_retry_errors

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove a surrounding markdown code fence, if present (PRD §4)."""
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


@dataclass
class ValidationResult:
    """Outcome of validating one LLM output."""

    items: list[Any] = field(default_factory=list)  # Requirement or Answer
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _parse_json_array(text: str) -> tuple[list[Any] | None, str | None]:
    try:
        data = json.loads(strip_fences(text))
    except json.JSONDecodeError as e:
        return None, f"output is not valid JSON: {e}"
    if not isinstance(data, list):
        return None, f"expected a JSON array, got {type(data).__name__}"
    return data, None


def validate_requirements(raw_llm_output: str) -> ValidationResult:
    """Validate Prompt A output into a list of Requirement (PRD §4, FR2 rules)."""
    result = ValidationResult()
    data, err = _parse_json_array(raw_llm_output)
    if err:
        result.errors.append(err)
        return result

    seen_ids: set[str] = set()
    for i, entry in enumerate(data):
        where = f"requirement[{i}]"
        if not isinstance(entry, dict):
            result.errors.append(f"{where}: not a JSON object")
            continue
        rid = entry.get("id")
        if not isinstance(rid, str) or not rid.strip():
            result.errors.append(f"{where}: missing or empty 'id'")
            continue
        rid = rid.strip()
        where = f"requirement '{rid}'"
        if rid in seen_ids:
            result.errors.append(f"{where}: duplicate id")
            continue
        seen_ids.add(rid)

        rtype = entry.get("type")
        if rtype not in REQUIREMENT_TYPES:
            result.errors.append(f"{where}: invalid type {rtype!r}")
            continue

        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            result.errors.append(f"{where}: missing or empty 'label'")
            continue
        if len(label.split()) > 4:
            result.warnings.append(f"{where}: label longer than 4 words: {label!r}")

        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            result.errors.append(f"{where}: missing or empty 'description'")
            continue

        unit = entry.get("unit")
        if unit is not None and not isinstance(unit, str):
            result.errors.append(f"{where}: 'unit' must be a string or null")
            continue
        options = entry.get("options")
        if options is not None and not (
            isinstance(options, list) and all(isinstance(o, str) for o in options)
        ):
            result.errors.append(f"{where}: 'options' must be an array of strings or null")
            continue

        if rtype == "enum" and not options:
            result.errors.append(f"{where}: enum requirement has no options")
            continue
        if rtype != "enum" and options:
            result.warnings.append(f"{where}: options given for non-enum type; dropped")
            options = None
        if rtype == "numeric" and not unit:
            result.warnings.append(f"{where}: numeric requirement has no unit")
        if rtype != "numeric" and unit:
            result.warnings.append(f"{where}: unit given for non-numeric type; dropped")
            unit = None

        result.items.append(
            Requirement(
                id=rid,
                label=label.strip(),
                description=description.strip(),
                type=rtype,
                unit=unit,
                options=options,
                required=bool(entry.get("required", False)),
            )
        )
    return result


def _coerce_value(entry_value: Any, req: Requirement, where: str, result: ValidationResult) -> Any:
    """Type-check/coerce an answer value against its requirement; None on error."""
    if entry_value == NOT_FOUND:
        return NOT_FOUND
    if req.type == "boolean":
        if isinstance(entry_value, bool):
            return entry_value
        result.errors.append(f"{where}: boolean answer must be true/false, got {entry_value!r}")
        return None
    if req.type == "numeric":
        if isinstance(entry_value, bool):  # bool is an int subclass; reject explicitly
            result.errors.append(f"{where}: numeric answer must be a number, got {entry_value!r}")
            return None
        if isinstance(entry_value, (int, float)):
            return entry_value
        if isinstance(entry_value, str):
            try:
                num = float(entry_value)
            except ValueError:
                result.errors.append(
                    f"{where}: numeric answer does not parse as a number: {entry_value!r}"
                )
                return None
            result.warnings.append(f"{where}: numeric value given as string; coerced")
            return int(num) if num.is_integer() else num
        result.errors.append(f"{where}: numeric answer must be a number, got {entry_value!r}")
        return None
    if req.type == "enum":
        if not isinstance(entry_value, str):
            result.errors.append(f"{where}: enum answer must be a string, got {entry_value!r}")
            return None
        options = req.options or []
        if entry_value in options:
            return entry_value
        for opt in options:  # case-insensitive rescue, normalized to the canonical option
            if opt.lower() == entry_value.lower():
                result.warnings.append(
                    f"{where}: enum value {entry_value!r} matched option {opt!r} case-insensitively"
                )
                return opt
        result.errors.append(f"{where}: enum value {entry_value!r} not in options {options}")
        return None
    # text
    if isinstance(entry_value, str):
        return entry_value
    result.errors.append(f"{where}: text answer must be a string, got {entry_value!r}")
    return None


def validate_answers(raw_llm_output: str, requirements: list[Requirement]) -> ValidationResult:
    """Validate Prompt B output against the committed requirements (PRD §4).

    Every requirement id must be present exactly once; unknown ids are dropped
    with a warning; values must match the requirement's type; NOT_FOUND is
    allowed anywhere and forces confidence "none".
    """
    result = ValidationResult()
    data, err = _parse_json_array(raw_llm_output)
    if err:
        result.errors.append(err)
        return result

    by_id = {r.id: r for r in requirements}
    seen: set[str] = set()

    for i, entry in enumerate(data):
        where = f"answer[{i}]"
        if not isinstance(entry, dict):
            result.errors.append(f"{where}: not a JSON object")
            continue
        aid = entry.get("id")
        if not isinstance(aid, str) or not aid.strip():
            result.errors.append(f"{where}: missing or empty 'id'")
            continue
        aid = aid.strip()
        where = f"answer '{aid}'"
        req = by_id.get(aid)
        if req is None:
            result.warnings.append(f"{where}: unknown requirement id; dropped")
            continue
        if aid in seen:
            result.errors.append(f"{where}: duplicate id")
            continue
        seen.add(aid)

        if "value" not in entry:
            result.errors.append(f"{where}: missing 'value'")
            continue
        value = _coerce_value(entry["value"], req, where, result)
        if value is None:
            continue

        confidence = entry.get("confidence")
        if value == NOT_FOUND:
            if confidence != "none":
                result.warnings.append(
                    f"{where}: NOT_FOUND requires confidence 'none' (got {confidence!r}); normalized"
                )
            confidence = "none"
        elif confidence not in CONFIDENCE_LEVELS:
            result.errors.append(f"{where}: invalid confidence {confidence!r}")
            continue
        elif confidence == "none":
            result.warnings.append(f"{where}: confidence 'none' on a found value")

        quote = entry.get("quote", "")
        if not isinstance(quote, str):
            result.warnings.append(f"{where}: non-string quote; dropped")
            quote = ""

        page = entry.get("page")
        if page is not None and not isinstance(page, int):
            result.warnings.append(f"{where}: non-integer page {page!r}; dropped")
            page = None

        unit = entry.get("unit")
        if unit is not None and not isinstance(unit, str):
            unit = None
        if req.type == "numeric" and value != NOT_FOUND and unit is None:
            unit = req.unit  # answers inherit the requirement's unit

        result.items.append(
            Answer(id=aid, value=value, unit=unit, quote=quote, page=page, confidence=confidence)
        )

    missing = [rid for rid in by_id if rid not in seen]
    for rid in missing:
        result.errors.append(f"answer '{rid}': missing (every requirement id must be present)")
    return result


class LLMClient(Protocol):
    """The llm_adapter interface (PRD §3.4)."""

    def complete(self, system: str, user: str) -> str: ...


def call_llm_validated(
    llm: LLMClient,
    user_prompt: str,
    validator: Callable[[str], ValidationResult],
    *,
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[ValidationResult, str]:
    """Call the LLM and validate; on failure, retry ONCE with the error list
    appended to the prompt (PRD §4). Returns (result, raw_llm_output).

    The returned raw output is what callers checkpoint to the LLM scratch
    cache (PRD §3.6b) — it corresponds to the returned ValidationResult.
    """
    raw = llm.complete(system_prompt, user_prompt)
    result = validator(raw)
    if result.ok:
        return result, raw
    retry_prompt = with_retry_errors(user_prompt, result.errors)
    raw2 = llm.complete(system_prompt, retry_prompt)
    result2 = validator(raw2)
    return result2, raw2


# --------------------------------------------------------------------------
# Orchestration (Stage 1). Progress states per PRD §3.2:
# queued -> extracting -> llm -> validating -> uploading -> done | failed
# --------------------------------------------------------------------------

ProgressCallback = Callable[[str, str], None]  # (state, detail)


class PipelineError(Exception):
    """A stage failed with a user-actionable reason (FR10)."""


class IstariClient(Protocol):
    """The istari_adapter interface the pipeline depends on."""

    def get_model_info(self, model_id: str) -> ModelInfo: ...
    def submit_extraction_job(self, model_id: str) -> str: ...
    def get_job_state(self, job_id: str) -> JobState: ...
    def get_extracted_text(self, model_id: str) -> str: ...
    def upload_json_artifact(
        self, model_id: str, name: str, payload: dict[str, Any],
        *, description: str | None = None,
    ) -> ArtifactInfo: ...
    def list_json_artifacts(
        self, model_id: str, name: str
    ) -> list[tuple[ArtifactInfo, dict[str, Any]]]: ...
    def create_link(self, source_revision_id: str, produced_revision_id: str): ...
    def list_links(self, revision_id: str): ...


REQUIREMENTS_ARTIFACT_NAME = "rfi-requirements.json"
ANSWERS_ARTIFACT_NAME = "rfi-answers.json"


def _notify(progress: ProgressCallback | None, state: str, detail: str) -> None:
    if progress is not None:
        progress(state, detail)


def wait_for_job(
    istari: IstariClient,
    job_id: str,
    *,
    poll_interval_s: float = 3.0,
    timeout_s: float = 900.0,
    progress: ProgressCallback | None = None,
) -> None:
    """Poll ``get_job_state(job_id)`` until terminal; raises PipelineError on
    failure/cancel/timeout. Restart-safe: callers persist the job id before
    calling this, so a crashed run re-polls instead of resubmitting (§3.6b)."""
    start = time.monotonic()
    while True:
        state = istari.get_job_state(job_id)
        if state is JobState.COMPLETED:
            return
        if state in (JobState.FAILED, JobState.CANCELED):
            raise PipelineError(f"extraction job {job_id} ended as {state.value}")
        elapsed = time.monotonic() - start
        if elapsed > timeout_s:
            raise PipelineError(
                f"extraction job {job_id} still {state.value} after {int(elapsed)}s"
            )
        _notify(progress, "extracting", f"job {job_id}: {state.value}")
        time.sleep(poll_interval_s)


@dataclass
class Stage1Result:
    """Extraction output handed to the review screen (FR1/FR2)."""

    rfi: ModelInfo
    rfi_revision_id: str
    requirements: list[Requirement]
    warnings: list[str]
    raw_llm_output: str
    llm_model: str


def run_stage1_extraction(
    istari: IstariClient,
    llm: LLMClient,
    rfi_uuid: str,
    *,
    revision_id: str | None = None,
    poll_interval_s: float = 3.0,
    job_timeout_s: float = 900.0,
    progress: ProgressCallback | None = None,
) -> Stage1Result:
    """Stage 1 up to (not including) commit: run Istari's PDF extraction on
    the RFI, send text + Prompt A to the LLM, validate into Requirements.
    Raises PipelineError with the reason on any failure (FR10)."""
    _notify(progress, "queued", f"fetching RFI {rfi_uuid}")
    rfi = istari.get_model_info(rfi_uuid)
    if revision_id is not None and revision_id not in rfi.revision_ids:
        raise PipelineError(f"revision {revision_id} not found on RFI {rfi_uuid}")
    rfi_revision_id = revision_id or rfi.latest_revision_id

    _notify(progress, "extracting", "submitting extraction job")
    job_id = istari.submit_extraction_job(rfi_uuid)
    wait_for_job(
        istari, job_id,
        poll_interval_s=poll_interval_s, timeout_s=job_timeout_s, progress=progress,
    )
    text = istari.get_extracted_text(rfi_uuid)

    _notify(progress, "llm", "extracting requirements with LLM")
    result, raw = call_llm_validated(llm, prompt_a(text), validate_requirements)
    _notify(progress, "validating", f"{len(result.items)} requirements")
    if not result.ok:
        raise PipelineError(
            "LLM requirements failed validation after retry:\n"
            + "\n".join(result.errors)
        )
    return Stage1Result(
        rfi=rfi,
        rfi_revision_id=rfi_revision_id,
        requirements=result.items,
        warnings=result.warnings,
        raw_llm_output=raw,
        llm_model=getattr(llm, "model", "unknown"),
    )


def commit_requirements(
    istari: IstariClient,
    *,
    rfi: ModelInfo,
    rfi_revision_id: str,
    requirements: list[Requirement],
    schema_version: str,
    llm_model: str,
    progress: ProgressCallback | None = None,
) -> tuple[RequirementsArtifact, ArtifactInfo]:
    """Commit reviewed requirements (FR2): upload the requirements artifact to
    the RFI model and link it to the source RFI revision. Never called with
    invalid requirements — the review screen re-validates before commit."""
    artifact = RequirementsArtifact(
        rfi_uuid=rfi.model_id,
        rfi_revision=rfi_revision_id,
        schema_version=schema_version,
        generated_at=datetime.now(timezone.utc).isoformat(),
        llm_model=llm_model,
        prompt_version=PROMPT_VERSION,
        requirements=requirements,
    )
    _notify(progress, "uploading", REQUIREMENTS_ARTIFACT_NAME)
    try:
        info = istari.upload_json_artifact(
            rfi.model_id,
            REQUIREMENTS_ARTIFACT_NAME,
            artifact.to_dict(),
            description=f"RFI requirements schema v{schema_version}",
        )
        istari.create_link(rfi_revision_id, info.revision_id)
    except IstariError as e:
        raise PipelineError(str(e)) from e
    _notify(progress, "done", f"requirements artifact {info.artifact_id}")
    return artifact, info


def next_schema_version(current: str | None) -> str:
    """Bumped schema_version for FR3 re-runs: '1.0' -> '1.1'; fallback '1.0'."""
    if not current:
        return "1.0"
    head, _, tail = current.rpartition(".")
    if head and tail.isdigit():
        return f"{head}.{int(tail) + 1}"
    return f"{current}.1"


# --------------------------------------------------------------------------
# Stage 2: per-response pipeline (explicit state machine, PRD §3.6b).
# The project file is saved atomically at EVERY transition.
# --------------------------------------------------------------------------

from pathlib import Path  # noqa: E402  (grouped with stage-2 additions)

from .models import (  # noqa: E402
    AnswersArtifact,
    PipelineState,
    Project,
    ResponseRecord,
)
from .persistence import (  # noqa: E402
    cache_llm_output,
    clear_cached_llm_output,
    load_cached_llm_output,
    save_project,
)
from .prompts import prompt_b, prompt_b_version  # noqa: E402

LogCallback = Callable[[str], None]


def _log(log: LogCallback | None, message: str) -> None:
    if log is not None:
        log(message)


def find_existing_answers(
    istari: IstariClient,
    response_uuid: str,
    response_revision: str,
    schema_version: str,
) -> tuple[ArtifactInfo, dict[str, Any]] | None:
    """FR5 idempotency probe: newest answers artifact on the response model
    matching (response revision, schema_version), or None."""
    for info, payload in istari.list_json_artifacts(response_uuid, ANSWERS_ARTIFACT_NAME):
        if (
            payload.get("response_revision") == response_revision
            and payload.get("schema_version") == schema_version
        ):
            return info, payload
    return None


def _find_answers_revision(
    istari: IstariClient, response_uuid: str, artifact_id: str
) -> str | None:
    for info, _payload in istari.list_json_artifacts(response_uuid, ANSWERS_ARTIFACT_NAME):
        if info.artifact_id == artifact_id:
            return info.revision_id
    return None


def _link_exists(istari: IstariClient, left: str, right: str) -> bool:
    return any(
        link.left_revision_id == left and link.right_revision_id == right
        for link in istari.list_links(left)
    )


def _restart_from_queued(
    record: ResponseRecord,
    project: Project,
    project_path: Path | str,
    reason: str,
    log: LogCallback | None,
) -> None:
    """FR11: checkpoint evidence unusable -> clean restart with a log note."""
    _log(log, f"response {record.uuid}: {reason} — restarting from queued")
    record.transition(PipelineState.FAILED, error=reason)
    record.transition(PipelineState.QUEUED)
    save_project(project, project_path)


def process_response(
    istari: IstariClient,
    llm: LLMClient,
    project: Project,
    project_path: Path | str,
    record: ResponseRecord,
    requirements_artifact: RequirementsArtifact,
    *,
    force: bool = False,
    poll_interval_s: float = 3.0,
    job_timeout_s: float = 900.0,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> ResponseRecord:
    """Drive one response through the state machine to done|failed (FR4/FR5,
    §3.6b). Works identically for fresh runs and resumes (FR11): each step
    continues from the record's persisted state and checkpoints evidence.

    ``record`` must already be in ``project.responses``. Raises nothing on
    pipeline failures — the record ends FAILED with ``record.error`` set;
    only programming errors propagate.
    """
    schema_version = requirements_artifact.schema_version
    requirements = requirements_artifact.requirements

    # in-memory carry between steps within this call (never persisted)
    validated: list[Answer] | None = None

    while record.state not in (PipelineState.DONE, PipelineState.FAILED):
        try:
            if record.state is PipelineState.QUEUED:
                _notify(progress, "queued", record.uuid)
                info = istari.get_model_info(record.uuid)
                if record.revision is None:
                    record.revision = info.latest_revision_id
                if record.vendor is None:
                    record.vendor = info.name
                if not force:
                    existing = find_existing_answers(
                        istari, record.uuid, record.revision, schema_version
                    )
                    if existing is not None:
                        info_art, _payload = existing
                        record.answers_artifact_uuid = info_art.artifact_id
                        record.schema_version = schema_version
                        for state in (
                            PipelineState.JOB_SUBMITTED, PipelineState.TEXT_RETRIEVED,
                            PipelineState.LLM_RETURNED, PipelineState.VALIDATED,
                            PipelineState.UPLOADED, PipelineState.DONE,
                        ):
                            record.transition(state)
                        save_project(project, project_path)
                        _log(log, f"response {record.uuid}: existing answers artifact "
                                  f"{info_art.artifact_id} matches — skipped (FR5)")
                        _notify(progress, "done", "loaded existing answers")
                        return record
                record.job_id = istari.submit_extraction_job(record.uuid)
                record.transition(PipelineState.JOB_SUBMITTED)
                save_project(project, project_path)
                _log(log, f"response {record.uuid}: extraction job {record.job_id}")

            elif record.state is PipelineState.JOB_SUBMITTED:
                assert record.job_id is not None
                try:
                    wait_for_job(
                        istari, record.job_id,
                        poll_interval_s=poll_interval_s, timeout_s=job_timeout_s,
                        progress=progress,
                    )
                except IstariError as e:  # job id no longer usable (FR11)
                    _restart_from_queued(record, project, project_path,
                                         f"job {record.job_id} unusable ({e})", log)
                    continue
                record.transition(PipelineState.TEXT_RETRIEVED)
                save_project(project, project_path)

            elif record.state is PipelineState.TEXT_RETRIEVED:
                _notify(progress, "llm", record.uuid)
                text = istari.get_extracted_text(record.uuid)
                result, raw = call_llm_validated(
                    llm, prompt_b(requirements, text),
                    lambda t: validate_answers(t, requirements),
                )
                record.llm_cache_path = str(
                    cache_llm_output(project_path, record.uuid, raw)
                )
                validated = result.items if result.ok else None
                if not result.ok:
                    record.transition(
                        PipelineState.FAILED,
                        error="LLM answers failed validation after retry:\n"
                        + "\n".join(result.errors),
                    )
                    save_project(project, project_path)
                    break
                for warning in result.warnings:
                    _log(log, f"response {record.uuid}: warning: {warning}")
                record.transition(PipelineState.LLM_RETURNED)
                save_project(project, project_path)

            elif record.state is PipelineState.LLM_RETURNED:
                _notify(progress, "validating", record.uuid)
                raw = (
                    load_cached_llm_output(record.llm_cache_path)
                    if record.llm_cache_path else None
                )
                if raw is None:  # cache evidence unusable (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "cached LLM output missing", log)
                    continue
                result = validate_answers(raw, requirements)
                if not result.ok:
                    record.transition(
                        PipelineState.FAILED,
                        error="cached LLM answers failed validation:\n"
                        + "\n".join(result.errors),
                    )
                    save_project(project, project_path)
                    break
                validated = result.items
                record.transition(PipelineState.VALIDATED)
                save_project(project, project_path)

            elif record.state is PipelineState.VALIDATED:
                _notify(progress, "uploading", record.uuid)
                if validated is None:  # resuming: rebuild from the scratch cache
                    raw = (
                        load_cached_llm_output(record.llm_cache_path)
                        if record.llm_cache_path else None
                    )
                    if raw is None:
                        _restart_from_queued(record, project, project_path,
                                             "cached LLM output missing", log)
                        continue
                    result = validate_answers(raw, requirements)
                    if not result.ok:
                        record.transition(
                            PipelineState.FAILED,
                            error="cached LLM answers failed validation:\n"
                            + "\n".join(result.errors),
                        )
                        save_project(project, project_path)
                        break
                    validated = result.items
                artifact = AnswersArtifact(
                    response_uuid=record.uuid,
                    response_revision=record.revision or "",
                    vendor=record.vendor or record.uuid,
                    schema_version=schema_version,
                    extracted_at=datetime.now(timezone.utc).isoformat(),
                    llm_model=getattr(llm, "model", "unknown"),
                    prompt_version=prompt_b_version(schema_version),
                    answers=validated,
                )
                info_art = istari.upload_json_artifact(
                    record.uuid, ANSWERS_ARTIFACT_NAME, artifact.to_dict(),
                    description=f"RFI answers (schema v{schema_version})",
                )
                record.answers_artifact_uuid = info_art.artifact_id
                record.schema_version = schema_version
                record.transition(PipelineState.UPLOADED)
                save_project(project, project_path)
                _log(log, f"response {record.uuid}: answers artifact "
                          f"{info_art.artifact_id} uploaded")

            elif record.state is PipelineState.UPLOADED:
                assert record.answers_artifact_uuid is not None
                answers_rev = _find_answers_revision(
                    istari, record.uuid, record.answers_artifact_uuid
                )
                if answers_rev is None:  # upload evidence unusable (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "uploaded answers artifact not found", log)
                    continue
                # provenance: response file revision -> answers artifact revision
                if record.revision and not _link_exists(istari, record.revision, answers_rev):
                    istari.create_link(record.revision, answers_rev)
                # discovery: requirements artifact revision -> response revision
                # (rebuild-from-platform traversal root, §3.6c)
                req_rev = project.requirements_artifact_revision
                if req_rev and record.revision and not _link_exists(istari, req_rev, record.revision):
                    istari.create_link(req_rev, record.revision)
                record.transition(PipelineState.DONE)
                save_project(project, project_path)
                clear_cached_llm_output(record.llm_cache_path)
                record.llm_cache_path = None
                save_project(project, project_path)
                _notify(progress, "done", record.uuid)
                _log(log, f"response {record.uuid}: done")

        except (IstariError, LLMError, PipelineError) as e:
            record.transition(PipelineState.FAILED, error=str(e))
            save_project(project, project_path)
            _notify(progress, "failed", str(e))
            _log(log, f"response {record.uuid}: FAILED: {e}")
    return record


def retry_response(
    record: ResponseRecord, project: Project, project_path: Path | str
) -> None:
    """FR4 retry action: a failed record goes back to queued (evidence
    cleared by the state machine) and is persisted."""
    record.transition(PipelineState.QUEUED)
    save_project(project, project_path)


def fetch_requirements_artifact(
    istari: IstariClient, project: Project
) -> RequirementsArtifact:
    """Reload the committed schema of record from the platform (startup
    resume, FR11 — table content is never stored locally)."""
    if project.requirements_artifact_uuid is None:
        raise PipelineError("project has no committed requirements artifact")
    for info, payload in istari.list_json_artifacts(
        project.rfi_uuid, REQUIREMENTS_ARTIFACT_NAME
    ):
        if info.artifact_id == project.requirements_artifact_uuid:
            if project.requirements_artifact_revision is None:
                project.requirements_artifact_revision = info.revision_id
            return RequirementsArtifact.from_dict(payload)
    raise PipelineError(
        f"requirements artifact {project.requirements_artifact_uuid} not found "
        f"on RFI {project.rfi_uuid}"
    )


def fetch_answers_artifact(
    istari: IstariClient, record: ResponseRecord
) -> AnswersArtifact:
    """Re-fetch a response's committed answers from the platform (the table
    is always rebuilt from Istari — PRD §3.6a)."""
    if record.answers_artifact_uuid is None:
        raise PipelineError(f"response {record.uuid} has no answers artifact")
    for info, payload in istari.list_json_artifacts(record.uuid, ANSWERS_ARTIFACT_NAME):
        if info.artifact_id == record.answers_artifact_uuid:
            return AnswersArtifact.from_dict(payload)
    raise PipelineError(
        f"answers artifact {record.answers_artifact_uuid} not found on "
        f"response {record.uuid}"
    )


# --------------------------------------------------------------------------
# Comparison rows (FR6/FR7): pure-python assembly shared by the Qt table
# model and the CSV/XLSX/HTML exporters (FR8). Content always comes from
# re-fetched platform artifacts, never from the project file.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonCell:
    value: Any  # bool | number | str | NOT_FOUND; None when id absent
    unit: str | None = None
    quote: str = ""
    page: int | None = None
    confidence: str = "none"

    @property
    def is_not_found(self) -> bool:
        return self.value == NOT_FOUND or self.value is None

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence in ("low", "none")

    def display(self) -> str:
        if self.is_not_found:
            return "—"  # flagged em-dash (FR6)
        if isinstance(self.value, bool):
            return "yes" if self.value else "no"
        return str(self.value)


@dataclass(frozen=True)
class ComparisonRow:
    vendor: str
    response_uuid: str
    response_revision: str | None
    answers_artifact_uuid: str | None
    schema_version: str | None
    stale: bool  # answered against an older schema (FR3/FR6)
    cells: dict[str, ComparisonCell]  # keyed by requirement id

    @property
    def response_uuid_short(self) -> str:
        return self.response_uuid[:8]

    @property
    def has_not_found(self) -> bool:
        return any(c.is_not_found for c in self.cells.values())

    @property
    def has_low_confidence(self) -> bool:
        return any(c.is_low_confidence for c in self.cells.values())


def build_comparison_rows(
    requirements: list[Requirement],
    entries: list[tuple[ResponseRecord, AnswersArtifact]],
    current_schema_version: str | None,
) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    for record, artifact in entries:
        by_id = {a.id: a for a in artifact.answers}
        cells: dict[str, ComparisonCell] = {}
        for req in requirements:
            answer = by_id.get(req.id)
            if answer is None:
                cells[req.id] = ComparisonCell(value=None)
            else:
                cells[req.id] = ComparisonCell(
                    value=answer.value, unit=answer.unit, quote=answer.quote,
                    page=answer.page, confidence=answer.confidence,
                )
        rows.append(
            ComparisonRow(
                vendor=artifact.vendor or record.vendor or record.uuid,
                response_uuid=record.uuid,
                response_revision=record.revision,
                answers_artifact_uuid=record.answers_artifact_uuid,
                schema_version=artifact.schema_version,
                stale=(
                    current_schema_version is not None
                    and artifact.schema_version != current_schema_version
                ),
                cells=cells,
            )
        )
    return rows


def _schema_sort_key(version: str) -> tuple:
    parts = version.split(".")
    return tuple(int(p) if p.isdigit() else -1 for p in parts), version


def rebuild_from_platform(
    istari: IstariClient,
    rfi_uuid: str,
    *,
    log: LogCallback | None = None,
) -> tuple[Project, RequirementsArtifact]:
    """FR12 / §3.6c: reconstruct a project from the platform alone.

    Locate the latest requirements artifact on the RFI model (highest
    schema_version wins; the choice is logged), then traverse links from its
    revision to response file revisions, and match each response's answers
    artifact by (response revision, schema_version).
    """
    istari.get_model_info(rfi_uuid)  # fail fast on a bad UUID
    candidates = istari.list_json_artifacts(rfi_uuid, REQUIREMENTS_ARTIFACT_NAME)
    if not candidates:
        raise PipelineError(f"RFI {rfi_uuid} has no {REQUIREMENTS_ARTIFACT_NAME} artifact")
    if len(candidates) > 1:
        _log(log, f"RFI {rfi_uuid}: {len(candidates)} requirements artifacts found")
    chosen_info, chosen_payload = max(
        candidates, key=lambda c: _schema_sort_key(c[1].get("schema_version", ""))
    )
    requirements_artifact = RequirementsArtifact.from_dict(chosen_payload)
    _log(log, f"chose requirements artifact {chosen_info.artifact_id} "
              f"(schema v{requirements_artifact.schema_version})")

    project = Project(
        rfi_uuid=rfi_uuid,
        rfi_revision=requirements_artifact.rfi_revision,
        requirements_artifact_uuid=chosen_info.artifact_id,
        requirements_artifact_revision=chosen_info.revision_id,
        schema_version=requirements_artifact.schema_version,
    )

    for link in istari.list_links(chosen_info.revision_id):
        if link.left_revision_id != chosen_info.revision_id:
            continue  # the rfi->requirements edge, not a discovery edge
        response_revision = link.right_revision_id
        try:
            response_uuid = istari.model_id_for_revision(response_revision)
        except IstariError as e:
            _log(log, f"skipping linked revision {response_revision}: {e}")
            continue
        existing = find_existing_answers(
            istari, response_uuid, response_revision,
            requirements_artifact.schema_version,
        )
        if existing is None:
            _log(log, f"response {response_uuid}: no matching answers artifact; skipped")
            continue
        info_art, payload = existing
        record = ResponseRecord(
            uuid=response_uuid,
            revision=response_revision,
            state=PipelineState.DONE,
            answers_artifact_uuid=info_art.artifact_id,
            schema_version=payload.get("schema_version"),
            vendor=payload.get("vendor"),
        )
        project.responses.append(record)
        _log(log, f"recovered response {response_uuid} "
                  f"(answers artifact {info_art.artifact_id})")
    return project, requirements_artifact
