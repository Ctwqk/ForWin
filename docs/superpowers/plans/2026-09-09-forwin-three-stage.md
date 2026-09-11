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
- [ ] Run approximately 20-chapter offline smoke, then fresh 100-chapter offline project with supported automatic pause/delegation policy. Isolated c62 smoke project `693a1a7df3124c1088f2201e9581f023`, task `08104286cb5b`, requested 20; accepted 1–5 and safely paused during chapter 6 after a proven rule-lifecycle context bug. The source fix passed independent 99-test regression; the old run was not patched or called a quality pass. Time/location model omissions remain documented; ending and L100 are unverified. New candidate verification follows.
- [ ] Verify finite repair, recovery idempotency and intended ending; record failures honestly. Use isolated publisher adapters for serial safety.
- [ ] Close Stage 1 only with actual evidence; do not mechanically continue a failed candidate as the same release identity.

### Task 7: Behavior-preserving owners and experiments (Stage 2)

**Files:** `forwin/generation/pipeline.py`, `pipeline_core/`, `review/repair/`, current runtime collaborators and fixed-response fixtures.

- [x] Freeze Writer behavior and replace both real callers with the concrete execution owner; delete the old mixin. `de3d854`: 344 related tests, independent 110, root 73 (overlapping).
- [x] Complete Review/Repair and Canon preparation separation from full Pipeline callbacks; replace callers and remove obsolete entries. `59b2bbb`: 387 related tests, independent 153; original transactions, budget, pause and trace behavior retained.
- [x] Replay A1/A2 with the same nine frozen response keys and independent labels. A1 keep after two known severe misses; A2 insufficient, retained. A3's consumer/version proof is insufficient; maintenance cadence and barrier stay unchanged.
- [x] Evaluate A4 condition: later smoke revealed 8 Writer calls and an unblocked scene/stitch time contradiction. A paired single/scene quality comparison remains absent; insufficient evidence to replace Scene, no permanent second writer. See the smoke report.
- [x] Record measured and missing quantities explicitly. Offline calls/input characters measured; real tokens, latency and narrative preference unmeasured. No claimed ≥15% production benefit or real-model paired trial. See [ablation report](../reports/2026-09-09-stage2-ablation.md), `45faa90`.
- [x] Run final full regression after integration. Frozen `1f9a9ad` (tree `fac2a1ca6161`): 2986 passed, 4 skipped, 6 subtests passed / 325.15s; imports use the archived source. Compileall, F/E9 and the source guard pass. Full Ruff: 1035 versus baseline 1219, zero new path/code/message diagnostic signatures. No production experiment flags or alternative implementations were retained. Long-run, final browser image and deployment acceptance remain separate open items.

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

### Task 10: External review follow-up before final candidate acceptance

- [x] Reproduce pending-revision pollution through real accepted history; bind summaries, notes, Arc, pacing and Band checks to active Canon identities. Preserve missing-evidence blocking and ordinary unaccepted draft reads.
- [x] Reproduce world-edit acceptance without successor validation, including future envelopes mutating historical shared metadata. Restrict that entry to pre-acceptance projects under the Canon project lock; share proposal chapter parsing and recheck concurrent revisions.
- [x] Fix the manual CQ replay reader using the same active version contract; four actual sole-Canon acceptance regressions and 39 related replay tests pass.
- [x] Invalidate reuse of stale Band checkpoint approval after an accepted revision; current inputs and deterministic results bind to existing events. Maintain recovery, read models, continue, human/Spark approval and short transaction boundaries. Core 23 PG cases, 13 action cases and 3 real caller cases; root combined 117 passed, independent action/caller 16 passed. Preserve continue's intentional skip without accepting existing failures.
- [x] Freeze all review fixes and run whole-candidate regression and role-image verification. `a2f780b` / tree `b09e09b06597`: 3063 passed, 4 skipped, 6 subtests; compile/F/E9/source guard clean, default Ruff 1119 versus original 1219 with zero new signatures. Both complete role images and actual browser launch verified; exact identities in the failure report below.
- [x] Start a fresh L100 on that verified candidate, preserving the held pre-writing run separately. Task `e022cbdf8cf5` accepted 1–2 then safely paused on a proven BODY/map contradiction. This completes the startup action, not Task 6 or long-run acceptance; follow Task 11.

