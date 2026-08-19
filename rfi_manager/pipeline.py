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

from .istari_adapter import (
    LLM_FUNCTION_EXTRACT_RESPONSE,
    LLM_FUNCTION_EXTRACT_RFI,
    LLM_RESPONSE_OUTPUT_ARTIFACT,
    LLM_RFI_OUTPUT_ARTIFACT,
    ArtifactInfo,
    CredentialSelection,
    IstariError,
    JobState,
    ModelInfo,
)
from .models import (
    CONFIDENCE_LEVELS,
    NOT_FOUND,
    REQUIREMENT_TYPES,
    Answer,
    Requirement,
    RequirementsArtifact,
)
from . import pdf_extraction

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


# --------------------------------------------------------------------------
# Orchestration. LLM calls are Istari Agent jobs (PRD §3.4,
# docs/LLM_Call_Flow.md). Progress states per PRD §3.2:
# queued -> extracting -> llm -> validating -> uploading -> done | failed
# --------------------------------------------------------------------------

ProgressCallback = Callable[[str, str], None]  # (state, detail)


class PipelineError(Exception):
    """A stage failed with a user-actionable reason (FR10)."""


class IstariClient(Protocol):
    """The istari_adapter interface the pipeline depends on."""

    def get_model_info(self, model_id: str) -> ModelInfo: ...
    def model_id_for_revision(self, revision_id: str) -> str: ...
    def read_revision_bytes(self, revision_id: str) -> bytes: ...
    def register_text_model(
        self, text: str, *, display_name: str, source_revision_id: str | None = None,
    ) -> ModelInfo: ...
    def submit_extraction_job(self, model_id: str) -> str: ...
    def submit_llm_job(
        self, model_id: str, function: str, parameters: dict[str, Any],
        credentials: CredentialSelection,
    ) -> str: ...
    def get_job_state(self, job_id: str) -> JobState: ...
    def get_extracted_text(self, model_id: str) -> str: ...
    def read_text_artifact(self, model_id: str, name: str) -> str: ...
    def find_artifact(self, model_id: str, name: str) -> ArtifactInfo | None: ...
    def upload_json_artifact(
        self, model_id: str, name: str, payload: dict[str, Any],
        *, description: str | None = None,
    ) -> ArtifactInfo: ...
    def upload_text_artifact(
        self, model_id: str, name: str, text: str,
        *, source_revision_id: str | None = None, description: str | None = None,
    ) -> ArtifactInfo: ...
    def list_json_artifacts(
        self, model_id: str, name: str
    ) -> list[tuple[ArtifactInfo, dict[str, Any]]]: ...
    def create_link(self, source_revision_id: str, produced_revision_id: str): ...
    def list_links(self, revision_id: str): ...


@dataclass(frozen=True)
class LLMJobConfig:
    """Everything an LLM job needs beyond its stage-specific inputs: the
    Linked Accounts to bind and the provider/model defaults forwarded as
    job parameters (PRD §3.3)."""

    credentials: CredentialSelection
    provider: str | None = None
    model: str | None = None

    @property
    def llm_model_stamp(self) -> str:
        """Value recorded as llm_model in artifact metadata."""
        if self.provider and self.model:
            return f"{self.provider}:{self.model}"
        return self.model or self.provider or "module-default"


