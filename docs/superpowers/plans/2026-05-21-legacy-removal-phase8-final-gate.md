# Legacy Removal Phase 8 Final Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans and forwin-operator. Use ForWin MCP tools for
> project/task/chapter truth.

**Goal:** Prove a clean 60 chapter engine-live run has zero compatibility
events, then remove the last runtime compatibility audit.

**Spec:** `docs/superpowers/specs/2026-05-21-legacy-removal-phase8-final-gate-design.md`

---

## Task 1: Container Preflight From Local Checkout

**Files:**

- temporary Compose override outside the repo

- [x] Create `/tmp/forwin-phase8-review-engine.override.yml` with review-engine
      test flags.
- [x] Run:

```bash
docker compose -f docker-compose.yml -f /tmp/forwin-phase8-review-engine.override.yml up -d --build forwin forwin-mcp
python3 scripts/check_codex_operator_ready.py
```

- [x] Confirm MCP tools are available and there is no active generation task.

Evidence: `python3 scripts/check_codex_operator_ready.py` passed after the
local container rebuild.

## Task 2: Start Clean 60 Chapter Pilot

- [x] Use `project_create` with `target_total_chapters=60`.
- [x] Generate required Genesis stages through MCP.
- [x] Start writing through `project_start_writing`.
- [x] Record project id and task id.

Project: `53e544b083ac4d7598bf072ac7f9f874`
(`六十章最终审计压力测试·星门余烬·干净版`).

## Task 3: Poll And Audit Pilot

- [x] Poll with `task_get` / `project_get`.
- [x] When 60 chapters complete, run:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60
```

- [x] Continue only if:
      `engine_live_chapters == 60`,
      `baseline_safety_net_chapters == []`,
      `severe_mismatch_chapters == []`,
      and the pre-deletion pilot had already reported
      `legacy_compat.total_events == 0`.

Final container audit result:
`{"engine_live_chapters": 60, "baseline_safety_net_chapters": [], "severe_mismatch_chapters": [], "passed": true}`.

## Task 4: Delete Runtime Compatibility Audit

**Files:**

- `forwin/review_engine/audit.py`
- `forwin/governance.py`
- `forwin/orchestrator_loop_core/governance.py`
- `forwin/orchestrator_loop_core/service.py`
- `scripts/audit_review_engine_cutover.py`
- tests covering review-engine audit and inventory
- `docs/designs/legacy-inventory.yaml`

- [x] Remove compatibility registry, payload builder, summary, static count, and
      per-feature detail helpers.
- [x] Remove decision event type and recorder binding.
- [x] Remove `--include-legacy-compat` CLI output.
- [x] Remove now-dead recorder calls.
- [x] Mark `legacy_compatibility_audit_runtime` deleted with narrow residual
      patterns.

## Task 5: Final Verification And Commit

- [x] Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --final --strict-patterns
python3 -m pytest tests/review_engine/test_audit.py tests/test_architecture_boundaries.py -q
python3 -m compileall -q forwin
git diff --check
```

- [x] Run full tests if execution time allows; otherwise record timeout.
- [ ] Commit only Phase 8 files:

```bash
git commit -m "refactor: remove legacy compatibility audit runtime"
```

Verification evidence:

- `python3 scripts/audit_legacy_inventory.py --check --final --strict-patterns`
  passed.
- `.venv/bin/python -m pytest tests/review_engine/test_audit.py tests/test_architecture_boundaries.py -q --tb=short --disable-warnings`
  passed.
- `.venv/bin/python -m pytest --ignore=tests/browser -q --tb=short --disable-warnings`
  passed: 1371 tests, 8 subtests.
- `.venv/bin/python -m pytest tests/browser -q --tb=short --disable-warnings --maxfail=20`
  passed: 25 tests, 3 skipped.
- `python3 -m compileall -q forwin` passed.
- `git diff --check` passed.
- Container rebuild from `/home/taiwei/ForWin` passed, then
  `docker compose -f docker-compose.yml -f /tmp/forwin-phase8-review-engine.override.yml exec -T forwin python scripts/audit_review_engine_cutover.py --project-id 53e544b083ac4d7598bf072ac7f9f874 --expected-chapters 60`
  passed.
