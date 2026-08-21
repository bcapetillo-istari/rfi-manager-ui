## Feature: T/O Compliance for RFI Responses

### Overview
AF Acquisition RFIs follow standard T/O grading mechanisms. T (the Threshold) is the minimum value desired for a system attribute/characteristic. O (the Objective) is the desired value for a system attribute/characteristic. T/O values are specified per requirement and exist for most requirements in an RFI, but not necessarily all. Some requirements may be informational and will not specify T/O thresholds. The purpose of this feature is to enable acquisition officers to quickly identify which vendors satisfy these T/O benchmarks without manually cross-referencing proposals.

### Example
"...Requirement 1.1: T=200km from launch and recovery (L/R) location to collection
area/O=1500km from L/R to collection area..."

Here, T/O values for this RFI are T=200km, O=1500km

### Notes
- (T=O) means that the threshold is also the objective-- no higher performance is desired.
- (T=O) does NOT automatically mean gradeable. The example RFI contains informational rows like "Vendor should provide altitude performance information. (T=O)". `gradeable` is therefore an explicit judgment emitted by the extraction LLM, never derived from the mere presence of T/O text.
- Coverage reality (from the committed example RFI): of ~43 requirements, only ~6 are numeric/enum/boolean; ~37 are text-type with qualitative T/O tiers ("T=Mission essential / O=Secure BLOS with FMV..."). This drove the hybrid grading design below.

### Grading categories
- BELOW_THRESHOLD (fails the threshold — for `at_least` direction: value < T; mirrored for `at_most`)
- MEETS_THRESHOLD (satisfies T but not O — for `at_least`: T <= value < O)
- MEETS_OBJECTIVE (satisfies O — for `at_least`: value >= O)
- NOT_GRADEABLE (informational requirement, unsupported unit conversion, or text-type answer the LLM declined/failed to grade; always carries a `reason`, e.g. "informational", "unsupported_conversion", "range_value")
- NOT_FOUND (value is not found in vendor response)

These are our categories; we are not concerned with values going above and beyond the objective — those fall into the same category as values that meet the objective and are graded the same (MEETS_OBJECTIVE).

Edge semantics:
- T=O: any passing value goes straight to MEETS_OBJECTIVE (the MEETS_THRESHOLD band is empty by construction).
- Range answers ("12,000–18,000 ft") and range thresholds → NOT_GRADEABLE in v1 (reason "range_value").
- A small relative epsilon (~1e-9) applies at band edges to absorb float noise from unit conversion.

### Plan

Hybrid grading — deterministic where possible, LLM only where necessary, with the grade's source always recorded:

**Numeric, enum, and boolean requirements → deterministic client-side code**, stamped `grade_source: "deterministic"`. A new pure module `rfi_manager/grading.py` (no Qt, no SDK — same architectural position as file_export.py) computes grades at export/report-build time from the committed answer + the requirement's T/O fields. Re-runnable offline; a changed T/O value re-grades these rows on the next export for free.
- Numeric: unit conversion + direction-aware comparison.
- Boolean (the "(T=O)" must-statement class, e.g. the KSAs): true → MEETS_OBJECTIVE, false → BELOW_THRESHOLD.
- Enum: deterministic only when the requirement carries `threshold_option`/`objective_option` mapping T and O onto its `options` list; otherwise NOT_GRADEABLE.

**Text requirements → LLM-graded inside the EXISTING Stage 2 job** (`extract_response_requirements`), stamped `grade_source: "llm"`. No new LLM call: that job already receives the full requirements JSON (now T/O-bearing) and the vendor text. We pass in the grading category vocabulary with instructions to extract AND grade all text-type gradeable requirements against their T/O tier definitions, judging from the quoted evidence — explicitly NOT parroting vendor self-assessments ("Meets objective: ..." appears verbatim throughout the real example responses).

**Precedence rule (enforced in grading.py, not assumed from the prompt):** if the LLM emits a grade for a numeric/enum/boolean requirement anyway, the deterministic grade always wins. The LLM grade is consulted only where deterministic code cannot compute.

