# PRD: RFI Manager (PySide6 desktop app on the Istari Platform)

## 1. Problem
A customer issues an RFI and receives hundreds of 20+ page PDF responses. Comparing
them by hand is infeasible. RFI Manager automates the pipeline end to end on the
Istari Platform: extract the RFI into requirements, extract each response into
requirement answers, and present everything in a searchable, sortable, filterable
table — with every value traceable to a versioned file on Istari.

## 2. Product summary
A desktop application (Python 3.11+, PySide6) named "RFI Manager" that uses the
Istari Python SDK (`istari-digital-client` — verify current package name against
docs.istaridigital.com). ALL LLM calls are routed through Istari Agent jobs —
the app never calls an LLM API directly (see docs/LLM_Call_Flow.md; the
`@istari_utils:rfi_manager` module hosts two LLM functions:
`extract_rfi_requirements` and `extract_response_requirements`, each owning its
prompt). Three-stage flow:

Stage 1 — Link RFI: user enters the Istari UUID of an RFI file (and optionally a
specific revision). App runs Istari's PDF data-extraction function on it, then
submits an `extract_rfi_requirements` job referencing the extracted-text
artifact revision; the job's raw-output artifact is read back, validated
client-side, and shown as a structured requirements list. User reviews/edits
the requirements in a review screen, then commits: the requirements JSON is
uploaded to Istari and linked to the source RFI file/revision. This JSON is the
schema of record.

Stage 2 — Ingest responses: user enters one or more UUIDs of RFI response PDFs
already on Istari (single entry field + multi-line batch entry). For each: run
the extraction function, then submit an `extract_response_requirements` job
(inputs: extracted-text revision reference + the committed requirements JSON,
so the module-side prompt cannot drift from the schema of record), read back
the raw-output artifact, validate the answers client-side, upload the answers
JSON to Istari linked to the response file/revision.

Stage 3 — Compare: all ingested responses render as rows in a table (columns =
requirements). Global search, per-column sort, filter by completeness/flags.
Every row shows its provenance (response file UUID + revision, answers artifact
UUID, schema version). Export CSV/XLSX and "Publish report" (self-contained
static HTML uploaded to Istari).

## 3. Architecture requirements
1. Layered: `istari_adapter.py` (all SDK calls behind a small interface —
   including LLM-function job submission, since LLM calls ARE platform jobs),
   `pipeline.py` (extract/LLM-job/validate/upload orchestration, no Qt imports),
   `models.py` (dataclasses for Requirement, Answer, ResponseRecord),
   `ui/` (PySide6 only, no SDK or HTTP calls directly from UI code).
2. All SDK/network work runs on QThreadPool workers (QRunnable) communicating
   via signals. The UI thread never blocks. Every long operation reports progress
   states: queued -> extracting -> llm -> validating -> uploading -> done|failed(reason).
3. Connection to the Istari registry comes from the UI: a connection bar with
   Registry URL and PAT text boxes (PAT masked, held in memory only, never
   written to disk by the app). `config.toml` is OPTIONAL and provides
   defaults/prefill only: registry URL prefill, default LLM provider/model
   (forwarded as job parameters), request timeouts, retry counts; the
   ISTARI_TOKEN env var, when set, prefills the PAT box. There is NO
   LLM API key on the client: LLM credentials are Istari Linked Accounts
   (stored credentials), bound to jobs by reference via `auth_bindings`. The
   UI offers credential pickers populated from `list_credentials()`.
4. LLM call contract: submit a job to the `@istari_utils:rfi_manager` module
   functions (`extract_rfi_requirements` for Stage 1,
   `extract_response_requirements` for Stage 2), attached to the model being
   analyzed. Job parameters reference the extracted-text artifact revision
   (never the text itself); Stage 2 additionally passes the committed
   requirements as JSON. Each job emits one raw-LLM-output artifact
   (`llm_output.json`) which the client reads, validates (§4), and — only
   after validation and review — turns into the final committed artifacts.
   The functions never upload final artifacts. Retry-once (§4) resubmits the
   job with a `validation_errors` parameter. Prompts live in the module repo,
   not this app. Function/tool identifiers live in one constants block in
   istari_adapter.py until the deployed manifest fixes them.
