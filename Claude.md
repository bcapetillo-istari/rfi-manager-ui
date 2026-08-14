# CLAUDE.md — RFI Manager

PySide6 desktop app that automates RFI response comparison on the Istari Platform:
extract requirements from an RFI PDF, extract answers from response PDFs (Istari
extraction + LLM), upload everything back to Istari with links, display in a
sortable/filterable table. **The full spec is `PRD-rfi-manager.md` — read it before
any work. The PRD wins over this file if they ever conflict.**

## Reference material

- **Istari SDK**: Istari has NO public REST API. All platform access goes through
  the official Python client. SDK reference and platform docs:
  https://docs.istaridigital.com — check the SDK/general-usage pages for client
  setup, file/model registration, running functions/jobs, and artifacts. If docs
  are ambiguous, introspect the installed package (`python -c "import ..."`,
  `help()`, `dir()`) and note assumptions in the adapter docstring.
- **Example implementations** (study before writing `istari_adapter.py`; imitate
  their client usage patterns, auth, and job polling):
  - /Users/benjamincapetillo/projects/istari-digital-internal-repos/istari-digital-examples
  - /Users/benjamincapetillo/projects/istari-digital-internal-repos/model_diff_ui
  If these are cloned locally, they live in `./reference/` (git-ignored). Read
  them; never copy code wholesale without checking its license header.
- LLM APIs: Anthropic messages API and a generic OpenAI-compatible chat endpoint
  (both behind `llm_adapter.py`; see PRD §3.4).

## Architecture rules (violations = bug)

- All Istari SDK calls live in `istari_adapter.py`. All LLM calls live in
  `llm_adapter.py`. Nothing else imports the SDK or makes HTTP calls.
- `pipeline.py` contains orchestration and validation. It never imports Qt.
- UI code (`ui/`) never calls adapters directly — it dispatches to QThreadPool
  workers and reacts to signals. The UI thread never blocks.
- Data contracts in PRD §4 are frozen. Changing a key name or shape requires
  updating the PRD first.
- The platform is the source of truth; the local project file is a pointer cache
  written atomically at every state transition (PRD §3.6). If a feature would
  make the local machine hold unrecoverable data, the design is wrong.
- Secrets (Istari token, LLM keys) come from environment variables only. Never
  write them to disk, project files, logs, or commits.

## Dev environment

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt                      # pin versions
cp config.example.toml config.toml                   # config.toml is git-ignored
pytest                                               # must be green before any commit
QT_QPA_PLATFORM=offscreen python -m rfi_manager --selftest   # UI smoke test
```

Python 3.11+. Tests use fake adapters — no network, no credentials, ever.
Run `pytest` after every meaningful change, not just at milestone ends.

## Git workflow

- This is a git repo. If not yet initialized: `git init`, create `.gitignore`
  FIRST (see below), then an initial commit of PRD + CLAUDE.md + scaffolding.
- One branch per PRD milestone: `m1-skeleton`, `m2-stage1`, `m3-stage2-table`,
  `m4-export-polish`. Merge to `main` only with pytest green.
- Commit small and often — a commit per working unit (a module + its tests),
  not one commit per milestone. Message format:
  `feat(pipeline): validate enum answers against options` /
  `fix(adapter): poll job status by id` / `test(persistence): resume from
  llm_returned state`. Reference the PRD item (FR/T number) in the body when
  applicable.
- Never commit: `config.toml`, `.env`, `*.rfiproj`, LLM scratch cache,
  `reference/`, `.venv/`, `__pycache__/`. Keep `.gitignore` current.
- Never use `git push --force` on `main`. Never rewrite published history.

## Working style

- Follow the PRD milestones in order. At the end of each milestone: run the full
  test suite, update `PROGRESS.md` (what's done, what's blocked, open SDK
  questions), and commit.
- STOP and ask (or log a blocking question in PROGRESS.md if running
  unattended) when: an SDK capability required by the PRD appears not to exist
  (especially link traversal, PRD §3.6c), a data contract seems wrong, or a
  dependency beyond requirements.txt seems needed.
- Prefer boring solutions: dataclasses over clever metaprogramming, explicit
  state machines over implicit flags, stdlib over new dependencies. Adding a
  dependency requires a one-line justification in the commit message.
- Type hints everywhere; docstrings on every adapter method naming the SDK call
  it wraps.

## What NOT to do

- Don't invent Istari SDK methods. If unsure a method exists, check docs or the
  reference repos, or stub it in the fake and flag it in PROGRESS.md.
- Don't call real endpoints from tests, and don't "quickly test" against a live
  Istari instance unless explicitly asked — live verification is a human-run step.
- Don't add frameworks (no Django/FastAPI/SQLAlchemy — there's no server and no
  database), no ORM, no async rewrite. QThreadPool is the concurrency model.
- Don't gold-plate past the PRD's non-goals (§6).
