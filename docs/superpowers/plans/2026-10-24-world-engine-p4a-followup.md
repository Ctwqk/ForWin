# World Engine P4a Follow-up (Audit Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four defects that the two-auditor audit found in P0–P4a before P4b starts. Two of them leak secrets to the writer, one lets the world tick contradict the chapter it follows, and one is the `plot_responds` drift from the design.

**Architecture:**
- The writer prompt loses its unfiltered Genesis sections.
- Review issues name hidden facts by topic only.
- The world tick stops moving or changing the chapter's onstage entities, and rejects overlapping moves.
- `plot_responds` keeps the world change pending as a held intent instead of committing it.

**Tech Stack:** Python 3.13, SQLAlchemy, PostgreSQL tests, pytest.

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md` (§5.1, §5.4, §3.3, §6.2). Audit sources: `.superpowers/sdd/2026-10-24-world-engine-audit/{report-opus,report-fable,comparison}.md`.

## Global Constraints

- **No secrets in writer prompts.** A writer prompt never contains the truth text of a secret that the chapter has no reveal directive for (spec §5.1). Hidden-fact references in writer-facing text are named by topic only (`forbidden_topic_text` with subject names), never by proposition.
- **No story vocabulary in runtime code.** New writer-facing text lives only in `forwin/writer/prompt_core/**`, with exact `max_literals` in `tests/overfit_allowlist.toml`.
- **Every non-create patch carries `old_value`.** No row lock is held across an LLM call. Story time is in integer minutes.
- **Committing.** Use `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit`. End with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. No model name in commit messages.

## Rulings (from the audit)

- **R41.** The writer prompt drops the three Genesis sections: `Genesis 世界总览`, `Genesis 地图总览` and `Genesis 长线引擎` (`writer/prompt_core/sections.py:93-96`). They are author-view text with no reveal filtering; a paraphrased secret ("父亲的归来") cannot be caught by substring checks. The revision-id line stays. The LLM reviewer may keep them.
- **R42.** Spec §6.2 holds. `plot_responds` does not commit the held change:
  - It commits only the intent's status patch to `held` (with `old_value`) and records `pending_plan_response`. The band planner then binds a responding goal, the existing R2/Task 8 mechanism.
  - **Release.** The tick releases a held intent back to `planned`, with `scheduled_at = now`, after the chapter bound to it is accepted. If that chapter's `intent_interactions` settled the intent, the existing settlement applies instead.
  - **Offered only for a live intent.** `plot_responds` is offered only when the conflict names a live, unsettled intent.
  - **Once per intent.** It is offered once per intent. After a resolved `plot_responds` conflict on the same `intent_ref`, the options are veto / accept / amend.
  - **Unchanged.** `accept_with_obligation` is unchanged and still commits.
- **R43 (W9).** Two rules protect what the chapter just narrated:
  1. **`movement_overlap`.** A move whose `depart_at` precedes the same mover's last arrival is an error. The last arrival comes from earlier movements in the same delta, the running tick state, and the chapter delta's movements. This applies in the tick hard rules and the chapter gate.
  2. **Onstage exclusion.** The tick after chapter N treats chapter N's onstage set as off limits. The set is the plan's `onstage_refs`, plus `pov_ref`, plus every mover and event participant in chapter N's committed extraction delta. For those entities:
     - Actor turns skip them.
     - A candidate that changes their location, alive_status, restraint or held items is not committed. Its intent stays due and gains `profile.requires_onstage = true`, with `old_value`, so the band planner stages it (Task 8 slip/bind). The drop is recorded as `WORLD_TICK_CANDIDATE_DROPPED` with reason `touches_onstage_entity`.
     - Knowledge and relation changes stay allowed.
- **R44.** Gate-issue text that reaches the writer names facts by topic. Fact refs render as topic text built from the fact's non-concealed subject names, the way the world view's forbidden topics are built. This applies to `contradicts_hidden_fact`, `premature_reveal`, `told_by_unaware_source` and every other code.
  - The proposition stays in `original_result`.
  - The P2 R40 pin test flips into a leak regression.

## Review Focus

1. A real multi-genre writer prompt holds no secret text from any author-side source. The test plants a unique marker in Genesis summaries and overviews, beat-sheet text, band-plan text and a gate message, then renders all five writer builders plus the repair overlay.
2. A held intent never silently disappears. Every held intent is eventually released or settled, and a second conflict on it cannot offer `plot_responds` again.
3. The onstage exclusion never strands an offstage intent forever. Each excluded intent becomes an onstage intent that the planner binds.
4. A historical rewrite that supersedes a `plot_responds` resolution un-holds the intent (P33 rollback) and retires its pending response.
5. A chapter whose two moves of one mover overlap is rejected at the gate. The same holds for a tick move that overlaps the chapter's own move.

---

### Task F1: Remove unfiltered Genesis text from the writer prompt (R41)

**Files:**
- Modify: `forwin/writer/prompt_core/sections.py:85-97`. Stop rendering the three overview lines in writer builders. The context fields may remain for the LLM reviewer.
- Modify: `forwin/context/providers/genesis_provider.py`, only if a writer-only field must be cleared.
- Test: `tests/test_writer_secret_sources.py` (new). Update prompt snapshots in `tests/fixtures/prompt_regression/` and the hashes in `tests/test_prompt_revision.py`, listing each changed hash.

- [ ] **Step 1: Write the failing test.**
  - Name: `test_no_author_side_text_reaches_any_writer_builder[genre-builder]`.
  - It is parametrised over the three multi-genre fixtures and all five builders.
  - Plant a unique marker string in the Genesis `long_arcs[].summary`, `world_bible.overview` and `map_atlas` overview, and in a hidden fact's proposition.
  - Render the prompt and assert none of the markers appear.
  - Also assert the interstellar fixture's real long-arc summary text is absent from a chapter-1 prompt.
- [ ] **Step 2: Run it and confirm it fails** on the Genesis markers.
- [ ] **Step 3: Implement.** Delete the three lines from the writer section builder. Keep `Genesis 根层版本`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_writer_secret_sources.py tests/test_writer_world_view_prompt.py tests/test_prompt_revision.py tests/test_writer_prompt_contract.py tests/test_no_story_content_in_runtime.py`. Expect PASS.
- [ ] **Step 5: Commit**: `fix(writer): stop showing the writer unfiltered Genesis overviews`.

### Task F2: Name hidden facts by topic in review text (R44)

**Files:**
- Modify: `forwin/review/world_consistency.py:116-153` (`_named`, `_issue`).
- Modify: `forwin/writer/prompt_core/world_view.py`, reusing `forbidden_topic_text`.
- Modify: `forwin/book_state/extraction/translator.py:240-256`. Keep names for non-fact refs, and add a topic map for facts, or build it in the reviewer.
- Test: flip `tests/test_world_consistency_review.py:233-247` into `test_hidden_fact_contradiction_names_only_the_topic`. Add a regression to `tests/test_writer_secret_sources.py` that drives a `contradicts_hidden_fact` / `premature_reveal` / `told_by_unaware_source` issue through repair into the writer prompt. Assert the proposition is absent and the topic present.

- [ ] **Step 1: Write the failing tests.** Covers the flipped pin and the repair-to-writer regression for all three codes.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
  - In writer-facing `description` and `suggested_fix`, render each `fact:<id>` as `forbidden_topic_text(subject names)`. Subject names come from the fact's related nodes that are not concealed, using the shared `is_concealed`.
  - When the fact has no nameable subject, use a neutral prompt_core label.
  - `original_result` keeps the raw message and the proposition.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_world_consistency_review.py tests/test_writer_secret_sources.py tests/test_world_gate_v6.py tests/test_plan_reviewer_bindings.py tests/test_no_story_content_in_runtime.py`. Expect PASS.
- [ ] **Step 5: Commit**: `fix(review): name hidden facts by topic in writer-facing review text`.

### Task F3: The tick leaves the chapter's onstage entities alone and rejects overlapping moves (R43, W9)

**Files:**
- Modify: `forwin/world_engine/running_state.py`. Track the last arrival per mover, seeded from the chapter delta's movements.
- Modify: `forwin/world_engine/constraints.py:229-345`. Add hard rule `movement_overlap` and hard rule `touches_onstage_entity`, the second taking an `onstage_refs: frozenset[str]` argument.
- Modify: `forwin/world_engine/actor_turns.py:147-183`. `rank_actors` excludes the onstage set.
- Modify: `forwin/world_engine/tick.py`. Compute chapter N's onstage set. Turn a dropped onstage-touching candidate into the intent's `requires_onstage` patch.
- Modify: `forwin/book_state/reviewer.py` `_movement_issues`. Add the gate code `movement_overlap` for one chapter delta.
- Test: `tests/test_world_tick_service.py`, `tests/test_world_tick_steps.py`, `tests/test_world_constraints.py`, `tests/test_world_gate_v6.py`.

- [ ] **Step 1: Write the failing tests.**
  - `test_tick_never_moves_or_harms_the_chapters_onstage_entities`
  - `test_offstage_intent_touching_an_onstage_entity_becomes_an_onstage_intent`
  - `test_onstage_actor_gets_no_actor_turn`
  - `test_tick_move_overlapping_the_chapter_move_is_rejected`
  - `test_gate_rejects_overlapping_moves_of_one_mover`
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement** per R43. Keep knowledge and relation changes allowed.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_world_tick_service.py tests/test_world_tick_steps.py tests/test_world_constraints.py tests/test_world_gate_v6.py tests/test_world_tick_multi_genre.py tests/test_planning_multi_genre.py tests/test_band_planner.py tests/test_no_story_content_in_runtime.py`. Expect PASS.
- [ ] **Step 5: Commit**: `fix(world_engine): keep the tick off the chapter's onstage entities and reject overlapping moves`.

### Task F4: `plot_responds` keeps the change pending as a held intent (R42)

**Files:**
- Modify: `forwin/world_engine/conflict_resolution.py:120-160`. Change `option_candidates` and the plot-response path.
- Modify: `forwin/world_engine/conflict_routing.py`. Offer `plot_responds` only for a live intent that has not been plot-responded before.
- Modify: `forwin/world_engine/tick.py` / `intents.py`. Release a held intent once its bound chapter is accepted and did not settle it.
- Modify: `forwin/world_engine/superseded_resolutions.py`. Rollback un-holds the intent.
- Modify: `forwin/planning/band_planner/inputs.py`, only if the pending-response payload needs the intent ref as a binding.
- Test: `tests/test_world_conflict_resolution.py`, `tests/test_world_conflict_api.py`, `tests/test_world_tick_service.py`, `tests/test_historical_rewrite_world_ticks.py`, `tests/test_band_planner.py`. Migrate the P3/P4a tests that assert a committed plot-response change, keeping what each one proves.

- [ ] **Step 1: Write the failing tests.**
  - `test_plot_responds_holds_the_intent_and_commits_no_world_change`, covering both the in-tick and HTTP paths.
  - `test_held_intent_is_released_after_its_bound_chapter_without_interaction`
  - `test_held_intent_settled_by_its_bound_chapter_stays_settled`
  - `test_plot_responds_is_offered_once_per_intent`
  - `test_plot_responds_unavailable_without_a_live_intent`
  - `test_rewrite_superseding_a_plot_response_unholds_the_intent`
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement** per R42. Writer-facing response text stays built from the change's public parts only (P32).
- [ ] **Step 4: Run** the files above, plus `tests/test_world_tick_multi_genre.py`, `tests/test_planning_multi_genre.py` and `tests/test_no_story_content_in_runtime.py`. Expect PASS.
- [ ] **Step 5: Commit**: `fix(world_engine): plot_responds holds the intent for the plot to answer, as designed`.

### Task F5: Docs, and the P4b plan amendment

**Files:**
- `Design-docs/{CURRENT_DESIGN,CURRENT_ARCHITECTURE,DESIGN_STATUS}.md` and `forwin/book_state/README.md`: document R41–R44.
  - Document the start-writing lock as the single exception, and fix the contradicting docstring at `forwin/planning/beat_sheet/service.py:67`.
  - Record that `models.role_preferences` covers only `review.world_conflict` and that there is no `extract.split_calls`.
  - Fix `DESIGN_STATUS.md:117`.
- `docs/superpowers/plans/2026-10-22-world-engine-p4b.md`: add the unowned audit items listed in `comparison.md` §B (B1–B14) to the owning P4b tasks or a new task. This is controller work, and the user reviews P4b.

- [ ] **Step 1:** Update docs and the docstring. Run `.venv/bin/python -m pytest -q tests/test_design_status_docs.py tests/test_no_story_content_in_runtime.py`.
- [ ] **Step 2: Commit**: `docs: record the P4a audit rulings`.

### Task F6: A validated writer-facing setting brief (FU-R51)

Background: FU-R45 removed the raw premise, setting and Genesis overviews from the writer prompt, so the writer gets no book-level world or tone description. The beat sheet now carries a short brief that is validated like the other writer-facing planner text.

**Files:**
- Modify: `forwin/planning/beat_sheet/types.py` (`ArcBeatSheetData.setting_brief: str = ""`), `forwin/planning/beat_sheet/service.py` (validation, payload), `forwin/planning/beat_sheet/fallback.py`
- Modify: `forwin/writer/prompt_core/planning.py` (the `ARC_BEATS_INSTRUCTIONS` field description and its writer-facing clause; keep `max_literals` exact), `forwin/writer/prompt_core/sections.py` (story-basics section)
- Modify: the context plumbing that already carries the beat sheet's reader promise for the chapter's own arc (`forwin/state/repo.py`, `forwin/context/providers/experience_provider.py` or the story-basics provider, `forwin/protocol/context.py` field `arc_setting_brief`)
- Test: `tests/test_arc_beat_sheet.py`, `tests/test_planner_writer_text.py`, `tests/test_writer_secret_sources.py`, `tests/test_writer_world_view_prompt.py`, one multi-genre fixture's `beat_sheet` stub plus an assertion in `tests/test_planning_multi_genre.py`; prompt snapshots/hashes only if they change

**Interfaces:**
- `ArcBeatSheetData.setting_brief: str` defaults to `""`, so stored sheets without it still load. Validation:
  - at most 200 characters after strip; issue code `setting_brief_too_long`;
  - `writer_text_states_secret` with the shared normalizer, against facts not revealed to the reader by the arc's `chapter_start`, the same rule as `reader_promise`;
  - the concealed-name check with the same arc-start reveal exception as phase purposes.
- The arc-beats prompt asks for one to three sentences on the world's look and texture and the story's tone, and states that it reaches the writer verbatim, with no secrets and no concealed names.
- The fallback beat sheet leaves `setting_brief` empty: no unvalidated text.
- The writer's story-basics section renders `设定简介：<brief>` from the chapter's own arc's active sheet, using the M5 read path, in all five builders. An empty brief renders nothing.
- `revise` keeps or regenerates the brief like any other field.

- [ ] **Step 1: Write the failing tests:**
  - `test_setting_brief_validates_like_other_writer_text` (secret proposition, concealed name, too long, allowed reveal exception)
  - `test_setting_brief_reaches_every_writer_builder`
  - `test_stored_sheet_without_setting_brief_loads`
  - `test_fallback_sheet_has_no_setting_brief`
  - the multi-genre fixture's brief appears in the chapter-1 prompt
- [ ] **Step 2: Run them; confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `.venv/bin/python -m pytest -q tests/test_arc_beat_sheet.py tests/test_planner_writer_text.py tests/test_writer_secret_sources.py tests/test_writer_world_view_prompt.py tests/test_planning_multi_genre.py tests/test_prompt_revision.py tests/test_no_story_content_in_runtime.py`. Expect PASS.
- [ ] **Step 5: Commit**: `feat(planning): give the writer a validated setting brief from the beat sheet`.
