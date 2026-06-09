# ForWin Image Role Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split browser-only runtime dependencies out of the default ForWin image while keeping current Compose services working.

**Architecture:** Convert `Dockerfile` into a multi-target build. The final default target is slim `forwin-runtime`; `publisher-browser` explicitly builds `publisher-browser-runtime`.

**Tech Stack:** Dockerfile multi-stage builds, Docker Compose, pytest source-structure tests.

---

## File Structure

- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `docs/operations/forwin-production-processes.md`
- Modify: `tests/test_docker_compose_profiles.py`
- Modify: `tests/test_lan_deployment_config.py`
- Modify: `docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md`

## Task 1: RED Tests

- [x] **Step 1: Add Dockerfile target tests**

Add tests asserting:

- `Dockerfile` defines `AS publisher-browser-runtime`.
- `Dockerfile` defines `AS forwin-runtime`.
- Browser packages and `playwright install --with-deps chromium` are only in the browser target section.
- The final Dockerfile target is `forwin-runtime`.

- [x] **Step 2: Add Compose target tests**

Add tests asserting:

- `publisher-browser` has `build.context == "."`.
- `publisher-browser` has `build.target == "publisher-browser-runtime"`.
- API/generation/MCP keep `build: "."`.

- [x] **Step 3: Add MCP image decision test**

Add a doc/source test asserting operations docs say MCP reuses the slim default runtime and is not split yet.

- [x] **Step 4: Run RED**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
```

Expected: FAIL because Dockerfile targets and Compose browser target are not present yet.

## Task 2: Implement Image Split

- [x] **Step 1: Refactor Dockerfile targets**

Create these stages:

- `world-studio-builder`
- `python-base`
- `publisher-browser-runtime`
- `forwin-runtime` as the final stage

Keep extension source and scripts in `python-base`, because API still serves
extension downloads and browser target needs launch scripts.

- [x] **Step 2: Update Compose browser service**

Change only `publisher-browser.build` to:

```yaml
build:
  context: .
  target: publisher-browser-runtime
```

- [x] **Step 3: Document MCP image decision**

Update `docs/operations/forwin-production-processes.md` to say MCP uses the
slim default runtime and should not get a separate image until measured deploy
or startup savings justify it.

## Task 3: Verify and Commit

- [x] **Step 1: Run role image tests**

Run:

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py -q
```

- [x] **Step 2: Run Compose config validation**

Run:

```bash
docker compose config
```

- [x] **Step 3: Mark Phase 4 complete**

Mark Phase 4 steps 1-4 complete in the master roadmap plan.

- [x] **Step 4: Commit**

Run:

```bash
git add Dockerfile docker-compose.yml docs/operations/forwin-production-processes.md tests/test_docker_compose_profiles.py tests/test_lan_deployment_config.py docs/superpowers/specs/2026-06-09-forwin-image-role-split-design.md docs/superpowers/plans/2026-06-09-forwin-image-role-split.md docs/superpowers/plans/2026-06-08-forwin-service-process-roadmap.md
git commit -m "build: split publisher browser image target"
```
