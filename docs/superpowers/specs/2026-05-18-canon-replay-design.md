# Canon Replay Tool Design

Date: 2026-05-18

Status: approved for implementation planning

## Scope

Build a standalone operator tool that re-runs the chapter review form on historical chapters of an existing project, generating fresh form-sourced canon rows (character state transitions, countdown ledger entries, obligation signals, etc.) from chapter text that has already been written and committed.

This is the complement to `scripts/migrate_legacy_canon_to_form.py`. Migration marks legacy rows superseded; replay regenerates form-sourced rows for the same chapters. The two scripts are intentionally separate because their operational characteristics differ: migration is metadata-only, idempotent, and seconds long; replay involves LLM calls, is non-idempotent, costs real money, and can run for hours.

## Use Cases

The tool serves four distinct operator workflows:

1. **Post-migration backfill**: after marking legacy canon rows superseded, regenerate form-sourced canon for chapters 1..N so the form has historical context when reviewing chapter N+1.
2. **Schema version upgrade**: when `form_schema_version` bumps, re-run all historical chapters under the new schema to produce canon rows compatible with current consumers.
3. **LLM upgrade re-validation**: after switching the canon-quality model, re-run on a sample range and compare verdicts against the previous model's output via diff mode.
4. **Targeted re-audit**: re-run a single chapter or small range to investigate a specific drift or false-positive case without regenerating chapter text.

## Goals

- One-shot operation that fully regenerates form-sourced canon for a chapter range without touching chapter text, drafts, or plans.
- Resumable: an interrupted run can be restarted and continues from where it left off, without redoing completed chapters.
- Atomic per chapter: a crash mid-run leaves the DB in a consistent state where completed chapters are committed and incomplete ones are untouched.
- Cost-bounded: pre-flight estimate, runtime token tracking, hard cap that aborts cleanly.
- Dry-run capable: full LLM call but no DB writes; produces the would-be canon rows for inspection.
- Diff mode: compares new form-sourced canon against existing form-sourced canon, reports row-level differences without writing.
- Decoupled from migration: this tool does not mark rows superseded or modify legacy data. It only writes new form-sourced rows.

## Non-Goals

- Not regenerating chapter text, plans, drafts, or any writer output.
- Not handling chapters whose `ChapterDraft.body` is missing (operator must regenerate text via the writer pipeline first).
- Not multi-project batch mode. One project per invocation.
- Not changing the form schema, validator behavior, or projection logic. This tool consumes the existing pipeline.
- Not backfilling derived data outside canon-quality (world-model projections, obsidian exports remain out of scope).
- Not building a UI. Command-line only, with structured JSON output for tooling.

## Approach Overview

The replay loop is conceptually simple:

```
for chapter in range(from_chapter, to_chapter + 1):
    body = read_chapter_body_from_db(project_id, chapter)
    writer_output = reconstruct_writer_output(body, chapter, project_id)
    result = analyze_writer_output_quality(
        session, project_id, chapter, writer_output, mode=..., persist=...
    )
    record_result(state_file, chapter, result)
```

The complexity lives in five places:

1. **WriterOutput reconstruction**: the existing pipeline expects a populated `WriterOutput`. The replay path must construct one from `ChapterDraft.body` alone. Most fields can be left empty because the form does not consume them, but this contract must be explicit.
2. **Atomic per-chapter persistence**: each chapter's canon rows must commit independently so a crash does not leave the DB half-updated.
3. **State file format**: a record of per-chapter status (completed | error | skipped) plus token usage so resume knows where to pick up.
4. **Cost accounting**: token usage per chapter is variable; the cap must be enforced before the next chapter, not after the fact.
5. **Diff computation**: for diff mode, comparing a candidate result against existing canon rows requires reading both into the same logical shape.

## Implementation Clarifications

- `--cost-cap-usd` may be unlimited while the cost phase is being built, but the final operator tool must require either `--cost-cap-usd <N>` or explicit `--no-cost-cap` before any LLM call.
- `--llm-profile <name>` resolves against the current `Config` primary LLM profile plus configured environment fallback profiles. If the requested profile cannot be resolved into an API key, base URL, and model, replay exits before any LLM call or DB write.

## Phases

### Phase 1: Foundation — Skeleton and WriterOutput Reconstruction

Purpose: set up the script structure and solve the hardest plumbing problem first.

Files:

- New: `scripts/canon_replay.py`
- New: `forwin/canon_quality/chapter_review_form/replay.py`
- New: `tests/test_canon_replay_reconstruction.py`

Steps:

1. Create `scripts/canon_replay.py` with an argparse stub: `--project-id` (required), `--from-chapter` (required), `--to-chapter` (optional, defaults to project's latest accepted chapter), `--dry-run` (default True until Phase 5 makes the alternative available). All flags are accepted; `main()` prints them and exits 0.

2. Create `replay.py` with a single function `reconstruct_writer_output(*, session, project_id, chapter_number) -> WriterOutput`:
   - Queries `ChapterDraft` for the chapter and takes the most recent accepted draft.
   - Returns a `WriterOutput` populated with: `project_id`, `chapter_number`, `title` (from chapter plan or draft), `body` (from draft), `end_of_chapter_summary` (from draft if present, else empty), `prompt_revision_hash="replay"`, `generation_meta={"source": "canon_replay", "replay_run_id": ...}`.
   - Leaves all other `WriterOutput` fields at their default empty values.
   - Documents inline that downstream consumers must not depend on those empty fields being meaningful for replay-sourced output.

3. Add a `ChapterDraftNotFound` exception raised by reconstruction when no accepted draft exists.

4. Pre-flight detection in the script: before any LLM call, walk the chapter range and verify each chapter has an accepted draft. Exit non-zero with a structured list of missing chapters if any are absent.

5. Tests:
   - Reconstruction returns the right body for a known chapter.
   - Reconstruction raises `ChapterDraftNotFound` if no accepted draft exists.
   - Reconstruction returns the latest draft when multiple accepted drafts exist for the same chapter.
   - Pre-flight detection fails fast on a range containing a missing chapter.

Acceptance:

- `scripts/canon_replay.py --help` shows the argument list.
- `reconstruct_writer_output` returns a valid `WriterOutput` for a real chapter draft.
- Missing-chapter detection works before any LLM call.

---

### Phase 2: Single-Chapter Replay Engine

Purpose: actually call the form pipeline on one chapter and persist the result. No resumability, no range loop, no cost cap.

Files:

- Modify: `scripts/canon_replay.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- New: `tests/test_canon_replay_single_chapter.py`

Steps:

1. Add `replay_single_chapter(*, session, project_id, chapter_number, llm_client, persist: bool, mode: str) -> ReplayChapterResult` to `replay.py`:
   - Calls `reconstruct_writer_output` from Phase 1.
   - Calls `analyze_writer_output_quality(...)` with the given `mode` and `persist`.
   - Wraps the result in `ReplayChapterResult` containing: `chapter_number`, `mode`, `status` (success | error), `signal_counts_by_severity`, `character_transitions_written`, `countdown_entries_written`, `validation_report_summary`, `token_usage` (placeholder until Phase 4), `error_message` (if any).

2. `scripts/canon_replay.py` resolves an `llm_client` from `--llm-profile <name>` via the existing factory. If no profile resolves, exit non-zero with a clear message.

3. Replay defaults to `persist=False` and `mode="dry_run"` unless `--persist` is passed. This protects against accidental overwrites during early use.

4. Per-chapter result is written to stdout as one JSON line, for downstream pipelines or just `jq` inspection.

5. Tests:
   - Successful replay with `persist=False` returns expected `ReplayChapterResult` shape and writes nothing.
   - Replay with no LLM client raises a clear error before any DB write.
   - Replay with `persist=True` writes canon rows with `payload.source == "chapter_review_form"`.
   - Replay on a chapter that the form rejects (e.g., LLM hallucinates an unverifiable quote) returns a `ReplayChapterResult` with rejected paths and no canon writes.

Acceptance:

- `python scripts/canon_replay.py --project-id <id> --from-chapter 5 --to-chapter 5 --llm-profile default --dry-run` runs the form on chapter 5 and prints a JSON result.
- Without `--persist`, no canon rows are written.
- With `--persist`, canon rows show up in the expected tables with the right payload source.

---

### Phase 3: Range Iteration and Resumability

Purpose: iterate over a chapter range, commit per chapter, support resume after interruption.

Files:

- Modify: `scripts/canon_replay.py`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- New: `tests/test_canon_replay_resume.py`

Steps:

1. Define `ReplayStateFile` schema:

   ```json
   {
     "schema_version": "canon_replay.v1",
     "project_id": "...",
     "from_chapter": 1,
     "to_chapter": 50,
     "started_at": "2026-05-18T...",
     "last_updated_at": "...",
     "chapters": {
       "5": {"status": "completed", "result_summary": {...}, "token_usage": {...}},
       "6": {"status": "completed", ...},
       "7": {"status": "error", "error_message": "..."}
     },
     "totals": {"completed": 6, "errors": 1, "skipped": 0}
   }
   ```

   Stored at `data/artifacts/canon_replay/<project_id>/<from>-<to>.state.json`.

2. Implement `replay_chapter_range(*, session_factory, project_id, from_chapter, to_chapter, ...)`:
   - Opens a fresh session per chapter.
   - Reads the state file; skips chapters with `status == "completed"` unless `--force-rerun` is set.
   - Calls `replay_single_chapter`.
   - On success: commits the session and updates the state file. Per-chapter atomic commit.
   - On error: rolls back the session, updates the state file with error info, continues to next chapter unless `--abort-on-error` is set.

3. Add flags:
   - `--resume`: load existing state file, skip completed chapters automatically.
   - Without `--resume`, refuse to start if a state file exists for the same range. Require operator to pass either `--resume` or `--force-restart`.
   - `--force-rerun`: ignore the "completed" status from state file and redo chapters anyway. Useful after LLM upgrade.
   - `--abort-on-error`: stop the range on first chapter error rather than continuing.

4. State file is updated atomically per chapter (write to tempfile, rename). A crash during write does not corrupt the state file.

5. Tests:
   - Range of 10 chapters completes and produces the expected state file.
   - Simulated interruption at chapter 5 leaves a state file with 4 completed; `--resume` picks up at chapter 5.
   - `--abort-on-error` stops at first failure.
   - `--force-rerun` redoes already-completed chapters.
   - State file write is atomic (no partial-write corruption in simulated crash).

Acceptance:

- A 10-chapter run interrupted at chapter 5 resumes cleanly and completes chapters 5-10 without redoing 1-4.
- Re-running without `--resume` or `--force-restart` refuses to start when a state file exists.
- Per-chapter commit verified: interruption at chapter 5 leaves chapters 1-4 fully committed in the DB.

---

### Phase 4: Cost Estimation and Safety Caps

Purpose: avoid runaway LLM bills. Estimate before running, track during running, abort when over cap.

Files:

- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- New: `forwin/canon_quality/chapter_review_form/cost_estimator.py`
- Modify: `scripts/canon_replay.py`
- New: `tests/test_canon_replay_cost.py`

Steps:

1. Implement `cost_estimator.estimate_run(*, project_id, from_chapter, to_chapter, session, llm_profile) -> CostEstimate`:
   - For each chapter in range, compute estimated input tokens = form schema size + prior canon summary size + chapter body size + ~500 system tokens. Use a token-per-char heuristic (~0.5 for Chinese, ~0.25 for ASCII) since the form is mostly Chinese.
   - Estimated output tokens = a fixed ~3000 per chapter (form answer payload).
   - Multiply by `llm_profile.pricing` from existing config.
   - Return `CostEstimate` with `total_input_tokens`, `total_output_tokens`, `total_usd`, and a per-chapter breakdown.

2. Add `--estimate-only` flag: runs `estimate_run` and exits without calling LLM. Output: total estimated cost, top-10 most expensive chapters, average cost per chapter, total tokens.

3. Add `--cost-cap-usd <N>` flag (default unlimited): track cumulative actual cost across chapters. Before each chapter, check if `current_cost + per_chapter_estimate > cap`. If so, abort cleanly: state file marks the would-be-next chapter as `skipped_due_to_cap`, script exits with non-zero status and a structured message.

4. Actual token tracking: extract `usage` from each LLM call response if available. If the client returns no usage data, log a warning and fall back to estimate-based tracking for cap enforcement.

5. Tests:
   - Estimate matches roughly the known token count on synthetic chapters within ±20%.
   - `--cost-cap-usd 1.00` aborts when cumulative actual cost exceeds $1.
   - Resume after cap abort: state file shows abort reason; resume continues from the right chapter.
   - LLM client without usage data triggers warning but cap still enforced via estimates.

Acceptance:

- `--estimate-only --from-chapter 1 --to-chapter 50` prints total cost estimate and exits.
- `--cost-cap-usd 1.00` aborts the run before cumulative cost exceeds $1.
- State file records the abort reason; future resume picks up cleanly.

---

### Phase 5: Dry-Run and Diff Mode

Purpose: let operators inspect what replay would do without writing, and compare new vs existing form-sourced canon.

Files:

- Modify: `forwin/canon_quality/chapter_review_form/replay.py`
- New: `forwin/canon_quality/chapter_review_form/replay_diff.py`
- Modify: `scripts/canon_replay.py`
- New: `tests/test_canon_replay_diff.py`

Steps:

1. `--dry-run` (now explicit; previously the default): runs the full pipeline including the LLM call, but `persist=False`. Per-chapter result JSON includes the would-be canon rows so operator can inspect. State file records `status: "dry_run_completed"`.

2. `--diff-mode`: runs the full pipeline, then for each canon row the replay would write, compares against the existing form-sourced row for the same `(project_id, chapter_number, character_name | countdown_key)`. Output structured diff per chapter:

   ```json
   {
     "chapter_number": 7,
     "differences": [
       {
         "kind": "character_state",
         "name": "...",
         "before": {"to_state": "dead", "evidence_quote": "...", "subject_of_quote": "..."},
         "after": {"to_state": "alive", "evidence_quote": "...", "subject_of_quote": "..."}
       },
       {
         "kind": "countdown",
         "key": "...",
         "before": null,
         "after": {"normalized_remaining_minutes": 30, "status": "active", ...}
       }
     ]
   }
   ```

   No DB writes. State file records `status: "diff_completed"`.

3. Implement `replay_diff.compute_diff(*, existing_rows, candidate_rows) -> list[ReplayDiff]`:
   - Pairs existing and candidate rows by `(chapter, kind, subject)`.
   - Reports adds (candidate only), removes (existing only), and changes (both, with field-level diff).
   - Compares only fields that matter for canon downstream: `to_state`, `terminality`, `evidence_quote`, `subject_of_quote`, `normalized_remaining_minutes`, `status`. Ignores volatile fields like timestamps and IDs.

4. Diff output is a JSON array (one entry per chapter with differences). Empty array means "no differences across the range". The script also prints a one-line summary per chapter to stderr for quick inspection.

5. Tests:
   - `--dry-run` does not write canon rows; would-be rows appear in result JSON.
   - `--diff-mode` shows the expected diff for a synthetic case where existing canon differs from replay verdict.
   - `--diff-mode` shows empty diff when both agree.
   - `--diff-mode` correctly classifies adds, removes, and changes.

Acceptance:

- `--dry-run` shows the candidate canon rows but writes nothing.
- `--diff-mode` shows row-level differences against existing form-sourced rows without writing.
- A diff between two passes that agree is empty.

---

### Phase 6: Operational Polish and Documentation

Purpose: make the tool safe and ergonomic for a real operator.

Files:

- Modify: `scripts/canon_replay.py`
- New: `docs/operations/canon_replay.md`
- Modify: `forwin/canon_quality/chapter_review_form/replay.py`

Steps:

1. Progress output:
   - Structured log line per chapter start and finish with tokens used, cost so far, status.
   - When stderr is a TTY, also print a one-line progress bar with chapter count, ETA, and cumulative cost.

2. Pre-flight checks (extend Phase 1's checks):
   - Project exists in DB.
   - All chapters in range have accepted drafts.
   - State file does not conflict with current flags (no `--resume` against missing state file, no orphan state without `--force-restart`).
   - Cost cap is set, or operator passed `--no-cost-cap` to explicitly opt out.
   - LLM client is reachable (probe call with a tiny payload).
   - Each check failure exits with a clear, structured message.

3. Final summary:
   - At end of run, print structured summary to stdout: chapters completed, chapters failed, chapters skipped (due to cap or error), total cost, average per-chapter cost, top errors (up to 5).
   - Write the same summary into the state file under a `summary` key.

4. `--schema-version <version>` flag:
   - Pin the form schema version. If different from current `FORM_SCHEMA_VERSION`, log a warning at start of run.
   - Useful for cross-version replay (use case 2). Default behavior: use current `FORM_SCHEMA_VERSION` unset, no warning.

5. State file management:
   - `--clear-state` flag removes the state file for the given range. Requires explicit `--confirm-clear` to actually delete (prevents accidents).
   - State files older than 30 days are flagged in pre-flight as possibly stale.

6. `docs/operations/canon_replay.md`: operator-facing documentation covering:
   - When to use replay versus not (decision tree).
   - Each of the four use cases with a complete command-line example.
   - Cost expectations (typical $/chapter, factors that drive cost up).
   - Resume workflow with example state file inspection.
   - Diff-mode workflow for verification before persisting.
   - Troubleshooting: missing drafts, LLM failures, state file corruption, cost cap aborts.

Acceptance:

- A 50-chapter replay run shows per-chapter progress and a final summary.
- Pre-flight catches the common operator errors before any LLM call.
- Operator documentation exists and is sufficient for someone who has read only `canon_replay.md` to use the tool correctly.
- All four use cases have a worked command-line example.

---

## Test Plan

Run after each phase:

- Phase 1: `pytest tests/test_canon_replay_reconstruction.py -q`
- Phase 2: `pytest tests/test_canon_replay_single_chapter.py -q`
- Phase 3: `pytest tests/test_canon_replay_resume.py -q`
- Phase 4: `pytest tests/test_canon_replay_cost.py -q`
- Phase 5: `pytest tests/test_canon_replay_diff.py -q`
- Phase 6: `pytest tests/test_canon_replay_*.py -q` plus a manual end-to-end on a staging project: 10 chapters with `--estimate-only`, then `--dry-run`, then `--diff-mode`, then `--persist`. Confirm artifacts and DB rows match expectations.

## Risk Controls

- **WriterOutput reconstruction yields fields the form does not need**: the form is documented to consume only `body` and identifying metadata. If a future form-pipeline change starts consuming other `WriterOutput` fields, replay must be updated to populate them, with a regression test catching the divergence.
- **Per-chapter atomic commit increases DB write overhead**: acceptable for one-shot replay; not a continuous operation. Bulk-commit alternative is out of scope and would defeat the resume guarantee.
- **State file corruption**: writes are atomic (tempfile + rename). Operator can always recover with `--clear-state --confirm-clear` and start fresh.
- **Cost cap miscalculation when LLM returns no usage data**: fall back to estimate-based tracking with a logged warning. Worst case: cap is loose by 10-20%, not unlimited.
- **Replay overwrites correct form-sourced rows with worse new ones**: `--diff-mode` is the operator's tool to catch this before any `--persist` run. Documentation must emphasize the recommended workflow: estimate → dry-run → diff → persist.
- **Long-running replay holds DB connections**: each chapter opens a fresh session and closes it after commit; no long-held connections.
- **LLM responses non-deterministic across runs**: documented limitation. Diff mode against existing form-sourced canon may show false-positive differences on rerun. Operator must judge meaningful vs. noise.

## Known Limitations and Deferred Work

- **No automatic comparison against legacy deterministic canon**: replay only knows form-sourced rows. Comparing against now-superseded deterministic rows requires a separate ad-hoc query.
- **No parallel chapter processing**: each chapter's prior-canon context depends on the prior chapter's freshly-generated form output. True parallelization would require rethinking the form's prior-state contract and is out of scope.
- **No streaming UI**: results stream to stdout one line per chapter, but there is no web dashboard for live monitoring.
- **No automatic chapter range detection**: operator must specify `--from-chapter`. Auto-detecting "first chapter with no form-sourced canon" is a nice-to-have for the post-migration use case but adds discovery complexity better left for a follow-up.
- **No multi-project mode**: one project per invocation. Batch over projects via a shell loop if needed.
- **No partial-chapter replay**: replay is per-chapter, not per-section or per-entity. Targeted re-audit of a single character within a chapter requires running the whole chapter.
- **No automatic rollback after persist**: if `--persist` writes wrong rows, operator must re-run with corrected inputs or restore from backup. There is no "undo last replay run" command.

## Done Criteria

- The tool reconstructs `WriterOutput` from `ChapterDraft.body` and runs the existing form pipeline on it.
- Operator can run a chapter range from N to M with per-chapter atomic commit.
- Interrupted runs resume cleanly from the right chapter without redoing committed work.
- `--estimate-only` shows expected cost before any LLM call.
- `--cost-cap-usd` aborts cleanly when actual cost approaches the cap.
- `--dry-run` runs the full pipeline without writing.
- `--diff-mode` shows row-level differences against existing form-sourced canon without writing.
- A 50-chapter replay run produces structured progress, per-chapter result JSON, and a final summary.
- Pre-flight catches missing drafts, missing LLM client, missing cost cap, and state-file conflicts before any LLM call.
- `docs/operations/canon_replay.md` covers the four use cases with worked examples.
- All `tests/test_canon_replay_*.py` pass.
