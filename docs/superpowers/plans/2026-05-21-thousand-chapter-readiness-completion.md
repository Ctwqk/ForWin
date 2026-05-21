# Thousand-Chapter Readiness Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the P0-P2 thousand-chapter readiness backend loop so accepted chapters expose pulp beat/extraction metrics, fatal payoff drift can stop a run, pressure reports compute real KPIs, and queued generation tasks can be reclaimed through DB leases.

**Architecture:** Keep the existing generation loop and API daemon threads, then add small canonical helpers around them: `Project.target_total_chapters` defaults to 50, pulp beat policy is evaluated from decision events, degraded structured extraction records deferred maintenance, pressure reports remain read-only, and durable execution is exposed through a focused lease worker. The implementation uses existing `DecisionEvent`, `GenerationTask`, `LongRunPolicy`, and deferred maintenance primitives without Saga, Kafka, or new legacy compatibility.

**Tech Stack:** Python 3.12+, Pydantic, SQLAlchemy/PostgreSQL, Alembic migrations, pytest, current ForWin orchestration/runtime modules.

---

## File Structure

- Modify `forwin/models/project.py`: change ORM project target default to 50.
- Modify `forwin/models/base.py`: keep bootstrap-created databases aligned with target default and restart recovery lease fields.
- Create `forwin/migrations/versions/0014_project_target_default.py`: set Postgres server default for new direct inserts.
- Modify `forwin/governance.py`: add `pulp_beat_evaluated`.
- Create `forwin/checker/pulp_policy.py`: evaluate warning/fatal pulp beat policy from recent decision events.
- Modify `forwin/orchestrator_loop_core/project_chapters.py`: record pulp beat events, apply fatal payoff policy, and defer degraded structured extraction.
- Modify `scripts/pulp_pressure_test.py`: add KPI fields and summary calculations.
- Modify `forwin/api_core/tasks.py`: make interrupted resumable generation tasks claimable instead of failed.
- Create `forwin/generation/worker.py`: claim and run one DB-lease-backed generation task.
- Add or extend tests:
  - `tests/test_project_schema_long_run.py`
  - `tests/test_pulp_beat_verifier.py`
  - `tests/test_hard_floor.py`
  - `tests/test_chapter_writer_extraction_windows.py`
  - `tests/test_pulp_pressure_test.py`
  - `tests/test_generation_task_lease.py`

## Task 1: Entry Defaults

**Files:**
- Modify: `forwin/models/project.py`
- Modify: `forwin/models/base.py`
- Create: `forwin/migrations/versions/0014_project_target_default.py`
- Test: `tests/test_project_schema_long_run.py`

- [ ] **Step 1: Add failing ORM default test**

Append to `tests/test_project_schema_long_run.py`:

```python
from forwin.models.project import Project


def test_project_model_default_target_total_is_long_run_ready() -> None:
    project = Project(title="长篇", premise="p", genre="都市")

    assert project.target_total_chapters is None or project.target_total_chapters == 50
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
python3 -m pytest tests/test_project_schema_long_run.py::test_project_model_default_target_total_is_long_run_ready -q
```

Expected: FAIL because the ORM default is still 3 or not initialized until flush.

- [ ] **Step 3: Implement defaults**

Change `forwin/models/project.py`:

```python
target_total_chapters: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
```

Add `forwin/migrations/versions/0014_project_target_default.py`:

```python
from __future__ import annotations

from alembic import op


revision = "0014_project_target_default"
down_revision = "0013_trope_usage_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("projects", "target_total_chapters", server_default="50")


def downgrade() -> None:
    op.alter_column("projects", "target_total_chapters", server_default="3")
```

- [ ] **Step 4: Align bootstrap upgrade**

Add a helper in `forwin/models/base.py` and call it from `_upgrade_postgresql_database()`:

```python
def _upgrade_project_target_default(conn) -> None:
    conn.execute(text("ALTER TABLE projects ALTER COLUMN target_total_chapters SET DEFAULT 50"))
```

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m pytest tests/test_project_schema_long_run.py -q
```

Expected: PASS.

## Task 2: Pulp Beat Policy Events And Fatal Consecutive Misses

**Files:**
- Modify: `forwin/governance.py`
- Create: `forwin/checker/pulp_policy.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_pulp_beat_verifier.py`
- Test: `tests/test_hard_floor.py`

- [ ] **Step 1: Add tests**

Add tests proving:

```python
def test_pulp_missing_payoff_is_warning_until_threshold() -> None:
    ...


def test_pulp_consecutive_missing_payoff_stops_current_run() -> None:
    ...


def test_standard_profile_missing_payoff_stays_warning_only() -> None:
    ...
```

The expected behavior is: standard mode accepts warning-only; pulp/factory mode fails the current chapter after `payoff_gap_limit` consecutive `visible_payoff_present=False` events.

- [ ] **Step 2: Add event type**

In `forwin/governance.py`, add:

```python
PULP_BEAT_EVALUATED = "pulp_beat_evaluated"
```

and include it in `KNOWN_DECISION_EVENT_TYPES`.

- [ ] **Step 3: Implement policy helper**

Create `forwin/checker/pulp_policy.py` with a function that accepts session, project id, chapter number, current hard-floor result, and long-run policy. It queries prior `DecisionEvent` rows in reverse order and returns a fatal reason when consecutive missing payoff count reaches the threshold.

- [ ] **Step 4: Wire orchestrator**

In `project_chapters.py`, after `run_hard_floor()`, always record a `pulp_beat_evaluated` event when pulp beat metadata exists. Then call the policy helper; if fatal, treat it like a hard-floor failure by marking the chapter failed, recording a hard gate event, appending the chapter to `failed_chapters`, and breaking the loop.

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m pytest tests/test_pulp_beat_verifier.py tests/test_hard_floor.py -q
```

