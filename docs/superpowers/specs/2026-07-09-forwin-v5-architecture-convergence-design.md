# ForWin v5 Architecture Convergence Design

**Status:** Approved
**Date:** 2026-07-09
**Repository baseline:** `master` at `107ac08`
**Source audit:** `forwin_architecture_consolidation_audit.md` / revised audit v2

## 1. Decision Summary

ForWin will perform an in-place v5 hard cutover. The implementation will retain proven domain algorithms and production topology, but it will not preserve old project data, old request payloads, old environment variables, old runtime settings, deprecated imports, or transitional execution paths.

The target is not a thinner compatibility shell around the current system. The target is a system where every decision has one owner, every operation has one public path, accepted chapter state has one writer, and entry adapters cannot reach into domain internals.

The work is split into six independently verifiable implementation slices:

1. Runtime policy and application command foundation.
2. Genesis and planning ownership.
3. Chapter review, repair, entity admission, and pipeline ownership.
4. Canon transaction and BookState read/write convergence.
5. Entry adapters, UI, and composition roots.
6. Schema baseline, deletion completion, documentation, and long-run verification.

Temporary intermediate commits may be non-deployable while a slice is being changed. Every completed slice must restore a runnable system and pass its declared verification gates. No compatibility layer may be introduced merely to keep an intermediate commit deployable.

## 2. Audit Evaluation

The revised v2 audit is directionally correct and well evidenced. Its strongest findings are accepted:

- `WritingOrchestrator` is assembled through cross-module method assignment rather than real ownership boundaries.
- runtime behavior is fragmented across too many modes, flags, and configuration stores;
- entity admission has multiple competing decision points;
- accepted chapters are written to BookState and legacy state simultaneously;
- API, worker, MCP, scheduler, CLI, and UI do not share a clean application boundary;
- dead compatibility modules and migration-era behavior remain active in the source tree.

The following audit recommendations are replaced or strengthened.

### 2.1 Hard cut instead of deprecation

The audit proposes one-version shims for several fields and imports. v5 will not keep them. `operation_mode`, checkpoint, copilot, progression mode, old review flags, deprecated API payloads, and old module aliases are removed from the accepted input surface.

### 2.2 Delete the dead orchestration ports

`forwin/orchestration/ChapterPipelinePorts` is not adopted. Its fields are typed as `Any`, it has no consumers, and it does not define enforceable behavior. Typed protocols will live next to their owning domain components.

### 2.3 Remove fail-verdict delegation entirely

The audit proposes a per-gate allowlist that defaults to excluding `chapter_blackbox_failure`. v5 deletes this override path. A candidate with a fail verdict or hard residual is not canon-eligible and therefore never reaches gate delegation.

### 2.4 Entity registration becomes precommit planning

The current `EntityRegistrar` writes `Entity` and `EntityAlias` rows before chapter acceptance. v5 changes it into an admission planner. It produces an immutable `EntityAdmissionPlan`; only the Canon transaction may apply the plan. Rejected drafts cannot pollute canon entities.

### 2.5 State convergence is a separate project-sized slice

Legacy `StateRepository` readers include context, reviewer, repair, governance, preview, retrieval, and API code. They will be split into typed owner-specific queries rather than replaced by another god repository. Legacy writes are removed only after those query contracts are complete.

### 2.6 API and Genesis have the same structural debt

The audit undercounts dynamic composition outside `WritingOrchestrator`. API Core uses wildcard imports, global export injection, and a dynamic module proxy. `BookGenesisService` is also assembled by method assignment over wildcard-imported modules. These structures are first-class deletion targets.

### 2.7 One schema system

The audit omits the duplicate migration systems. ForWin currently has Alembic plus roughly 800 lines of hand-written forward migration logic in `models/base.py`. v5 uses one Alembic baseline for new databases and removes the custom `schema_migrations` system.

### 2.8 Forwarding shims are not zero-risk deletions

