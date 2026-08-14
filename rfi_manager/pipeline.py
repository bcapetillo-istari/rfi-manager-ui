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
