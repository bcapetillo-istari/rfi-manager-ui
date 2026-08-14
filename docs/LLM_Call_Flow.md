# LLM-Call Flow in RFI Manager Tool

## Overview

The RFI Manager uses LLM calls to extract requirements from RFI PDFs and answers
from RFI response PDFs. **All LLM calls are routed through Istari Agents** — the
desktop app never calls an LLM API directly. Rationale:

- **Auth consolidation** — LLM API keys live as Linked Accounts (stored
  credentials) on the Istari platform. The UI binds a credential to a job *by
  reference*; the raw key never passes through the app, a params file, or a log.
- **Enclave compatibility** — desktop clients may have no egress to LLM APIs;
  the agent is the network path.
- **Auditability** — every LLM call becomes a platform job with an id,
  parameters, and a results artifact: full provenance for the most
  consequential step in the pipeline.
- **Stronger crash recovery** — the job's raw-output artifact replaces the
  local LLM scratch cache (PRD §3.6b): a crashed client re-polls the job and
  reads the artifact from the platform. The "never re-pay for an LLM call"
  guarantee becomes platform-native.
- **Infrastructure reuse** — same module scaffold, agent hosting, and
  auth_info credential delivery already proven by Smart Diff / Model One.

## Architecture: two dedicated LLM functions

Instead of one generic LLM proxy, we deploy **two purpose-built functions** in
the **`@istari_utils:rfi_manager` module** (maintained in its own repo,
separate from this app):

| Function | Purpose | Owns |
|---|---|---|
| `extract_rfi_requirements` | Stage 1: extract the requirements schema from an RFI's extracted text | Prompt A (requirements-extraction prompt lives inside this script) |
| `extract_response_requirements` | Stage 2: extract one response's answers, matched against the committed RFI fields | Prompt B (answers-extraction prompt lives inside this script; receives the RFI fields JSON as input) |

Why two functions rather than one generic proxy:

- **Separation of concerns** — each stage's prompt, input schema, and output
  shape evolve independently and version independently.
- **Prompts live with their executor** — each prompt is embedded in its own
  script, versioned by the module. The UI no longer builds prompts; it passes
  data. Prompt B still "cannot drift" from the committed requirements (FR9's
  intent) because `extract_response_requirements` *generates* its prompt from
  the requirements JSON the UI passes in.
- **Tighter contracts** — the manifest can validate each function's inputs
  specifically instead of accepting arbitrary prompt strings.

## Flow

1. **UI initiates the LLM call.**
   - After a PDF extraction job completes (its `text.txt` artifact is on the
     platform), the UI submits the appropriate LLM job via the SDK
     (`client.add_job(...)`), attached to the model being analyzed (the RFI
     for stage 1, the response for stage 2).
   - Parameters are small (no document text): revision references to the
     extracted-text artifact, plus — for `extract_response_requirements` —
     the committed requirements as JSON.
   - Auth: `auth_bindings=[NewCredentialBinding(input_name=...,
     credential_id=...)]` binds the user's Linked Accounts (Istari token +
     LLM provider key). The UI populates a **credential dropdown** from
     `client.list_credentials()` (StoredCredential: id, name, auth_type,
     account_identity) — same UX as Smart Diff.

2. **Istari Agent executes the function.**
   - The agent invokes the module scaffold entrypoint
     (`{entrypoint} <FunctionName> --input-file … --output-file …
     --temp-dir …`). `auth_info` inputs arrive as paths to token-standard
     credential files (`{"token": …, "api_key": …}`) materialized from the
     bound Linked Accounts.
   - The script downloads the extracted-text artifact revision
     (`read_contents(content_token)`, as Smart Diff does), assembles its
     prompt, calls the configured LLM provider, and writes the **raw LLM
     output** to the temp dir. The agent collects it as a job artifact.

3. **UI handles results.**
   - Polls the job (job id persisted in the project file before polling —
     crash-safe resume by re-polling, never resubmitting).
   - Reads the raw-output artifact, then runs the **client-side** PRD §4
     validation: parse, id coverage, type coercion. On validation failure,
     the retry-once path submits a second job carrying the error list (see
     contract below).
   - Requirements flow: user reviews/edits in the review screen, then the
     client uploads `rfi-requirements.json` and links it (unchanged).
   - Answers flow: the client uploads `rfi-answers.json` with provenance and
     creates links (unchanged).
   - **The functions never upload final artifacts.** An artifact that failed
     validation is never uploaded (PRD §4), and validation lives client-side
     where the review UI needs it.

## Contract (what this app codes against)

**Module:** `@istari_utils:rfi_manager` (separate repo; deployed on an Istari
Agent alongside the Smart Diff module).

### Function `extract_rfi_requirements` (Stage 1)

- Job attachment: `add_job(model_id=<RFI model id>,
  function="@istari_utils:extract_rfi_requirements", ...)`