`planning/future_plan_auditor.py` is a forwarding module, but production modules still import it. Its consumers must move before the file is deleted.

## 3. Goals and Non-Goals

### 3.1 Goals

- one explicit application service per user or worker operation;
- one chapter pipeline with typed collaborators;
- one entity admission decision point;
- one canon eligibility policy;
- one atomic Canon commit transaction;
- one accepted-state writer based on BookState graph deltas;
- owner-specific read services instead of `StateRepository` as a universal dependency;
- one runtime policy model and one immutable task policy snapshot;
- one schema migration system;
- no dynamic module proxies, method injection, wildcard composition, or fake `__module__` identity;
- architecture tests that make deleted paths difficult to reintroduce;
- a clean-project 200-chapter run with no code hotfixes.

### 3.2 Non-goals

- no microservice topology redesign;
- no multi-tenancy, RBAC, or billing work;
- no migration or preservation of existing project data;
- no retcon of accepted chapters;
- no weakening of BookMap runtime behavior;
- no weakening of publisher browser isolation, encryption, CAPTCHA, or MFA stops;
- no expansion of Spark delegation into Genesis or publisher operations;
- no physical split of the shared production PostgreSQL database.

## 4. Target Package Ownership

### 4.1 Application layer

`forwin/application/` owns use cases and transaction boundaries:

- `GenesisApplicationService`: create, refine, lock, and hand off Genesis;
- `GenerationApplicationService`: start, continue, pause, resume, and execute durable generation tasks;
- `ReviewApplicationService`: inspect candidates, resolve eligible pauses, retry blocked candidates;
- `PublisherApplicationService`: enqueue, publish, and report post-canon publication work.

Application services accept command DTOs, call domain services, commit owned transactions, and return result DTOs. They do not expose SQLAlchemy rows or the internal chapter pipeline.

### 4.2 Runtime and composition

`forwin/runtime/` owns:

- infrastructure configuration;
- runtime policy types and resolution;
- model/profile factories;
- the explicit composition root.

The composition root constructs typed collaborators. It never mutates a service after construction and never injects names into another module.

### 4.3 Genesis and planning

`forwin/genesis/` replaces `book_genesis_core`, `genesis_workspace`, and `genesis_handoff` as one bounded package. Genesis is a six-stage pre-writing blueprint. A successful handoff materializes runtime plans and permanently freezes the Genesis revision.

`forwin/planning/` owns book, arc, band, and chapter runtime plans. `PlanningService` is the write facade. `PlanningQuery` is the read interface. `PlanHealthService` unifies future-plan audit, patch validation, scenario rehearsal, and band-boundary health into a typed result with severity, scope, evidence, and blocking status.

After handoff, changes use runtime plan proposals. They do not edit Genesis.

### 4.4 Generation pipeline

`forwin/generation/` owns durable task execution and `ChapterPipeline`. The pipeline receives all collaborators in its constructor and has no access to an orchestrator service bag.

`GateDelegationService` also lives at this boundary. It receives an already canon-eligible `PauseGate`, records a human pause or calls `SparkGateDelegate`, and returns a gate resolution. It cannot import Canon writers or mutate review and eligibility results.

The pipeline stages are:

1. load typed context;
2. write immutable candidate draft;
3. build entity admission plan;
4. review candidate;
5. repair and verify candidate within budget;
6. evaluate final residual eligibility;
7. resolve an optional pause gate;
8. prepare and submit a Canon commit plan;
9. persist accepted result and schedule projections.

`WritingOrchestrator`, `forwin/orchestrator/`, and `forwin/orchestrator_loop_core/` are deleted after their live algorithms move to owners.

The `107ac08` `chapter_review_gate.py` extraction is treated as a temporary seam. It still accepts `self`, reads old operation modes, and permits fail-verdict delegation. Its logic moves into `FinalResidualPolicy` and `HumanGateDelegation`, then the file is removed.

### 4.5 Review and repair

`forwin/review/` consolidates the live behavior currently spread across `reviewer`, `review_engine`, and `reviser`:

- `DraftReviewService` aggregates draft signals and produces a `ReviewVerdict`;
- `QualityAnalysisService` caches expensive analysis by candidate body hash and plan revision;
- `RepairService` owns scope selection, patching, rewriting, verification, budgets, and exhaustion;
- `FinalResidualPolicy` decides whether a fully reviewed candidate is canon-eligible.

Draft review produces evidence. It does not commit canon. A repaired body creates a new immutable candidate version and a new quality cache key.

### 4.6 Entity admission

`forwin/naming/EntityRegistrar` becomes a planner with four explicit outcomes:

- register a new canonical entity;
- register an alias to an existing entity;
- classify a mention as background-generic;
- report a plan/canon conflict.

The result is an `EntityAdmissionPlan` attached to the candidate. Classifier failure, omitted names, ambiguous alias targets, and uniqueness conflicts fail closed to `needs_review`. No entity or alias row is written before Canon commit.

The following independent decision paths are removed:

- canon-time `_validate_subworld_admission` classification;
- `subworld_admission_patch` repair scope;
- nonblocking subworld force-accept glue;
- summary-derived character-name bridges;
- expanding hard-coded regex/name exceptions.

Deterministic reference classification may remain as an input to the registrar. It cannot make a separate admission decision.

### 4.7 Canon ownership

`forwin/canon/` owns:

- `CanonQualityService`;
- `CanonCommitPlan`;
- `CanonAdmissionService`;
- adapters to BookState, BookMap, entity storage, obligations, audit, and outbox.

Canon admission is the only code allowed to convert a candidate into accepted state. Neither human approval, Spark delegation, final residual eligibility, nor a review verdict may bypass it.

### 4.8 BookState and projections

`forwin/book_state/` owns accepted narrative state. `GraphDeltaRow`, snapshots, and `BookStateCompiler` remain authoritative.

Reads are split by owner:

- `BookStateQuery`: entities, relations, threads, timeline, events, and state facts;
- `BookMapQuery`: map topology, visibility, movement, and subworld roster;
- `PlanningQuery`: active plans and constraints;
- `ReviewQuery`: recent review notes and candidate history.

`world_model`, LLM KB, Qdrant, Obsidian, and export pages are projections. Live projection implementations move into `knowledge_system` or `obsidian`; the deprecated `forwin/world_model` facade is deleted.

### 4.9 Audit and policy naming

`forwin/audit/` owns decision event types, audit payload contracts, PromptTrace correlation, and performance spans.

Project runtime settings move out of `governance.py` into runtime policy. Keyword/style review moves into `forwin/review/`. Codex permission checks remain in the Codex bridge boundary. The overloaded `governance` name is no longer used for unrelated concerns.

### 4.10 Entry adapters

`forwin/http/` owns the FastAPI app factory and HTTP route adapters. Routes validate transport input and call application services.

The other adapters behave as follows:

- worker calls `GenerationApplicationService.execute_task`;
- automation scheduler calls the generation enqueue use case only;
- MCP remains an HTTP client when deployed as a separate process, so it reaches the same application use cases through the API;
- CLI either uses the HTTP client or an explicitly constructed application service;
- UI calls HTTP endpoints and contains no independent policy normalization.

`api.py`, `api_core/exports.py`, `orchestrator/loop.py`, and `orchestrator_loop_core/exports.py` dynamic proxy behavior is deleted. Deployment targets are updated to the new explicit app factory.

## 5. Runtime Policy

### 5.1 Sources

v5 has three configuration roles, not four competing stores:

1. `InfrastructureConfig`: environment-only credentials, endpoints, worker sizing, model catalogs, and storage settings.
2. `RuntimePolicy`: the mutable per-project generation policy and its version.
3. `GenerationTask.policy_snapshot`: the immutable policy used for a task attempt.

API schemas are transport DTOs, not a configuration source. `RuntimeSettingsStore` and `Project.governance_json` are removed. A new project receives an explicit policy created from bootstrap defaults.

