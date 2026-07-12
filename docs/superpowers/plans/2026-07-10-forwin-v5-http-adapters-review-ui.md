# ForWin v5 HTTP Adapters and Layered Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Slice 5 by replacing import-time API globals with an explicit FastAPI factory/runtime, routing every generation entry through application use cases, and exposing the five review/repair/eligibility/delegation/Canon layers as one truthful operator view.

**Architecture:** `forwin.http.create_app()` owns one `HttpRuntime` instance attached to `app.state`; all stateful task, scheduler, publisher, and route dependencies close over that instance. HTTP route registration lives under `forwin/http`, project/task/project-control/publisher mutations call application services, MCP remains an HTTP client, and CLI generation becomes an HTTP workflow instead of constructing `ChapterPipeline`. Review detail returns typed layer records that the existing server-rendered console displays in a dedicated modal.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2, Pydantic v2, vanilla JavaScript/CSS, pytest.

## Global Constraints

- No module-global API session factory, pipeline, task cache, scheduler thread, publisher manager, or mutable config.
- No compatibility module for `forwin.api_core.app`, `forwin.api_core.state`, or `forwin.api_route_registry`.
- `forwin.api` remains the production ASGI entry and exports only `app`, `create_app`, and `lifespan`.
- Durable generation tasks remain the only chapter execution boundary.
- MCP remains a deployed HTTP client; publisher worker/browser isolation and encryption checks remain unchanged.
- CLI may call HTTP or explicitly constructed application services, but may not build or run `ChapterPipeline`.
- `/api/generate` is deleted; project creation, Genesis handoff, and continue-generation remain the only writing workflow.
- Review UI shows draft verdict, repair, residual eligibility, gate delegation, and Canon as separate layers.
- Existing projects and databases are not migrated.
- Subagents may be used for independent side work when useful; no visual companion.

---

### Task 1: Enforce the Factory and Entry Boundaries

**Files:**
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `tests/test_api_split_modules.py`
- Modify: `tests/test_large_module_boundaries.py`
- Create: `tests/test_http_app_factory.py`

**Interfaces:**
- Consumes: approved v5 entry-adapter and deletion contracts.
- Produces: failing guards for `forwin.http.create_app`, instance-owned runtime state, removed `api_core`/root route registry, removed `/api/generate`, and CLI pipeline prohibition.

- [x] Add AST/source guards requiring `forwin/http/app.py`, `forwin/http/runtime.py`, and `create_app` while rejecting `forwin/api_core`, `forwin/api_route_registry.py`, mutable API globals, and `RuntimeContainer`/`ChapterPipeline` imports in `forwin/cli.py`.
- [x] Add a factory isolation test that creates two apps, asserts distinct runtime/task-cache/scheduler state, and confirms route sets are equal.
- [x] Add an API contract test that `/api/generate` is absent while project create, start-writing, and continue-generation remain registered.
- [x] Run the four test files and confirm the new assertions fail against the current import-time app.

### Task 2: Build Instance-Owned HTTP Runtime and App Factory

**Files:**
- Create: `forwin/http/__init__.py`
- Create: `forwin/http/app.py`
- Create: `forwin/http/runtime.py`
- Move: `forwin/api_route_registry.py` to `forwin/http/routes.py`
- Move: `forwin/api_core/tasks.py` to `forwin/http/tasks.py`
- Move: `forwin/api_core/generation.py` to `forwin/http/generation.py`
- Move: `forwin/api_core/automation.py` to `forwin/http/automation.py`
- Move: `forwin/api_core/project_helpers.py` to `forwin/http/project_support.py`
- Move: `forwin/api_core/runtime.py` to `forwin/http/request_support.py`
- Delete: `forwin/api_core/app.py`
- Delete: `forwin/api_core/state.py`
- Delete: `forwin/api_core/__init__.py`
- Modify: `forwin/api.py`

**Interfaces:**
- Consumes: `InfrastructureConfig`, `RuntimeContainer`, current route dependency groups, task persistence helpers, publisher manager, automation scheduler.
- Produces: `HttpRuntime`, `create_app(*, runtime: HttpRuntime | None = None) -> FastAPI`, `lifespan_for(runtime)`, and `app.state.forwin_runtime` / `app.state.forwin_handlers`.

- [x] Define a slots dataclass `HttpRuntime` containing config/container/services, engine/session factory/pipeline/publisher manager/task-center service, task cache/lock, scheduler thread/stop event, and retention counters with independent default factories.
- [x] Give `HttpRuntime` explicit `startup()`, `shutdown()`, `get_session()`, `build_genesis_service()`, and `close_genesis_service()` operations; startup builds one runtime container and scheduler, shutdown closes only resources owned by that instance.
- [x] Change stateful task/generation/automation helpers to accept `runtime: HttpRuntime`; retain pure serializers/status predicates as module functions.
- [x] Move URL registration under `forwin.http.routes`, inject closures bound to one runtime, attach the returned handler map to `app.state`, and remove `/api/generate`.
- [x] Implement `create_app()` with CORS/basic-auth middleware and an instance-bound lifespan; instantiate the production `app` only in `forwin/api.py`.
- [x] Delete `forwin/api_core` and root `api_route_registry.py`, update production imports directly, and run factory/architecture/import tests.

### Task 3: Finish Application Adapter Convergence