5. SDK usage notes for the implementer: Istari has no public REST API — use the
   official Python client. Consult docs.istaridigital.com for current client usage
   (register/add model, get file/revision, run function/job, poll job status, list
   and download artifacts, upload artifact/file, create links between files). If a
   needed operation is unclear, introspect the installed client package and write
   the adapter against what exists; keep ALL such calls inside istari_adapter.py.
6. Persistence & crash recovery — three tiers, platform is source of truth:
   a. Project file (index/cache only): one JSON file per RFI (`<name>.rfiproj`)
      holding: RFI UUID + revision, committed requirements artifact UUID +
      schema_version, and per response: UUID, revision, pipeline state,
      Istari job id (when submitted), answers artifact UUID (when uploaded).
      Written at EVERY state transition (never only on exit), atomically
      (write temp file, fsync, rename). Format has a version field.
      Table content is NOT stored here — it is re-fetched from Istari on load.
   b. In-flight checkpoints: each response's pipeline is an explicit state
      machine (queued -> job_submitted -> text_retrieved -> llm_job_submitted
      -> llm_returned -> validated -> uploaded -> done | failed(reason)).
      Persist evidence with each transition: extraction job id on submit and
      LLM job id on LLM submit (restart polls the same job instead of
      resubmitting); the LLM job's raw-output artifact on the platform IS the
      post-LLM checkpoint (LLM calls are the expensive step — never re-pay
      for a crash between LLM return and upload; no local scratch cache).
      The retry-once counter (llm_attempts) is persisted so a crash cannot
      cause more than one retry. On startup, offer to resume any response in
      an intermediate state from its furthest checkpoint.
   c. Rebuild from platform: given only an RFI UUID, the app must be able to
      reconstruct the project by traversing Istari links: locate the latest
      requirements artifact (schema + version), then locate answers artifacts
      via links/metadata. To make this possible, all uploaded artifacts MUST
      be discoverable: include a type tag in the artifact name
      ("rfi-requirements", "rfi-answers") and full self-describing metadata
      per section 4. This is also the multi-user story: two machines pointed
      at the same RFI converge on the same table.

## 4. Data contracts
Requirement (Stage 1 output, list of):
  { "id": "3.2.1" | "C-01",       # reuse RFI's own numbering when present
    "label": "Unit weight (kg)",   # <= 4 words, column heading
    "description": "<the requirement as stated in the RFI, condensed>",
    "type": "boolean" | "numeric" | "enum" | "text",
    "unit": "kg" | null,           # numeric only
    "options": ["Compliant", ...] | null,   # enum only
    "required": true | false,
    # T/O compliance fields (docs/T-O_COMPLIANCE.md) — all optional/nullable;
    # absent fields mean the requirement predates T/O extraction (gradeable
    # defaults false). T and O are normalized into `unit` by the extractor.
    "threshold": <number|bool|null>,        # T value
    "objective": <number|bool|null>,        # O value
    "direction": "at_least" | "at_most" | null,   # comparison polarity
    "gradeable": true | false,     # explicit LLM judgment, NOT derived from
                                   # T/O presence (informational "(T=O)" rows)
    "threshold_option": "<option>" | null,  # enum only: option satisfying T
    "objective_option": "<option>" | null,  # enum only: option satisfying O
    "to_raw": "<verbatim T/O text from the RFI>" | null }   # audit trail
Requirements artifact uploaded to Istari wraps the list with metadata:
  { "rfi_uuid": ..., "rfi_revision": ..., "schema_version": "1.0",
    "generated_at": iso8601, "llm_model": ..., "requirements": [ ... ] }

