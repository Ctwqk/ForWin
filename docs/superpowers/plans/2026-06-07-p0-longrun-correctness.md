# P0 Long-Run Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent long-running generation from accepting chapters with failed canon commits, expiring active worker leases, or continuing after core chapter failures by default.

**Architecture:** Keep the changes local to the existing orchestration and worker control-flow modules. `CanonApplyOutcome` remains the shared canon-blocking contract, worker lease refresh reuses the existing `heartbeat_generation_task`, and chapter failure stopping reads the existing optional `long_run_policy` attribute without adding a new `Config` field.

**Tech Stack:** Python, SQLAlchemy, Pydantic models, pytest, existing ForWin orchestration and generation worker modules.

---

## Scope Check

The approved spec contains three P0 repairs that all protect the same long-run correctness boundary. They can be implemented in one plan because each task is independently testable and the files touched are limited to canon commit handling, worker lease updates, and chapter failure control flow. P1/P2/P3 backlog items remain documented in `docs/superpowers/specs/2026-06-07-p0-longrun-correctness-design.md` and are not part of this plan.

## File Structure

- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
  - Return a blocked `CanonApplyOutcome` on canon apply exceptions even when no frozen artifact exists.
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
  - Add a small helper for default-true `stop_on_chapter_failure` handling and use it in the generic chapter exception path.
- Modify: `forwin/generation/worker.py`
  - Thread `worker_id` and `lease_seconds` into `_db_task_updater` and refresh leases via `heartbeat_generation_task` on progress updates.
- Modify: `tests/test_canon_repair_stage.py`
  - Reverse the existing freeze-disabled canon exception assertion and add a run-level regression.
- Modify: `tests/test_generation_task_lease.py`
  - Add runtime task-update lease refresh coverage.
- Create: `tests/test_chapter_failure_stop_policy.py`
  - Cover default failure-stop behavior and explicit opt-out.

### Task 1: Canon Apply Exceptions Always Block Acceptance

**Files:**
- Modify: `tests/test_canon_repair_stage.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py:1093-1098`

- [ ] **Step 1: Replace the existing freeze-disabled canon exception test**

In `tests/test_canon_repair_stage.py`, replace `test_apply_canon_candidate_exception_without_freeze_returns_unblocked_outcome` with:

```python
def test_apply_canon_candidate_exception_without_freeze_returns_blocked_outcome(
    monkeypatch,
):
    failed_rows: list[dict[str, object]] = []

    class _CandidateDraftRepo:
        def __init__(self, _session) -> None:
            return None

        def mark_canon_failed(self, **kwargs):
            failed_rows.append(kwargs)
            return None

    class _Session:
        def __init__(self) -> None:
            self.rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

    class _ArtifactStore:
        def save_frozen_candidate(self, **_kwargs):
            raise AssertionError("freeze_failed_candidates=False should not freeze")

    class _Orchestrator:
        config = SimpleNamespace(freeze_failed_candidates=False)
        artifact_store = _ArtifactStore()

        def _record_decision_event(self, **_kwargs) -> None:
            return None

        def _apply_canon_quality_gate(self, **_kwargs):
            raise RuntimeError("canon apply failed")

    monkeypatch.setattr(
        quality_gates_module,
        "CandidateDraftRepository",
        _CandidateDraftRepo,
    )
    session = _Session()

    outcome = quality_gates_module._apply_canon_candidate(
        _Orchestrator(),
        session=session,
        repo=object(),
        updater=object(),
        project_id="p",
        chapter_number=2,
        writer_output=object(),
        verdict=object(),
    )

    assert isinstance(outcome, CanonApplyOutcome)
    assert outcome.blocked
    assert outcome.blocked_path == ""
    assert outcome.block_kind == "canon_apply_error"
    assert session.rolled_back is True
    assert failed_rows[0]["canon_artifact_path"] == ""
```

- [ ] **Step 2: Add the run-level canon exception regression**

In `tests/test_canon_repair_stage.py`, after `test_coerce_canon_apply_outcome_rejects_truthy_non_string_values`, add:

```python
def test_canon_apply_exception_without_freeze_pauses_chapter_instead_of_accepting():
    class PassReviewHub:
        def review(self, **_kwargs) -> ReviewVerdict:
            return ReviewVerdict(
                verdict="pass",
                issues=[],
                review_summary="accepted by test reviewer",
            )

    db_path = postgres_test_url("canon-apply-exception-no-freeze")
    orchestrator = WritingOrchestrator(
        Config(
            database_url=db_path,
            minimax_api_key="",
            minimax_model="fake-model",
            chapter_review_form_mode="off",
            operation_mode="blackbox",
            freeze_failed_candidates=False,
            auto_band_checkpoint=False,
            manual_checkpoints_enabled=False,
        )
    )
    try:
        orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: _one_chapter_arc(
            "canon apply exception"
        )
        orchestrator.writer.write_chapter = lambda context: _writer_output(context.chapter_number)
        orchestrator.review_hub = PassReviewHub()

        def fail_canon_quality_gate(**_kwargs):
            raise RuntimeError("canon apply failed")

        orchestrator._apply_canon_quality_gate = fail_canon_quality_gate

        result = orchestrator.run("p", "g", 1)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            plan = session.execute(select(ChapterPlan)).scalar_one()
        finally:
            session.close()
            engine.dispose()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert result.status == "needs_review"
    assert result.completed_chapters == []
    assert result.paused_chapters == [1]
    assert plan.status == "needs_review"
```

