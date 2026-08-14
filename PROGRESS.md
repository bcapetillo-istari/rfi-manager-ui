# PROGRESS

## Re-architecture — LLM calls as Istari Agent jobs (done, 2026-08-14)

Per docs/LLM_Call_Flow.md (PRD updated first): all LLM calls now run as
platform jobs against the `@istari_utils:rfi_manager` module — functions
`extract_rfi_requirements` and `extract_response_requirements`, each owning
its prompt module-side.

- Deleted client-side LLM path (`llm_adapter.py`, `prompts.py`, anthropic +
  httpx deps). No LLM key exists client-side: Linked Accounts bound to jobs
  via `add_job(auth_bindings=[NewCredentialBinding(...)])`; UI credential
  pickers fed by `list_credentials()`.
- State machine gains `llm_job_submitted`; ResponseRecord persists
  `llm_job_id` + `llm_attempts` (crash-safe retry-once via the
  `validation_errors` job parameter). The LLM job's `llm_output.json`
  artifact replaces the local scratch cache as the post-LLM checkpoint.
- Jobs reference the extracted-text artifact revision — document text never
  travels through job parameters.
- pytest: 91 passed; headless smoke green (stage1 → commit → batch →
  table → rebuild → reopen).

Blocked / waiting on external:
- The `@istari_utils:rfi_manager` module does not exist yet. All identifiers
  (function names, tool_name/tool_version/OS, `llm_output.json`, parameter
  names) live in one constants block in `istari_adapter.py` — adjust to the
  deployed manifest on delivery. Live verification is a human-run step.
- Confirm an `@token:llm` auth integration exists on the target instance.

## M3 — Stage 2 + comparison table + resume/rebuild (done, 2026-08-14)

Done:
- `process_response` state machine (§3.6b): project saved atomically at every
  transition; job id persisted before polling; raw LLM output checkpointed;
  idempotency probe with force bypass (FR5); provenance + discovery links.
- Resume (FR11): same loop continues from any intermediate state; dead job ids
  and missing caches restart cleanly from queued with a session-log note.
- `rebuild_from_platform` (FR12/§3.6c): highest schema_version wins (choice
  logged), traverses discovery links, matches answers by (revision, schema).
- UI: Stage2Page (single + batch input, per-response status, retry action,
  force re-extract), ComparisonPage (QTableView + proxy: numeric-aware sort,
  global search, all/NOT_FOUND/low-confidence/stale filters, em-dash + tint
  rendering, FR7 detail pane with copyable provenance), MainWindow navigation,
  Open project… with "Resume N incomplete extractions?" prompt, Open from RFI
  UUID…. Batches run sequentially in one worker so project writes stay
  single-threaded.
- Headless smoke: stage1 → commit → 2-response batch → table (filters,
  search, detail) → rebuild-from-UUID → reopen project. pytest: 88 passed
  (T2 and T6 complete).

Blocked: nothing.

## M2 — Stage 1 end-to-end (done, 2026-08-14)

Done:
- Pipeline orchestration (no Qt): `run_stage1_extraction` (FR1) with PRD §3.2
  progress states and restart-safe job polling; `commit_requirements` (FR2)
  uploading `rfi-requirements.json` + linking source revision → artifact
  revision; `next_schema_version` (FR3); `prompt_b_version` stamps
  template+schema (T3).
- UI: `Stage1Page` (UUID + optional revision, extract button, progress),
  `ReviewScreen` (editable table, add/delete/reorder, inline validation per
  FR2, user-settable schema_version, commit disabled while invalid),
  `MainWindow` (QThreadPool workers via signals, FR3 re-run warning + version
  bump, session-log dock seed for FR10, atomic project save on commit).
- Headless smoke run (offscreen, fakes): extract → review → commit → project
  file written, link created. pytest: 73 passed (adds T2 stage-1, T3).

Blocked: nothing.

## M1 — Skeleton (done, 2026-08-14)

Done:
- Layers scaffolded: `models.py`, `pipeline.py`, `persistence.py`, `config.py`,
  `prompts.py`, `istari_adapter.py`, `llm_adapter.py`, `ui/` (placeholder).
- PRD §4 data contracts as dataclasses; pipeline state machine (§3.6b) with
  legal-transition enforcement; retry-from-failed clears stale evidence.
- Validation: all §4 rules incl. fence stripping, id coverage, per-type
  coercion, NOT_FOUND handling, retry-once LLM path.
- Persistence: atomic `.rfiproj` writes (temp + fsync + rename), LLM scratch
  cache (§3.6b).
- Real `istari_adapter.py` written against istari-digital-client 11.2.0,
  usage patterns verified against the internal reference repos
  (istari-digital-examples, model_diff_ui). `FakeIstari`/`FakeLLM` for tests,
  with a signature-conformance test pinning fake to real.
- pytest: 59 passed (T1, T4, T6 persistence parts, adapter conformance).

Blocked: nothing.

Open SDK questions / assumptions (verify on first live run — human step):
1. **Link traversal (PRD §3.6c)** uses the v3 revision-relationship API
   (`V3Client.create_revision_relationship` / `list_revision_relationships`).
   Methods exist in SDK 11.2.0 but are exercised nowhere in the reference
   repos. Assumed a `produces` relationship type exists on the platform
   (resolved by name at first use; adapter raises with the list of available
   types if not). Links are between *revisions*, not models.
2. **Artifacts cannot be listed per job** (known SDK gap) — adapter matches by
   artifact name and iterates newest-first. A concurrent extraction on the
   same model could interleave; acceptable for v1 (one project at a time).
3. Extraction function pinned to `@istari:extract` / `open_pdf` 1.0.0 /
   Ubuntu 22.04 (from the extract-pdf reference notebook); reads the
   `text.txt` artifact. There is no function-discovery API.
4. v3 pagination cursor field assumed `next_cursor` (guarded with getattr).

Next: M2 — Stage 1 end-to-end (extract → review screen → commit), T2 stage-1,
T3 prompt-generation tests.