### Task 11: Repair the L100 map handoff and review input loss

The frozen `a2f780b` sample safely paused with chapters 1–2 accepted. Chapter 2's final BODY traverses a locked 10-minute route in two minutes and a 15-minute route in the same minute. A pure replay of the actual Genesis input reproduced generated `portal` edges taking 0.01 hours; the accepted Writer artifact uses two unresolved scene locations and an unparsed 13-minute duration. This is failed L100 evidence, not a completed acceptance run.

Architecture: keep BookMap as the runtime owner. Its existing generator imports explicit Genesis node routes, preserving source references, direction, numerical hours and verbatim procedural costs. Only maps without explicit node routes use procedural generation. Do not invent connectors, geography or supernatural transport to make a supplied topology connected. Existing deployed maps and frozen samples are not rewritten. Reviewer and Writer consume the same route evidence; unsupported time/location prose remains unknown instead of acquiring a guessed hard rule.

Files: `forwin/map/{protocol,genesis_adapter,generator,service,validator}.py`, `forwin/genesis/handoff/map_bootstrap.py`, `forwin/context/assembler_core/{map_context,book_state_overlay}.py`, `forwin/review/{map_movement,llm_webnovel}.py`, `forwin/writer/prompt_core/sections.py`, `forwin/utils/duration.py`, `tests/test_genesis_route_contract.py` and current design.

- [x] Reproduce before changes: 10 failures / 3 controls in the route/context/duration tests; an additional cross-world test proves pair deduplication loses an independent route. Preserve immutable BODY/Genesis/artifact hashes under the run's `map-diagnosis/` directory.
- [x] Import authored local edges in the existing generator; pass authored cross-world edges to the same persistence owner with their original endpoints. Empty authored interconnections must not create default gates. Preserve the explicit procedural API for maps that actually request generation.
- [x] Share a finite explicit-duration parser (Chinese/Arabic minutes and compound hours), preserving unknown prose. Include route constraints in the existing Writer overview and BODY review evidence, without adding a new LLM gate or bypassing residual/Canon checks.
- [x] Verify actual PG persistence and full handoff: exact cross-world endpoints, multiple same-world-pair routes, direction, missing-reference refusal, sparse authored graphs and unchanged procedural generation. Hidden/access fields persist and Writer filtering remains; disconnected assembly rolls back. Independent PG review also proved failed expansion preserves prior map, region and generation rows after commit/new Session. Structural weak connectivity is order-independent; runtime reverse travel still fails for a one-way route. Related group: 78 passed, including Chinese compound durations and clipped Writer route evidence.
- [ ] Verify the real frozen atlas through the fixed pure owners, preserving original BODY and all old outcomes. Run relevant regression, full frozen-source QA and source/lock-exact role images. Update current design and the finite failure report with limitations, including unresolved free-form scene locations.
  - Actual atlas/BODY replay verified in frozen `a141d0a`, but its whole suite had one feedback fixture timing failure (3104 passed). Captured host/PG clock skew caused the unchanged effect owner to correctly reject after-comment evidence. Align only the simulated platform/ingestion clock with PostgreSQL, preserve failure evidence, then freeze again. Do not rerun-to-green or launch that prepared smoke.
- [ ] Start a separately frozen smoke/L100 candidate only after this package passes. No resume, policy relaxation, manual chapter release, production data rewrite or deployment qualification may turn the paused old sample into a pass.

Verification commands: `.venv/bin/python -m pytest -q tests/test_genesis_route_contract.py tests/test_map_genesis_adapter.py tests/test_map_generation.py tests/test_map_world_integration.py tests/test_map_pathfinding.py tests/test_map_cognition_path.py tests/test_reviewer_split.py`; then the existing frozen-source QA/image procedure from Task 7. Run locally under executing-plans; do not create another user task or duplicate the observer.

### Task 12: Preserve Genesis reference facts through final model inputs

