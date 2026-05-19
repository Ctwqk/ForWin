# Review Engine P0 Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three verified review-system bugs before adding the review-engine architecture.

**Architecture:** Keep this as a narrow stabilization patch. Preserve arc-level repair scope, wire existing production review quota into real executor callbacks, and pass deferred-obligation budget state into the existing canon admission gate without changing gate rules.

**Tech Stack:** Python 3, Pydantic, SQLAlchemy, pytest, existing ForWin reviewer, production scheduler, narrative obligation, and canon quality modules.

---

### Task 1: Preserve Arc Repair Scope During Review Merge

**Files:**
- Modify: `forwin/reviewer/hub.py`
- Test: `tests/test_reviewer_split.py`

- [ ] **Step 1: Write the failing merge test**

Add this test to `tests/test_reviewer_split.py`:

```python
def test_historical_review_hub_merge_preserves_arc_repair_scope() -> None:
    from forwin.protocol.review import RepairInstruction
    from forwin.reviewer.hub import HistoricalReviewHub

    base = RepairInstruction(
        repair_scope="chapter_plan",
        failure_type="mixed",
        must_fix=["chapter pacing"],
        scope_reason="chapter-level issue",
    )
    arc = RepairInstruction(
        repair_scope="arc",
        failure_type="mixed",
        must_fix=["identity ambiguity"],
        scope_reason="arc-level issue",
    )

    merged = HistoricalReviewHub._merge_repair_instructions(
        continuity_instruction=base,
        governance_instruction=None,
        webnovel_instruction=arc,
    )

    assert merged is not None
    assert merged.repair_scope == "arc"
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m pytest tests/test_reviewer_split.py::test_historical_review_hub_merge_preserves_arc_repair_scope -q
```

Expected: FAIL because the merged scope is normalized or downgraded away from `arc`.

- [ ] **Step 3: Implement the minimal scope fix**

In `forwin/reviewer/hub.py`, update `_merge_repair_instructions()` so arc is preserved:

```python
merged_scope = normalize_repair_scope(
    merged_scope,
    preserve_v4=(merged_scope in {"arc", "world_model"}),
)
```

Remove any line that rewrites `merged_scope == "arc"` to `band` or `band_plan`.

- [ ] **Step 4: Run the targeted test and related reviewer tests**

Run:

```bash
python3 -m pytest tests/test_reviewer_split.py::test_historical_review_hub_merge_preserves_arc_repair_scope -q
python3 -m pytest tests/test_reviewer_split.py tests/test_repair_scope_router.py -q
```

Expected: all pass.

### Task 2: Carry Review Chapter Status Through Production Plans

**Files:**
- Modify: `forwin/production/planner.py`
- Test: `tests/test_production_planner.py`

- [ ] **Step 1: Add planner status metadata test**

Add:

```python
def test_planner_records_review_candidate_statuses() -> None:
    plan = ProductionPlanner().plan(
        policy=ProductionPolicy(enabled=True, quota=ProductionQuota(write=0, review=3), stop_when_review_pending=False),
        backlog=ProductionBacklog(
            project_id="project-1",
            needs_review=[2],
            drafted_unreviewed=[3, 4],
            chapter_plan_count=4,
        ),
        now=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )

    assert plan.review_chapters == [2, 3, 4]
    assert plan.review_chapter_statuses == {2: "needs_review", 3: "drafted", 4: "drafted"}
```

- [ ] **Step 2: Run the planner test and verify RED**

Run:

```bash
python3 -m pytest tests/test_production_planner.py::test_planner_records_review_candidate_statuses -q
```

Expected: FAIL because `ProductionPlan` has no `review_chapter_statuses`.

- [ ] **Step 3: Add status metadata to `ProductionPlan`**

In `forwin/production/planner.py`:

```python
class ProductionPlan(BaseModel):
    project_id: str
    date: str
    plan_chapters: list[int] = Field(default_factory=list)
    write_chapters: list[int] = Field(default_factory=list)
    review_chapters: list[int] = Field(default_factory=list)
    review_chapter_statuses: dict[int, str] = Field(default_factory=dict)
    publish_chapters: list[int] = Field(default_factory=list)
```

When review candidates are selected:

```python
if policy.quota.review > 0:
    review_candidates = [*backlog.needs_review, *backlog.drafted_unreviewed]
    plan.review_chapters.extend(review_candidates[: policy.quota.review])
    plan.review_chapter_statuses.update(
        {
            chapter_number: "needs_review"
            for chapter_number in backlog.needs_review
            if chapter_number in plan.review_chapters
        }
    )
    plan.review_chapter_statuses.update(
        {
            chapter_number: "drafted"
            for chapter_number in backlog.drafted_unreviewed
            if chapter_number in plan.review_chapters
        }
    )
```

- [ ] **Step 4: Run production planner tests**

Run:

```bash
python3 -m pytest tests/test_production_planner.py -q
```

Expected: all pass.

### Task 3: Execute Production Review Quota Jobs

**Files:**
- Modify: `forwin/production/events.py`
- Modify: `forwin/production/executor.py`
- Modify: `forwin/production/scheduler.py`
- Modify: `forwin/api_automation.py`
- Modify: `forwin/api_core/automation.py`
- Test: `tests/test_production_executor.py`
- Test: `tests/test_production_scheduler.py`

- [ ] **Step 1: Add executor test for review jobs**

Add to `tests/test_production_executor.py`:

```python
def test_executor_consumes_review_quota_jobs_before_reporting_idle() -> None:
    review_calls: list[tuple[str, int]] = []
    approve_calls: list[tuple[str, int]] = []
    project = Project(id="project-1", title="测试书", premise="前提", genre="玄幻")
    plan = ProductionPlan(
        project_id=project.id,
        date="2026-05-05",
        review_chapters=[2, 3],
        review_chapter_statuses={2: "needs_review", 3: "drafted"},
    )

    result = ProductionExecutor(
        create_generation_task=lambda **_kwargs: "unexpected",
        create_continue_generation_task=lambda **_kwargs: "unexpected",
        active_generation_task_error_cls=ActiveGenerationTaskError,
        review_chapter=lambda project_id, chapter_number: review_calls.append((project_id, chapter_number)),
        approve_chapter_review=lambda project_id, chapter_number: approve_calls.append((project_id, chapter_number)),
    ).execute(
        plan=plan,
        project=project,
        policy=policy_from_automation(
            normalize_project_automation({"daily_chapter_quota": 1, "daily_review_quota": 2})
        ),
        runtime_config=SimpleNamespace(),
    )

    assert result.action == "ran_review_jobs"
    assert result.review_job_count == 2
    assert approve_calls == [(project.id, 2)]
    assert review_calls == [(project.id, 3)]
```

- [ ] **Step 2: Run the executor test and verify RED**

Run:

```bash
python3 -m pytest tests/test_production_executor.py::test_executor_consumes_review_quota_jobs_before_reporting_idle -q
```

Expected: FAIL because executor has no review callbacks or review action.

- [ ] **Step 3: Add review action event and result field**

In `forwin/production/events.py`:

```python
ACTION_RAN_REVIEW_JOBS = "ran_review_jobs"
```

Add message handling:

```python
if action == ACTION_RAN_REVIEW_JOBS:
    return f"已按计划处理 {max(0, int(chapter_count or 0))} 个 review 任务。"
```

In `ProductionExecutionResult`:

```python
review_job_count: int = 0
```

- [ ] **Step 4: Add executor review callbacks**

In `ProductionExecutor.__init__()` add:

```python
review_chapter: Callable[[str, int], Any] | None = None,
approve_chapter_review: Callable[[str, int], Any] | None = None,
```

Store them:

```python
self.review_chapter = review_chapter
self.approve_chapter_review = approve_chapter_review
```

Add helper:

```python
def _execute_review_jobs(self, *, plan: ProductionPlan, project: Project) -> int:
    total = 0
    for chapter_number in plan.review_chapters:
        normalized_chapter = int(chapter_number or 0)
        if normalized_chapter <= 0:
            continue
        status = str(plan.review_chapter_statuses.get(normalized_chapter, "") or "").strip()
        callback = self.approve_chapter_review if status == "needs_review" else self.review_chapter
        if callback is None:
            continue
        callback(str(project.id), normalized_chapter)
        total += 1
    return total
```

Call it after generation task creation and before publish jobs:

```python
review_job_count = self._execute_review_jobs(plan=plan, project=project)
if action == ACTION_IDLE and review_job_count > 0:
    action = ACTION_RAN_REVIEW_JOBS
```

Return `review_job_count` and use it as `chapter_count` for `ACTION_RAN_REVIEW_JOBS`.

- [ ] **Step 5: Wire scheduler and API callbacks**

