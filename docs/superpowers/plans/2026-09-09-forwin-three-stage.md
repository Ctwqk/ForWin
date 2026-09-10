# ForWin Three-Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved reliable production, evidence-led simplification, and feedback roadmap without compromising existing Canon or publication evidence.

**Architecture:** Retain PostgreSQL BookState Canon, durable workers, existing publisher attempts and outbox. Use stable ChapterPlan identity, immutable acceptance revisions and a current acceptance pointer; serialize revision, publication and capacity decisions on the project row. Experiments and live acceptance follow completed prerequisites and preserve their actual outcomes.

**Tech Stack:** Python, SQLAlchemy/PostgreSQL, Alembic, pytest, existing Docker role targets and uv.lock.

**Spec:** `docs/superpowers/specs/2026-09-09-forwin-three-stage-design.md`

## Global Constraints

- Source and all coding activity remain in `/Users/magi1/ForWin-source-github`, branch `codex/three-stage-improvements`; integrate independent reviewed commits into `master`.
- Baseline `521228871a5752ebe8572c057caa9f4944bb0295`; preserve the pre-existing untracked historical-rewrite plan.
- Do not mutate existing production works, publish real content, create public novel repositories or erase historical data.
- Add forward migrations; do not change `0001_v5_baseline.py` or recreate an existing database.
- Frozen public content and unresolved external mutations cannot be replaced. A failed revision leaves original accepted state intact.
- Capacity: `B=min(N,max(3,ceil(N*0.05)))`, `G+R-P<=B`; only explicit daily_serial is constrained by confirmed contiguous primary-platform publication.
- All model/long-run evidence binds a source, image, effective policy and model. Smoke then fresh offline L100 replaces duplicate historical L200 prerequisites.
- P2 signals remain observation-only until their provenance, analysis and downstream consumption contracts pass.

## Work packages and verification

### Task 1: Current documentation and full-source build (P1-0)

**Files:** `README.md`, `AGENTS.md`, `Design-docs/CURRENT_DESIGN.md`, `CURRENT_ARCHITECTURE.md`, `DESIGN_STATUS.md`, `Dockerfile`, `scripts/pre_pr_eval.sh`, new `scripts/check_runtime_source.py`, `tests/test_runtime_source_policy.py`.

**Interfaces:** Build uses repository source and frozen dependencies; source checker reports added production-copy or runtime-patch paths relative to a recorded base. Existing rollback artifacts are inventory-only until full image and compatibility restoration are verified.

- [x] Record baseline source and read-only runtime inventory. App/generation/MCP currently run `compat-6809782`; publisher/outbox/browser run `deploy-d4fceac68343`.
- [x] Save approved spec and make current docs/Agent navigation point to it; explicitly supersede duplicate L200 and destructive migration language.
- [x] First reproduce checker failure for added source-copy / runtime patch and valid ordinary source edits with temporary Git repositories.
- [x] Implement source enforcement in existing pre-PR path and frozen-lock full-image build.
- [x] Verify missing compatibility behavior with source and targeted tests before retiring rollback assembly files. `a0898fe` retires 11 directories after complete-image checks and historical regression; immutable Git history and production rollback images remain.
- [x] Build full role images and record source/image/lock identities; retain rollback evidence until verified. Both `c62f6d0` targets passed actual imports/CLI/browser checks; the unused candidate browser image was subsequently reclaimed for disk capacity, with evidence retained. Final deployment rebuilds its final source.

### Task 2: Stable revisions and publication protection (P1-1)

**Files:** `forwin/models/{project,canon}.py`, new forward migration, `forwin/canon/{admission,historical_rewrite,plan,review_recovery}.py`, `forwin/application/projects/reviews.py`, `forwin/publisher_runtime/{canon_jobs,attempts,receipts}.py`, affected active-commit read consumers.

**Interfaces:** ChapterPlan.id is stable; one active commit pointer; per-chapter acceptance revisions; Project.book_revision detects any mainline switch. Persist public/reserved publication identity independently of task deletion. Shared lock order is Project → Chapter → Job → Attempt.