- [x] Reproduce the accepted smoke BODY's historical-date drift against the frozen Genesis revision and actual Writer requests; retain the safely paused sample and terminal exports.
- [x] Extend the existing Genesis context provider with one bounded, verbatim reference projection: world history, root axioms and named character secrets, each with its original field path. Carry the same projection and revision through chapter/review packs, all writing prompts and the main final-BODY review payload/evidence index. Keep whole facts or mark omissions; do not silently clip facts or dump the complete Genesis pack.
- [x] Preserve the distinction between author-only background, character knowledge, reveal permission, future intentions and accepted current state. Do not add a new checker, gate, model call or story-specific date rule.
- [x] Test the real provider → assembler → final messages, including different historical events, literal quoted claims, hidden information, absent/malformed input and bounded omissions. Reproduce red before implementation, then run related regression and independent review.
- [ ] Update the current design and failure report, freeze the reviewed source, run full QA and verify both role images. Start a new smoke, followed only after acceptance by a fresh separate L100; the failed sample cannot qualify the replacement source.
  - `03143ef` completed 3123 passed / 4 skipped / 6 subtests and both exact role images. Fresh smoke preparation reached Genesis Map but no writing task: a route permission/risk input loss was found during review. Preserve this engineering pass and the held pre-writing run; follow Task 13 before another candidate starts.

### Task 13: Preserve authored route permission and risk text

The verified `03143ef` candidate has no chapter task in its fresh smoke yet. Its canonical Map contains six routes with twelve `access/risk` strings, but independent replay retains none in BookMap, Writer or main review. This is a pre-writing input-contract defect, not a failed BODY. Preserve that held run and its passing engineering evidence.

- [x] Reproduce access/risk loss, including existing control/hazard, coexisting distinct values, hidden routes and actual final Writer/reviewer messages. Ten corrected contract assertions failed before implementation; the initial incomplete test fixtures were fixed before recording that red result.
- [x] Normalize these original strings once in the existing Genesis edge adapter. Reuse the same result in Genesis preview; retain existing BookMap metadata and render both permission and risk in Writer. Do not invent access-rule IDs, risk scores, permissions granted or incidents that occurred.
- [x] Verify local and cross-world persistence, independent review and the same immutable actual Map replay. Update current design and the map report. Related 192 passed; independent 52 passed (overlapping). Exact same-input replay at five layers improves 0/12 to 12/12 source conditions, travel cost remains 6/6. This proves input transport, not model compliance.
- [ ] Freeze new source, full QA and both exact role images; start a fresh smoke before a separate L100. No runtime patch or reuse of held generated material as new qualification.

### Task 14: Complete the current repair contract at the Writer boundary

The frozen `0b3c49a` smoke stopped at chapter 3 after three ordinary repairs; only chapters 1–2 were accepted. Keep that sample and its terminal BODY, identities, costs and gates. A pure frozen-source replay and independent review found that all five Writer prompts receive only the first three `must_fix` entries and omit `must_preserve` / repair-specific `must_not_reveal`. This transport defect does not explain every narrative failure: the actual prompts still contain the accepted folder-state invariant, and a reviewer-suggested expansion can itself conflict with an earlier, finer-grained fact.

**Design:** Give the current rewrite a transient typed view of the same three contract lists used by verification. Attach it after every plan rebuild and account for it in the existing soft context budget. Render all entries through one section shared by single, preview, breakdown, scene and stitch. Stop persisting generic repair instructions as cumulative experience-plan anchors. Keep existing countdown constraints, Canon priority, finite retries, verification and residual gates. Do not promote every earlier `must_fix` into a permanent rule or add a new approval/quality mechanism.

- [x] Preserve terminal evidence and reproduce missing fourth fix, preservation and secrecy entries on frozen source without model calls.
- [x] Add failing owner-to-prompt tests for all repair scopes, complete contract coverage, replacement/isolation and budget accounting. Initial 21 failed / 1 passed, then complete coverage and replacement regression pass.
- [x] Implement the transient contract in existing protocol, plan-patch, retrieval and prompt owners; independently review and run related regressions. Root 108 + 39 and independent 30 tests passed (overlapping); [evidence and limits](../reports/2026-09-10-repair-writer-contract.md).
- [ ] Freeze the new candidate, run complete QA and both role-image checks, then use a fresh smoke before a separate L100. Never resume the failed sample as qualification.