**Files:**
- Create: `forwin/application/tasks.py`
- Create: `forwin/application/project_control/__init__.py`
- Create: `forwin/application/project_control/service.py`
- Move: `forwin/api_project_control_ops.py` to `forwin/application/project_control/operations.py`
- Move: `forwin/api_project_control_support.py` to `forwin/application/project_control/support.py`
- Move: all `forwin/api_*_routes.py` modules to `forwin/http/adapters/`
- Modify: `forwin/http/routes.py`
- Modify: `forwin/production/scheduler.py`
- Modify: `forwin/mcp/http.py`
- Modify: `forwin/cli.py`
- Modify: `forwin/llm_eval/runner.py`

**Interfaces:**
- Consumes: existing `ProjectApplicationService`, `GenerationApplicationService`, `PublisherApplicationService`, task/project-control operations, and MCP HTTP client.
- Produces: `TaskApplicationService`, `ProjectControlApplicationService`, thin HTTP adapter builders, and HTTP-only CLI generation/read/status workflows.

- [x] Move task mutation/list/get behavior behind `TaskApplicationService`; the HTTP task adapter performs only request binding and calls service methods.
- [x] Move project-control operations/support behind `ProjectControlApplicationService`; the HTTP adapter has no SQLAlchemy model imports.
- [x] Relocate all route adapters under `forwin/http/adapters`, update registry imports, and delete root `api_*_routes.py` files.
- [x] Remove `/api/generate`, `GenerateRequest`, browser fixtures for the endpoint, and the obsolete remote LLM-eval caller; retain local model evaluation paths.
- [x] Rewrite CLI `generate` as the explicit HTTP workflow `project_create -> six Genesis generate/lock stages -> project_start_writing`; rewrite `read` and `status` through the same HTTP client. Remove CLI API-key/model/database generation overrides and add `--api-base-url`.
- [x] Confirm worker calls `GenerationApplicationService.execute_claimed`, scheduler only calls enqueue, MCP uses HTTP, and publisher remains isolated.
- [x] Run adapter, worker, MCP, scheduler, CLI, task persistence, project operation, and publisher focused tests.

### Task 4: Add the Typed Five-Layer Review Contract

**Files:**
- Modify: `forwin/api_schema/review.py`
- Modify: `forwin/application/projects/reviews.py`
- Modify: `tests/test_reviewer_split.py`
- Modify: `tests/test_project_operation_guards.py`
- Modify: `tests/test_api_pages_rendering.py`

**Interfaces:**
- Consumes: `ChapterReview`, rewrite attempts, `FinalResidualDecision`, latest `CandidateDraftRecord`, Canon commit identity/status, and chapter decision events.
- Produces: `ChapterDecisionLayerInfo` and `ChapterReviewDetail.decision_layers` ordered as `draft_review`, `repair`, `residual_eligibility`, `gate_delegation`, `canon`.

- [x] Add a frozen transport DTO with `layer`, `status`, `outcome`, `summary`, `blocking`, `evidence_refs`, and filtered `decision_refs`.
- [x] Build all five layers in `get_chapter_review`; represent missing/not-required work explicitly instead of collapsing it into the draft verdict.
- [x] Derive Canon state from the latest candidate/commit fields and gate traces from `GATE_DELEGATION_*` decision events; never infer approval from a generic review event.
- [x] Add regression cases for repaired candidate, hard residual block, no-delegation path, Spark approved gate, Canon committed, and Canon blocked.

### Task 5: Replace the Alert Review UI with a Layered Modal

**Files:**
- Modify: `forwin/ui_assets/home/body.html`
- Modify: `forwin/ui_assets/home/app_task_progress.js`
- Modify: `forwin/ui_assets/home/app_library.js`
- Modify: `forwin/ui_assets/home/page.css`
- Modify: `tests/test_api_pages_rendering.py`
- Modify: `tests/browser/test_project_control_and_chapters.py`

**Interfaces:**
- Consumes: `ChapterReviewDetail.decision_layers`, issues, rewrite attempts, residual decision, and decision event references.
- Produces: `openReviewModal`, `closeReviewModal`, five stable layer panels, issue/repair detail regions, and existing approve/retry/audit-chain commands.

- [x] Add a dedicated review modal shell with a fixed five-column desktop flow and vertical mobile flow; no nested cards and no `window.alert` review rendering.
- [x] Render each layer with stable dimensions, status badge, outcome, summary, evidence, and audit-link control; show issues and repair attempts below the flow.
- [x] Wire existing retry/approve/jump-to-audit actions into the modal and replace obsolete `review_engine_decision` reads with `rule_decision`/typed layers.
- [x] Update stale project-control wording and ensure all buttons/long labels wrap without overlap.
- [x] Verify rendered HTML/JavaScript syntax and focused browser behavior without starting a visual companion.

### Task 6: Document and Commit Slice 5

**Files:**
- Modify: `Design-docs/CURRENT_ARCHITECTURE.md`
- Modify: `Design-docs/DESIGN_STATUS.md`
- Modify: `README.md`
- Modify: `docs/codex-forwin-mcp.md`
- Modify: this plan

**Interfaces:**
- Consumes: completed factory/adapter/UI implementation and verification evidence.
- Produces: authoritative Slice 5 completion record and a clean commit before final release verification.

- [x] Update current architecture and deprecation matrix with the `forwin.http` factory, instance-owned runtime, deleted `api_core`, entry use-case map, and layered review contract.
- [x] Update operator/README commands while keeping `forwin.api:app` as the production ASGI target.
- [x] Run production Ruff/compileall, architecture tests, focused adapter/UI tests, rendered JavaScript checks, and full test collection.
- [x] Mark every plan item complete and commit the coherent Slice 5 cutover.