- [x] Reproduce public/unknown/multiplatform/deleted-job freeze bypass and retry invalidating accepted state.
- [x] Add forward migration preserving original chapter and candidate/receipt references; reject ambiguous archived identities.
- [x] Implement pointer-based effective commit selection and immutable history; remove negative chapter archival and mutation of original evidence.
- [x] Persist external-action protection before mutation; verify active identity on job release/claim/action authorization; reject stale queued payloads.
- [x] Run transaction, migration, publisher receipt/lease and retry regressions, including real PostgreSQL racing transactions.
- [x] Review and commit this package independently.

### Task 3: Isolated full-suffix revision validation (P1-2)

**Files:** new `forwin/canon/revision_validation.py`, existing Canon preparation/admission/rewrite, BookState projection and review components, outbox.

**Interfaces:** A durable result binds base book revision, complete affected range, candidate/content hashes, coverage and evidence; pass/fail/unknown. Final Canon transaction checks identical baseline and publication protection before switching all required revisions.

- [x] Reproduce key removal, knowledge/death/time/place conflicts, wording-only validation, unknown/timeout and partial coverage.
- [x] Build isolated candidate state and re-run existing checks against every successor body; old delta replay alone never earns pass.
- [x] Preserve original deltas/snapshots and construct new acceptance identities where their context changes.
- [x] Atomically switch the validated set and enqueue external work only after admission; inject failures at each transaction stage.
- [x] Verify simultaneous successor edits and publication invalidate prepared results; commit reviewed package. `c62f6d0`; explicit unsupported legacy provenance / changed-origin obligation cases remain unknown, documented in the revision evidence report.

### Task 4: Serial capacity and continued publishing (P1-3)

**Files:** `forwin/production/{policy,planner,backlog,scheduler}.py`, new capacity owner, `forwin/long_run_policy.py`, generation enqueue/worker, Canon admission, forward migration.

**Interfaces:** Explicit primary platform and versioned capacity configuration; task-bound production mode; durable chapter reservations fenced by generation task lease/epoch. Canon admission invokes the same capacity owner under the project lock.

- [x] Tests use literal N/B pairs 60/3, 100/5, 200/10, 500/25 and 1000/50.
- [x] Reproduce publication starvation with an active generation task and accepted queued content.
- [x] Bound enqueue batches, reserve before starting new chapters, revalidate on acceptance; retries reuse a chapter slot.
- [x] Verify concurrent final-slot claims, crash/restart, receipt holes, platform changes, lowered N, excess old backlog and offline exemptions.
- [x] Expose normal capacity wait without failed-generation repair and keep publication planning active; review/commit.

### Task 5: Temporary feedback quarantine

**Files:** production feedback context providers, reviewer inputs, planning/experience/world-simulation consumers, corresponding tests.

- [x] Prove existing stored feedback cannot automatically rewrite plot/global rules or block content before P2 fixes.
- [x] Preserve collection, display and existing rows; neutralize unqualified feedback at automated consumer boundaries without a new permanent mode.
- [x] Test no-comment and existing-feedback production, review and planning; independent review complete, 59 related tests passed.

### Task 6: Frozen candidate acceptance (P1-4)

**Files:** existing long-run harness and operating evidence under `docs/operations/`.

- [x] Run per-package related tests, then full pytest, Ruff and compileall from one source candidate. Frozen `c62f6d0`: 2658 passed, 4 skipped, 6 subtests passed; compileall/F/E9 clean. Full Ruff 1179 versus original 1219, not zero warnings.
- [x] Validate restore/migration in isolation, build full images, record source/image/model/effective policy. The restored production backup passed through `0004_revision_validation`; frozen `c62f6d0` role images passed actual execution checks. Full policy readback changed only three supported automation fields.
- [ ] Run approximately 20-chapter offline smoke, then fresh 100-chapter offline project with supported automatic pause/delegation policy. Isolated smoke project `693a1a7df3124c1088f2201e9581f023`, task `08104286cb5b`, started with `run_until_chapter=20`; first four chapters accepted, ongoing; independent BODY review found time/field continuity defects, so acceptance is not a quality-pass claim. Preserve actual model retries/fallback and usage gaps. L100 not started.
- [ ] Verify finite repair, recovery idempotency and intended ending; record failures honestly. Use isolated publisher adapters for serial safety.
- [ ] Close Stage 1 only with actual evidence; do not mechanically continue a failed candidate as the same release identity.

### Task 7: Behavior-preserving owners and experiments (Stage 2)

**Files:** `forwin/generation/pipeline.py`, `pipeline_core/`, `review/repair/`, current runtime collaborators and fixed-response fixtures.

