# Continue Generation Patch Audit - 2026-05-12

## Scope

This review covers the current local patch on `master` relative to `origin/master`.

Changed areas:

- `continue-generation` requested chapter count handling.
- Band checkpoint approval status validation.
- `world_model_v4` / `reviewer_v4` compatibility import forwarding.
- Browser mock regression coverage.
- 60-chapter real-run report.

Primary review focus:

- Patch-on-patch risk.
- Dead or misleading compatibility code.
- Potential runtime bugs not covered by the new tests.
- Follow-up improvements needed before treating this patch as complete.

## Findings

### P1 - Architecture boundary test currently fails

`forwin/world_model_v4/compiler.py` was converted into a thin forwarding module:

```python
from forwin.world_v4_compat.compiler import WorldModelCompiler
```

But `tests/test_architecture_boundaries.py::test_legacy_world_model_is_labeled_projection_in_runtime_paths` still reads that file directly and expects the phrase `compatibility projection rows`.

Observed verification:

```bash
.venv/bin/python -m pytest tests/test_architecture_boundaries.py::test_legacy_world_model_is_labeled_projection_in_runtime_paths -q
```

Result:

```text
1 failed
assert 'compatibility projection rows' in 'from __future__ import annotations...'
```

Impact:

- CI can fail even though the import alias test passes.
- The alias migration moved code but did not move or preserve the architecture boundary marker.

Recommended fix:

- Either add an explicit compatibility-projection docstring to the forwarding module, or update the architecture boundary test to inspect `forwin/world_v4_compat/compiler.py` as the new canonical implementation.
- Prefer keeping the boundary marker in both places while the legacy import path remains public.

### P1 - `requested_chapters` can still be overwritten by stale progress payloads

The API entry point now clamps initial task creation to `max_chapters`:

- `forwin/api_project_ops.py`: `requested_chapters = min(..., max_chapters)`.

The worker path still emits progress before the active-arc scoped chapter list is calculated:

- `forwin/orchestrator/loop.py` records `payload={"requested_chapters": len(chapter_plans)}` at run start.
- `forwin/orchestrator/loop.py` emits `requested_chapters=len(pending_chapter_numbers)` during `resolving_arc_envelope`.
- `forwin/api_runtime.py` blindly copies `requested_chapters` from progress payloads into the persisted task.

Impact:

- A task started with `max_chapters=2` can be created as `requested_chapters=2`, then later show a larger count in task-center state.
- This matches the already observed report note that the frontend continue response and authoritative task state diverged.
- The new regression test only patches task creation and does not exercise the worker progress callback, so it can pass while the real UI still regresses.

Recommended fix:

- Centralize requested-count calculation in the orchestrator path after active-arc scoping, or pass a task-level requested limit through the runtime progress handler and clamp progress payloads before `update_task`.
- Add a regression test that runs `run_continue_project_with_config` or an equivalent orchestrator progress callback and asserts persisted task `requested_chapters` never exceeds `max_chapters`.

### P2 - Active-arc scoping and API pre-count can diverge

`continue_project_generation()` counts all `planned` / `failed` chapter plans in the project. The orchestrator later calls `_pending_chapter_numbers_for_active_arc()` and only writes chapters from the active arc.

Impact:

- If future arcs already have planned chapters, the API task count can overstate actual work.
- If there are no remaining materialized plans but a planned future Genesis arc exists, the API falls back to `requested_chapters=1`; the orchestrator may then materialize a full arc and emit a larger count.

Recommended fix:

- Extract a single helper for "next continue-generation workset" that accounts for active arc, future arc materialization, failed chapters, and `max_chapters`.
- Use that helper for API task creation, orchestrator progress, MCP response, and tests.

### P2 - Invalid historical checkpoint statuses can still break read paths

The write request now rejects unknown approval statuses:

- `BandCheckpointApproveRequest.status: Literal["pass", "overridden"]`.
- MCP client also rejects values outside `pass` / `overridden`.

However, read models still validate `BandCheckpointDetail.status` against `CheckpointStatus`, which does not include the old invalid value `approved`.

Observed verification:

```bash
.venv/bin/python - <<'PY'
from pydantic import ValidationError
from forwin.governance import BandCheckpointDetail
try:
    BandCheckpointDetail(status="approved")
except ValidationError as exc:
    print(type(exc).__name__)
PY
```

Result:

```text
ValidationError
```

Impact:

- Existing rows with `status="approved"` can still make project detail, checkpoint detail, or task-center serialization fail.
- The patch prevents future bad writes but does not repair or tolerate historical bad data.

Recommended fix:

- Add a migration or startup repair for invalid `band_checkpoints.status` values.
- Harden `serialize_band_checkpoint()` / `_band_checkpoint_detail()` to normalize unknown statuses to `overridden` or expose a safe error status.
- Add a regression test that seeds an invalid historical row and verifies the read API does not 500.

### P2 - Continue-after-review paths still use total chapter count