Answer (Stage 2 output, list of):
  { "id": "<requirement id>",
    "value": <bool|number|string|"NOT_FOUND">,
    "unit": "kg" | null,
    "quote": "<verbatim supporting sentence>",   # may be "" when NOT_FOUND
    "page": <int|null>,
    "confidence": "high" | "medium" | "low" | "none",
    # T/O compliance fields (docs/T-O_COMPLIANCE.md) — optional/nullable.
    # Populated by the LLM for text-type gradeable requirements ONLY;
    # numeric/enum/boolean grading is deterministic client-side and lives in
    # the compliance report artifacts, never here.
    "llm_grade": "BELOW_THRESHOLD" | "MEETS_THRESHOLD" | "MEETS_OBJECTIVE"
                 | "NOT_GRADEABLE" | "NOT_FOUND" | null,
    "llm_grade_rationale": "<one line citing the quote>" | null }
Answers artifact wraps with provenance:
  { "response_uuid": ..., "response_revision": ..., "vendor": "<from doc or user>",
    "schema_version": "<matches requirements artifact>", "extracted_at": iso8601,
    "llm_model": ..., "answers": [ ... ] }

Validation rules (pipeline, applied to all LLM output): strip markdown fences;
JSON must parse; every requirement id present exactly once; no unknown ids
(unknown -> warning, dropped); numeric values parse as numbers; enum values in
options; booleans are true/false; NOT_FOUND allowed anywhere with confidence
"none". On validation failure: retry the LLM job ONCE, passing the error list
via the validation_errors job parameter (the module appends it to its prompt);
if still invalid, mark the item failed with reasons shown in the UI. Never
upload an artifact that failed validation.

T/O field validation (requirements): threshold/objective types must match the
requirement type (numbers for numeric, booleans for boolean); direction must
be "at_least"/"at_most" when set; when T != O numerically, direction must
agree with T/O ordering (mismatch -> warning, gradeable forced false);
threshold_option/objective_option must be members of options (enum only);
gradeable=true requires T or O present for the requirement's type. Missing
T/O fields are legal everywhere (backcompat: parse cleanly, gradeable
defaults false). Answers: llm_grade must be one of the five grade categories
when set; llm_grade on a non-text requirement is a warning (deterministic
grading wins; see docs/T-O_COMPLIANCE.md precedence rule).

## 5. Functional requirements
FR1  Stage 1 UI: UUID (+ optional revision) input; "Extract requirements" button;
     progress states visible; on LLM return, open review screen.
FR2  Review screen: editable table of requirements (id, label, description, type,
     unit, options, required); add/delete/reorder rows; inline validation
     (duplicate ids, enum without options, numeric without unit warning);
     "Commit to Istari" uploads the requirements artifact, links it to the RFI
     file, records schema_version (user-settable, default 1.0).
FR3  Re-running Stage 1 with a committed schema warns and, on confirm, commits a
     new artifact with bumped schema_version. Existing response rows keep their
     original schema_version stamp and are flagged stale in the table.
FR4  Stage 2 UI: single-UUID field plus batch box (one UUID per line);
     per-response status list; failures show reason and a retry action.
FR5  Idempotency: before processing a response, check the project file (and,
     if cheaply possible via the SDK, existing linked artifacts) for an
     answers artifact matching (response revision, schema_version); if found,
     load instead of re-running. A "Force re-extract" action bypasses this.
FR6  Comparison table: QTableView + QSortFilterProxyModel. Columns: Vendor,
     one per requirement, then provenance columns (response UUID short form,
     schema rev). Numeric-aware sorting; global search box; filters: all /
     has NOT_FOUND / has low-confidence / stale schema. Cell states: NOT_FOUND
     rendered as flagged em-dash; low/medium confidence tinted.
FR7  Row detail pane: selecting a row shows per-requirement value, quote, page,
     confidence, and full provenance UUIDs (copyable).
FR8  Export: CSV and XLSX of current (filtered) view. "Publish report": render a
     self-contained static HTML table (no scripts required to read) and upload it
     to Istari linked to the RFI.
