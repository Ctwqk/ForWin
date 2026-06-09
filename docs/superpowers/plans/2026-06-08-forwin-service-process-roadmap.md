# ForWin Service Process Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> before implementing code tasks from this plan. This plan intentionally spans
> multiple phases; implement one phase or one task group at a time.

**Goal:** Move ForWin toward a production-grade modular monolith with explicit
service-process boundaries, shared infrastructure, clear write ownership, and a
safe future outbox/image/schema path.

**Architecture:** Keep one codebase and centralized 150-hosted
Postgres/Qdrant/MinIO. Run role-specific processes for API/UI, generation
worker, MCP, publisher worker, publisher browser, and later knowledge/outbox
workers. Do not convert BookState/canon, review, or Genesis into remote
services during the early phases.

**Tech Stack:** Python 3.12/3.13, FastAPI, SQLAlchemy, PostgreSQL row locks,
Docker Compose/Swarm operations, pytest, optional import-boundary test helpers.

---

## Scope Check

This is a master implementation plan for a multi-phase architecture roadmap.
The phases touch independent subsystems: generation, MCP, publisher runtime,
knowledge indexing, outbox, Docker images, production operations, and schema
ownership. Do not execute every phase as one change set.

Execution rule:

- Phase 0 and Phase 1 may be implemented as small documentation/test changes.
- Phase 2 must be split into separate task plans for generation ports, canon
  ports, review ports, publisher ports, knowledge ports, and role-aware runtime.
- Phase 3 must be its own outbox design and plan before code changes.
- Phase 4 must be its own Docker/image design and plan before code changes.
- Phase 5 must be its own schema/database ownership design before any migration.

This plan is complete as a roadmap plan. It is not a substitute for the
phase-specific code-level plans required before implementing later phases.

## File Structure

Roadmap and operations documents:

- `docs/superpowers/specs/2026-06-08-forwin-service-process-roadmap-design.md`
  records the architecture decision, boundaries, phases, and acceptance
  criteria.
- `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`
  records this phase-by-phase master implementation plan.
- `docs/operations/forwin-service-process-roadmap.md` should summarize the
  accepted roadmap for operators.
- `docs/operations/forwin-production-processes.md` should describe the concrete
  126/150 production process roles and checks.

Boundary and port files expected by later phases:

- `tests/test_service_process_boundaries.py` should contain source-boundary
  tests.
- `forwin/generation/ports.py` should own generation task contracts after Phase
  2.
- `forwin/book_state/ports.py` should own the canon write contract after Phase
  2.
- `forwin/reviewer/ports.py` should own review contracts after Phase 2.
- `forwin/publisher_runtime/ports.py` should own publisher job contracts after
  Phase 2.
- `forwin/retrieval/ports.py` should own knowledge index contracts after Phase
  2.
- `forwin/outbox/store.py` and `forwin/outbox/worker.py` should own outbox
  persistence and worker execution after Phase 3.

## Phase 0: Boundary Audit and Ownership Markers

**Goal:** Make the current boundaries explicit and add tests that prevent new
cross-boundary drift.

**Files:**

- Modify or create: `docs/operations/forwin-service-process-roadmap.md`
- Modify or create: `docs/operations/forwin-production-processes.md`
- Create: `tests/test_service_process_boundaries.py`
- Modify: existing docs that describe operator or deploy expectations, if they
  conflict with the roadmap

- [x] **Step 1: Add current ownership documentation**

  Document the current writer authority for:

  - generation task enqueue/control
  - generation task lease/progress
  - BookState/canon writes
  - review/governance results
  - publisher upload/comment/cover jobs
  - MCP operations
  - knowledge index writes
  - observability/artifacts

  The document must state that ownership is logical write authority, not
  physical database ownership.

- [x] **Step 2: Add initial boundary tests**

  Create `tests/test_service_process_boundaries.py` with focused source scans:

  - `forwin/mcp/*.py` must not import SQLAlchemy models/base/session modules.
  - `forwin/generation/*.py` must not import `forwin.api_core.*`, except a
    temporary allowlist for the current auto-continue completion handler.
  - browser launch/check scripts must not call `get_engine`,
    `get_session_factory`, or import `forwin.models.base`.

  Keep the tests narrow so they pass against the current repository while still
  exposing the known allowlist debt.