### Task 15: One explicit Genesis route contract

The fresh `5524e36` candidate passed 3156 tests and both complete role-image checks, but its new project is held before Map lock and writing. The actual accepted Map payload represents eight durations as strings in `travel_time`, thirteen access conditions as a `constraints` list and eight transport modes in `mode`. Frozen production-owner replay and independent review prove that all these route-bound facts disappear while eight risk strings survive. The generator only requested an arbitrary edge dictionary, so this is a producer/consumer contract gap, not evidence that the model violated an existing schema. Keep this writing-free sample and its exact source payload; do not rename its fields to make the candidate appear qualified.

**Design:** Introduce one typed Genesis route and one parser shared by generation validation, complete/targeted refinement validation, Map lock, preview and materialization. Specify canonical endpoints, direction, visibility, permission reference, duration text, mode, condition strings and risk strings. Interpret already supported legacy forms only in this parser; retain original revision dictionaries and source provenance. Numeric legacy `travel_time` remains hours; unit-bearing strings use the existing finite duration parser, and unqualified numbers/ranges remain unknown. Multiple duration sources must agree; unsupported fields/types and contradictions produce path-specific errors before a new revision or BookMap is accepted. Existing legacy reads remain available. Invalid authored routes must not become an empty/procedural fallback map. Preserve existing topology, visibility, Canon and writing-quality boundaries; do not rebuild deployed BookMaps.

- [x] Preserve the canonical revision and reproduce the complete source-to-preview/import/Writer loss without DB or model calls; independently review the producer schema and owner chain.
- [x] Write failing transport, numeric/text/conflict, unsupported-field, hidden-route, persistence and generation/refinement/lock regressions. Initial 28 failed / 1 passed; subsequent independent review added primitive, parent-reference, coarse visibility, missing-route and preview regressions. Incomplete test fixtures were corrected before checking their behavior.
- [x] Implement the shared route model/parser; make producer prompts/schema and existing write boundaries explicit, remove duplicate route interpretations, and retain legacy source evidence.
- [x] Verify regressions and the unchanged actual-source replay; root related suite 144 passed and independent route suite 98 passed (overlapping), with no remaining blocker. Same actual Map retains 8/8 durations, 13/13 conditions, 8/8 modes and 8/8 risks. See [contract report](../reports/2026-09-10-genesis-route-contract.md); no BODY qualification.
- [ ] Freeze a new source candidate, complete whole QA and both images, then a fresh smoke followed by an independent L100. This held Map and all earlier failed samples remain evidence only.

### Task 16: Validate complete Genesis maps before normalization

The fresh `b61a718` candidate passed 3203 tests and both exact role images. Its Map response contains only thirteen `edges`, matching an output schema that accidentally describes only that field. Normalization supplies default geography with three nodes; all twenty-six route endpoint references are unresolved. The sample remains unlocked at revision 6 with no writing task. Preserve its SHA-verified request, response and canonical revision as failure evidence.

**Design:** Define the existing six-part MapAtlas output in full, reuse the route contract, and validate authored identities and references before normalization can substitute defaults. Full generation requires all six fields; complete refinement retains finite legacy route compatibility. A targeted refinement or patch validates the merged map, not the isolated target. Map lock and import share the same reference checks. Historical reads remain available, initial World scaffolds remain possible, and no deployed map or held sample is rewritten.

- [x] Preserve actual artifacts and reproduce unresolved endpoints; add failing complete-output and atomic write tests (18 failed / 1 passed before implementation).
- [x] Implement the complete output contract and shared reference validation without a new map owner or model gate.
- [x] Verify valid maps, malformed structure, dangling/cross-parent references, duplicate identities, explicit empty sections, complete/targeted changes and unchanged rejected revisions. Root related 235 passed; independent final 167 passed (overlapping), no remaining blocker. Current design and [failure report](../reports/2026-09-10-genesis-map-completeness.md) updated.
- [ ] Freeze new source, run complete QA and both images, then a fresh smoke followed by a separate L100 and ending review. Earlier engineering passes and held maps cannot qualify this candidate.

### Task 17: Diagnose accepted continuity failures and remove invented role aliases

