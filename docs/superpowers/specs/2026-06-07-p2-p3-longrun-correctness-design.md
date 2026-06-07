# P2/P3 Long-Run Correctness Design

## Scope

This spec covers the remaining P2/P3 correctness items from the long-run review:

- Reduce pulp `single` writer's static LLM calls from 4 to no more than 3.
- Replace the single urban-biased pulp beat keyword table with a small track-aware matrix.
- Remove unreachable legacy code in `book_genesis_core/materialize.py`.
- Make protagonist macro status distinguish BookState fact evidence from legacy chapter-plan projection.
- Audit arc macro boundaries that were passed during a resume, not only boundaries equal to the current chapter.

Service-izing the module-function attachment pattern is out of scope for this pass.

## P2 Writer Cost Design

`ChapterWriter._write_single_chapter()` will stop calling `_extract_structured()` synchronously. The single path will:

1. make one LLM call for the chapter draft,
2. parse title/body/summary locally,
3. build a `WriterOutput` with empty structured lists,
4. set `generation_meta["call_count"] = 1`,
5. set `generation_meta["structured_extraction"] = "deferred"` and each structured extraction part to `"deferred"`.

The existing deferred-maintenance route will treat `"deferred"` like a structured extraction maintenance request. This preserves chapter acceptance speed for pulp while still leaving an explicit maintenance record for later full extraction.

Scene mode is unchanged. Preview mode is unchanged.

## P2 Pulp Beat Design

`forwin/checker/pulp_beat.py` will keep `verify_pulp_beats(body)` as the stable API and add an optional `track` argument. If no track is provided, it will infer a track from text keywords.

Initial tracks:

- `urban`: current city/workplace/business vocabulary.
- `xuanhuan`: cultivation/progression vocabulary.
- `rural`: village/family/small-business reversal vocabulary.
- `rebirth_period`: rebirth/period-era/resource and reputation vocabulary.
- `treasure_medicine`: appraisal/medical/antique treasure vocabulary.

Each track owns separate pressure/action/payoff/audience/damage/gain/hook word sets. Payoff and gain must not be identical lists. The verifier still returns the same `PulpBeatResult`, so hard-floor and policy callers remain compatible.

This is a deterministic first pass, not a semantic classifier.

## P2 Materialize Cleanup

`forwin/book_genesis_core/materialize.py` currently delegates to `self.handoff.*_materializer` and then contains large unreachable legacy bodies after the early returns. The cleanup will delete only the unreachable bodies behind:

- `materialize_book_arcs`
- `materialize_arc_chapter_plans`

The wrappers stay. `_ensure_arc_map_expansion` and `promote_next_arc_if_needed` stay.

## P3 Macro Status Design

`derive_protagonist_macro_status()` will use a layered evidence source:

1. BookState `FactNodeRow` rows with macro status fields in `state_json` or `metadata_json`.
2. Accepted chapter `experience_plan_json["macro_status"]` rows with explicit `evidence_refs`.
3. Accepted chapter `experience_plan_json["macro_status"]` rows without explicit evidence refs as legacy fallback.

The `source` field will no longer claim all results are `book_state_macro_projection`.

Sources:

- `book_state_macro_fact`
- `accepted_chapter_macro_evidence`
- `accepted_chapter_macro_legacy_projection`

For fact rows, evidence refs come from `source_refs_json` when present, otherwise `fact_node:<id>`. For accepted chapter rows, explicit `evidence_refs` are retained; legacy rows use `chapter_plan:<chapter_number>` and are clearly marked as legacy projection.

## P3 Arc Boundary Design

Future plan macro boundary audit will select active project arcs where:

- `chapter_end > 0`
- `chapter_end <= current_chapter`

It will skip only arcs already recorded as successfully boundary-audited in previous non-failing `future_plan_audit_runs.metadata_json["macro_boundary_audited_arc_ids"]`.

The current audit run will write `macro_boundary_audited_arc_ids` to its metadata for all boundary arcs it considered. Failed runs are not treated as successful audited markers, so a previously failed boundary can still be rechecked after repair.

## Error Handling

Writer single-mode deferred extraction must not fail the chapter write. It only marks metadata for deferred maintenance.

Pulp beat track inference falls back to `urban` if no track keywords match.

Macro status parsing ignores malformed JSON and malformed tier values. Unknown macro source rows are skipped unless they contain at least one recognized macro field.

Boundary audit dedupe never skips failed historical audit runs.

## Tests

Add or update focused tests for:

- single writer call count is 1 and structured extraction is deferred,
- accepted single-mode chapters record deferred structured extraction maintenance,
- xuanhuan and treasure/medicine pulp beat bodies satisfy core payoff signals without urban words,
- payoff and gain signals are distinct enough to catch separate track words,
- materialize wrappers no longer contain unreachable legacy bodies,
- macro status can derive from BookState fact rows and sets `source="book_state_macro_fact"`,
- accepted chapter macro status with explicit evidence uses `accepted_chapter_macro_evidence`,
- legacy accepted chapter macro projection is still compatible but marked legacy,
- arc boundary audit catches `chapter_end < current_chapter`,
- previously successful audited arc boundaries are not re-audited.

## Out Of Scope

This pass does not implement a full semantic pulp beat classifier, a new macro-status extraction pipeline, or a broad service refactor of the orchestrator module attachment pattern.