def _llm_parameters(
    config: LLMJobConfig,
    *,
    extra: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Job parameters per the @istari_utils:rfi_manager function_schema
    (verified live 2026-08-14 via get_function_schema): provider/model
    defaults, the §4 retry-once validation error list, and caller-supplied
    stage-specific identifying parameters (rfi_uuid/rfi_rev or
    response_uuid/response_rev/requirements_json) via ``extra``. There is no
    parameter for the extracted text itself — it travels as the job's staged
    input_model (see _stage_text_model)."""
    params: dict[str, Any] = {}
    if config.provider:
        params["provider"] = config.provider
    if config.model:
        params["model"] = config.model
    if extra:
        params.update(extra)
    if validation_errors:
        params["validation_errors"] = validation_errors
    return params


def _find_text_artifact(istari: IstariClient, model_id: str) -> ArtifactInfo:
    from .istari_adapter import EXTRACT_TEXT_ARTIFACT

    info = istari.find_artifact(model_id, EXTRACT_TEXT_ARTIFACT)
    if info is None:
        raise PipelineError(
            f"model {model_id} has no {EXTRACT_TEXT_ARTIFACT} artifact — "
            "did the extraction job complete?"
        )
    return info


def _stage_text_model(
    istari: IstariClient, model_id: str, *, display_name: str,
    do_custom_extraction: bool = False,
    revision_id: str | None = None,
    log: "LogCallback | None" = None,
) -> ModelInfo:
    """Register the extracted text as its own standalone Model so an LLM job
    can stage it as ``input_model`` (attaching a job to the RFI/response
    model directly stages that model's own latest revision — its source
    PDF — never an artifact hanging off it; verified live 2026-08-14).

    A prior attempt attaching a job directly to the text.txt ARTIFACT
    produced "Could not load file" in Job Details. A follow-up attempt using
    this standalone-Model approach produced the same symptom with the new
    model showing a 0-byte revision — so the failure is not "artifact vs
    model": either the text we read here was already empty, or the upload
    pipeline drops content even for a genuine model. ``len(text)`` is logged
    so the next live run tells us which.

    ``do_custom_extraction`` (DO_CUSTOM_EXTRACTION) skips Istari's own
    @istari:extract job/text.txt artifact entirely and extracts locally via
    pdfplumber instead (pdf_extraction.py) — ``revision_id`` must then be the
    RFI/response revision to read the source PDF bytes from."""
    if do_custom_extraction:
        from .istari_adapter import EXTRACT_TEXT_ARTIFACT

        if revision_id is None:
            raise PipelineError(
                "custom extraction requires a revision id to read the source PDF from"
            )
        pdf_bytes = istari.read_revision_bytes(revision_id)
        text = pdf_extraction.extract_text(pdf_bytes)
        _log(
            log,
            f"custom extraction (pdfplumber) of revision {revision_id}: "
            f"{len(text)} chars"
            + (f" (preview: {text[:120]!r})" if text else " — EMPTY"),
        )
        # Mirror Istari's own @istari:extract job: record the extracted text
        # as a text.txt artifact on the source model first (provenance
        # parity — a model looks the same in the platform whether extraction
        # ran as a real job or locally), then stage it exactly like that path.
        text_artifact = istari.upload_text_artifact(
            model_id, EXTRACT_TEXT_ARTIFACT, text,
            source_revision_id=revision_id,
            description="Locally-extracted text (pdfplumber, DO_CUSTOM_EXTRACTION).",
        )
        _log(
            log,
            f"uploaded custom-extracted text as artifact {text_artifact.artifact_id} "
            f"({EXTRACT_TEXT_ARTIFACT}) on model {model_id}",
        )
        text_model = istari.register_text_model(
            text, display_name=display_name, source_revision_id=text_artifact.revision_id,
        )
        _log(log, f"staged custom-extracted text as model {text_model.model_id}")
        return text_model

    from .istari_adapter import EXTRACT_TEXT_ARTIFACT

    text_artifact = _find_text_artifact(istari, model_id)
    text = istari.get_extracted_text(model_id)
    _log(
        log,
        f"read {EXTRACT_TEXT_ARTIFACT} artifact {text_artifact.artifact_id}: "
        f"{len(text)} chars"
        + (f" (preview: {text[:120]!r})" if text else " — EMPTY"),
    )
    text_model = istari.register_text_model(
        text, display_name=display_name, source_revision_id=text_artifact.revision_id,
    )
    _log(
        log,
        f"staged extracted text as model {text_model.model_id} "
        f"(from {EXTRACT_TEXT_ARTIFACT} artifact {text_artifact.artifact_id}, "
        f"{len(text)} chars written)",
    )
    return text_model


def _log_llm_submission(
    log: "LogCallback | None",
    *,
    function: str,
    attached_resource: str,
    parameters: dict[str, Any],
    credentials: CredentialSelection,
) -> None:
    """Log exactly what an LLM job submission looks like (requested for
    diagnosing manifest/contract mismatches against the deployed module)."""
    _log(
        log,
        f"LLM job submit: function={function} attached_to={attached_resource} "
        f"credentials(llm={credentials.llm_credential_id}) "
        f"parameters={parameters}",
    )


def _read_llm_output(
    istari: IstariClient,
    model_id: str,
    *,
    output_artifact: str,
    log: "LogCallback | None" = None,
) -> str:
    """Read the LLM job's real result artifact — cl_module flattens every
    file the script writes into individual same-named artifacts (verified
    live 2026-08-17); the script's actual output is named by its own
    --output default (requirements_raw.json / answers_raw.json), not
    "stdout" — from the text-model resource the job ran against."""
    try:
        raw = istari.read_text_artifact(model_id, output_artifact)
    except IstariError as e:
        raise IstariError(
            f"no {output_artifact} artifact on resource {model_id}: {e}"
        ) from e
    _log(log, f"LLM {output_artifact} artifact found on resource {model_id}")
    return raw


def _read_llm_stderr(
    istari: IstariClient, model_id: str, *, log: "LogCallback | None" = None
) -> str | None:
    """Best-effort read of the job's stderr artifact for diagnostics when
    validation fails or no output is found — surfaces the function's actual
    traceback/error instead of leaving the user guessing."""
    from .istari_adapter import LLM_STDERR_ARTIFACT

    try:
        return istari.read_text_artifact(model_id, LLM_STDERR_ARTIFACT)
    except IstariError:
        return None


def run_llm_job_validated(
    istari: IstariClient,
    model_id: str,
    function: str,
    llm_config: LLMJobConfig,
    validator: Callable[[str], ValidationResult],
    *,
    output_artifact: str,
    extra_parameters: dict[str, Any] | None = None,
    do_custom_extraction: bool = False,
    revision_id: str | None = None,
    poll_interval_s: float = 3.0,
    job_timeout_s: float = 900.0,
    progress: ProgressCallback | None = None,
    log: "LogCallback | None" = None,
) -> tuple[ValidationResult, str]:
    """Submit an LLM-function job, poll, read its stdout artifact, and
    validate client-side; on failure retry ONCE by resubmitting with the
    validation_errors parameter (PRD §4). Returns (result, raw_output).

    The extracted text is staged as its own Model (_stage_text_model) once,
    then the job is submitted against that model — reused across the retry
    attempt, since the input text doesn't change, only validation_errors.

    Used by interactive Stage 1; Stage 2 drives the same steps through the
    persisted state machine instead so every step checkpoints.
    """
    text_model = _stage_text_model(
        istari, model_id, display_name=f"extracted-text-{model_id}",
        do_custom_extraction=do_custom_extraction, revision_id=revision_id, log=log,
    )
    errors: list[str] | None = None
    result = ValidationResult()
    raw = ""
    for _attempt in range(2):
        params = _llm_parameters(llm_config, extra=extra_parameters, validation_errors=errors)
        _log_llm_submission(
            log, function=function, attached_resource=text_model.model_id,
            parameters=params, credentials=llm_config.credentials,
        )
        job_id = istari.submit_llm_job(
            text_model.model_id, function, params, llm_config.credentials
        )
        _log(log, f"LLM job submitted: job_id={job_id}")
        _notify(progress, "llm", f"LLM job {job_id} submitted ({function})")
        wait_for_job(
            istari, job_id,
            poll_interval_s=poll_interval_s, timeout_s=job_timeout_s,
            progress=progress, kind="LLM", progress_state="llm",
        )
        try:
            raw = _read_llm_output(
                istari, text_model.model_id, output_artifact=output_artifact, log=log
            )
        except IstariError as e:
            stderr = _read_llm_stderr(istari, text_model.model_id, log=log)
            if stderr:
                _log(log, f"LLM job {job_id} stderr:\n{stderr}")
            raise PipelineError(f"LLM job {job_id} produced no readable output: {e}") from e
        _notify(progress, "validating", f"LLM job {job_id} output")
        result = validator(raw)
        if result.ok:
            return result, raw
        errors = result.errors
        stderr = _read_llm_stderr(istari, text_model.model_id, log=log)
        if stderr:
            _log(log, f"LLM job {job_id} stderr (validation failed):\n{stderr}")
    return result, raw


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
    kind: str = "extraction",
    progress_state: str = "extracting",
) -> None:
    """Poll ``get_job_state(job_id)`` until terminal; raises PipelineError on
    failure/cancel/timeout. Restart-safe: callers persist the job id before
    calling this, so a crashed run re-polls instead of resubmitting (§3.6b).
    ``kind``/``progress_state`` label the messages ("extraction"/"LLM")."""
    start = time.monotonic()
    while True:
        state = istari.get_job_state(job_id)
        if state is JobState.COMPLETED:
            return
        if state in (JobState.FAILED, JobState.CANCELED):
            raise PipelineError(f"{kind} job {job_id} ended as {state.value}")
        elapsed = time.monotonic() - start
        if elapsed > timeout_s:
            raise PipelineError(
                f"{kind} job {job_id} still {state.value} after {int(elapsed)}s"
            )
        _notify(progress, progress_state, f"job {job_id}: {state.value}")
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
    llm_config: LLMJobConfig,
    rfi_uuid: str,
    *,
    revision_id: str | None = None,
    do_custom_extraction: bool = False,
    poll_interval_s: float = 3.0,
    job_timeout_s: float = 900.0,
    progress: ProgressCallback | None = None,
    log: "LogCallback | None" = None,
) -> Stage1Result:
    """Stage 1 up to (not including) commit: extract text from the RFI PDF,
    then the extract_rfi_requirements LLM job on the extracted-text artifact,
    and validate the raw output into Requirements client-side. Raises
    PipelineError with the reason on any failure (FR10).

    ``do_custom_extraction`` (DO_CUSTOM_EXTRACTION) extracts locally via
    pdfplumber instead of submitting Istari's own @istari:extract job."""
    _notify(progress, "queued", f"fetching RFI {rfi_uuid}")
    rfi = istari.get_model_info(rfi_uuid)
    if revision_id is not None and revision_id not in rfi.revision_ids:
        raise PipelineError(f"revision {revision_id} not found on RFI {rfi_uuid}")
    rfi_revision_id = revision_id or rfi.latest_revision_id

    if do_custom_extraction:
        _notify(progress, "extracting", "extracting text locally (pdfplumber)")
        _log(log, f"custom extraction enabled — skipping Istari extraction job for {rfi_uuid}")
    else:
        _notify(progress, "extracting", "submitting extraction job")
        job_id = istari.submit_extraction_job(rfi_uuid)
        _log(log, f"extraction job submitted: job_id={job_id} model_id={rfi_uuid}")
        wait_for_job(
            istari, job_id,
            poll_interval_s=poll_interval_s, timeout_s=job_timeout_s, progress=progress,
        )

    result, raw = run_llm_job_validated(
        istari, rfi_uuid, LLM_FUNCTION_EXTRACT_RFI, llm_config,
        validate_requirements,
        output_artifact=LLM_RFI_OUTPUT_ARTIFACT,
        extra_parameters={"rfi_uuid": rfi_uuid, "rfi_rev": rfi_revision_id},
        do_custom_extraction=do_custom_extraction, revision_id=rfi_revision_id,
        poll_interval_s=poll_interval_s, job_timeout_s=job_timeout_s,
        progress=progress, log=log,
    )
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
        llm_model=llm_config.llm_model_stamp,
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
        # prompts live module-side (FR9): stamp the function that produced them
        prompt_version=LLM_FUNCTION_EXTRACT_RFI,
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
from .persistence import save_project  # noqa: E402


def response_prompt_version(schema_version: str) -> str:
    """prompt_version stamp for answers artifacts: the module function that
    owns Prompt B plus the schema it was generated from (FR9/T3)."""
    return f"{LLM_FUNCTION_EXTRACT_RESPONSE}+schema-{schema_version}"

LogCallback = Callable[[str], None]


def _log(log: LogCallback | None, message: str) -> None:
    if log is not None:
        log(message)


def resolve_response_revisions(
    istari: IstariClient, revision_ids: list[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Resolve each user-entered response revision id to its owning model id
    (Stage 2 now takes a specific file revision, not a model UUID, so a
    response can be ingested against an exact historical revision rather
    than always whatever is currently latest). One bad id must not lose the
    rest of a batch, so failures are collected rather than raised.

    Returns ``(resolved, failed)`` — ``resolved`` as ``(revision_id,
    model_id)`` pairs, ``failed`` as ``(revision_id, reason)`` pairs.
    """
    resolved: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for revision_id in revision_ids:
        try:
            model_id = istari.model_id_for_revision(revision_id)
        except IstariError as e:
            failed.append((revision_id, str(e)))
            continue
        resolved.append((revision_id, model_id))
    return resolved, failed


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
            isinstance(payload, dict)  # a JSON artifact need not be an object
            and payload.get("response_revision") == response_revision
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


# One clean restart per process_response call (FR11 says restart *cleanly*,
# singular). Without a cap, a persistent platform error — e.g. the deployed
# module writing its output under a different artifact name — would loop
# forever, resubmitting paid jobs every lap.
_MAX_RESTARTS = 1


class _RestartBudget:
    def __init__(self) -> None:
        self.used = 0


def _restart_from_queued(
    record: ResponseRecord,
    project: Project,
    project_path: Path | str,
    reason: str,
    log: LogCallback | None,
    budget: _RestartBudget,
) -> None:
    """FR11: checkpoint evidence unusable -> clean restart with a log note.
    A second unusable-evidence event in the same run fails the record instead
    of looping (and re-spending) forever."""
    budget.used += 1
    if budget.used > _MAX_RESTARTS:
        _log(log, f"response {record.uuid}: {reason} — evidence unusable again "
                  "after a clean restart; failing")
        record.transition(
            PipelineState.FAILED,
            error=f"unusable checkpoint evidence after clean restart: {reason}",
        )
        save_project(project, project_path)
        return
    _log(log, f"response {record.uuid}: {reason} — restarting from queued")
    record.transition(PipelineState.FAILED, error=reason)
    record.transition(PipelineState.QUEUED)
    save_project(project, project_path)


def process_response(
    istari: IstariClient,
    llm_config: LLMJobConfig,
    project: Project,
    project_path: Path | str,
    record: ResponseRecord,
    requirements_artifact: RequirementsArtifact,
    *,
    force: bool = False,
    do_custom_extraction: bool = False,
    poll_interval_s: float = 3.0,
    job_timeout_s: float = 900.0,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
) -> ResponseRecord:
    """Drive one response through the state machine to done|failed (FR4/FR5,
    §3.6b). Works identically for fresh runs and resumes (FR11): each step
    continues from the record's persisted state and checkpoints evidence.
    The post-LLM checkpoint is the LLM job's raw-output artifact on the
    platform; the retry-once counter (llm_attempts) is persisted.

    ``do_custom_extraction`` (DO_CUSTOM_EXTRACTION) still passes through
    JOB_SUBMITTED (so the state machine's legal edges are untouched and
    resume behavior is unchanged) but never submits or waits on an Istari
    extraction job — record.job_id stays None and text comes from a local
    pdfplumber pass instead. Resuming a record must use the same flag value
    it started with, the same as any other persisted checkpoint evidence.

    ``record`` must already be in ``project.responses``. Raises nothing on
    pipeline failures — the record ends FAILED with ``record.error`` set;
    only programming errors propagate.
    """
    schema_version = requirements_artifact.schema_version
    requirements = requirements_artifact.requirements
    requirements_json = [r.to_dict() for r in requirements]

    # in-memory carry between steps within this call (never persisted)
    validated: list[Answer] | None = None
    restarts = _RestartBudget()

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
                            PipelineState.LLM_JOB_SUBMITTED, PipelineState.LLM_RETURNED,
                            PipelineState.VALIDATED, PipelineState.UPLOADED,
                            PipelineState.DONE,
                        ):
                            record.transition(state)
                        save_project(project, project_path)
                        _log(log, f"response {record.uuid}: existing answers artifact "
                                  f"{info_art.artifact_id} matches — skipped (FR5)")
                        _notify(progress, "done", "loaded existing answers")
                        return record
                if do_custom_extraction:
                    record.transition(PipelineState.JOB_SUBMITTED)
                    save_project(project, project_path)
                    _log(log, f"response {record.uuid}: custom extraction enabled — "
                              "skipping Istari extraction job")
                else:
                    record.job_id = istari.submit_extraction_job(record.uuid)
                    record.transition(PipelineState.JOB_SUBMITTED)
                    save_project(project, project_path)
                    _log(log, f"response {record.uuid}: extraction job {record.job_id}")

            elif record.state is PipelineState.JOB_SUBMITTED:
                if do_custom_extraction:  # no Istari job was ever submitted
                    record.transition(PipelineState.TEXT_RETRIEVED)
                    save_project(project, project_path)
                    continue
                if record.job_id is None:  # corrupt/hand-edited evidence (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "no extraction job id persisted", log,
                                         restarts)
                    continue
                try:
                    wait_for_job(
                        istari, record.job_id,
                        poll_interval_s=poll_interval_s, timeout_s=job_timeout_s,
                        progress=progress,
                    )
                except IstariError as e:  # job id no longer usable (FR11)
                    _restart_from_queued(record, project, project_path,
                                         f"job {record.job_id} unusable ({e})", log,
                                         restarts)
                    continue
                record.transition(PipelineState.TEXT_RETRIEVED)
                save_project(project, project_path)

            elif record.state is PipelineState.TEXT_RETRIEVED:
                _notify(progress, "llm", record.uuid)
                text_model = _stage_text_model(
                    istari, record.uuid, display_name=f"extracted-text-{record.uuid}",
                    do_custom_extraction=do_custom_extraction, revision_id=record.revision,
                    log=log,
                )
                record.llm_input_model_id = text_model.model_id
                params = _llm_parameters(
                    llm_config,
                    extra={
                        "response_uuid": record.uuid,
                        "response_rev": record.revision,
                        "requirements_json": requirements_json,
                    },
                )
                _log_llm_submission(
                    log, function=LLM_FUNCTION_EXTRACT_RESPONSE,
                    attached_resource=text_model.model_id, parameters=params,
                    credentials=llm_config.credentials,
                )
                record.llm_job_id = istari.submit_llm_job(
                    text_model.model_id, LLM_FUNCTION_EXTRACT_RESPONSE, params,
                    llm_config.credentials,
                )
                record.llm_attempts = 1
                record.transition(PipelineState.LLM_JOB_SUBMITTED)
                save_project(project, project_path)
                _log(log, f"response {record.uuid}: LLM job {record.llm_job_id} "
                          f"(input model {text_model.model_id})")

            elif record.state is PipelineState.LLM_JOB_SUBMITTED:
                if record.llm_job_id is None:  # corrupt evidence (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "no LLM job id persisted", log, restarts)
                    continue
                try:
                    wait_for_job(
                        istari, record.llm_job_id,
                        poll_interval_s=poll_interval_s, timeout_s=job_timeout_s,
                        progress=progress, kind="LLM", progress_state="llm",
                    )
                except IstariError as e:  # LLM job id no longer usable (FR11)
                    _restart_from_queued(record, project, project_path,
                                         f"LLM job {record.llm_job_id} unusable ({e})",
                                         log, restarts)
                    continue
                record.transition(PipelineState.LLM_RETURNED)
                save_project(project, project_path)

            elif record.state is PipelineState.LLM_RETURNED:
                _notify(progress, "validating", record.uuid)
                if record.llm_input_model_id is None:  # corrupt evidence (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "no LLM input model id persisted", log,
                                         restarts)
                    continue
                try:  # the raw-output artifact IS the checkpoint (§3.6b)
                    raw = _read_llm_output(
                        istari, record.llm_input_model_id,
                        output_artifact=LLM_RESPONSE_OUTPUT_ARTIFACT, log=log,
                    )
                except IstariError as e:
                    _restart_from_queued(record, project, project_path,
                                         f"LLM output artifact unusable ({e})", log,
                                         restarts)
                    continue
                result = validate_answers(raw, requirements)
                if not result.ok:
                    stderr = _read_llm_stderr(istari, record.llm_input_model_id, log=log)
                    if stderr:
                        _log(log, f"response {record.uuid}: LLM job stderr:\n{stderr}")
                    if record.llm_attempts < 2:  # retry ONCE (§4), crash-safe
                        _log(log, f"response {record.uuid}: validation failed — "
                                  "resubmitting LLM job with validation_errors")
                        # burn the retry budget BEFORE submitting: a crash in
                        # this window then loses the retry rather than
                        # granting a second one (§3.6b)
                        record.llm_attempts += 1
                        save_project(project, project_path)
                        # reuse the same staged text model — the input hasn't
                        # changed, only validation_errors
                        params = _llm_parameters(
                            llm_config,
                            extra={
                                "response_uuid": record.uuid,
                                "response_rev": record.revision,
                                "requirements_json": requirements_json,
                            },
                            validation_errors=result.errors,
                        )
                        _log_llm_submission(
                            log, function=LLM_FUNCTION_EXTRACT_RESPONSE,
                            attached_resource=record.llm_input_model_id,
                            parameters=params, credentials=llm_config.credentials,
                        )
                        record.llm_job_id = istari.submit_llm_job(
                            record.llm_input_model_id, LLM_FUNCTION_EXTRACT_RESPONSE,
                            params, llm_config.credentials,
                        )
                        record.transition(PipelineState.LLM_JOB_SUBMITTED)
                        save_project(project, project_path)
                        continue
                    record.transition(
                        PipelineState.FAILED,
                        error="LLM answers failed validation after retry:\n"
                        + "\n".join(result.errors),
                    )
                    save_project(project, project_path)
                    break
                for warning in result.warnings:
                    _log(log, f"response {record.uuid}: warning: {warning}")
                validated = result.items
                record.transition(PipelineState.VALIDATED)
                save_project(project, project_path)

            elif record.state is PipelineState.VALIDATED:
                _notify(progress, "uploading", record.uuid)
                if validated is None:  # resuming: crash between upload and persist?
                    # If a matching answers artifact already exists, the crash
                    # happened AFTER the upload — adopt it instead of
                    # uploading a duplicate (§3.6b/T6: upload not duplicated).
                    if record.revision:
                        existing = find_existing_answers(
                            istari, record.uuid, record.revision, schema_version
                        )
                        if existing is not None:
                            info_art, _payload = existing
                            record.answers_artifact_uuid = info_art.artifact_id
                            record.schema_version = schema_version
                            record.transition(PipelineState.UPLOADED)
                            save_project(project, project_path)
                            _log(log, f"response {record.uuid}: adopted existing "
                                      f"answers artifact {info_art.artifact_id}")
                            continue
                    if record.llm_input_model_id is None:  # corrupt evidence (FR11)
                        _restart_from_queued(record, project, project_path,
                                             "no LLM input model id persisted", log,
                                             restarts)
                        continue
                    try:
                        raw = _read_llm_output(
                        istari, record.llm_input_model_id,
                        output_artifact=LLM_RESPONSE_OUTPUT_ARTIFACT, log=log,
                    )
                    except IstariError as e:
                        _restart_from_queued(record, project, project_path,
                                             f"LLM output artifact unusable ({e})", log,
                                             restarts)
                        continue
                    result = validate_answers(raw, requirements)
                    if not result.ok:
                        record.transition(
                            PipelineState.FAILED,
                            error="LLM answers failed validation:\n"
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
                    llm_model=llm_config.llm_model_stamp,
                    prompt_version=response_prompt_version(schema_version),
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
                if record.answers_artifact_uuid is None:  # corrupt evidence (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "no answers artifact id persisted", log,
                                         restarts)
                    continue
                answers_rev = _find_answers_revision(
                    istari, record.uuid, record.answers_artifact_uuid
                )
                if answers_rev is None:  # upload evidence unusable (FR11)
                    _restart_from_queued(record, project, project_path,
                                         "uploaded answers artifact not found", log,
                                         restarts)
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
                _notify(progress, "done", record.uuid)
                _log(log, f"response {record.uuid}: done")

        except (IstariError, PipelineError) as e:
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
        if isinstance(payload, dict) and info.artifact_id == project.requirements_artifact_uuid:
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
        if isinstance(payload, dict) and info.artifact_id == record.answers_artifact_uuid:
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
    candidates = [(i, p) for i, p in candidates if isinstance(p, dict)]
    if not candidates:
        raise PipelineError(
            f"RFI {rfi_uuid} has no readable {REQUIREMENTS_ARTIFACT_NAME} artifact"
        )
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

    # Traverse discovery links from EVERY requirements artifact revision, not
    # just the chosen one: responses ingested under an earlier schema were
    # linked from that schema's artifact and must survive a rebuild — they
    # render flagged stale (FR3/FR6), matching what a machine with a local
    # project file shows (§3.6c convergence).
    seen: set[tuple[str, str]] = set()
    for cand_info, _cand_payload in candidates:
        for link in istari.list_links(cand_info.revision_id):
            if link.left_revision_id != cand_info.revision_id:
                continue  # the rfi->requirements edge, not a discovery edge
            response_revision = link.right_revision_id
            try:
                response_uuid = istari.model_id_for_revision(response_revision)
            except IstariError as e:
                _log(log, f"skipping linked revision {response_revision}: {e}")
                continue
            if (response_uuid, response_revision) in seen:
                continue  # duplicate discovery edges must not duplicate rows
            # newest answers artifact for this response revision, any schema —
            # the actual schema_version is stamped so staleness renders right
            match = next(
                (
                    (info, payload)
                    for info, payload in istari.list_json_artifacts(
                        response_uuid, ANSWERS_ARTIFACT_NAME
                    )
                    if isinstance(payload, dict)
                    and payload.get("response_revision") == response_revision
                ),
                None,
            )
            if match is None:
                _log(log, f"response {response_uuid}: no matching answers artifact; skipped")
                continue
            seen.add((response_uuid, response_revision))
            info_art, payload = match
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
                      f"(answers artifact {info_art.artifact_id}, "
                      f"schema v{payload.get('schema_version')})")
    return project, requirements_artifact