The frozen `00610f2` candidate passed 3240 tests and both complete role images. Its fresh smoke accepted chapters 1–3, but independent reading found a changed ledger carrier and reversed refund/approval order. Canonical safe pause was requested during chapter 4; this is failed smoke evidence, not a qualified replacement for L100. Preserve the accepted identities, full BODY, raw model artifacts, finite provider failures and gate results.

**Confirmed code defect:** ordinary unnamed staff actions trigger `bare_role_placeholder_leakage`; an automatic fix then globally replaces `工作人员` with the invented alias `具体见证人`, including structured evidence. The raw Writer result and persisted output prove this mutation. Remove that unsupported semantic replacement and constrain deterministic role-label detection to explicit identity placeholders. Ordinary role descriptions remain valid; genuine missing names and internal state keys still follow the existing review and finite repair path. Keep exact canonical name correction and its re-review unchanged.

**Continuity investigation:** trace the actual writer/reviewer inputs, history selection, extraction and budgeting before choosing a correction. Do not assume that improving role handling fixes the missing chronology, add a novel-specific date rule, relax quality gates or patch the failed runtime.

- [x] Preserve accepted chapters 1–3 and hash-verified original artifacts; request canonical safe pause without restarting the task.
- [x] Reproduce ordinary-role false positives and unsupported global replacement; remove the duplicate semantic rewrite and verify genuine placeholder handling through the review owner. Initial 8 failed / 2 passed; independent findings added identity-slot, word-boundary, role-list and no-op local repair cases. Final root related 103 passed; independent 87 plus 3 canonical-name probes passed (overlapping). Same raw BODY replay verifies 17 ordinary roles were globally replaced only by the old code. See [failure and repair evidence](../reports/2026-09-10-accepted-continuity-failure.md).
- [x] Independently trace the actual history inputs and reproduce the information-loss boundaries offline. Task 18 implements the bounded corrections below; the precise dates/carrier were absent from existing summaries and the first 500 characters of memory, so this diagnosis is not proof that the narrative failure is resolved.
- [ ] After Task 18, freeze a new candidate and repeat whole QA, both images, fresh smoke and separate L100 with ending review.

### Task 18: Preserve existing history through budgeting and prompt rendering

Independent offline diagnosis of `00610f2` confirmed that identical visible Writer inputs lose two memories and one of two summaries solely when 65,000 characters of invisible provenance are added. The main LLM Reviewer drops all already-assembled previous summaries; Writer world-page rendering excludes Current State and can contain only frontmatter. These are input-contract defects in existing owners. Exact BODY chronology was not fully extracted, and the chapter-2 plan already requested a different ledger carrier; these fixes cannot by themselves establish story consistency.

**Scope:** preserve the current bounded context and single reviewer/Writer paths. Do not add a reviewer, new memory store, a novel-specific rule, a higher budget or a production hotfix. Keep `knowledge_system_context` intact as provenance, but exclude it from Writer budget estimation because it is not rendered. Add complete retained summaries to the main reviewer payload and bind evidence IDs to input positions, without inventing absolute chapter numbers. Render only allowed world-page Canon Summary and Current State through the existing shared section; omit frontmatter, manual notes, proposed corrections and hidden-truth pages. Preserve existing reveal guards. Keep memory excerpt behavior unchanged in this work because actual dates are outside the stored excerpt.

- [x] Add a failing same-visible-input regression proving large provenance must not change history retention or any of the five Writer prompt modes; implement the estimator-only correction and preserve metadata/input immutability.
- [x] Add a failing context-builder → reviewer-message regression with a late-summary fact and valid evidence references; wire the complete retained summaries into the existing payload without a second truncation.
- [x] Add failing exported-world-page regressions for summary/state visibility across five Writer modes, hidden/revealed/false-rumor boundaries and omitted manual content; reuse the established visibility and section parsers, with per-page bounded text. Independent review found exported hidden status/tags and stale projection gaps: shared canonical node visibility now protects both export and as-of read copies, map/book source identities are distinct, and future rows are excluded before selection. Newly rendered Current State is limited to individual world/map pages with source identity; mixed-visibility aggregates keep their existing summary only. Root 61 contract tests and 273 related tests pass.
- [x] Run focused and related regressions, independent review and final static checks; update current contracts and failed-sample evidence with precise limitations. Independent follow-up: 84 related tests and two stale-truth probes pass, no remaining finding within patch scope; root F/E9, compilation, new-file Ruff and diff checks pass. Commit the coherent correction before freezing a new release candidate. This does not complete smoke/L100 acceptance.