**Where grades and converted values live:**
- Deterministic grades and converted values exist only in the compliance report artifacts, computed at export time. Committed answers artifacts are never mutated or re-uploaded (preserves provenance and FR5 idempotency). Auditability: compliance_report.json carries BOTH the original value/unit and the converted value/unit per row.
- LLM text grades ride inside the answers artifacts (produced at ingest time by the Stage 2 job). Consequence: changing a T/O value re-grades deterministic rows at the next export, but text rows keep their old LLM grade until the response is re-ingested — consistent with the existing stale-schema (FR3) flow.
- The compliance report is the single unified view where both grade sources appear together.

**Polarity/direction:** `value >= T` is wrong for lower-is-better requirements (weight, cost, lead time). The requirements schema gains an explicit `direction` field (`"at_least" | "at_most"`), LLM-extracted, cross-checked client-side against T/O ordering when T ≠ O (mismatch → validation warning + gradeable=false), and human-correctable in the Stage 1 review screen. For T=O numeric requirements direction is unknowable from the values alone — the LLM must emit it.

**Grading policy (PO decisions, 2026-08-21 live tuning):**
- **Trust the vendor.** RFI responses are taken at face value — the grader
  compares what the vendor SAYS against the requirement tiers; it never
  audits honesty or discounts claims. An explicit "meets T=O" statement
  grades as met. Verification of claims belongs to later acquisition phases
  (proposals, demos), not this tool. (Prompt B3.)
- **Multi-configuration responses** (a vendor offering e.g. Block 1 and
  Block 2 variants) are an edge case deferred until confirmed in real data —
  extraction may currently mix configurations across rows.
- **T=none rows where the capability is absent grade MEETS_THRESHOLD** (the
  threshold is trivially satisfied) — accepted for v1; the hover rationale
  explains it.
- **Single-call text grading accepted** (extraction + grading in one Stage 2
  run). Known tradeoff, decided knowingly: full-document context makes the
  grade MORE accurate but less traceable — the grade is not provably a
  function of the recorded value/quote (deterministic types are unaffected;
  their grades are pure functions of the extracted value). Revisit and split
  into a separate grade_answers module function (answers JSON + tiers only,
  no document text; re-runnable without re-ingest) if any of these occur:
  (a) grades observed contradicting their own recorded value/quote,
  (b) humans editing extracted answers need grades to follow,
  (c) the eval framework needs grading skill isolated from extraction skill.

**Deferred (explicitly out of scope for v1):** confidence-dependent coloring/markers in the HTML report (grade drives color; extraction confidence stays in the tooltip/JSON only); LLM grading of enum types; grade coloring in the in-app Qt comparison table; range-value grading.

### Schema Changes (PRD §4 must be amended FIRST — hard rule)

Requirements JSON (`rfi-requirements.json`) — per requirement, all optional/nullable:
| Field | Type | Meaning |
|---|---|---|
| `threshold` | number \| bool \| null | T value, normalized into `unit` |
| `objective` | number \| bool \| null | O value, normalized into `unit` |
| `direction` | "at_least" \| "at_most" \| null | comparison polarity |
| `gradeable` | bool | explicit LLM judgment; false for informational rows |
| `threshold_option` | string \| null | enum only: the option that satisfies T |
| `objective_option` | string \| null | enum only: the option that satisfies O |
| `to_raw` | string \| null | verbatim T/O text from the RFI, for audit |

T and O share the requirement's existing single `unit` (the module normalizes both into it at extraction time).

Answers JSON (`rfi-answers.json`) — per answer, all optional/nullable:
| Field | Type | Meaning |
|---|---|---|
| `llm_grade` | grade category \| null | populated for text-type requirements only |
| `llm_grade_rationale` | string \| null | one-line justification citing the evidence |

Versioning/backcompat: schema_version bumps via the existing mechanics; old artifacts missing the new fields parse cleanly (from_dict defaults: gradeable=false, grades null) and grade as NOT_GRADEABLE — never crash. Adding T/O to an existing project requires re-running Stage 1, which bumps schema_version and flags existing answers stale (expected; matches FR3).