In `ProductionScheduler.__init__()` add and store `review_chapter` and `approve_chapter_review` callbacks, then pass them into `ProductionExecutor`.

In `api_automation.run_automation_scheduler_pass()` add optional callback parameters and include them in `scheduler_kwargs`.

In `forwin/api_core/automation.py`, add:

```python
def _run_scheduled_review_action(project_id: str, chapter_number: int) -> Any:
    if api_state._orchestrator is None:
        return None
    return api_state._orchestrator.accept_review(
        project_id,
        int(chapter_number),
        reason="production_scheduler_review_quota",
    )
```

Pass this function for both P0 callbacks:

```python
review_chapter=_run_scheduled_review_action,
approve_chapter_review=_run_scheduled_review_action,
```

- [ ] **Step 6: Run production tests**

Run:

```bash
python3 -m pytest tests/test_production_executor.py tests/test_production_planner.py tests/test_production_scheduler.py -q
```

Expected: all pass.

### Task 4: Pass Obligation Budget Into Deferred Canon Admission

**Files:**
- Modify: `forwin/narrative_obligations/transaction.py`
- Test: `tests/test_defer_acceptance_transaction.py`

- [ ] **Step 1: Add budget-exceeded transaction regression**

Add a test that seeds two existing structural P1 obligations, then attempts a third structural P1 deferral in the same arc. Assert:

```python
assert result.success is False
assert "obligation_budget_exceeded" in result.errors
assert session.get(NarrativeObligationRow, "obl-budget") is None
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m pytest tests/test_defer_acceptance_transaction.py::test_defer_acceptance_transaction_blocks_when_obligation_budget_is_exceeded -q
```

Expected: FAIL because transaction succeeds instead of blocking on budget.

- [ ] **Step 3: Add transaction budget helpers**

In `forwin/narrative_obligations/transaction.py`, import:

```python
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from .budget import evaluate_obligation_budget
```

Add `_open_obligations_for_project()`, `_budget_band_bounds()`, `_budget_arc_bounds()`, and `_evaluate_transaction_budget()` helpers. The helpers should:

- include open statuses `proposed`, `planned`, `active`, `expired`
- exclude the new prepared obligation id
- derive band bounds from target band or affected chapters
- derive arc bounds from target plan, target band, target arc, or arc row covering the affected chapter

- [ ] **Step 4: Pass budget result into canon admission**

Before `with self.session.begin_nested():`, compute:

```python
budget_result = _evaluate_transaction_budget(
    session=self.session,
    repo=repo,
    obligation=prepared_obligation,
    plan_patch=prepared_patch,
    current_chapter=current_chapter,
    target_total_chapters=target_total_chapters,
    target_plan=target_plan,
    target_band=target_band,
)
```

In the gate call:

```python
gate_result = evaluate_canon_admission(
    ...,
    over_budget=budget_result.over_budget,
)
```

- [ ] **Step 5: Run deferred acceptance tests**

Run:

```bash
python3 -m pytest tests/test_defer_acceptance_transaction.py tests/test_obligation_budget.py tests/test_orchestrator_deferred_acceptance.py -q
```

Expected: all pass.

### Task 5: Final P0 Verification

**Files:**
- Verify all modified P0 files.

- [ ] **Step 1: Run all focused P0 tests**

Run:

```bash
python3 -m pytest tests/test_reviewer_split.py tests/test_repair_scope_router.py -q
python3 -m pytest tests/test_production_executor.py tests/test_production_planner.py tests/test_production_scheduler.py -q
python3 -m pytest tests/test_defer_acceptance_transaction.py tests/test_obligation_budget.py tests/test_orchestrator_deferred_acceptance.py -q
```

Expected: all pass.

- [ ] **Step 2: Run syntax and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin
git diff --check
```

Expected: both pass with no output.

- [ ] **Step 3: Commit P0 only**

Stage only P0 files:

```bash
git add forwin/reviewer/hub.py \
  forwin/production/events.py \
  forwin/production/planner.py \
  forwin/production/executor.py \
  forwin/production/scheduler.py \
  forwin/api_automation.py \
  forwin/api_core/automation.py \
  forwin/narrative_obligations/transaction.py \
  tests/test_reviewer_split.py \
  tests/test_production_planner.py \
  tests/test_production_executor.py \
  tests/test_defer_acceptance_transaction.py
git commit -m "fix: stabilize review engine prerequisites"
```