### Task 19: Preserve provisional scene continuity within the existing Writer

Frozen `1614560` passed 3322 tests and both exact role images, but its new smoke accepted two chapters with inconsistent central project identifiers. It is safely paused with no active generation task. Original stitch, Writer output and accepted BODY hashes agree; the contradiction was already in model output. Independent baseline review confirms that scene calls receive only their own plan, without the rest of the chapter's scene plan or preceding draft. This does not explain every cross-chapter omission.

The naive A4 candidate (existing single plus the same three extraction passes) is rejected before live model testing: a fixed BODY and time contract fails the existing deterministic movement reviewer with two scene locations, but passes after single produces no scene outputs. This is loss of a known check, not evidence that the whole Canon pipeline would accept the chapter. No replacement Writer or weaker quality policy is introduced.

**Bounded correction:** keep the current sequential Scene → stitch → extraction owner. Send the generated scene plans as intentions and the already completed scene prefix as provisional draft evidence to each scene call. Canon, repair preservation and reveal constraints remain authoritative; draft narration is not proof that every character knows it. Reuse the existing scene representation. Limit the additional rendered handoff to the existing chapter maximum character setting, prefer a contiguous suffix of complete preceding scene records, then complete plan records; explicitly report omitted records. Do not silently shorten a fact or claim complete coverage. Preserve execution order, per-call isolation, finite fallback, all scene output metadata, final BODY review and map checks. No additional model call, store, permanent flag or novel-specific rule.

**Files:** `forwin/writer/chapter_writer.py`, `forwin/writer/prompt_core/builders.py`, `tests/test_scene_handoff.py`, current design and existing continuity/ablation reports.

- [x] Preserve canonical pause, BODY identities, original requests/responses, costs and missing evidence; independently check severity and source causality. Pure A4 map-parity probe uses actual Writer extraction and movement owners, four fixture adapter calls, zero real model calls.
- [x] Reproduce the missing plan/prefix through actual Writer-to-prompt calls; initial 5 failed / 1 passed. Cover first scene, preceding-only chronology, repeated chapters/repairs, budget omissions, Canon/reveal priority and unchanged input objects.
- [x] Wire the finite handoff through the existing loop and prompt builder; preserve byte-identical stitch rendering and eight logical calls for three scenes. Nine contract tests and root 162 related tests pass.
- [x] Run focused and related regressions, replay immutable actual scenes to measure added/omitted input, obtain independent review, and update current contracts and limitations. Independent 97 related tests and 400 budget probes pass (test counts overlap), no blocking finding. Nine real recorded inputs add 1213–2992 characters, retain all prior bodies, and explicitly omit two additional plans in each third-scene call; this is not a model-quality replay.
- [ ] Freeze the reviewed candidate; complete fresh full QA and both role images, then fresh smoke20 and independent L100 with ending review. Never resume the failed sample as qualification.

### Task 20: Charge the world context's actual Writer representation once

Task 19's independent baseline probe found that 10,000 extra characters under a world page's Manual Notes change no Writer prompt, yet increase the budget estimate and evict a previous summary. Task 18 excluded the separate knowledge provenance dictionary; full world-page markdown and specialized-list copies are still charged. This is independently reproducible, but the unavailable original runtime pack prevents attributing the smoke's identifier failure to it.

**Bounded correction:** move the existing world-context renderer, without changing its text, limits or visibility semantics, into one leaf Writer module used by both prompt sections and RetrievalBroker's estimate. Only the estimate replaces the full world object with its rendered string; stored pages, hashes, provenance and Reviewer context remain intact. Component accounting includes this representation once and recalculates after page pruning. Keep empty-world accounting, all other estimates, the configured budget and eviction order unchanged. Do not expand memory or introduce a second projection or renderer.