- [x] Freeze Writer behavior and replace both real callers with the concrete execution owner; delete the old mixin. `de3d854`: 344 related tests, independent 110, root 73 (overlapping).
- [x] Complete Review/Repair and Canon preparation separation from full Pipeline callbacks; replace callers and remove obsolete entries. `59b2bbb`: 387 related tests, independent 153; original transactions, budget, pause and trace behavior retained.
- [x] Replay A1/A2 with the same nine frozen response keys and independent labels. A1 keep after two known severe misses; A2 insufficient, retained. A3's consumer/version proof is insufficient; maintenance cadence and barrier stay unchanged.
- [x] Evaluate A4 condition: later smoke revealed 8 Writer calls and an unblocked scene/stitch time contradiction. A paired single/scene quality comparison remains absent; insufficient evidence to replace Scene, no permanent second writer. See the smoke report.
- [x] Record measured and missing quantities explicitly. Offline calls/input characters measured; real tokens, latency and narrative preference unmeasured. No claimed ≥15% production benefit or real-model paired trial. See [ablation report](../reports/2026-09-09-stage2-ablation.md), `45faa90`.
- [ ] Run final full regression after integration. No production experiment flags or alternative implementations were retained.

### Task 8: Feedback correctness and finite loop (Stage 3)

**Files:** `models/publisher.py`, comment ingest, `audience/{feedback,actions}.py`, `simulation/world.py`, `state/repo.py`, feedback context and planning consumers, forward migrations.

- [x] Separate source chapter/publication revision and observed/ingested/analyzed times from current generation progress. `99dcd6b`, forward migration `0005_comment_analysis`; old provenance stays unknown.
- [x] Query uncompleted analysis before pagination; version completion including zero signals; bounded failures, content-edit hash and scoped idempotency. Exact old input restoration reactivates completed evidence; project binding can be resolved independently of chapter binding. Caller rollback/crash limit documented; automatic consumer transaction integration follows.
- [x] Test 100 comments with batch size 8, zero signals, out-of-order/backfill and duplicate book titles. 75 package/probe tests; independent 54 plus final 32 (overlapping), including both independently reproduced defects.
- [x] One aggregation/decision owner, correct all-comment denominators, distinct scoped authors, prediction and directional action mapping. Independent review fixed stale ORM proof, cross-platform severity borrowing and invalid confidence; risk watchlist and overlapping opposite directions remain observation-only.
- [x] Track proposed/selected/actually-included/applied/observed separately; only record Writer inputs after trimming. Future plans share CAS with existing writers; actual BODY observation defaults unknown and later comparisons remain noncausal.
- [x] Demonstrate one traceable accepted future adjustment and one justified rejection using low/late/conflicting/repeated-single-reader evidence. Five finite cases use real owners including five sole-Canon admissions; model/quality/publication inputs are explicitly frozen fixtures. See [finite-loop evidence](../reports/2026-09-09-stage3-feedback-finite-loop.md).
- [x] Reenable only the qualified canonical Writer provider and bounded future-plan consumer after related regression. Legacy global calibration/world/review effects remain neutral; passive observation errors cannot add a new content gate. Independent integration review: 72 passed; [evidence](../reports/2026-09-09-stage3-feedback-integration.md). Final whole-candidate verification remains in Task 7.

### Task 9: Optional book export

**Files:** existing artifact/outbox owners and proposal import boundary.

- [x] Export Markdown + manifest keyed by book revision with stable chapter/commit/hash/base/receipt references; no raw comments or credentials. Freeze a retained snapshot before IO, with capture-time publication semantics and a read-only rebuild path.
- [x] Test retry deduplication, out-of-order delivery, export failure independence and no frozen-content bypass. Independent review fixed retained BODY-hash contradiction and root-directory replacement; original probes plus Canon/export/Obsidian regression: 96 passed. See [export report](../reports/2026-09-09-novel-export.md).
- Conditional Git management not selected: no demonstrated need beyond the local Markdown/manifest workflow. No repository, remote or direct Canon import was added; existing proposal/admission remains the only editing boundary.

## Initial evidence

`2026-09-09`: `.venv/bin/python -m pytest -q tests/test_v5_live_migration.py tests/test_production_planner.py` → **8 passed**. This is a targeted baseline only, not Stage 1 acceptance.