### 5.2 User-visible policy

The user-visible policy contains:

```text
quality_profile: standard | pulp
model_profile_id: string
chapter_length: bounded integer range
pause_policy:
  review_interval_chapters: non-negative integer
  manual_checkpoints: boolean
  band_checkpoint_action: continue | pause_on_warn | pause_always
  generation_audit_interval: non-negative integer
  generation_audit_pauses: boolean
  gate_delegate: human | spark
```

There is no `operation_mode`. Generation is always the strict blackbox pipeline. There is no `premium`, `writer_mode`, `progression_mode`, hybrid mode family, or public review-engine feature flag.

`standard` and `pulp` resolve to complete typed policy objects. Tests use explicit test policy factories and dependency injection, not persisted production flags.

### 5.3 Spark gate delegation

The implementation is renamed from reckless review to Spark gate delegation:

- `SparkGateDelegate` evaluates only optional pause gates for canon-eligible candidates;
- model mismatch, parse failure, timeout, or missing evidence rejects the delegation;
- a fail verdict or hard residual never reaches delegation;
- delegation cannot alter `ReviewVerdict`, `FinalResidualDecision`, or `CanonCommitPlan`;
- audit events become `GATE_DELEGATION_REQUESTED`, `DECIDED`, `FAILED`, and `APPROVED`;
- PromptTrace remains complete and records requested and actual model identities.

## 6. Candidate and Canon Data Flow

### 6.1 Durable candidate lifecycle

The canonical candidate state machine is:

```text
writing -> drafted -> reviewing -> repairing -> ready_for_canon
        -> committing -> accepted

any pre-canon state -> needs_review | failed
committing conflict -> ready_for_canon
```

Every candidate version has a stable id, parent candidate id, body hash, plan revision, writer artifact reference, review result, repair history, entity admission plan, eligibility decision, and policy version.

LLM calls never run while holding a database transaction. Each stage persists enough information to resume after process loss or lease recovery.

### 6.2 Eligibility before delegation

`FinalResidualPolicy` runs after repair verification. Hard floor failures, canon conflicts, unverified repairs, fail verdicts, and hard residuals produce `needs_review` or `failed`. Only a canon-eligible candidate may encounter a policy-driven pause gate.

Human or Spark approval removes the pause. It does not force-accept the candidate.

### 6.3 Canon preparation

Expensive extraction and analysis run before the write transaction and produce a complete `CanonCommitPlan` containing:

- approved BookState graph deltas;
- BookMap mutations;
- entity and alias mutations from `EntityAdmissionPlan`;
- obligation and plan lifecycle mutations;
- expected prior chapter and snapshot versions;
- accepted chapter metadata;
- audit events and outbox records;
- an idempotency key derived from project, chapter, and candidate.

### 6.4 Atomic Canon transaction

`CanonAdmissionService` acquires a project-scoped lock and opens one short database transaction. It rechecks candidate hash, plan revision, previous accepted chapter, and expected BookState version, then applies all authoritative writes.

The following commit or roll back together:

- BookState deltas and snapshots;
- BookMap authoritative mutations;
- entities and aliases;
- obligation/plan state changes;
- chapter accepted state;
- acceptance audit rows;
- projection/publisher outbox rows.

The same idempotency key returns the prior commit result. A stale version conflict rolls back and returns the candidate to `ready_for_canon`. Any other write failure rolls back the entire acceptance.

### 6.5 Post-commit projections

Qdrant, knowledge views, Obsidian/export data, artifact indexing, and publisher enqueue run from the outbox after commit. Projection failure marks the project degraded and schedules retry. It never rolls back, duplicates, or hides the accepted chapter.

## 7. Error Semantics

Errors use explicit categories:

- `DomainBlocked`: valid input cannot proceed because a review, plan, entity, or Canon invariant failed;
- `RetryableInfrastructureError`: transient model, database, storage, or network failure;
- `PermanentConfigurationError`: missing or invalid infrastructure configuration;
- `LeaseLost`: another worker owns execution and the current worker must stop without mutation;
- `Cancelled` or `Paused`: operator-requested control flow, not failure;
- `InvariantViolation`: internal contradiction that fails the task and emits full diagnostics.

Blocked and failed candidates are always preserved as immutable records; `freeze_failed_candidates` is removed as a switch. Catch-all exceptions may add diagnostics but cannot convert failure into acceptance.

Retry budgets live in typed policy. Retry exhaustion is observable and deterministic. Model fallback cannot silently change a decision made by a model-specific gate.

## 8. Database and Schema Cutover

Existing project data is not migrated.

After v5 models stabilize:

1. remove obsolete models and legacy state tables;
2. remove custom `schema_migrations` and all `_upgrade_*` functions from `models/base.py`;
3. replace the historical Alembic chain with one `0001_v5_baseline` revision;
4. make production initialization use Alembic as the only schema authority;
5. keep `Base.metadata.create_all` only in isolated tests if useful;
6. refuse startup against a non-v5 schema with an explicit operator error;
7. provide an explicit destructive database recreation command/runbook for deployment.

The application never auto-drops a production database on startup.

## 9. Deletion Contract

The implementation is incomplete while any of the following remain in production paths:

- `operation_mode`, checkpoint mode, copilot mode, progression mode, or premium profile;
- `review_engine_*_enabled` runtime flags;
- `review_engine_live_cutover_*`, shadow routing, or runtime parity routing;
- `review_delegation_mode` or `chapter_blackbox_failure` delegation;
- `_compile_world_model_after_acceptance`;
- `_apply_world_v4_gate` naming;
- `WritingOrchestrator` method assignment or module back-injection;
- `BookGenesisService` method assignment;
- API or orchestrator module proxy classes;
- `forwin/orchestration` dead ports;
- independent subworld admission decision paths;
- Canon-path `StateUpdater.apply_state_changes`, `apply_events`, `apply_thread_beats`, or `apply_time_advance`;
- production imports from deprecated `world_model`, `reviewer_v4`, scenario rehearsal aliases, or forwarding audit shims;
- hand-written runtime schema upgrade functions;
- route, MCP, scheduler, CLI, or UI code that implements generation business rules.

## 10. Testing Strategy

### 10.1 Unit tests

- runtime policy construction for standard and pulp;
- pause policy and human/Spark delegation;
- fail verdicts and hard residuals never reach delegation;
- review, repair routing, verification, and exhaustion;
- quality cache keying and invalidation;
- entity admission outcomes and fail-closed classification;
- Canon plan validation, idempotency, stale-version rejection, and rollback;
- BookState compiler and query behavior;
- PlanHealth severity and blocking behavior.

### 10.2 Integration tests

- task claim, lease, resume, pause, cancellation, and retry;
- complete draft -> review -> repair -> eligibility -> Canon flow;
- rejected candidate leaves BookState, BookMap, entities, aliases, and chapter status unchanged;
- Canon transaction rolls back all authoritative writes on each injected failure;
- post-commit projection failure preserves accepted canon and retries from outbox;
- Genesis handoff is idempotent and freezes the revision;
- API, MCP, scheduler, CLI, and worker reach the same application use cases;
- publisher failure never rolls back chapter acceptance.

### 10.3 Architecture tests

Architecture tests reject:

- module `__class__` proxying and fake `__module__` values;
- cross-module service method assignment or export injection;
- wildcard imports in application, runtime, generation, review, canon, Genesis, and HTTP packages;
- adapter imports from domain implementation modules;
- deprecated mode/config names;
- deprecated package imports;
- legacy accepted-state writes;
- more than one schema migration authority;
- untyped `Any` collaborator bags at major service boundaries.

### 10.4 UI verification

The settings UI exposes only quality profile, model profile, chapter length, pause policy, and gate delegate. Review views show draft verdict, repair result, residual eligibility, Canon decision, and gate delegation traces as distinct layers.

