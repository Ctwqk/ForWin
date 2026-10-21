# forwin.book_state

Status: CANON runtime.

`book_state` owns the BookState v6 runtime: GraphDelta ledger, extraction contract, deterministic review gate, compiler, snapshot/replay, and projection. P1 adds the story clock, per-book lexicon, versioned node identity, knowledge events, and narrative snapshots. The schema head is `0003_book_state_v6`.

Rules:

- `BookStateCompiler` is the final GraphDelta canon writer and is constructed only by `CanonAdmissionService`.
- `BookStateReviewGate` must run before new changes enter canon, including the Genesis seed.
- For `source_type="chapter_extraction"` deltas the gate also runs the v6 world checks on the delta's `movements`, `knowledge_tags` and `item_transfers` metadata: time offsets, reachability over the mover's known routes, travel speed within `review.travel_tolerance`, restrained movers, entities dead before the chapter acting (flashbacks exempt), item holders, told sources, and reader reveals against the reveal schedule. Location changes without a movement record (a faction's, say), and all other deltas, keep the patch-based reachability check.
- Accepted chapters enter through `CanonAdmissionService.commit_plan`; world edits use `commit_world_edit`, clamp to the latest canon chapter, and recheck under the project lock. The removed `BookStateDirectCommitService` is not an entry point.
- World, map, cognition, and narrative are always enabled; `canon.book_state_layers` has been removed.
- Obsidian, LLM KB, wiki/export, and debug APIs must be rebuildable projections from BookState or declared integration rows.

P1 Genesis handoff:

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
- P2 owns chapter-level story-time spans, time monotonicity / flashback rules, and map `travel_time` conversion to minutes.
- P4 owns removal of `finalization._seed_state`, `RunControlStage.run`, and `SubWorldManager.apply_initial_arc_plan` with ArcDirector, plus replacement of `sub_world_roster_items` / `plan_band_activation` by band `onstage_refs`. The world engine and beat-table planner remain later-phase work.
