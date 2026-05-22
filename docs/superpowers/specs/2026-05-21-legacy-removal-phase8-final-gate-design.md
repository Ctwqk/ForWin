# Legacy Removal Phase 8 Final Gate Design

## Context

After Phase 7, the only active final-audit blocker is:

- `legacy_compatibility_audit_runtime`

This runtime audit is intentionally the last legacy component because it is the
tool that proves a clean 60 chapter run no longer touches compatibility paths.

The user has authorized execution after this spec and plan are written. No
approval pause is required.

## Selected Approach

Run a clean 60 chapter pilot first, using the local `/home/taiwei/ForWin`
container deployment. Delete the runtime compatibility audit only after the
pilot proves it is unused.

## Deployment Design

Container deployment must use the local checkout, not a worktree. To avoid
editing `.env` or copying secrets, the run uses a temporary Compose override
file outside the repo that adds only test flags:

- `FORWIN_REVIEW_ENGINE_REPAIR_V2_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_ARC_PATCHER_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_BOOK_PATCHER_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_OBLIGATION_VERIFIER_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_AUTO_APPROVE_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_LOCAL_REWRITE_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_COMMIT_WITH_OBLIGATION_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_ARC_BOOK_BUDGET_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_ENABLED=true`
- `FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST=`

Empty allowlist plus live flag means all projects run engine-live.

## Pilot Design

Create a new clean 60 chapter Genesis project through ForWin MCP, not by
directly editing database state. Lock/generate Genesis stages through supported
operator tools, then call `project_start_writing` with normal auto-continue.

Success criteria for the pilot audit:

- expected chapters: 60
- engine live chapters: 60
- baseline safety-net chapters: none
- severe mismatch chapters: none
- legacy compatibility total events: 0

## Deletion Design

Only after the pilot passes:

- remove `LEGACY_COMPATIBILITY_REGISTRY` and related payload/summary/static
  analysis helpers from `forwin/review_engine/audit.py`;
- remove `LEGACY_COMPATIBILITY_USED` decision event type;
- remove `_record_legacy_compatibility_event` from orchestrator governance and
  service binding;
- remove `--include-legacy-compat` and `legacy_compat` output from
  `scripts/audit_review_engine_cutover.py`;
- remove residual recorder calls that are now unreachable/dead;
- delete or repoint tests that only validate removed audit runtime;
- mark `legacy_compatibility_audit_runtime` deleted.

## Testing

Before deletion:

```bash
python3 scripts/check_codex_operator_ready.py
```

After the pilot completes:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id <project_id> \
  --expected-chapters 60
```

After deletion:

```bash
python3 scripts/audit_legacy_inventory.py --check --final --strict-patterns
python3 -m pytest tests/review_engine/test_audit.py tests/test_architecture_boundaries.py -q
python3 -m compileall -q forwin
git diff --check
```

The full test suite is a Phase 8 final verification target. If a long-running
test command exceeds the available execution window, record the timeout and keep
the backend pilot running for follow-up inspection.

## Rollback

Rollback is a git revert of the Phase 8 deletion commit. If the pilot audit
reports any compatibility event, do not delete the runtime audit; fix the
reported path or reclassify it in inventory first.
