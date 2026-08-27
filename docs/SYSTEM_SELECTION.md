# Feature: System-Based File Selection

## Overview

Replace the copy/paste UUID/revision-ID workflow with selection from an Istari
System. The user creates an "RFI" system in the Istari UI containing the RFI
and all response files at the same level (possibly 100+ responses). The app
then works from the System UUID:

- Stage 1: enter the System UUID, pick a branch, pick the RFI file from a
  dropdown — no more pasting the RFI resource UUID.
- Stage 2: select responses from a checkbox list of the system's files
  (default all checked), with the RFI entry greyed out so it cannot be
  accidentally ingested as a response — no more pasting revision IDs.

Goal: make bulk handling of 100+ RFI responses practical.

## Decisions (PO, 2026-08-21)

- **Branch selection is a UI dropdown** (`list_branches`), not hardcoded to
  main/baseline.
- **Manual UUID/revision entry is REPLACED** by the system flow. FR12
  "Open from RFI UUID" remains untouched as the rebuild/recovery path.
- **Batch processing stays sequential** in one worker for v1. A 100+ batch
  may run hours; FR5 idempotency + FR11 resume make interruption safe.
  Parallel ingestion is a separate future effort (requires rethinking
  single-threaded project-file writes).

## SDK support (verified against installed istari-digital-client 11.2.0)

Legacy `Client` surface (what istari_adapter.py already uses):
- `client.get_system(system_id)` -> `System`
- `System.list_branches()` -> `list[SnapshotTag]` (excludes the baseline tag);
  `System.get_branch(name)` -> `SnapshotTag`
- `System.list_branch_revisions(branch)` -> `list[SnapshotRevisionSearchItem]`
  with `resource_id`, `revision_id`, `file_id`, `name`, `display_name`,
  `resource_type` — internally paginated, 100+ files fine.

Key win: the listing yields `(revision_id, resource_id)` pairs directly, so
system-selected responses SKIP `resolve_response_revisions` entirely (no
per-response resolution round-trip — ~100 API calls saved per batch) and
ingest exactly the revision the branch snapshot tracks (stronger provenance
than "latest").

Live-verification items (first live run, human step):
- Whether the Istari UI's default branch shows up in `list_branches()` and
  under what name ("main"?), and whether a fresh system needs the baseline
  tag offered as a fallback entry in the dropdown.
- `resource_type` values for uploaded PDFs (used only for display).

## Design

### Adapter (istari_adapter.py) — two new methods + dataclasses

```python
@dataclass(frozen=True)
class BranchInfo:
    name: str            # SnapshotTag.tag
    snapshot_id: str

@dataclass(frozen=True)
class SystemFileInfo:
    resource_id: str     # the model id the whole pipeline runs on
    revision_id: str     # revision pinned by the branch snapshot
    name: str            # display_name or name, for the pickers
    resource_type: str | None

def list_system_branches(self, system_id: str) -> list[BranchInfo]: ...
def list_system_files(self, system_id: str, branch_name: str) -> list[SystemFileInfo]: ...
```

FakeIstari mirrors both (plus an `add_system(name, file_model_ids)` test
helper); the signature-conformance test pins fake to real.

### Stage 1 page (stage1_page.py)

Replaces the RFI-UUID text box:
1. `System UUID` line edit + **Load System** button -> Worker ->
   `list_system_branches`; populate the **Branch** combo (select "main" when
   present, else first entry).
2. Branch selection -> Worker -> `list_system_files`; populate the **RFI
   file** combo (file names, resource ids as item data).
3. **Extract Requirements** enabled once a file is selected; emits the chosen
   `resource_id` into the existing extraction flow — everything downstream of
   `start_extraction` is unchanged (optional revision override field is
   dropped along with manual entry).

### Stage 2 page (stage2_page.py)

Replaces the revision-ID paste box:
- Checkable file list (`QListWidget`, `ItemIsUserCheckable`) populated from
  the same `list_system_files` result, **all checked by default**.
- The entry whose `resource_id == project.rfi_uuid` is unchecked, disabled,
  and greyed, labeled "(RFI — cannot be a response)". No new bookkeeping:
  the project already stores the RFI resource id.
- Select All / Select None buttons + "N of M selected" label.
- **Ingest** feeds the checked entries' `(revision_id, resource_id)` pairs
  straight into the existing batch flow (`_run_responses`), bypassing
  `resolve_response_revisions`.

### Project file (persistence, §3.6a)

New optional fields on `Project`: `system_uuid`, `system_branch`. Backward
compatible (`from_dict` uses `.get()`); old project files load with both None
— reopening such a project simply requires re-entering the system id to
repopulate pickers. Written atomically as always.

### Reload/rebuild flows

- `open_project`: after the requirements artifact reloads, if `system_uuid`
  is set, a worker re-lists branches+files to repopulate both pickers.
- `rebuild_from_platform` (FR12): unchanged — recovery stays UUID-based and
  does not require a system.

## Work items

1. PRD update FIRST (hard rule): FR1/FR4 flow text, §3.6a project-file
   fields. §4 data contracts are untouched (no artifact schema changes).
2. `istari_adapter.py`: `BranchInfo`/`SystemFileInfo`, `list_system_branches`,
   `list_system_files` (docstrings naming the SDK calls per CLAUDE.md).
3. `tests/fakes.py`: fake mirrors + `add_system` helper; conformance-test
   method list gains both names.
4. `models.py`: `Project.system_uuid`/`system_branch` (+ round-trip test).
5. `ui/stage1_page.py`: system/branch/file pickers; `extract_requested`
   emits the selected resource id.
6. `ui/stage2_page.py`: checkable response list, grey-out rule, select
   all/none, count label; `ingest_requested` emits `(revision_id,
   resource_id)` pairs.
7. `ui/main_window.py`: load-system / load-files workers (standard
   Worker/signal pattern), wire new signals, populate pickers on
   project open; delete the manual-entry paths.
8. `pipeline.py`: accept pre-resolved `(revision_id, model_id)` pairs for
   batch start (thin — the resolution skip).
9. Tests: adapter/fake conformance, project round-trip, pipeline pair-based
   ingest; ui_smoke scenarios reworked for picker-driven Stage 1/Stage 2
   including the grey-out rule and select-all default.
10. PROGRESS.md entry.

## Non-goals (this feature)

- Parallel batch processing (explicitly deferred — see Decisions).
- Creating/modifying systems from this app (systems are authored in the
  Istari UI; this app only reads them).
- Committing result artifacts onto the system/branch itself (artifacts
  continue to upload to the RFI model as today) — possible follow-up.