The direct continue endpoint was patched, but two related paths still pass total materialized chapter count into `create_continue_generation_task()`:

- `approve_chapter_review(... continue_generation=True)` uses `requested_chapters=int(total_chapters or 0)`.
- `retry_chapter_review(... continue_generation=True)` uses `requested_chapters=int(total_chapters or 0)`.

Impact:

- After accepting or retrying one chapter, the task may report the whole book or whole materialized plan count, not the remaining workset.
- This is the same class of bug as the direct continue-generation mismatch.

Recommended fix:

- Reuse the same continue workset helper for direct continue, accept-and-continue, retry-and-continue, production scheduler, and MCP.
- Add tests for review-accept continue and retry continue requested-count behavior.

### P3 - `max_chapters` validation is inconsistent across API and MCP

The MCP client rejects `max_chapters < 1`, but `ProjectContinueGenerationRequest` accepts any integer and the API coerces `0` or negative values to `1`.

Impact:

- Direct REST and MCP callers observe different behavior for the same invalid input.
- Silent coercion can hide UI or operator bugs.

Recommended fix:

- Put `max_chapters: int | None = Field(default=None, ge=1)` in `ProjectContinueGenerationRequest`.
- Add route-level tests for `0`, negative, and valid values.

### P3 - Checkpoint approval does not set `resolved_at`

`BandCheckpoint` has `resolved_at`, and serializers expose it, but `approve_band_checkpoint()` only updates `status` and `reason`.

Impact:

- Resolved checkpoints remain without a resolved timestamp.
- Audit views cannot reliably distinguish old unresolved checkpoints from manually resolved ones using timestamp alone.

Recommended fix:

- Set `row.resolved_at` when status moves to `pass` or `overridden`.
- Add a regression assertion on serialized checkpoint `resolved_at`.

### P3 - Browser mock regression is useful but brittle

The new browser test hardcodes `project-2` and `task-2`, relying on fixture initialization order. The mock start-writing handler also returns a generic sample task and does not model real project status transitions.

Impact:

- The test can break when fixture defaults change.
- It verifies frontend wiring and refresh persistence, but not the real backend state machine for Genesis handoff or continue-generation task counts.

Recommended fix:

- Derive project/task ids from captured responses instead of hardcoding `project-2` / `task-2`.
- Extend the mock to update project `creation_status` and chapter/task state closer to the real API contract.
- Keep this test labeled as CI mock regression, not live LLM E2E coverage.

### P4 - Tracked runtime artifacts and local scripts should be cleaned up separately

The repository has tracked `.playwright-mcp/*` run logs and `.codex-tmp/raspi-bastion-hardening.sh`.

Impact:

- These files are not part of the current patch, but they are repository hygiene debt.
- They can obscure review and make future patch boundaries noisier.

Recommended fix:

- Decide whether these artifacts are intentionally versioned.
- If not, remove them from git and add ignore rules.

## Patch-Stack Assessment

The current patch has three signs of patch stacking:

1. The direct `continue-generation` response was fixed, but worker progress, accept-and-continue, and retry-and-continue still use older requested-count semantics.
2. The checkpoint write path was tightened, but historical read compatibility was not addressed.
3. The world v4 alias migration added forwarding modules, but architecture-boundary assertions and module-level design markers were not fully moved with the code.

None of these require a large rewrite, but they should be handled before declaring the patch complete.

## Suggested Fix Order

1. Restore or move the world v4 compatibility boundary marker so the architecture test passes.
2. Create one continue-generation workset/count helper and use it across API, orchestrator progress, review accept/retry continue, MCP, and production scheduler.
3. Add worker-level tests proving `requested_chapters` cannot be overwritten above `max_chapters`.
4. Add schema validation for `ProjectContinueGenerationRequest.max_chapters`.
5. Add checkpoint status historical-data hardening and `resolved_at` update.
6. Make the browser mock regression derive ids from responses.
7. Clean tracked runtime artifacts in a separate hygiene patch.

## Verification Run During Review

Commands run:

```bash
git status --short --branch
git diff --name-status origin/master...HEAD
git diff --check origin/master...HEAD
.venv/bin/python -m pytest tests/test_architecture_boundaries.py::test_legacy_world_model_is_labeled_projection_in_runtime_paths -q
.venv/bin/python -m pytest tests/test_project_operation_guards.py::ProjectOperationGuardTests::test_continue_generation_task_requested_chapters_honors_max_chapters -q
.venv/bin/python - <<'PY'
from pydantic import ValidationError
from forwin.governance import BandCheckpointDetail
try:
    BandCheckpointDetail(status="approved")
except ValidationError as exc:
    print(type(exc).__name__)
PY
```

Observed results:

- Working tree was clean before this document was added; branch was ahead of `origin/master` by one commit.
- Current patch files are the one-commit diff from `origin/master...HEAD`.
- `git diff --check origin/master...HEAD` passed.
- Architecture boundary test failed as described above.
- New continue-generation task creation test passed.
- Historical invalid checkpoint status reproduces a Pydantic validation error.