- [ ] **Step 3: Run the canon tests and confirm failure**

Run:

```bash
pytest tests/test_canon_repair_stage.py::test_apply_canon_candidate_exception_without_freeze_returns_blocked_outcome tests/test_canon_repair_stage.py::test_canon_apply_exception_without_freeze_pauses_chapter_instead_of_accepting -q
```

Expected: both tests fail before implementation. The unit test should show `outcome.blocked` is false; the run-level regression should show the chapter reaches `completed` or `accepted` instead of `needs_review`.

- [ ] **Step 4: Change canon exception return semantics**

In `forwin/orchestrator_loop_core/quality_gates.py`, replace the final conditional return in the exception branch:

```python
        if frozen_path:
            return CanonApplyOutcome(
                blocked_path=frozen_path,
                block_kind="canon_apply_error",
            )
        return CanonApplyOutcome()
```

with:

```python
        return CanonApplyOutcome(
            blocked_path=frozen_path,
            block_kind="canon_apply_error",
        )
```

- [ ] **Step 5: Run the canon tests and confirm pass**

Run:

```bash
pytest tests/test_canon_repair_stage.py::test_apply_canon_candidate_exception_without_freeze_returns_blocked_outcome tests/test_canon_repair_stage.py::test_canon_apply_exception_without_freeze_pauses_chapter_instead_of_accepting -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add forwin/orchestrator_loop_core/quality_gates.py tests/test_canon_repair_stage.py
git commit -m "fix: block canon apply failures without artifacts"
```

Expected: commit succeeds with only the canon files staged.

### Task 2: Refresh Worker Lease On Runtime Progress Updates

**Files:**
- Modify: `tests/test_generation_task_lease.py`
- Modify: `forwin/generation/worker.py:186`
- Modify: `forwin/generation/worker.py:234`
- Modify: `forwin/generation/worker.py:259-268`

- [ ] **Step 1: Import the updater helper in the lease tests**

In `tests/test_generation_task_lease.py`, replace:

```python
from forwin.generation.worker import run_one_generation_task
```

with:

```python
from forwin.generation.worker import _db_task_updater, run_one_generation_task
```

- [ ] **Step 2: Add the failing runtime lease refresh test**

In `tests/test_generation_task_lease.py`, after `test_heartbeat_extends_matching_running_lease`, add:

```python
def test_db_task_updater_refreshes_owned_running_lease_and_prevents_reclaim() -> None:
    engine = get_engine(postgres_test_url("generation-task-runtime-heartbeat"))
    init_db(engine)
    Session = get_session_factory(engine)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-runtime-heartbeat",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="worker-1",
                    lease_expires_at=expired,
                    heartbeat_at=expired,
                )
            )

        update_task = _db_task_updater(
            Session,
            worker_id="worker-1",
            lease_seconds=300,
        )
        update_task(
            "task-runtime-heartbeat",
            current_stage="writing_chapter",
            current_chapter=2,
        )

        with Session.begin() as session:
            task = session.get(GenerationTask, "task-runtime-heartbeat")
            assert task is not None
            assert task.current_stage == "writing_chapter"
            assert task.current_chapter == 2
            assert task.heartbeat_at is not None
            assert task.lease_expires_at is not None
            assert task.heartbeat_at != expired
            assert task.lease_expires_at != expired

        with Session.begin() as session:
            claim = claim_generation_task(
                session,
                worker_id="worker-2",
                lease_seconds=300,
            )

        assert claim is None
    finally:
        engine.dispose()
```

- [ ] **Step 3: Run the worker lease test and confirm failure**

Run:

```bash
pytest tests/test_generation_task_lease.py::test_db_task_updater_refreshes_owned_running_lease_and_prevents_reclaim -q
```

Expected: FAIL with `TypeError: _db_task_updater() got an unexpected keyword argument 'worker_id'`.

- [ ] **Step 4: Pass worker identity into runtime task updaters**

In `forwin/generation/worker.py`, replace both default executor updater assignments:

```python
        update_task = _db_task_updater(session_factory)
```

with:

```python
        update_task = _db_task_updater(
            session_factory,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
```

There are two occurrences: one in `_default_continue_executor`, and one in `_default_new_executor`.

