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

- [ ] Create `/tmp/forwin-phase8-review-engine.override.yml` with review-engine
      test flags.
- [ ] Run:

```bash
docker compose -f docker-compose.yml -f /tmp/forwin-phase8-review-engine.override.yml up -d --build forwin forwin-mcp
python3 scripts/check_codex_operator_ready.py
```

- [ ] Confirm MCP tools are available and there is no active generation task.

## Task 2: Start Clean 60 Chapter Pilot

- [ ] Use `project_create` with `target_total_chapters=60`.
- [ ] Generate required Genesis stages through MCP.
- [ ] Start writing through `project_start_writing`.
- [ ] Record project id and task id.

## Task 3: Poll And Audit Pilot

- [ ] Poll with `task_get` / `project_get`.
- [ ] When 60 chapters complete, run:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60 \
  --include-legacy-compat
```

- [ ] Continue only if:
      `engine_live_chapters == 60`,
      `baseline_safety_net_chapters == []`,
      `severe_mismatch_chapters == []`,
      and `legacy_compat.total_events == 0`.

## Task 4: Delete Runtime Compatibility Audit

**Files:**

- `forwin/review_engine/audit.py`
- `forwin/governance.py`
- `forwin/orchestrator_loop_core/governance.py`
- `forwin/orchestrator_loop_core/service.py`
- `scripts/audit_review_engine_cutover.py`
- tests covering review-engine audit and inventory
- `docs/designs/legacy-inventory.yaml`

- [ ] Remove compatibility registry, payload builder, summary, static count, and
      per-feature detail helpers.
- [ ] Remove decision event type and recorder binding.
- [ ] Remove `--include-legacy-compat` CLI output.
- [ ] Remove now-dead recorder calls.
- [ ] Mark `legacy_compatibility_audit_runtime` deleted with narrow residual
      patterns.

## Task 5: Final Verification And Commit

- [ ] Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --final --strict-patterns
python3 -m pytest tests/review_engine/test_audit.py tests/test_architecture_boundaries.py -q
python3 -m compileall -q forwin
git diff --check
```

- [ ] Run full tests if execution time allows; otherwise record timeout.
- [ ] Commit only Phase 8 files:

```bash
git commit -m "refactor: remove legacy compatibility audit runtime"
```