### Work Items

**This repo (rfi-manager-ui):**
1. PRD §4 amendment — the requirements + answers contract additions above. Must land before any code.
2. `models.py` — new Requirement fields (threshold, objective, direction, gradeable, threshold_option, objective_option, to_raw) and Answer fields (llm_grade, llm_grade_rationale), with backward-compatible to_dict/from_dict.
3. `pipeline.py` / `validate_requirements` — validate the new fields: types, direction/ordering cross-check (when T ≠ O), gradeable coherence, threshold_option/objective_option membership in options. Failures feed the existing retry-once validation_errors channel.
4. `pipeline.py` / `validate_answers` — accept/validate the optional llm_grade vocabulary and rationale.
5. New `rfi_manager/units.py` — deterministic unit conversion: a `{family: {alias: factor}}` table (length, time, speed, mass, data rate) plus an alias-normalization layer ("KTAS", "kts", "hrs", "ft MSL", "nmi"/"nautical miles"). Pure, stdlib only — no pint, per the dependency policy. Unknown or cross-family units → NOT_GRADEABLE with reason "unsupported_conversion" — fail fast, never guess.
6. New `rfi_manager/grading.py` — deterministic grader + precedence logic + grade-record assembly (grade, grade_source, reason, original/converted values). Pure: no Qt, no SDK.
7. `file_export.py` — `build_compliance_report_json` and `build_compliance_report_html` builders, new ExportName members, grade color map, upload_exports dispatch.
8. `ui/main_window.py` — include the two new builders in the Commit-to-Istari worker (5 artifacts total per commit).
9. `ui/review_screen.py` — display + allow correction of T/O/direction/gradeable before commit (probabilistic extraction must be human-correctable).
10. Tests — units.py conversion/alias table; grading.py (all five categories, both directions, T=O, T=none, precedence rule, epsilon edges, backcompat with missing fields); export builders (colors, grade_source in tooltip, escaping); fakes + ui_smoke.py scenario for the 5-artifact commit.
11. PROGRESS.md entry when the milestone lands.

**Module repo (rfi-manager-module-functions) — separate effort:**

12. `extract_rfi_requirements` — prompt + output schema emit threshold, objective, direction, gradeable, threshold_option/objective_option, to_raw. Normalize T and O into the requirement's unit.
13. `extract_response_requirements` — accept T/O-bearing requirements JSON; for text-type gradeable requirements, emit llm_grade + llm_grade_rationale judged against tier definitions using quoted evidence (explicitly NOT vendor self-claims).
14. Redeploy module; verify via the prompt_version stamp (FR9).

### Export Artifacts (following file_export.py conventions)

Two NEW artifacts added to the existing one-click Commit to Istari (5 total); existing artifacts (answers_tidy.json, answers.csv, review.html) unchanged:
1. `compliance_report.json` — tidy-formatted JSON like our existing export, plus per row: grade, grade_source ("deterministic" | "llm"), grade_reason (for NOT_GRADEABLE), original_value, original_unit, converted_value, converted_unit, threshold, objective, direction.
2. `compliance_report.html` — renders basically the same as our current html export (spreadsheet grid, sticky vendor column, hover overlay with provenance + grade + grade_source), with color-coded cells per validation result:
    - BELOW_THRESHOLD = red
    - MEETS_THRESHOLD = light green
    - MEETS_OBJECTIVE = darker green
    - NOT_FOUND and NOT_GRADEABLE = grey

### Rollout Sequencing (client-first, module-second)

1. PRD §4 amendment.
2. Client release: parses/validates the optional new fields, defaults gradeable=false, grading + compliance reports behind that default — deployable with zero module change (reports render all-grey gracefully).
3. Module prompt/schema deploy.
4. Re-run Stage 1 on a live RFI; verify new fields arrive; re-ingest responses; verify text-type llm_grades and the full compliance report.