- Inputs (proposed; final schema owned by the module repo):
  - `source_resource_id` / `source_revision_id` (parameters) — the RFI
    model's extracted-text artifact to read
  - `provider` / `model` (parameters, optional with module defaults)
  - `validation_errors` (parameter, optional) — error list from a failed
    client-side validation; when present the script appends it to the prompt
    (the PRD §4 retry-once path)
  - `istari_auth` (auth_info, `@token:istari`), `llm_auth` (auth_info,
    `@token:llm`)
- Output: one file artifact — the raw LLM output (JSON array of
  requirements per PRD §4 shape, unvalidated)

### Function `extract_response_requirements` (Stage 2)

- Job attachment: `add_job(model_id=<response model id>,
  function="@istari_utils:extract_response_requirements", ...)`
- Inputs (proposed): everything above, plus
  - `requirements_json` (parameter) — the committed RFI fields (the
    `requirements` list from the schema-of-record artifact) that the script
    matches answers against and builds its prompt from
- Output: one file artifact — the raw LLM output (JSON array of answers per
  PRD §4 shape, unvalidated)

### To confirm against the deployed manifest

- The exact `tool_name` / `tool_version` / `operating_system` values for
  `add_job` (Smart Diff precedent: function key is namespaced in the
  manifest; the tool identifies the module build).
- The output artifact names (this app locates raw output by artifact name,
  newest-first, as it does for `text.txt`).
- Parameter names above are this app's proposal — final say is the module
  repo's manifest; adjust `istari_adapter` constants to match on delivery.

## Components

1. **PySide6 UI** (this repo)
   - Submits extraction and LLM jobs via the SDK (all SDK calls in
     `istari_adapter.py`); polls; validates; uploads final artifacts.
   - New: linked-account credential dropdown (from `list_credentials()`)
     used when submitting LLM jobs.
   - `llm_adapter.py`'s direct HTTP providers (`anthropic`,
     `openai_compatible`) are removed from production use; `FakeLLM`-style
     fakes remain for tests. The LLM step becomes job orchestration in
     `pipeline.py` like any other platform job.

2. **`@istari_utils:rfi_manager` module** (separate repo)
   - Two minimal Python function scripts (`extract_rfi_requirements`,
     `extract_response_requirements`), each owning its prompt; module
     manifest exposes both functions; agent collects each run's raw-output
     file as a job artifact.
   - The module version + function name are stamped into final artifact
     metadata (replacing the client-side prompt version), so hosted-script
     version skew is detectable.

3. **Istari Platform**
   - Source of truth: files read from / uploaded to here; jobs, artifacts,
     links, and stored credentials (Linked Accounts) all live here.

## What changes in this codebase

- `istari_adapter.py`: add `list_credentials()` and a generic
  `submit_function_job(model_id, function, parameters, auth_bindings)` +
  raw-output artifact read path.
- `llm_adapter.py`: retired as a production surface (fakes remain for
  tests); the LLM step becomes two pipeline job calls.
- `prompts.py`: prompt templates move to the module repo. The client keeps
  only what validation/review needs; version stamps come from the module.
  T3 tests change accordingly (drift-prevention now = passing the committed
  requirements JSON to `extract_response_requirements`).
- `pipeline.py` state machine: `text_retrieved -> llm_returned` becomes
  submit-LLM-job -> poll -> read raw-output artifact; checkpoint evidence is
  (LLM job id, artifact) instead of a local scratch file. Local `.llm_cache/`
  goes away. Retry-once resubmits with `validation_errors`.
- `config.toml`: LLM section reduces to function/tool identifiers and
  provider/model defaults; `RFI_LLM_API_KEY` env var goes away (keys live on
  the platform as Linked Accounts).
- UI: credential picker (dropdown) surfaced on Stage 1/Stage 2 actions;
  persisted choice per session/project.
- PRD updates required first (per CLAUDE.md): §3.3 (config/secrets), §3.4
  (LLM adapter contract), §3.6b (LLM checkpoint evidence), FR9 (prompt
  ownership moves to the module; drift-prevention via requirements_json
  input).

## Verified against (2026-08-14)

- SDK 11.2.0: `add_job(..., auth_bindings=list[NewCredentialBinding])`;
  `NewCredentialBinding(input_name, credential_id)`; `list_credentials() ->
  list[StoredCredential]`.
- Smart Diff module repo: `module_manifest.json` function schema
  (`parameter` / `user_model` / `auth_info` input types, file outputs),
  scaffold entrypoint contract, token-standard credential files, revision
  download via `V3Client.get_resource_revision` + `read_contents`.

## Open questions

1. `tool_name`/`tool_version`/OS values and output artifact names — finalize
   when the `@istari_utils:rfi_manager` manifest exists (parameter names
   above are proposals to mirror in that manifest).
2. `@token:llm` credential type — confirm the auth integration exists on the
   target instance (Smart Diff's manifest already declares it).
3. Retry-once cost: a validation failure means a second LLM job (queue +
   poll overhead ×2 worst case). Accepted?
4. Review-screen edits + Stage 2: the committed `rfi-requirements.json` is
   the schema of record passed to `extract_response_requirements` — i.e. the
   platform artifact, not any local state. (This is the plan; noting it so
   the module repo reads the same contract.)
