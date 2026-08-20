# PRD: RFI Extraction Pipeline Eval Framework (v1)

## 1. Problem

The RFI Manager pipeline uses LLM extraction at two stages (requirements from RFIs, matched answers from vendor responses) on top of deterministic PDF preprocessing. Quality regressions to date have been caught by manual review and ad-hoc diffing across runs. Observed failure classes include: preprocessing content loss (pypdf table-cell loss, corrupted OCR text layers), dropped/invented/shifted requirement IDs, lossy value paraphrasing, and semantic mis-joins between responses and requirements. We need a repeatable, on-command evaluation that turns "the pipeline seems fine" into measurable, attributable results — before and after any change to prompts, models, schemas, or extraction libraries.

## 2. Goals

- Detect regressions at each pipeline stage independently, with per-field diffs that identify the failing stage and field in one report.
- Deterministic scoring only (no LLM-as-judge in v1).
- Runnable on command, per stage or full suite, against the real platform (true integration — no mocks).
- Every result attributable to an exact configuration (model, prompt, temperature, library versions, git commit).

**Non-goals (v1):** perturbation/robustness testing, LLM-judged subjective scoring, cost/latency gating, dashboards, per-commit execution of the full suite.

## 3. Design

### 3.1 Stages

Each LLM stage consumes the **golden** upstream artifact (not live upstream output), so stage scores are isolated and a single root cause produces a single red stage. An optional full-chain run scores the integrated pipeline end-to-end.

**Stage 1 — Text extraction (deterministic).**
Runs production PDF→text extraction on golden PDFs; asserts *completeness properties* of the output rather than byte equality:
- Every requirement ID appears as a literal string
- Every golden quote is findable as a substring
- Page markers intact; no known corruption patterns (mojibake, digit/letter substitutions)
A reduced version (one small PDF) also runs per-commit in normal CI as a smoke test.

**Stage 2 — Requirements extraction (LLM).**
Input: golden RFI text. Compares extracted requirements against the golden requirements file:
- Exact match: IDs, types, units, required flags, threshold/objective values
- Substring rules: descriptions must contain listed key facts
- Named cases: KSA ID convention; 1.12a/1.12b duplicate handling; ID-sequence completeness

**Stage 3 — Response matching (LLM).**
Input: golden requirements schema + golden response text, per vendor. Compares against golden answers:
- Exact match: requirement IDs (whitelist-only), values, units, statuses (NOT_FOUND / not-offered)
- Quote grounding: quote is an exact substring of source text; quote contains listed key facts
- Named per-vendor traps: Talon (prose with no IDs; nmi→km conversion; declined capabilities), Coyote (Block 1 baseline policy; 1.12a NOT_FOUND; hedged values), Ironvale (45-vs-50 Mbps text preserved; future-dated compliance), Meridian (clean baseline; full coverage)

### 3.2 Cross-cutting requirements

- **Stability check:** each LLM stage runs twice; outputs must be identical (guards temp-0 assumption and silent model-alias drift).
- **Config stamping:** every report records model version, prompt versions, temperature/sampling config, extraction library versions, golden-set version, git SHA.
- **Failure output:** per-field diff (expected vs. actual, field path) for every mismatch; aggregate scores per stage (coverage, value accuracy, ID fidelity, hallucination count).
- **Re-blessing flow:** `--update-golden` writes actual→expected for review; golden changes land via git commit + review, never silently.
- **Results log:** one JSON report per run, stored in `eval/results/`, keyed by git SHA + timestamp.

## 4. Golden resources

| Asset | Contents |
|---|---|
| `golden/rfi/` | Attritable ISR RFI PDF (corrupted text layer — kept deliberately); `expected_text.txt`; `expected_requirements.json` (43 requirements, hand-verified) |
| `golden/responses/<vendor>/` | 4 vendor PDFs (Meridian: clean tables; Talon: ID-less prose, imperial units; Coyote: dual-config, hedged values, threshold failure; Ironvale: compliance matrix, partial/future compliance); per-vendor `expected_text.txt` and `expected_answers.json` (43 answers each, hand-verified) |
| `match_rules` config | Per-field rule map: exact / numeric-tolerance / must-contain-substrings / excluded |

Golden growth path: accepted human-review corrections on production documents become new golden items (where data handling permits).

## 5. Technology

| Concern | Choice | Rationale |
|---|---|---|
| Harness | **pytest** (+ markers per stage, parametrize per vendor/requirement) | Stage selection, per-case reporting, fixtures, `--lf` iteration; no custom runner |
| Report | **pytest-json-report** | Machine-readable per-test results + metadata, zero custom code |
| Diffing | **DeepDiff** + small substring matcher | Per-field path diffs, numeric tolerance, path exclusions |
| Schemas | **Pydantic** (shared with production module) | Validation *is* the conformance test; no eval/pipeline schema drift |
| Storage | Git-committed golden files (git-lfs for PDFs); JSON results in repo/artifacts | Reviewable re-blessing; no DB until volume demands it |
| Platform calls | Production SDK + **tenacity** retries | Real integration; tolerate transient platform errors |
| Explicitly deferred | promptfoo / DeepEval / Ragas / LangSmith | No LLM-judge or dashboard needs in v1; plain asserts stay auditable |

## 6. Layout

```
eval/
  conftest.py            # fixtures, config stamping, --update-golden
  golden/                # per §4
  match_rules.py
  test_stage1_text.py
  test_stage2_requirements.py
  test_stage3_matching.py
  test_stability.py
  results/
```

## 7. Milestones

1. **M1:** Harness skeleton + Stage 1 + golden plumbing + JSON report
2. **M2:** Stage 3 (highest semantic risk) + stability check
3. **M3:** Stage 2 + full-chain optional run + results trend script
4. **Exit criteria:** full suite green on current pipeline config; one intentional prompt regression demonstrably caught with a per-field diff

## 8. Success metrics

- Any prompt/model/library change can be evaluated in one command with attributable results
- A stage regression identifies the failing stage and fields without manual archaeology
- Zero silent golden-file changes (all via reviewed commits)