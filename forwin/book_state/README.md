# forwin.book_state

Status: CANON runtime.

`book_state` owns the BookState v6 runtime: GraphDelta ledger, extraction contract, deterministic review gate, compiler, snapshot/replay, and projection. P1 adds the story clock, per-book lexicon, versioned node identity, knowledge events, and narrative snapshots. P2 adds unified chapter-delta extraction, translation and world review. P3 adds world ticks and world–plan conflict resolution through Canon admission. The schema head is `0005_world_plan_conflicts` (parent `0004_drop_parallel_ledgers`), adding `world_plan_conflicts`; `0004` dropped `countdown_ledgers` and `character_state_transitions`.

Rules:

- `BookStateCompiler` is the final GraphDelta canon writer and is constructed only by `CanonAdmissionService`.
- `BookStateReviewGate` must run before new changes enter canon, including the Genesis seed.
- For `source_type="chapter_extraction"` deltas the gate also runs the v6 world checks on the delta's `movements`, `knowledge_tags` and `item_transfers` metadata: time offsets, reachability over the mover's known routes, travel speed within `review.travel_tolerance`, restrained movers, entities dead before the chapter acting (flashbacks exempt), item holders, told sources, and reader reveals against the reveal schedule. Location changes without a movement record (a faction's, say), and all other deltas, keep the patch-based reachability check.
- Accepted chapters enter through `CanonAdmissionService.commit_plan`; world edits use `commit_world_edit`, clamp to the latest canon chapter, and recheck under the project lock. The removed `BookStateDirectCommitService` is not an entry point.
- World, map, cognition, and narrative are always enabled; `canon.book_state_layers` has been removed.
- Obsidian, LLM KB, wiki/export, and debug APIs must be rebuildable projections from BookState or declared integration rows.

Genesis handoff:

- Genesis runs `brief → world → map → story_engine → book_blueprint → world_seed`; the old bootstrap stage was removed. The LLM generates `world_seed`, `forwin/genesis/world_seed.py` validates it deterministically, and validation errors prevent locking. Fallback produces a degraded structural shell with actor entries for every story_engine core_cast / opposition / faction id, without inventing story content.
- story_engine entities have ids (generated defaults `cast-n` / `opp-n` / `faction-n`), structured `relationship_axes` / `long_arcs`, and core_cast `secrets` lists.
- Start-writing materializes arcs, bootstraps the map, commits the seed, then creates arc-1 chapter plans. Arc `title` / `goal` / `stakes` / `payoff_direction` persist on `arc_plan_versions`. Chapter planning still calls `launch_arc_N` until P4.
- `WorldSeedCommitter` builds chapter-0 GraphDeltas with `source_type="genesis_seed"`, passes them through `BookStateReviewGate`, and calls `CanonAdmissionService.commit_world_seed`. That entry locks the project `FOR NO KEY UPDATE`. Chapter-0 subworld character generation is removed.
- `GenesisMapBootstrapError` / `WorldSeedCommitError` inherit `HandoffStageError` and produce `MAP_GENERATION_FAILED` / `WORLD_SEED_COMMIT_FAILED` records.

P1 state contracts:

- Story time is integer minutes since the book's epoch. `graph_deltas.story_time_start/end` store numeric values; the seed uses the opening minute for both. `story_calendars` stores the calendar, and `format_story_time` formats display text.
- `book_lexicons` stores time-unit aliases, rank ladders, currencies, appellations, generic character refs, travel modes, and news speed. Generic refs consume this lexicon; canon name anchors consume BookState kinship edges with `metadata.appellation`, read as “source's appellation → target.” Map generation derives structure from Genesis without genre keyword inference.
- Node `name`, `aliases`, `profile`, and `is_active` are versioned per chapter in `world_node_states`; `world_nodes` is the latest projection. Narrative data is stored in world snapshots. Compilation into an existing chapter builds on that chapter, preserving its accepted changes.
- `agenda`, `intent`, and `clock` are new node types with v6 value validation. Character fields include `alive_status`, `restraint`, and `travel_mode_ids`; location remains `state.location_id`. Actor state includes `actor_tier`, `next_turn_at`, and `resources`; rule profiles include `hardness`, `statement`, and `checkable`. Social edges support `rival_of`.
- Cognition writes use knowledge events and `knowledge_by_ref`; default visibility follows fact sensitivity. Reader promises are narrative `promise` nodes. `book_reader_promises` and proposal `metadata.reader_promise_patches` are removed.

Deferred scope and design choices:

- The seed's `location_ref` is a Genesis map id, translated at commit to a database map-node id in `state.location_id`. `reveal_candidates` stays in the frozen Genesis pack; `reveal_schedule.earliest_reveal_chapters` reads it as each secret's earliest reveal chapter (its arc's `chapter_start`).
- P2 implements chapter-level story-time spans, time monotonicity / flashback rules, and map `travel_time` conversion to minutes.
- P4 owns removal of `finalization._seed_state`, `RunControlStage.run`, and `SubWorldManager.apply_initial_arc_plan` with ArcDirector, plus replacement of `sub_world_roster_items` / `plan_band_activation` by band `onstage_refs`. The P3 world engine is implemented; the beat-table planner remains deferred to P4.

P2 chapter pipeline:

- `ensure_chapter_delta` runs `ChapterDeltaExtractor` at stage `chapter_delta_extraction` once per candidate body (validation retries allowed, including one retry that names refs outside its input tables), reusing matching `body_sha256` results in draft review and canon preparation. Its input carries the minimal BookState as-of N−1 entities and locations, plan goals / obligations / reward targets, intents, secrets, calendar units, time aliases and travel modes.
- `ChapterDeltaTranslator` turns `WriterOutput.chapter_delta` into graph deltas with `old_value` / `field_path` on non-create patches, `metadata["movements"]` and item `state.holder_id`. `BookStateReviewGate` v6 checks the translated delta; `WorldConsistencyReviewer` exposes the same checks as the `world_consistency` draft signal and routes errors to repair.
- Structured `plan_fulfillment` evidence judges goals and narrative obligations. Keyword checks, `MapMovementReviewer`, writer-contract / regex fallback extraction, countdown/custody audits and canon_quality's parallel ledgers are deleted. The chapter review form reads BookState and persists quality signals only.
- Start-writing flushes arcs, map, chapter-0 seed and arc-1 plans in one caller-owned transaction. On `HandoffStageError` it rolls back all handoff writes, then flushes only the failure event; successful handoff locks the revision and enters writing.

P3 world-engine boundary:

- Post-Canon runs `("world", "planning", "arc", "feedback")`; world runs `WorldTickService.run` before the legacy `WorldSimulator`. Its step `result_json` contains `tick_id`, `graph_delta_ids`, `conflict_ids`, `dropped_candidate_ids`, `postponed_candidate_ids`, `warnings`, `skipped_reason`, plus simulator `pressure_level`.
- A tick uses `tick-<commit id>`, the chapter's integer-minute story-time window and a deterministic seed. Disabled, non-latest and already-ticked chapters skip. Candidates come from intent interactions, clock expiries, onstage bookkeeping (the interim P3 slip rule, replaced by the P4 band planner), due offstage intents (`WorldAdjudicator`, at most 20 per tick), and `ActorTurnRunner`. Hard rules and per-candidate review gate precede impact/budget and narrative-boundary review; LLM candidates get one retry, with actor feedback sanitized to its view, then `WORLD_TICK_CANDIDATE_DROPPED`. LLM outages skip nodes without failing the tick.
- One news propagation pass feeds `WorldTickDeltaBuilder`; in-transit news lives in delta `metadata["news_in_transit"]`. The engine never writes reader cognition: the gate rejects it with `world_engine_reader_knowledge`. Non-create patches carry `old_value` and `field_path`.
- `CanonAdmissionService.commit_world_tick` accepts only `world_engine` deltas on the latest canon chapter, serializes writers with `FOR NO KEY UPDATE`, and is idempotent by `tick_id`. The tick checks its original world snapshot under that lock before admission; intervening canon changes make the step stale and retryable. The engine does not write world/map/cognition/narrative tables directly.
- Historical rewrite replays recorded tick deltas with CAS. Failed tick deltas retire and create human `replay_cas` conflicts; dependent tick deltas drop in turn, while a dependent non-tick delta refuses the rewrite.
- `world_plan_conflicts` owns held changes, offered options, decider, blocking chapter and decision/result refs. Historical rewrite or critical routes to human, minor to auto veto, moderate/major to Codex when `world.conflict_delegate="codex"`, otherwise human. `WorldConflictDelegate` defaults to `models.role_preferences["review.world_conflict"] = ("gpt-6-astra",)`, shares Spark's `run_codex_structured_call` and independent audit transactions, and escalates failures or `amend_plan`. `FORWIN_CODEX_MODELS` declares Codex models.
- Historical rewrite offers only `veto_world`; protection/milestone/reveal_schedule boundaries offer veto/amend; other conflicts offer veto/plot_responds/amend, plus `accept_with_obligation` at minor/moderate. Veto marks unsettled intents vetoed and schedules actor rerolls only while the actor's `veto_world` resolution count on conflicts detected at or after chapter `latest - world.max_rerolls` is below `world.max_rerolls`. Accept commits a `world_change_response` obligation with deadline `min(latest+6, target)` (`latest` is the latest canon chapter; `target` is `project.target_total_chapters`); plot_responds adds a goal to the next unaccepted plan; human amend commits and releases broken protections. Direct resolution uses `commit_world_tick(tick_id="conflict-<id>")` within one SAVEPOINT covering world changes, plan/obligation effects and conflict closure.
- An open human conflict blocks from `blocks_from_chapter`: progress `paused_for_review`, task `needs_review`, derived reason `world_conflict_pending`. Continue and review-approve with `continue_generation=true` return 409 while blocked. Resolve through the HTTP/MCP conflict surface, reread project/task state, then explicitly continue with no active generation task.
- Deterministic map materialization uses the project/subworld SHA-256 seed and declared edge endpoints; existing maps are not automatically rebuilt. Clock producers expire clocks or advance named intents; form protection keeps only the nearest six positive-time running clocks. Secret clock descriptions and later writer constraints hide labels/ids. Travel uses shared `estimate_travel` and the book's modes/known routes; the cognition graph already includes `false_edges` in path queries.

Explicitly deferred to P4:

- Protection producers (the engine already reads narrative `review_constraint`), critical/milestone producers, `rewrite_chapter` and the other conflict-source producers, `world.drift`, and evaluation of rule `checkable` predicates. P3 uses an empty milestone-ref set, and never writes reader cognition or triggers a reveal_schedule conflict.
- Delta-only re-tick after rewriting a latest chapter already ticked: P3 skips a second tick, so the rewritten chapter's own events receive no new tick news. Builder `runtime=` reuse and re-queuing forbidden deliveries are also deferred; `_unprotected` currently drops those deliveries.
- Beat-table/band planning, separate map access rules/team speed/edge multiplier checks, and new actors introduced by intents. `ChapterWorldView` (§5.1) supplies writer clock/restraint context in P4. Full design choices and unproduced signals are in `Design-docs/CURRENT_ARCHITECTURE.md`; code completion does not establish production rollout.