- [ ] **Step 5: Extend `_db_task_updater` to refresh active owned leases**

In `forwin/generation/worker.py`, replace `_db_task_updater` with:

```python
def _db_task_updater(
    session_factory: Callable[[], Any],
    *,
    worker_id: str = "",
    lease_seconds: int = 300,
) -> Callable[..., None]:
    def _update(task_id: str, **changes: Any) -> None:
        with session_factory.begin() as session:
            row = session.get(GenerationTask, task_id)
            if row is None:
                return
            _apply_task_changes(row, changes)
            if str(worker_id or "").strip():
                heartbeat_generation_task(
                    session,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
            session.add(row)

    return _update
```

This reuses `heartbeat_generation_task`, which only updates the lease when the row is still `running` and still owned by the same worker.

- [ ] **Step 6: Run the worker lease test and confirm pass**

Run:

```bash
pytest tests/test_generation_task_lease.py::test_db_task_updater_refreshes_owned_running_lease_and_prevents_reclaim -q
```

Expected: PASS.

- [ ] **Step 7: Run adjacent generation worker tests**

Run:

```bash
pytest tests/test_generation_task_lease.py tests/test_generation_worker_observability.py -q
```

Expected: PASS. Existing claim, resume, reclaim, and heartbeat failure behavior remains unchanged.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add forwin/generation/worker.py tests/test_generation_task_lease.py
git commit -m "fix: refresh generation worker leases during progress"
```

Expected: commit succeeds with only worker lease files staged.

### Task 3: Stop On Core Chapter Failure By Default

**Files:**
- Create: `tests/test_chapter_failure_stop_policy.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py:22-38`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py:1011-1017`

- [ ] **Step 1: Add failure stop policy tests**

Create `tests/test_chapter_failure_stop_policy.py` with:

```python
from __future__ import annotations

from sqlalchemy import select

from forwin.config import Config
from forwin.long_run_policy import LongRunPolicy
from forwin.models.base import get_engine, get_session_factory
from forwin.models.project import ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator_loop_core.quality_gates import CanonApplyOutcome
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from tests.postgres import postgres_test_url


class PassReviewHub:
    def review(self, **_kwargs) -> ReviewVerdict:
        return ReviewVerdict(
            verdict="pass",
            issues=[],
            review_summary="accepted by test reviewer",
        )


def _two_chapter_arc() -> dict[str, object]:
    return {
        "arc_synopsis": "failure stop policy",
        "setting_summary": "无",
        "chapters": [
            {
                "chapter_number": 1,
                "title": "第一章",
                "one_line": "开场",
                "goals": ["推进主线"],
            },
            {
                "chapter_number": 2,
                "title": "第二章",
                "one_line": "承接",
                "goals": ["继续推进主线"],
            },
        ],
        "characters": [],
        "locations": [],
        "factions": [],
        "relations": [],
        "plot_threads": [],
        "initial_time": {"label": "开始", "description": "开始"},
    }


def _writer_output(chapter_number: int) -> WriterOutput:
    return WriterOutput(
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        body="正文" * 900,
        char_count=1800,
        end_of_chapter_summary="ok",
        state_changes=[],
        new_events=[],
        thread_beats=[],
        time_advance=None,
    )


def _config(database_url: str, *, stop_on_chapter_failure: bool | None = None) -> Config:
    config = Config(
        database_url=database_url,
        minimax_api_key="",
        minimax_model="fake-model",
        chapter_review_form_mode="off",
        operation_mode="blackbox",
        freeze_failed_candidates=False,
        auto_band_checkpoint=False,
        manual_checkpoints_enabled=False,
    )
    if stop_on_chapter_failure is not None:
        object.__setattr__(
            config,
            "long_run_policy",
            LongRunPolicy(stop_on_chapter_failure=stop_on_chapter_failure),
        )
    return config


def test_generic_chapter_failure_stops_run_by_default() -> None:
    db_path = postgres_test_url("chapter-failure-stop-default")
    orchestrator = WritingOrchestrator(_config(db_path))
    calls: list[int] = []
    try:
        orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: _two_chapter_arc()
        orchestrator.review_hub = PassReviewHub()
        orchestrator._apply_canon_candidate = lambda **_kwargs: CanonApplyOutcome()

        def write_chapter(context):
            calls.append(context.chapter_number)
            if context.chapter_number == 1:
                raise RuntimeError("generic writer failure")
            return _writer_output(context.chapter_number)

        orchestrator.writer.write_chapter = write_chapter

        result = orchestrator.run("p", "g", 2)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            statuses = [
                (row.chapter_number, row.status)
                for row in session.execute(
                    select(ChapterPlan).order_by(ChapterPlan.chapter_number.asc())
                ).scalars()
            ]
        finally:
            session.close()
            engine.dispose()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert calls == [1]
    assert result.status == "failed"
    assert result.completed_chapters == []
    assert result.failed_chapters == [1]
    assert statuses == [(1, "failed"), (2, "planned")]


def test_generic_chapter_failure_can_continue_when_policy_disables_stop() -> None:
    db_path = postgres_test_url("chapter-failure-stop-disabled")
    orchestrator = WritingOrchestrator(
        _config(db_path, stop_on_chapter_failure=False)
    )
    calls: list[int] = []
    try:
        orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: _two_chapter_arc()
        orchestrator.review_hub = PassReviewHub()
        orchestrator._apply_canon_candidate = lambda **_kwargs: CanonApplyOutcome()

        def write_chapter(context):
            calls.append(context.chapter_number)
            if context.chapter_number == 1:
                raise RuntimeError("generic writer failure")
            return _writer_output(context.chapter_number)

        orchestrator.writer.write_chapter = write_chapter

        result = orchestrator.run("p", "g", 2)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            statuses = [
                (row.chapter_number, row.status)
                for row in session.execute(
                    select(ChapterPlan).order_by(ChapterPlan.chapter_number.asc())
                ).scalars()
            ]
        finally:
            session.close()
            engine.dispose()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert calls == [1, 2]
    assert result.status == "partial_failed"
    assert result.completed_chapters == [2]
    assert result.failed_chapters == [1]
    assert statuses == [(1, "failed"), (2, "accepted")]
```

