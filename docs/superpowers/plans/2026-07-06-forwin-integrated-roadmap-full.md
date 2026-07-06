# ForWin Integrated Roadmap Full Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. User
> approval for this plan is pre-granted by the request.

**Goal:** Implement the full integrated roadmap from
`forwin_integrated_roadmap_phase0.md`, test it, deploy it to the local ForWin
target, and complete 100-chapter real-machine verification.

**Spec:** `docs/superpowers/specs/2026-07-06-forwin-integrated-roadmap-full-design.md`

---

## Task 0: Baseline Inventory

- [x] Confirm Phase 0 code exists and full tests passed before this full-roadmap
  execution.
- [x] Confirm previous 100-chapter Phase 0 verification project has chapters
  1-100 accepted and no active generation task.
- [x] Identify the Phase 1 band scheduler duplicate mid-power defect in current
  code.

## Task 1: Phase 1 Retrieval And Band Scheduler

**Files:**
- Modify: `forwin/experience/band_scheduler.py`
- Modify: `tests/test_trope_selector.py`
- Modify: `forwin/retrieval/memory_index.py`
- Modify: `forwin/config.py`
- Create: `scripts/reembed_memory_index.py`
- Create or modify retrieval tests.

- [x] Add a failing regression for duplicate mid-band `power/micro_progress_power`
  rewards when `boost_reward_density` is active.
- [x] Fix the scheduler blueprint so the same category, slot, and intent is not
  inserted twice.
- [x] Run `pytest tests/test_trope_selector.py tests/test_experience_planning_service.py -q`.
- [x] Add a production semantic embedding backend path that is selected from
  runtime config while preserving explicit `hash` fallback for tests.
- [x] Add a safe reembed script that can rebuild a target Qdrant collection from
  accepted chapter memories.
- [x] Test semantic retrieval ordering and collection-name rollback behavior.

## Task 2: Phase P Pulp BookState Commit Readiness

**Files:**
- Modify: `forwin/writer/chapter_writer.py` or acceptance-time extraction helpers.
- Modify: BookState commit/orchestrator acceptance path.
- Create or modify pulp BookState tests.

- [x] Add a failing test proving single-writer accepted pulp chapters cannot
  leave structured extraction empty.
- [x] Implement lightweight state/event extraction or deferred extraction
  consumption before BookState commit.
- [x] Add tests proving accepted pulp chapters create world-layer BookState facts
  and expose them through the BookState repository.

## Task 3: Phase 2 Arc Activation Review Pack

**Files:**
- Modify: `forwin/book_genesis_core/planning.py`
- Create: arc activation review-pack module and tests.
- Modify DecisionEvent recording where arc planning is invoked.

- [x] Add `ArcActivationReviewPack` construction from accepted summaries,
  character state, obligations, BookState, map/faction snapshots, director
  imbalance, and audience signals.
- [x] Inject the pack into `_plan_arc_chapters` and increase the token budget.
- [x] Add explicit degraded/needs-review tracing for LLM planning failure.
- [x] Add tests proving facts used for planning are recorded in DecisionEvents.

## Task 4: Phase 3 Fatal Admission Profile And Auto-Continue Healing

**Files:**
- Modify: `forwin/canon_quality/gate.py`
- Modify: config quality profile defaults.
- Modify: `forwin/generation/auto_continue.py`
- Create or modify canon admission and auto-continue tests.

- [x] Add pulp/serial fatal admission profile tests for resurrection, rollback,
  duplicate artifact/resource, faction reversal, resource debt mismatch, and
  teleport blockers.
- [x] Implement P0/P1/P2 obligation tier behavior.
- [x] Let auto-continue self-heal eligible soft review blockers.
- [x] Block auto force on hard canon, subworld admission, and active-rule
  failures.
- [x] Add a regression proving `no_rule_matched` is never emitted as a stop
  reason.

## Task 5: Phase 4 Trope Library And Fanqie Defaults

**Files:**
- Modify: `forwin/experience/trope_selector.py` or related selector/library files.
- Modify: `Design-docs/trope_library_pulp_v1.md`.
- Modify: prompt assembly tests.

- [x] Ensure the loaded trope library contains at least 50 templates with
  genre, audience, platform, cost, and payoff metadata.
- [x] Add fanqie/pulp defaults for visible payoff, controlled ambiguity,
  power/status movement, and lower setup cost.
- [x] Inject execution constraints into prompts, not keyword-only hints.
- [x] Add repetition tests: no same sub-trope three times in a 20-chapter window.

## Task 6: Phase 5 Operator UI And Docs

**Files:**
- Modify: `forwin/ui_assets/home/*`
- Modify: API payload builders as needed.
- Modify: `Design-docs/CURRENT_ARCHITECTURE.md`
- Modify: `Design-docs/DESIGN_STATUS.md`
- Create or modify browser/UI tests.

- [x] Add needs-review and repair-exhausted queues with stop reason distribution
  and auto-continue chain health.
- [x] Expose one-click actions for retry, accept soft, register subworld entity,
  genericize background reference, and create obligation.
- [x] Update architecture/status docs to describe the implemented roadmap.
- [x] Run UI/browser regression tests for the home blocker workflow.

## Task 7: Full Verification, Deploy, Real-Machine Run

- [x] Run focused tests for Tasks 1-6.
- [x] Run `.venv/bin/pytest tests -q`.
- [x] Confirm no active generation task with `task_active_generation_check`.
- [ ] Commit and push from `/Users/magi1/ForWin-source-github`.
- [ ] Deploy through:
  `ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`
- [ ] Verify local deployment health for `10.0.0.126`.
- [ ] Use ForWin MCP tools to run or inspect a 100-chapter project and confirm
  chapters 1-100 accepted, no pending review blockers, and no active generation.