Expected: PASS.

## Task 3: Deferred Structured Extraction Maintenance

**Files:**
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_chapter_writer_extraction_windows.py`
- Test: `tests/test_hard_floor.py`

- [ ] **Step 1: Add failing test**

Add a loop test where accepted `WriterOutput.generation_meta` contains:

```python
{
    "structured_extraction": "partial_degraded",
    "state_event_extraction": "degraded"
}
```

Expected: chapter remains accepted and a deferred maintenance event is recorded with `task_type == "structured_extraction"`.

- [ ] **Step 2: Implement helper**

Add a small helper in `project_chapters.py` that scans `writer_output.generation_meta` for `structured_extraction in {"degraded", "partial_degraded"}` and calls `record_deferred_maintenance()`.

- [ ] **Step 3: Verify**

Run:

```bash
python3 -m pytest tests/test_chapter_writer_extraction_windows.py tests/test_hard_floor.py -q
```

Expected: PASS.

## Task 4: Pressure Report KPIs

**Files:**
- Modify: `scripts/pulp_pressure_test.py`
- Test: `tests/test_pulp_pressure_test.py`

- [ ] **Step 1: Extend seeded test data**

Seed decision events for `pulp_beat_evaluated`, `hard_gate_hit`, and `deferred_maintenance_recorded`, plus chapter experience plans with repeated trope ids/categories.

- [ ] **Step 2: Implement summary fields**

Add summary fields:

```python
"p95_wall_time_seconds"
"prompt_char_count_slope"
"context_pack_char_count_slope"
"reward_gap_p95"
"visible_payoff_missing_rate"
"hard_floor_fail_rate"
"canon_extraction_failure_rate"
"repeat_trope_template_rate"
"repeat_trope_category_rate"
```

- [ ] **Step 3: Add per-row pulp fields**

Add `visible_payoff_present`, `pulp_missing_fields`, and `structured_extraction_status` to `ChapterMetric`.

- [ ] **Step 4: Verify**

Run:

```bash
python3 -m pytest tests/test_pulp_pressure_test.py -q
```

Expected: PASS and README remains read-only telemetry wording.

## Task 5: Durable Worker And Restart Recovery

**Files:**
- Modify: `forwin/api_core/tasks.py`
- Create: `forwin/generation/worker.py`
- Test: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Add recovery test**

Add a test that creates a non-terminal running generation task with an expired or missing lease and asserts restart recovery leaves it claimable instead of failed.

- [ ] **Step 2: Add worker test**

Add a test for `run_one_generation_task()` using monkeypatched execution functions. Expected behavior: it claims a queued task, records heartbeat fields, dispatches continue generation for project tasks, and returns a result with the task id.

- [ ] **Step 3: Implement recovery change**

In `_recover_interrupted_generation_tasks()`, replace the non-cancel/non-pause failure branch with claimable queued state when the row has no failed chapters:

```python
task["status"] = "queued"
task["current_stage"] = "queued"
task["lease_owner"] = ""
task["lease_expires_at"] = now
task["message"] = "服务重启后生成任务已重新排队，等待 durable worker 接管。"
task["error"] = None
```

Keep failed-chapter blockers failed.

- [ ] **Step 4: Implement worker**

Create `forwin/generation/worker.py` with `run_one_generation_task(session_factory, worker_id, config, execute_continue=None, execute_new=None)`. The function claims one task via `claim_generation_task()`, commits the lease, calls the injected execution callback, heartbeats on success, and returns a small Pydantic result.

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py -q
```

Expected: PASS.

## Task 6: Final Verification

**Files:**
- All touched files

- [ ] **Step 1: Focused tests**

Run:

```bash
python3 -m pytest tests/test_long_run_policy.py tests/test_project_schema_long_run.py tests/test_hard_floor.py tests/test_deferred_maintenance.py tests/test_pulp_beat_verifier.py tests/test_pulp_pressure_test.py tests/test_chapter_writer_extraction_windows.py tests/test_generation_task_lease.py tests/test_retrieval_typed_budget.py tests/test_trope_cooldown.py -q
```

Expected: PASS.

- [ ] **Step 2: Compile**

Run:

```bash
python3 -m compileall -q forwin scripts
```

Expected: exit 0.

- [ ] **Step 3: Legacy audit**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
```

Expected: PASS with zero issues.

- [ ] **Step 4: Diff check**

Run:

```bash
git diff --check
```

Expected: no output.

## Self-Review

- Spec coverage: every completion spec goal has a task: defaults in Task 1, pulp beat policy in Task 2, deferred extraction in Task 3, pressure KPIs in Task 4, durable worker/restart recovery in Task 5, verification in Task 6.
- Placeholder scan: no intentionally undefined requirements remain in this plan.
- Type consistency: task names use existing modules and new helper names consistently: `pulp_beat_evaluated`, `structured_extraction`, `run_one_generation_task`, and `Project.target_total_chapters`.