- [ ] **Step 2: Run the default failure-stop test and confirm failure**

Run:

```bash
pytest tests/test_chapter_failure_stop_policy.py::test_generic_chapter_failure_stops_run_by_default -q
```

Expected: FAIL because the current loop continues to chapter 2, so `calls` is `[1, 2]` and status is `partial_failed`.

- [ ] **Step 3: Add the policy helper**

In `forwin/orchestrator_loop_core/project_chapters.py`, after `_coerce_canon_apply_outcome`, add:

```python
def _stop_on_chapter_failure_enabled(config: object) -> bool:
    policy = getattr(config, "long_run_policy", None)
    value = getattr(policy, "stop_on_chapter_failure", True)
    return value if isinstance(value, bool) else True
```

- [ ] **Step 4: Use the policy in the generic exception path**

In `forwin/orchestrator_loop_core/project_chapters.py`, replace:

```python
            if isinstance(exc, TransientLLMChapterFailure) or self._is_transient_llm_like(exc):
                logger.warning(
                    "Stopping run after transient LLM failure on chapter %d to avoid cascading failures.",
                    chapter_num,
                )
                break
            continue
```

with:

```python
            if isinstance(exc, TransientLLMChapterFailure) or self._is_transient_llm_like(exc):
                logger.warning(
                    "Stopping run after transient LLM failure on chapter %d to avoid cascading failures.",
                    chapter_num,
                )
                break
            if _stop_on_chapter_failure_enabled(self.config):
                logger.warning(
                    "Stopping run after chapter %d failure because stop_on_chapter_failure is enabled.",
                    chapter_num,
                )
                break
            continue
```

- [ ] **Step 5: Run failure policy tests and confirm pass**

Run:

```bash
pytest tests/test_chapter_failure_stop_policy.py -q
```

Expected: PASS. The default test stops after chapter 1; the explicit opt-out test continues and accepts chapter 2.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add forwin/orchestrator_loop_core/project_chapters.py tests/test_chapter_failure_stop_policy.py
git commit -m "fix: stop generation on chapter failure by default"
```

Expected: commit succeeds with only failure policy files staged.

### Task 4: Focused Verification

**Files:**
- No new files.
- Verify all files changed by Tasks 1-3.

- [ ] **Step 1: Run focused P0 regression tests**

Run:

```bash
pytest tests/test_canon_repair_stage.py tests/test_generation_task_lease.py tests/test_generation_worker_observability.py tests/test_long_run_policy.py tests/test_chapter_failure_stop_policy.py -q
```

Expected: PASS.

- [ ] **Step 2: Run compile sanity check**

Run:

```bash
python -m compileall forwin
```

Expected: command exits with status 0 and no syntax errors.

- [ ] **Step 3: Inspect final working tree**

Run:

```bash
git status --short
```

Expected: no unstaged or uncommitted implementation changes. If only this plan file remains uncommitted, decide whether to include it in the final implementation commit or leave it as planning documentation according to the execution mode selected by the user.

- [ ] **Step 4: Summarize verification evidence**

Record the exact commands and outcomes in the final response:

```text
pytest tests/test_canon_repair_stage.py tests/test_generation_task_lease.py tests/test_generation_worker_observability.py tests/test_long_run_policy.py tests/test_chapter_failure_stop_policy.py -q
python -m compileall forwin
```

Expected: final response includes whether both commands passed and notes any tests that could not be run.