**Files:** new `forwin/writer/world_context.py`, `forwin/writer/prompt_core/sections.py`, `forwin/retrieval/broker_core/broker.py`, `tests/test_world_context_budget.py`, current design and the existing continuity report.

- [x] Reproduce same-prompt/different-retention through real typed contexts and both estimators; initial 6 failed / 5 passed. Twelve final tests cover unrendered markdown, source metadata, hidden content, specialized copies, visible positive controls, input immutability and page pruning.
- [x] Share the unchanged renderer and charge its output once, without changing other context owners or budget thresholds. The additional empty-world regression caught an omitted historical allowance (1870 versus 2130); restore it before independent review and preserve that failed-check evidence.
- [x] Verify old/new prompt equality, focused and related regressions, and independent review. Root 188 related plus 37 additional tests pass; independent 130 related tests and 20 empty-world comparison probes pass, with overlapping test counts and no blocking finding. Commit separately from the scene handoff. At the original fixed budget 2836, equal world views now both estimate 2193 and retain two summaries and one memory.
- [x] Freeze combined source `2df4f4f`: 3343 passed, 4 skipped, 6 subtests; both complete role images pass. The fresh smoke is safely paused after accepted chapters 1–4 because of an important evidence-custody gap between chapters 2 and 3; preserve this failure and do not resume it as qualification.
- [ ] Complete a fresh smoke20, independent L100 and ending review after the diagnosed corrections. Engineering results alone do not qualify the source.

### Task 21: Preserve current BookState across mixed node updates

The new smoke's exact WriterOutput contains an item status change followed by non-schema `contents` stored in metadata. Canonical chapter-2 snapshot exposes that item's status as `active`; the two original field updates replay the same reset offline. `ObjectiveWorldGraph` retains current state in its state index, but a metadata/profile/alias update serializes the stale node-state copy and overwrites that index. The owner is unchanged between `1614560` and `2df4f4f`; this is a separately demonstrated mutation defect, not proof that earlier prompt fixes caused the narrative failure.

**Bounded design:** keep the existing state index authoritative for non-state updates. Existing legal root `state` patches must compare their actual before-image, record versioned state rows, honor explicit empty snapshot values and retain the same delta provenance in revision replicas. Keep old snapshots, deltas, BODY and failed runtime immutable. Do not add another state representation, memory store, model call or story-specific rule. This correction does not by itself make the next Writer select or render the bag's contents.

**Files:** `forwin/book_state/runtime.py`, `compiler.py`, `repository.py`, `forwin/canon/revision_replica.py`; existing runtime, Writer contract, projection and replica tests; current design and continuity report.

- [x] Preserve the single canonical pause, active count 0, terminal accepted identities and raw artifact hashes; independently distinguish the material evidence bridge failure from possible separate volumes and valid travel.
- [x] Reproduce the original two-field reset offline; write failing mixed-update, restored-state, full-state and explicit-empty snapshot tests. Extend the real Writer contract test through review, compilation and reload; old code loses the custody state. Root-state before-image, persistence and revision provenance are tested with existing legal inputs and a negative metadata-source control.
- [x] Correct the existing runtime/compiler/repository/replica owners; no database migration, runtime hotfix, new toggle or extra model step. Keep historical snapshot data unchanged. Correct one existing test's initial-state chapter from 1 to 0 so it precedes compilation; retain every behavior assertion.
- [x] Complete related regressions and independent review: root 143 focused/related and 486 broader BookState/Canon/revision tests pass (overlapping). Independent runtime 13 tests plus stale-before-image, empty snapshot and invalid provenance probes pass, no remaining blocking finding. Commit the coherent owner correction with evidence and limits; final full QA and role images still belong to a new frozen candidate.
- [ ] Trace remaining evidence selection/rendering from retained inputs before choosing another narrative-context change; then freeze the candidate for full QA, images, a fresh smoke and separate L100 with ending review. Never substitute repaired old samples or source-level tests for BODY qualification.

## Initial evidence

`2026-09-09`: `.venv/bin/python -m pytest -q tests/test_v5_live_migration.py tests/test_production_planner.py` → **8 passed**. This is a targeted baseline only, not Stage 1 acceptance.