Frontend changes use the repository's existing Vite/React conventions and are verified in the browser at desktop and mobile sizes.

### 10.5 Real generation gates

All runs use a fresh v5 project and no code changes during the run:

- 30 chapters after runtime policy and entry cutover;
- 60 chapters after chapter/review/entity convergence;
- 100 chapters after BookState single-writer cutover;
- 200 chapters as the final no-hotfix acceptance run.

The final run must have no duplicate chapters, no duplicate graph deltas, no unclassified entity hotfix, no bypassed hard blocker, no lost task lease, and no manual source-code change.

## 11. Implementation Slices

This document is the umbrella architecture contract. Each slice receives its own implementation plan, task-level tests, commits, and completion audit. A slice may not be declared complete from the umbrella criteria alone, and the next slice does not erase unfinished acceptance work from an earlier slice.

### Slice 1: Runtime policy and application foundation

- introduce typed runtime policy and project/task policy persistence;
- remove old modes, profiles, runtime setting stores, governance setting overlap, and review feature flags;
- establish application command DTOs and generation enqueue/execute boundaries;
- update task payloads and basic settings UI/API;
- add architecture guards for removed config names.

### Slice 2: Genesis and planning

- consolidate Genesis packages and remove unreachable workflow bodies;
- replace `BookGenesisService` method injection;
- freeze Genesis after handoff;
- introduce PlanningService, PlanningQuery, and PlanHealthService;
- migrate and delete planning forwarding shims.

### Slice 3: Chapter pipeline, review, repair, and entities

- create explicit ChapterPipeline;
- consolidate review and repair ownership;
- add quality analysis cache;
- turn EntityRegistrar into EntityAdmissionPlan builder;
- remove old subworld admission paths and nonblocking glue;
- remove fail-verdict delegation and rename Spark gate delegation;
- delete migrated orchestrator method families.

### Slice 4: Canon and BookState convergence

- introduce CanonCommitPlan and atomic Canon transaction;
- rename the BookState commit path;
- split legacy StateRepository readers into owner queries;
- migrate context, reviewer, repair, governance, retrieval, preview, and API readers;
- remove legacy accepted-state writes and obsolete state tables;
- move live projection code and delete deprecated world-model facades;
- delete the remaining WritingOrchestrator.

### Slice 5: Entry adapters and UI

- build explicit HTTP app factory and route adapters;
- remove API global injection and dynamic proxy modules;
- route worker, scheduler, MCP, CLI, and UI through application use cases;
- preserve publisher process/browser isolation;
- finish settings and layered review UI;
- update deployment entrypoints.

### Slice 6: Schema, documentation, and release verification

- generate the v5 Alembic baseline and remove custom migration machinery;
- remove every item in the deletion contract;
- update `DESIGN_STATUS.md` and archive historical cutover plans;
- run unit, integration, architecture, frontend, publisher, and deployment tests;
- perform 30/60/100/200 clean-project generation gates;
- recreate and deploy the v5 production database through the approved deployment path.

## 12. Completion Criteria

ForWin v5 architecture convergence is complete only when all of the following are evidenced in the current repository and runtime:

1. all six implementation slices are complete;
2. every deletion-contract search is clean;
3. application and domain boundaries are enforced by tests;
4. accepted chapter state has one atomic writer;
5. entity admission cannot mutate canon before acceptance;
6. Spark cannot approve a fail or hard-blocked candidate;
7. all entry adapters reach the same application use cases;
8. Alembic is the only production schema authority;
9. full automated verification passes;
10. the 200-chapter no-hotfix run succeeds on a fresh v5 project;
11. source changes are committed, pushed, and deployed through the 150 GitHub sync path;
12. deployed API, worker, MCP, publisher, PostgreSQL, Qdrant, and MinIO roles pass smoke checks.

Passing a narrower test suite, finishing only the safe deletions, or leaving compatibility paths disabled but present does not satisfy completion.
