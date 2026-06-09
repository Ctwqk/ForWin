# ForWin Runtime Container Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit runtime roles to `RuntimeContainer` so API, generation worker, and publisher worker construction paths are visible and testable.

**Architecture:** Keep the existing full service graph for compatibility, but add validated role metadata and role-specific constructors. Update API/CLI call sites to pass the role they are starting. This creates the boundary needed before later resource-laziness work.

**Tech Stack:** Python dataclasses, Literal typing, pytest source tests.

---

## File Structure

- Modify: `forwin/runtime/container.py`
  - Adds `RuntimeRole`, validated `role`, and role-specific constructors.
- Modify: `forwin/api_core/app.py`
  - Constructs the API runtime with `role="api"`.
- Modify: `forwin/cli.py`
  - Constructs generation and publisher workers with explicit roles.
- Modify: `tests/test_runtime_container.py`
  - Tests role validation and constructors.
- Create or modify: `tests/test_runtime_container_roles.py`
  - Tests API/CLI source call sites use explicit roles.
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`
  - Marks Phase 2 Step 6 complete after verification.

## Task 1: RED Tests

- [x] **Step 1: Add role constructor tests**

Add tests that assert:

- `RuntimeContainer.from_config(config, role="api").role == "api"`
- `RuntimeContainer.for_generation_worker(config).role == "generation_worker"`
- invalid roles raise `ValueError`

- [x] **Step 2: Add call-site source tests**

Add tests that assert:

- `forwin/api_core/app.py` calls `RuntimeContainer.from_config(..., role="api")`
- `forwin/cli.py` uses `role="generation_worker"` and `role="publisher_worker"`

- [x] **Step 3: Run RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_runtime_container.py::test_runtime_container_records_validated_role tests/test_runtime_container_roles.py -q
```

Expected: FAIL because role support and call-site roles do not exist.

## Task 2: Implement Runtime Roles

- [x] **Step 1: Modify `RuntimeContainer`**

Add `RuntimeRole = Literal["full", "api", "generation_worker", "publisher_worker", "mcp", "maintenance"]`.
Add a `role` dataclass field defaulting to `"full"`.
Validate role in `from_config()`.

- [x] **Step 2: Add role-specific constructors**

Add `for_api()`, `for_generation_worker()`, and `for_publisher_worker()` classmethods.

- [x] **Step 3: Update call sites**

Update API app and CLI worker paths to pass explicit roles.

## Task 3: Verify and Commit

- [x] **Step 1: Run runtime tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_runtime_container.py tests/test_runtime_container_roles.py -q
```

- [x] **Step 2: Run worker/API regression tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_generation_worker_cli.py tests/test_docker_compose_profiles.py -q
```

- [x] **Step 3: Mark Phase 2 Step 6 complete in the master plan**

Change only Phase 2 Step 6 to `[x]`.

- [x] **Step 4: Commit**

Run:

```bash
git add forwin/runtime/container.py forwin/api_core/app.py forwin/cli.py tests/test_runtime_container.py tests/test_runtime_container_roles.py docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md docs/superpowers/plans/2026-06-09-forwin-runtime-container-roles.md
git commit -m "refactor: add runtime container roles"
```