FR9  Prompts: Prompt A (requirements extraction) and Prompt B (answers
     extraction) live in the `@istari_utils:rfi_manager` module repo, each
     embedded in its own function script. Prompt B is generated module-side
     from the committed requirements JSON the app passes as a job parameter,
     so it cannot drift from the schema of record. Artifact metadata stamps
     the function identifier as prompt_version (module version appended when
     the deployed manifest exposes it), so hosted-script version skew is
     detectable.
FR10 All errors are surfaced in the UI with actionable text; no silent failures;
     a session log panel records every SDK call, job id, artifact UUID (this is
     the operator's audit view).
FR11 Startup resume: on opening a project file, reload committed state from
     Istari and detect responses in intermediate pipeline states; prompt
     "Resume N incomplete extractions?"; resume each from its furthest
     checkpoint per section 3.6b (poll existing job ids; reuse cached LLM
     output; never resubmit completed work). A response whose checkpoint
     evidence is unusable (e.g. job id no longer found) restarts cleanly from
     queued with a note in the session log.
FR12 Rebuild from platform: "Open from RFI UUID" flow that reconstructs a
     project with no local file, per section 3.6c. Conflicts (multiple
     requirements artifacts) resolve to the highest schema_version with the
     user shown what was chosen.

## 6. Non-goals (v1)
No editing answer values in the table (re-extract instead); no scoring/weighting;
no multi-RFI concurrent projects in one window (one project file at a time);
no packaging/installer work (run from source; packaging is v2); no direct PDF
viewing (show UUIDs/links to open in Istari instead).

## 7. Testing
pytest, no network in tests. `istari_adapter` (incl. the LLM functions) gets fake
implementations for tests.
  T1 validation: table-driven tests covering every rule in section 4, including
     fence stripping, retry-once path, unknown ids, type coercion failures.
  T2 pipeline: end-to-end with fakes — Stage 1 produces a requirements artifact
     upload call with correct linkage args; Stage 2 produces answers artifact
     with correct provenance; idempotency skip path; force re-extract path.
  T3 LLM-job contract: the extract_response_requirements job's
     requirements_json parameter contains every committed requirement id and
     its constraints; changing schema changes the artifact prompt-version
     stamp; auth_bindings carry the selected credentials by reference (never
     a raw key in parameters).
  T4 models/serialization round-trip; project file save/load.
  T5 report renderer: published HTML contains all visible rows, no <script src>,
     no external URLs, escapes user data.
  T6 persistence: project file round-trip; atomic write survives simulated
     crash mid-write (temp file present, original intact); state-machine resume
     from every intermediate state using fakes (job re-polled not resubmitted;
     cached LLM output reused; upload not duplicated); rebuild-from-UUID with
     fakes reconstructs an equivalent project.
UI smoke: a `--selftest` launch flag that loads fakes, runs Stage 1 + two
responses, and exits nonzero on failure (usable in CI without a display via
QT_QPA_PLATFORM=offscreen).

## 8. Milestones
M1 Skeleton: layers, config, adapters (real istari_adapter written against the
   SDK docs; fakes for tests), models, validation, project-file persistence
   with atomic writes and the pipeline state machine; pytest green on T1/T4/T6
   (persistence parts).
M2 Stage 1 end-to-end incl. review screen and commit; T2 (stage 1), T3.
M3 Stage 2 batch ingest + comparison table (FR4-FR7) + startup resume (FR11)
   and rebuild-from-UUID (FR12); T2 and T6 complete.
M4 Export + publish report (FR8), session log (FR10), selftest; T5; polish.

## 9. Code quality
Type hints throughout; dataclasses for contracts; no business logic in Qt slots
beyond dispatch; docstrings on every adapter method describing the SDK call it
wraps and why; README with setup (venv, pip install, config.toml example,
env vars) and a "running against a real Istari instance" section.