- [x] **Step 3: Verify Phase 0**

  Run:

  ```bash
  python3 -m pytest tests/test_service_process_boundaries.py -q
  python3 -m pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
  ```

  Expected: all selected tests pass.

---

## Phase 1: Production Process Split

**Goal:** Make existing worker boundaries operational in production and local
docs.

**Files:**

- Modify: `README.md`
- Modify or create: `docs/operations/forwin-production-processes.md`
- Modify: `docker-compose.yml` if local service parity needs tightening
- Modify: deploy/sync or Swarm stack documentation if present
- Test: `tests/test_docker_compose_profiles.py`
- Test: `tests/test_lan_deployment_config.py`

- [x] **Step 1: Document production process roles**

  Document the intended 126-host processes:

  - `forwin-app-swarm`
  - `forwin-generation-worker-swarm`
  - `forwin-mcp-swarm`
  - `forwin-publisher-worker-swarm`
  - optional `forwin-publisher-browser-swarm`

  Also document that production data stores remain on 150.

- [x] **Step 2: Define worker operational checks**

  Add commands or documented checks for:

  - queued/running task counts
  - worker lease owner and lease expiry
  - recent worker heartbeat
  - MCP health/upstream API connectivity
  - publisher browser heartbeat

  The checks should prefer existing API/MCP/task surfaces over raw DB
  inspection when an equivalent operator tool exists.

- [x] **Step 3: Tighten local Compose parity**

  Confirm local Compose has:

  - `generation-worker` with disabled HTTP healthcheck
  - `forwin-mcp` with API base URL
  - `publisher-browser` profile and debug bind defaults restricted to localhost

  Add or update tests only if the current tests do not cover these facts.

- [x] **Step 4: Verify Phase 1**

  Run:

  ```bash
  python3 -m pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
  python3 -m forwin.cli generation-worker --help
  python3 -m forwin.cli publisher-worker --help
  ```

  Expected: tests pass and both CLI commands are present.

---

## Phase 2: Internal Ports and Dependency Direction

**Goal:** Reduce monolith coupling without adding remote services.

**Files:**

- Create or modify: `forwin/generation/ports.py`
- Create or modify: `forwin/book_state/ports.py`
- Create or modify: `forwin/reviewer/ports.py`
- Create or modify: `forwin/publisher_runtime/ports.py`
- Create or modify: `forwin/retrieval/ports.py`
- Modify: `forwin/generation/worker.py`
- Modify: `forwin/generation/auto_continue.py`
- Modify: `forwin/api_core/generation.py`
- Modify: `forwin/runtime/container.py`
- Test: `tests/test_service_process_boundaries.py`
- Add focused contract tests for each port introduced

- [x] **Step 1: Introduce generation task port**

  Extract generation enqueue/active-check/follow-up task creation into a port
  that can be used by both API code and worker auto-continue logic.

  The worker must no longer import `forwin.api_core.generation`.

- [x] **Step 2: Introduce CanonPort**

  Create a narrow canon write contract for approved GraphDelta input and
  snapshot/result output. Do not change canon behavior in this step; wrap the
  current write path first.

- [x] **Step 3: Introduce ReviewPort**

  Create a narrow review contract for chapter review, retry/repair instruction,
  and verdict output. Keep review execution in-process.

- [x] **Step 4: Introduce PublisherJobClient**

  Wrap upload, cover, and comment-sync job creation/claim/result flows behind a
  publisher job contract. Keep browser worker API-only as the future target.

- [x] **Step 5: Introduce KnowledgeIndexPort**

  Isolate expensive rebuild/index commands from read APIs. Do not remote-split
  query/read paths yet.

- [x] **Step 6: Make RuntimeContainer role-aware**

  Add a role-oriented construction path or laziness so API startup does not
  eagerly build browser/worker-only resources. Keep the existing
  `RuntimeContainer.from_config()` compatibility path until call sites are
  migrated.

- [x] **Step 7: Verify Phase 2**

  Run:

  ```bash
  python3 -m pytest tests/test_service_process_boundaries.py -q
  python3 -m pytest tests/test_generation_auto_continue.py tests/test_project_operation_guards.py -q
  python3 -m pytest tests/test_generation_task_lease.py tests/test_generation_worker_cli.py -q
  ```

  Expected: all selected tests pass, and the generation-to-api_core allowlist is
  removed from the boundary test.

---

## Phase 3: Postgres Outbox and Async Side Effects

