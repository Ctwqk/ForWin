# ForWin 4.0 World Model Implementation Plan

> Status: historical implementation plan. This document explains why `world_model_v4` and `reviewer_v4` were created side-by-side. Current architecture is `BookState DB Canon + BookMap / Scheme C`; see `Design-docs/CURRENT_ARCHITECTURE.md` and `Design-docs/DESIGN_STATUS.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build ForWin 4.0 as a multi-layer information-asymmetry world model system while allowing unfinished 3.0 work to continue in parallel.

**Architecture:** Add v4 modules side-by-side first, then switch the canon commit path only after the v4 extractor/reviewer/compiler gate is test-covered. The new source of truth is append-only world/cognition/reveal/reader-experience ledgers compiled by `WorldModelCompiler`; `EntityState` and `CanonEvent` become derived compatibility views. Existing planning, writer, reviewer, retrieval, and runtime modules are upgraded in place only where they cross the v4 boundary.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, SQLite migrations through `forwin/models/base.py`, FastAPI, pytest.

---

## Implementation Progress

- 2026-04-24: Tasks 1-13 implemented through the v4 domain model, planning contracts, writer context/protocol, extractor, reviewers, compiler, and orchestrator compiler gate.
- 2026-04-24: Tasks 14-18 implemented through role-specific retrieval packs, provisional shadow projections, v4 debug API, derived export pages, and the Arc 2 homeworld-crisis E2E scenario.
- Verified: `pytest -q` -> `284 passed, 8 subtests passed`.
- Pending: Phase 4 hardening beyond this plan boundary, including richer UI/debug surfacing and production-grade migration polish.

## Operating Rules

- 4.0 is breaking by design. Do not preserve old world-model API semantics unless a facade is explicitly listed below.
- Keep 3.0 work unblocked by isolating new code under `world_v4`, `world_model_v4`, `extractor`, and `reviewer_v4` modules until Phase 3.
- Every implementation task starts with a failing test and ends with the exact pytest command passing.
- `WriterOutput` may self-report structured world data, but canon accepts only extractor output that passes reviewer gates and compiler validation.
- `StateUpdater` must not remain a canon source after Phase 3; it becomes a projection/materialized-view helper.

## File Responsibility Map

- Create `forwin/protocol/world_v4.py`: Pydantic enums and DTOs for world lines, deltas, cognition, gaps, reveals, reader experience, compile inputs, and snapshots.
- Create `forwin/models/world_v4.py`: SQLAlchemy rows for v4 ledgers, snapshots, contracts, compile runs, and review audit records.
- Modify `forwin/models/base.py`: register v4 models and add lightweight forward-only SQLite migrations.
- Create `forwin/world_model_v4/repository.py`: read/write API for v4 rows.
- Create `forwin/world_model_v4/projection.py`: rebuild cognition/world snapshots and derived `EntityState` / `CanonEvent` summaries.
- Create `forwin/world_model_v4/compiler.py`: sole v4 canon writer.
- Create `forwin/planning/world_contracts.py`: arc/band/chapter world contract builders and validators.
- Modify `forwin/orchestrator/phase24.py`: generate v4 planning contracts beside existing arc/band/chapter plans.
- Modify `forwin/protocol/context.py` and `forwin/context/assembler.py`: add v4 context fields and writer-safe filtering.
- Modify `forwin/protocol/writer.py` and `forwin/writer/prompts.py`: add optional v4 self-report fields and prompt guidance.
- Create `forwin/extractor/world_v4.py`: extract actual world/cognition/reveal/reader-experience changes from writer output and chapter body.
- Create `forwin/reviewer_v4/*.py`: cognitive, world-delta, reveal, and reader-cognition reviewers.
- Modify `forwin/protocol/review.py`: extend `ReviewVerdict` and `RepairInstruction`.
- Modify `forwin/orchestrator/loop.py`: replace direct canon application with extractor -> v4 review gate -> compiler.
- Modify `forwin/retrieval/broker.py`: produce role-specific context packs.
- Create `forwin/api_world_model_v4_routes.py` and modify `forwin/api_route_registry.py`: v4 debug/API endpoints.
- Create tests under `tests/test_world_v4_*.py`, `tests/test_planning_world_contracts.py`, `tests/test_world_v4_review_gate.py`, and `tests/test_world_v4_e2e.py`.

## Phase 1: Domain Model / Schema Redesign

### Task 1: Define v4 Protocol Models

**Files:**
- Create: `forwin/protocol/world_v4.py`
- Test: `tests/test_world_v4_protocol.py`

- [ ] Write tests for model validation:
  - `WorldDelta` accepts visible, offscreen, hint, knowledge, and reveal delta kinds.
  - `Belief` supports true, false, partial, unknown, outdated, manipulated, suspected, disputed, confirmed, and rejected states.
  - `KnowledgeGap` can represent reader hidden, reader hinted, protagonist suspected, and later closed states for the homeworld-siege scenario.
  - Run: `pytest tests/test_world_v4_protocol.py -v`
  - Expected: FAIL because `forwin.protocol.world_v4` does not exist.
- [ ] Implement Pydantic enums and DTOs:
  - Observer: `reader`, `character`, `faction`, `group`, `system`.
  - Ledger objects: `WorldLine`, `WorldDelta`, `Belief`, `CognitionState`, `KnowledgeGap`, `RevealEvent`, `KnowledgeUpdateEvent`, `ReaderExperienceDelta`, `WorldModelSnapshot`.
  - Compile DTOs: `ExtractedWorldChangeSet`, `ApprovedWorldChangeSet`, `WorldCompileRequest`, `WorldCompileResult`.
- [ ] Run: `pytest tests/test_world_v4_protocol.py -v`
  - Expected: PASS.

### Task 2: Add v4 ORM Rows and Migrations

**Files:**
- Create: `forwin/models/world_v4.py`
- Modify: `forwin/models/base.py`
- Test: `tests/test_world_v4_schema.py`

- [ ] Write migration tests that initialize a fresh SQLite database and assert these tables exist: `world_lines`, `world_deltas`, `beliefs`, `cognition_snapshots`, `knowledge_gaps`, `reveal_events`, `knowledge_update_events`, `reader_experience_deltas`, `world_model_snapshots_v4`, `world_compile_runs_v4`, `arc_world_contracts`, `band_world_contracts`, `chapter_world_delta_intents`.
- [ ] Implement SQLAlchemy rows with JSON fields only where the value is naturally nested, such as observer maps, evidence refs, fairness requirements, and source refs.
- [ ] Register the model module in metadata import paths and add forward-only `ALTER TABLE` / `CREATE TABLE` migration logic in `upgrade_db`.
- [ ] Run: `pytest tests/test_world_v4_schema.py -v`
  - Expected: PASS.

### Task 3: Implement Repository and Snapshot Projection

**Files:**
- Create: `forwin/world_model_v4/repository.py`
- Create: `forwin/world_model_v4/projection.py`
- Test: `tests/test_world_v4_repository.py`

- [ ] Write tests that append two world deltas, append a hint event, rebuild a snapshot, and verify the ledger rows remain append-only.
- [ ] Implement repository methods:
  - `create_world_line`
  - `append_world_delta`
  - `append_belief`
  - `create_or_update_gap`
  - `append_reveal_event`
  - `append_knowledge_update`
  - `append_reader_experience_delta`
  - `get_snapshot_as_of_chapter`
- [ ] Implement projection functions that rebuild `WorldModelSnapshot` and `CognitionState` from ledgers without mutating ledger rows.
- [ ] Run: `pytest tests/test_world_v4_repository.py -v`
  - Expected: PASS.

### Task 4: Bootstrap Initial World Model

**Files:**
- Create: `forwin/world_model_v4/bootstrap.py`
- Modify: `forwin/book_genesis.py` only at the v4 handoff boundary
- Test: `tests/test_world_v4_bootstrap.py`

- [ ] Write tests that create a project through existing genesis flow, bootstrap a v4 world model, and verify a `primary_visible_line` plus initial reader/protagonist observer records exist.
- [ ] Implement bootstrap so it reads current project/genesis/canon summaries and seeds only the initial objective state; do not attempt lossless migration of old projects.
- [ ] Run: `pytest tests/test_world_v4_bootstrap.py -v`
  - Expected: PASS.

## Phase 2: Planning Contract / Writer Protocol Redesign

### Task 5: Add Arc, Band, and Chapter World Contracts

**Files:**
- Create: `forwin/planning/world_contracts.py`
- Reuse rows from: `forwin/models/world_v4.py`
- Test: `tests/test_planning_world_contracts.py`

- [ ] Write tests for `ArcWorldContract`, `BandWorldContract`, and `ChapterWorldDeltaIntent` persistence and retrieval.
- [ ] Implement contract objects covering world lines, hidden lines, knowledge gaps, reveal ladder, false beliefs, reader cognition trajectory, payoff windows, and `must_not_reveal`.
- [ ] Store contracts in dedicated v4 rows linked to existing `ArcPlanVersion`, `BandExperiencePlan`, and `ChapterPlan`; do not overload `experience_plan_json`.
- [ ] Run: `pytest tests/test_planning_world_contracts.py -v`
  - Expected: PASS.

### Task 6: Integrate Contracts into Phase 2/4 Planning

**Files:**
- Modify: `forwin/orchestrator/phase24.py`
- Modify: `forwin/director/arc_director.py`
- Test: `tests/test_planning_world_contracts.py`

- [ ] Write tests that an arc plan for "new colony + homeworld crisis" creates:
  - foreground colony line
  - hidden homeworld siege line
  - `gap_homeworld_siege`
  - planned chapter 22 hint, chapter 25 partial reveal, chapter 28 closure
- [ ] Implement contract generation beside existing `ReaderPromise`, `ArcPayoffMap`, `BandDelightSchedule`, and `ChapterExperiencePlan`.
- [ ] Keep old delight structures as compatibility summaries derived from reader cognition transitions.
- [ ] Run: `pytest tests/test_planning_world_contracts.py -v`
  - Expected: PASS.

### Task 7: Add v4 Context Fields and Writer-Safe Filtering

**Files:**
- Modify: `forwin/protocol/context.py`
- Modify: `forwin/context/assembler.py`
- Modify: `forwin/retrieval/broker.py`
- Test: `tests/test_world_v4_context_pack.py`

- [ ] Write tests that chapter 23 writer context includes乱码通讯、旧部呼号、`must_not_reveal=father_sieged`, but does not include direct objective text saying the father is surrounded.
- [ ] Add context fields for active world lines, visible world lines, hidden world lines, active gaps, planned reveal ladder, reader cognition state, character cognition states, observer visibility states, promise debts, and recent reader experience deltas.
- [ ] Implement writer-safe filtering in the retrieval broker so hidden objective truth is present in review/compiler packs but excluded from writer packs unless the chapter intent permits hint/reveal.
- [ ] Run: `pytest tests/test_world_v4_context_pack.py -v`
  - Expected: PASS.

### Task 8: Upgrade Writer Output Contract

**Files:**
- Modify: `forwin/protocol/writer.py`
- Modify: `forwin/writer/prompts.py`
- Test: `tests/test_writer_split_pipeline.py`
- Test: `tests/test_world_v4_writer_protocol.py`

- [ ] Write tests that legacy writer output still parses and that v4 optional fields parse when present.
- [ ] Add optional writer self-report fields: `world_deltas`, `belief_updates`, `knowledge_gap_updates`, `reveal_events`, `reader_experience_deltas`, `observer_visibility_updates`, `must_preserve_facts`, and `must_not_reveal_violations`.
- [ ] Update prompts to ask for preliminary structured intent without promising that writer self-report enters canon.
- [ ] Run: `pytest tests/test_writer_split_pipeline.py tests/test_world_v4_writer_protocol.py -v`
  - Expected: PASS.

## Phase 3: Extractor / Reviewer / Compiler Redesign

### Task 9: Implement Actual Change Extractor

**Files:**
- Create: `forwin/extractor/world_v4.py`
- Test: `tests/test_world_v4_extractor.py`

- [ ] Write tests that extractor reads a chapter body plus `WriterOutput` and emits actual world deltas, belief updates, reveal events, reader experience deltas, and gap updates.
- [ ] Implement extraction from body text, scene outputs, state changes, new events, thread beats, time advance, writer notes, lore candidates, timeline hints, and chapter intent.
- [ ] Return `ExtractedWorldChangeSet` with source refs back to body spans or writer fields.
- [ ] Run: `pytest tests/test_world_v4_extractor.py -v`
  - Expected: PASS.

### Task 10: Implement v4 Reviewers

**Files:**
- Create: `forwin/reviewer_v4/cognitive.py`
- Create: `forwin/reviewer_v4/world_delta.py`
- Create: `forwin/reviewer_v4/reveal.py`
- Create: `forwin/reviewer_v4/reader_cognition.py`
- Create: `forwin/reviewer_v4/gate.py`
- Test: `tests/test_world_v4_review_gate.py`

- [ ] Write tests for these failures:
  - protagonist acts on unknown father-sieged truth
  - mother world falls without source type
  - chapter 23 directly reveals a chapter 25 reveal
  - chapter only opens new questions while accumulated promise debt has no payoff plan
- [ ] Implement reviewers that return fail/warn/pass issues with evidence refs and repair patches.
- [ ] Implement a gate aggregator that blocks compiler input on fail and preserves warnings for audit.
- [ ] Run: `pytest tests/test_world_v4_review_gate.py -v`
  - Expected: PASS.

### Task 11: Upgrade Review Verdict and Repair Instruction

**Files:**
- Modify: `forwin/protocol/review.py`
- Modify: `forwin/reviewer/hub.py`
- Test: `tests/test_world_v4_review_gate.py`
- Test: `tests/test_governance_review_and_checkpoint.py`

- [ ] Write tests that existing review flow still produces a verdict and that v4 review gate can attach `required_delta_patch`, `required_belief_patch`, `required_hint_patch`, `required_payoff_patch`, `must_preserve`, and `must_not_reveal`.
- [ ] Extend `RepairInstruction` with repair scope values `scene`, `chapter`, `band`, `arc`, and `world_model`.
- [ ] Extend verdict payloads with extracted actuals, approved refs, rejected refs, and compiler gate status.
- [ ] Run: `pytest tests/test_world_v4_review_gate.py tests/test_governance_review_and_checkpoint.py -v`
  - Expected: PASS.

### Task 12: Implement WorldModelCompiler

**Files:**
- Create: `forwin/world_model_v4/compiler.py`
- Modify: `forwin/state/updater.py`
- Test: `tests/test_world_v4_compiler.py`

- [ ] Write tests that an approved change set writes world/cognition/gap/reveal ledgers, rebuilds snapshots, and emits derived `EntityState` and `CanonEvent` summaries.
- [ ] Write tests that a failed review verdict blocks compile and records no ledger rows.
- [ ] Implement compiler as the only v4 canon writer, with compile run audit, source refs, forced accept reason, and derived projection calls.
- [ ] Refactor `StateUpdater` usage so it is callable only from projection/materialization code for v4 paths.
- [ ] Run: `pytest tests/test_world_v4_compiler.py -v`
  - Expected: PASS.

### Task 13: Switch Orchestrator Canon Commit Path

**Files:**
- Modify: `forwin/orchestrator/loop.py`
- Modify: `forwin/reviewer/context_builder.py`
- Test: `tests/test_world_v4_orchestrator_gate.py`

- [ ] Write tests that accepted chapter flow calls extractor, v4 reviewers, and compiler before any derived canon state is written.
- [ ] Replace direct `_apply_canon_candidate` behavior with `extract -> review gate -> repair or compile`.
- [ ] Preserve existing repair loop behavior, but attach v4 repair instructions when the gate fails.
- [ ] Run: `pytest tests/test_world_v4_orchestrator_gate.py tests/test_continue_project_orphan_review.py -v`
  - Expected: PASS.

## Phase 4: Runtime / Retrieval / API / Debug / Export

### Task 14: Add Role-Specific Retrieval Packs

**Files:**
- Modify: `forwin/retrieval/broker.py`
- Modify: `forwin/protocol/context.py`
- Test: `tests/test_world_v4_retrieval_packs.py`

- [x] Write tests for `PlanningPack`, `WritingPack`, `ReviewPack`, `CompilerPack`, `ReaderExperiencePack`, `CognitionPack`, and `RevealPack`.
- [x] Ensure writer pack excludes hidden objective truth while review/compiler packs include objective truth and planned reveal ladder.
- [x] Run: `pytest tests/test_world_v4_retrieval_packs.py -v`
  - Expected: PASS.

### Task 15: Upgrade Provisional / Projection Semantics

**Files:**
- Modify: `forwin/models/phase.py`
- Modify: `forwin/orchestrator/phase24.py`
- Create: `forwin/world_model_v4/provisional.py`
- Test: `tests/test_world_v4_provisional.py`

- [x] Write tests that planned and provisional deltas can be generated for arc/band pressure tests but do not enter actual canon.
- [x] Implement explicit states: `actual_state`, `planned_projection`, and `provisional_projection`.
- [x] Require compiler promotion for any provisional delta to become actual.
- [x] Run: `pytest tests/test_world_v4_provisional.py -v`
  - Expected: PASS.

### Task 16: Add v4 API and Debug Endpoints

**Files:**
- Create: `forwin/api_world_model_v4_routes.py`
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/api_schemas.py`
- Test: `tests/test_world_v4_api.py`

- [x] Write API tests for active world lines, hidden lines, open gaps, planned reveals, accepted/rejected deltas, reader cognition, protagonist beliefs, and promise debts.
- [x] Implement read-only debug endpoints first; mutating endpoints remain compiler-only.
- [x] Register routes through the existing route registry.
- [x] Run: `pytest tests/test_world_v4_api.py -v`
  - Expected: PASS.

### Task 17: Add Derived Wiki / Obsidian Export

**Files:**
- Create: `forwin/world_model_v4/export.py`
- Test: `tests/test_world_v4_export.py`

- [x] Write tests that export pages include actual world state, objective timeline, world lines, delta sources, reader cognition, character cognition, knowledge gaps, reveal ladder, fair misdirection, and review checks.
- [x] Ensure each exported page records `state_layer`, `world_line_id`, `as_of_chapter`, `as_of_story_time`, `visibility`, `truth_relation`, and `source_refs`.
- [x] Run: `pytest tests/test_world_v4_export.py -v`
  - Expected: PASS.

### Task 18: Add End-to-End Homeworld Crisis Scenario

**Files:**
- Test: `tests/test_world_v4_e2e.py`

- [x] Build a deterministic test fixture for Arc 2: new colony and homeworld crisis.
- [x] Assert chapter 21 keeps reader/protagonist hidden/unknown while offscreen siege begins.
- [x] Assert chapter 22 changes reader to hinted and protagonist to suspicious through communication interference.
- [x] Assert chapter 23 allows乱码 and old-call-sign hint but blocks direct father-sieged reveal.
- [x] Assert chapter 25 partial reveal updates reader and protagonist cognition while gap remains partially closed.
- [x] Assert chapter 28 full reveal closes the gap and opens the next long-term desire.
- [x] Run: `pytest tests/test_world_v4_e2e.py -v`
  - Expected: PASS.

## Legacy Module Decisions

- `forwin/models/event.py`: keep `CanonEvent` as a derived compatibility summary.
- `forwin/models/entity.py`: keep `EntityState` as materialized current view, not source of objective truth.
- `forwin/state/updater.py`: retain as projection helper after Phase 3.
- `forwin/reviewer/hub.py`: keep as legacy facade until v4 gate fully owns canon safety.
- `forwin/protocol/experience.py`: keep reward tags and schedules as secondary metadata derived from reader cognition transitions.
- `forwin/models/phase4.py`: treat `NPCIntentSnapshot` and `WorldSimulationTurn` as pressure/offscreen source candidates, not canon truth.
- Existing missing v3 world-model source should not be reconstructed from `.pyc`; rebuild v4 from source modules above.

## Test Suite Rewrite Policy

- Keep tests for genesis, runtime task state, subworld control, writer split pipeline, governance checkpoints, and API route registration where behavior remains valid.
- Rewrite tests that assume `CanonEvent` or `EntityState` is the core source of truth.
- Add v4 tests before switching runtime canon paths.
- Run phase-level suites before merge:
  - Phase 1: `pytest tests/test_world_v4_protocol.py tests/test_world_v4_schema.py tests/test_world_v4_repository.py tests/test_world_v4_bootstrap.py -v`
  - Phase 2: `pytest tests/test_planning_world_contracts.py tests/test_world_v4_context_pack.py tests/test_world_v4_writer_protocol.py -v`
  - Phase 3: `pytest tests/test_world_v4_extractor.py tests/test_world_v4_review_gate.py tests/test_world_v4_compiler.py tests/test_world_v4_orchestrator_gate.py -v`
  - Phase 4: `pytest tests/test_world_v4_retrieval_packs.py tests/test_world_v4_provisional.py tests/test_world_v4_api.py tests/test_world_v4_export.py tests/test_world_v4_e2e.py -v`

## Acceptance Gates

- Phase 1 is complete when the system can persist and rebuild the homeworld-siege hidden/hinted/suspected/partial/closed cognition sequence without using `CanonEvent` as truth.
- Phase 2 is complete when chapter 23 can be planned with visible, offscreen, hint, knowledge, reveal, false-belief, reader-experience, and `must_not_reveal` intent.
- Phase 3 is complete when WriterOutput cannot directly mutate canon and failed v4 review blocks compiler commit.
- Phase 4 is complete when the Arc 2 chapter 21/22/23/25/28 scenario runs end-to-end and produces ledgers, snapshots, derived views, review history, and debug/export output.

## Commit Boundaries

- Commit 1: v4 protocol models and tests.
- Commit 2: v4 ORM rows, migrations, repository, and snapshot projection.
- Commit 3: bootstrap and Phase 1 acceptance scenario.
- Commit 4: planning contracts and contract persistence.
- Commit 5: context pack, retrieval filtering, and writer protocol upgrades.
- Commit 6: extractor and v4 reviewers.
- Commit 7: review verdict/repair instruction upgrade.
- Commit 8: compiler and projection integration.
- Commit 9: orchestrator canon gate switch.
- Commit 10: role-specific retrieval, provisional shadow layer, API/debug/export, and E2E.