**Goal:** Move selected non-critical side effects to an observable Postgres
outbox while preserving the synchronous generation/canon path.

**Files:**

- Create: `forwin/models/outbox.py`
- Create: the next Alembic migration under `forwin/migrations/versions/` with
  a descriptive suffix such as `outbox_events.py`
- Create: `forwin/outbox/store.py`
- Create: `forwin/outbox/worker.py`
- Modify: `forwin/cli.py`
- Modify: selected artifact/knowledge/publisher/observability call sites
- Test: `tests/test_outbox_worker.py`
- Test: contract tests for moved side effects

- [ ] **Step 1: Add outbox schema and store**

  Add `outbox_events` with:

  - `event_id`
  - `aggregate_type`
  - `aggregate_id`
  - `event_type`
  - `payload_json`
  - `status`
  - `attempts`
  - `created_at`
  - `available_at`
  - `processed_at`
  - `error_message`

- [ ] **Step 2: Add outbox worker CLI**

  Add:

  ```text
  forwin outbox-worker --once
  forwin outbox-worker --poll-interval 2
  ```

  Use row locking and bounded retries. Do not block main generation if the
  outbox worker is down.

- [ ] **Step 3: Move only safe side effects**

  Candidate side effects:

  - artifact indexing
  - LLM-KB rebuild/index requests
  - non-blocking projection refresh
  - publisher follow-up enqueue where eventual consistency is acceptable
  - observability retention/maintenance

  Do not move BookState compile or chapter acceptance to outbox.

- [ ] **Step 4: Verify Phase 3**

  Run:

  ```bash
  python3 -m pytest tests/test_outbox_worker.py -q
  python3 -m pytest tests/test_generation_task_lease.py tests/test_generation_auto_continue.py -q
  ```

  Expected: outbox tests pass, and generation/canon tests still prove the main
  writing path does not depend on outbox worker availability.

---

## Phase 4: Image and Runtime Role Split

**Goal:** Split heavy runtime images only after role contracts are stable.

**Files:**

- Modify or create: Dockerfiles for role-specific images
- Modify: `docker-compose.yml`
- Modify: production deploy docs/stack definitions
- Test: Docker/Compose structure tests

- [ ] **Step 1: Extract publisher-browser image**

  Create the first role-specific image for Chromium/extension/browser scripts.
  Keep API and generation worker images unchanged at this step.

- [ ] **Step 2: Slim web-api image**

  Remove browser-only dependencies from the API image after browser image
  extraction is proven.

- [ ] **Step 3: Decide whether MCP image split is worth it**

  Only split `forwin-mcp` if the smaller image materially improves deployment
  speed or operational risk.

- [ ] **Step 4: Verify Phase 4**

  Run:

  ```bash
  docker compose config >/tmp/forwin-compose-config.txt
  python3 -m pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
  ```

  Expected: Compose config is valid and role-specific service tests pass.

---

## Phase 5: Schema and Database Ownership

**Goal:** Move from shared-table access to explicit schema ownership, then
consider physical database split only if proven necessary.

**Files:**

- Create or modify: schema ownership docs
- Add migrations only after specific ownership changes are approved
- Add contract tests around any schema ownership enforcement

- [ ] **Step 1: Document schema ownership**

  Define owner domains for publisher, observability, knowledge, generation, and
  BookState/canon. This is a documentation and contract step, not a physical DB
  split.

- [ ] **Step 2: Add ownership enforcement tests**

  Add tests or lint checks that prevent direct writes from non-owner modules
  where practical.

- [ ] **Step 3: Evaluate physical DB candidates**

  Consider physical DB splits only in this order:

  1. publisher runtime data
  2. observability/artifact metadata
  3. knowledge index metadata
  4. generation task history
  5. BookState/canon last

- [ ] **Step 4: Require a separate design before any physical split**

  Each physical DB split needs its own design covering migration, rollback,
  idempotency, failure behavior, backup/restore, and operator checks.

---

## Overall Verification

After implementing any phase, run the phase-specific tests plus a focused
regression set:

```bash
python3 -m pytest tests/test_generation_task_lease.py tests/test_generation_worker_cli.py -q
python3 -m pytest tests/test_generation_auto_continue.py tests/test_project_operation_guards.py -q
python3 -m pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
```

For documentation-only changes, run at minimum:

```bash
python3 -m pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
python3 -m forwin.cli generation-worker --help
python3 -m forwin.cli publisher-worker --help
```
