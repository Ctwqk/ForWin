# ForWin Auto-Continue Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal ForWin book generation continue through the intended target chapter automatically, while treating arc and band boundaries as audited continuation points rather than manual restart gates.

**Architecture:** Add a small generation auto-continue layer above the existing continue-generation workset and task creation paths. Keep writer, review, and arc materialization logic intact; the new code only resolves run targets, decides whether a completed task should spawn the next task, records audit events, and passes the decision through API/MCP.

**Tech Stack:** Python, FastAPI/Pydantic schemas, SQLAlchemy models/session helpers, existing ForWin MCP HTTP client, pytest.

---

## File Structure

- Create `forwin/generation/run_target.py`: target-resolution helpers for `target_total_chapters`, `run_until_chapter`, and `max_chapters`.
- Create `forwin/generation/auto_continue.py`: controller and decision dataclasses for post-task continuation.
- Modify `forwin/generation/__init__.py`: export the new helpers.
- Modify `forwin/governance.py`: add `DecisionEventType.AUTO_CONTINUE_DECISION` to known event types.
- Modify `forwin/api_schema/genesis.py`: add `StartWritingRequest`.
- Modify `forwin/api_schema/project.py`: add `auto_continue` and `run_until_chapter` to `ProjectContinueGenerationRequest`.
- Modify `forwin/project_ops/genesis.py`: accept start-writing options and pass them into task creation.
- Modify `forwin/project_ops/generation.py`: accept continue-generation options and pass them into task creation.
- Modify `forwin/api_core/generation.py`: persist auto-continue parameters in task creation closures and hook the completion handler.
- Modify `forwin/mcp/client.py` and `forwin/mcp/http.py`: expose `auto_continue` and `run_until_chapter` to MCP callers.
- Modify `forwin/ui_assets/home/app_genesis.js`: update the start-writing confirmation copy so the UI no longer says the run stops at the current arc.
- Add `tests/test_generation_run_target.py`: unit tests for target semantics.
- Add `tests/test_generation_auto_continue.py`: unit tests for continuation decisions and audit payloads.
- Modify `tests/test_project_operation_guards.py`: API regression tests for start/continue request semantics and task creation kwargs.
- Modify `tests/test_mcp_server.py`: MCP regression tests for new optional parameters and old defaults.
- Modify `tests/browser/test_mock_book_creation_generation_regression.py`: preserve empty body compatibility or adjust expected payload only if the UI starts sending explicit defaults.

### Task 1: Run Target Semantics

**Files:**
- Create: `forwin/generation/run_target.py`
- Modify: `forwin/generation/__init__.py`
- Test: `tests/test_generation_run_target.py`

- [ ] **Step 1: Write failing target-resolution tests**

Create `tests/test_generation_run_target.py`:

```python
from __future__ import annotations

import pytest

from forwin.generation.run_target import (
    GenerationRunTarget,
    resolve_generation_run_target,
)


class ProjectStub:
    def __init__(self, target_total_chapters: int) -> None:
        self.target_total_chapters = target_total_chapters


def test_run_target_defaults_to_project_total() -> None:
    target = resolve_generation_run_target(
        ProjectStub(target_total_chapters=60),
        next_chapter=13,
    )

    assert target == GenerationRunTarget(
        target_total_chapters=60,
        run_until_chapter=60,
        next_chapter=13,
        max_chapters=None,
        effective_max_chapters=48,
    )


def test_run_target_uses_explicit_run_until() -> None:
    target = resolve_generation_run_target(
        ProjectStub(target_total_chapters=60),
        next_chapter=25,
        run_until_chapter=36,
    )

    assert target.run_until_chapter == 36
    assert target.effective_max_chapters == 12


def test_run_target_combines_run_until_and_max_chapters() -> None:
    target = resolve_generation_run_target(
        ProjectStub(target_total_chapters=60),
        next_chapter=25,
        run_until_chapter=60,
        max_chapters=5,
    )

    assert target.run_until_chapter == 29
    assert target.effective_max_chapters == 5


def test_run_target_rejects_past_run_until() -> None:
    with pytest.raises(ValueError, match="run_until_chapter must be >= next_chapter"):
        resolve_generation_run_target(
            ProjectStub(target_total_chapters=60),
            next_chapter=25,
            run_until_chapter=24,
        )


def test_run_target_rejects_run_until_beyond_book_total() -> None:
    with pytest.raises(ValueError, match="run_until_chapter must be <= target_total_chapters"):
        resolve_generation_run_target(
            ProjectStub(target_total_chapters=60),
            next_chapter=25,
            run_until_chapter=61,
        )
```

- [ ] **Step 2: Run the failing tests**

Run: `pytest tests/test_generation_run_target.py -q`

Expected: import failure for `forwin.generation.run_target`.

- [ ] **Step 3: Add target-resolution implementation**

Create `forwin/generation/run_target.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GenerationRunTarget:
    target_total_chapters: int
    run_until_chapter: int
    next_chapter: int
    max_chapters: int | None
    effective_max_chapters: int


def resolve_generation_run_target(
    project: Any,
    *,
    next_chapter: int,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
) -> GenerationRunTarget:
    target_total = int(getattr(project, "target_total_chapters", 0) or 0)
    if target_total <= 0:
        raise ValueError("target_total_chapters must be positive")

    normalized_next = int(next_chapter or 0)
    if normalized_next <= 0:
        raise ValueError("next_chapter must be positive")

    normalized_until = int(run_until_chapter or target_total)
    if normalized_until < normalized_next:
        raise ValueError("run_until_chapter must be >= next_chapter")
    if normalized_until > target_total:
        raise ValueError("run_until_chapter must be <= target_total_chapters")

    normalized_max = int(max_chapters) if max_chapters is not None else None
    if normalized_max is not None and normalized_max < 1:
        raise ValueError("max_chapters must be positive when provided")

    remaining_to_until = normalized_until - normalized_next + 1
    effective_max = remaining_to_until
    if normalized_max is not None:
        effective_max = min(effective_max, normalized_max)
        normalized_until = normalized_next + effective_max - 1

    return GenerationRunTarget(
        target_total_chapters=target_total,
        run_until_chapter=normalized_until,
        next_chapter=normalized_next,
        max_chapters=normalized_max,
        effective_max_chapters=effective_max,
    )
```

Modify `forwin/generation/__init__.py`:

```python
from .auto_continue import AutoContinueDecision, GenerationAutoContinueController
from .continue_workset import ContinueGenerationWorkset, build_continue_generation_workset
from .run_target import GenerationRunTarget, resolve_generation_run_target

__all__ = [
    "AutoContinueDecision",
    "ContinueGenerationWorkset",
    "GenerationAutoContinueController",
    "GenerationRunTarget",
    "build_continue_generation_workset",
    "resolve_generation_run_target",
]
```

- [ ] **Step 4: Verify target-resolution tests pass**

Run: `pytest tests/test_generation_run_target.py -q`

Expected: `5 passed`.

- [ ] **Step 5: Commit Task 1**

```bash
git add forwin/generation/run_target.py forwin/generation/__init__.py tests/test_generation_run_target.py
git commit -m "feat: add generation run target semantics"
```

### Task 2: Auto-Continue Controller

**Files:**
- Create: `forwin/generation/auto_continue.py`
- Modify: `forwin/governance.py`
- Test: `tests/test_generation_auto_continue.py`

- [ ] **Step 1: Write failing controller tests**

Create `tests/test_generation_auto_continue.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from forwin.generation.auto_continue import (
    AutoContinueDecision,
    GenerationAutoContinueController,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project


@dataclass
class ResultStub:
    project_id: str
    status: str = "completed"
    completed_chapters: list[int] | None = None
    failed_chapters: list[int] | None = None
    paused_chapters: list[int] | None = None
    paused: bool = False
    cancelled: bool = False


def _session_factory(name: str):
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, get_session_factory(engine)


def _project(session, project_id: str = "project-auto", total: int = 6) -> Project:
    project = Project(
        id=project_id,
        title="Auto Book",
        premise="premise",
        genre="玄幻",
        creation_status="writing",
        target_total_chapters=total,
    )
    session.add(project)
    return project


def _arc(session, *, project_id: str, arc_id: str, number: int, status: str, start: int, end: int) -> None:
    session.add(
        ArcPlanVersion(
            id=arc_id,
            project_id=project_id,
            arc_number=number,
            status=status,
            chapter_start=start,
            chapter_end=end,
            planned_target_size=end - start + 1,
            arc_synopsis=f"arc {number}",
        )
    )


def _chapter(session, *, project_id: str, arc_id: str, number: int, status: str) -> None:
    session.add(
        ChapterPlan(
            id=f"plan-{number}",
            project_id=project_id,
            arc_plan_id=arc_id,
            chapter_number=number,
            title=f"第{number}章",
            status=status,
        )
    )


def test_controller_continues_to_future_arc_when_no_blocker() -> None:
    engine, Session = _session_factory("auto-continue-future-arc")
    calls: list[dict[str, object]] = []
    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            _arc(session, project_id=project.id, arc_id="arc-2", number=2, status="planned", start=4, end=6)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: calls.append(kwargs) or "task-next",
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=6,
            max_chapters=None,
            auto_continue=True,
        )

        assert decision == AutoContinueDecision(
            decision="continue",
            reason="future_arc_materialized",
            next_task_id="task-next",
            next_chapter=4,
            run_until_chapter=6,
            target_total_chapters=6,
            requested_chapters=3,
            workset_reason="future_arc_materialization_required",
        )
        assert calls[0]["project_id"] == "project-auto"
        assert calls[0]["requested_chapters"] == 3
        assert calls[0]["max_chapters"] == 3
        assert calls[0]["run_until_chapter"] == 6
        assert calls[0]["auto_continue"] is True
    finally:
        engine.dispose()


def test_controller_stops_when_run_until_reached() -> None:
    engine, Session = _session_factory("auto-continue-until-reached")
    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: "unexpected",
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=3,
            max_chapters=None,
            auto_continue=True,
        )

        assert decision.decision == "stop"
        assert decision.reason == "run_until_reached"
        assert decision.next_task_id == ""
    finally:
        engine.dispose()


def test_controller_stops_on_pending_review_and_records_audit_event() -> None:
    engine, Session = _session_factory("auto-continue-review-block")
    try:
        with Session.begin() as session:
            project = _project(session)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            _chapter(session, project_id=project.id, arc_id="arc-1", number=1, status="accepted")
            _chapter(session, project_id=project.id, arc_id="arc-1", number=2, status="needs_review")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: "unexpected",
        )
        decision = controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1]),
            parent_task_id="task-prev",
            run_until_chapter=6,
            max_chapters=None,
            auto_continue=True,
        )

        with Session() as session:
            events = session.query(DecisionEvent).filter_by(project_id="project-auto").all()

        assert decision.decision == "stop"
        assert decision.reason == "pending_review_blocker"
        assert any(event.event_type == "auto_continue_decision" for event in events)
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run the failing controller tests**

Run: `pytest tests/test_generation_auto_continue.py -q`

Expected: import failure for `forwin.generation.auto_continue` or missing event type.

- [ ] **Step 3: Add event type**

Modify `forwin/governance.py`:

```python
class DecisionEventType:
    GENERATION_REQUESTED = "generation_requested"
    CONTINUE_REQUESTED = "continue_requested"
    AUTO_CONTINUE_DECISION = "auto_continue_decision"
    RUN_STARTED = "run_started"
```

Add it to `KNOWN_DECISION_EVENT_TYPES` near `CONTINUE_REQUESTED`:

```python
DecisionEventType.CONTINUE_REQUESTED,
DecisionEventType.AUTO_CONTINUE_DECISION,
DecisionEventType.RUN_STARTED,
```

- [ ] **Step 4: Add controller implementation**

Create `forwin/generation/auto_continue.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.generation.run_target import resolve_generation_run_target
from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.project import ChapterPlan, Project
from forwin.state.updater import StateUpdater


@dataclass(frozen=True)
class AutoContinueDecision:
    decision: str
    reason: str
    next_task_id: str = ""
    next_chapter: int = 0
    run_until_chapter: int = 0
    target_total_chapters: int = 0
    requested_chapters: int = 0
    workset_reason: str = ""


class GenerationAutoContinueController:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        create_continue_generation_task: Callable[..., str],
    ) -> None:
        self.session_factory = session_factory
        self.create_continue_generation_task = create_continue_generation_task

    def after_task_completion(
        self,
        result: Any,
        *,
        parent_task_id: str,
        run_until_chapter: int | None,
        max_chapters: int | None,
        auto_continue: bool,
        runtime_config: Any = None,
    ) -> AutoContinueDecision:
        project_id = str(getattr(result, "project_id", "") or "").strip()
        if not project_id:
            return AutoContinueDecision(decision="stop", reason="missing_project_id")
        if not auto_continue:
            return self._record_decision(
                project_id=project_id,
                parent_task_id=parent_task_id,
                decision=AutoContinueDecision(decision="stop", reason="auto_continue_disabled"),
            )
        terminal_block_reason = self._terminal_block_reason(result)
        if terminal_block_reason:
            return self._record_decision(
                project_id=project_id,
                parent_task_id=parent_task_id,
                decision=AutoContinueDecision(decision="stop", reason=terminal_block_reason),
            )

        with self.session_factory() as session:
            project = session.get(Project, project_id)
            if project is None:
                return AutoContinueDecision(decision="stop", reason="project_not_found")

            plans = list(
                session.execute(
                    select(ChapterPlan)
                    .where(ChapterPlan.project_id == project_id)
                    .order_by(ChapterPlan.chapter_number.asc())
                ).scalars()
            )
            accepted_max = max(
                (int(plan.chapter_number or 0) for plan in plans if str(plan.status or "") == "accepted"),
                default=0,
            )
            if any(str(plan.status or "") == "needs_review" for plan in plans):
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason="pending_review_blocker",
                        run_until_chapter=int(run_until_chapter or getattr(project, "target_total_chapters", 0) or 0),
                        target_total_chapters=int(getattr(project, "target_total_chapters", 0) or 0),
                    ),
                )
            if any(str(plan.status or "") == "drafted" for plan in plans):
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason="pending_acceptance_blocker",
                        run_until_chapter=int(run_until_chapter or getattr(project, "target_total_chapters", 0) or 0),
                        target_total_chapters=int(getattr(project, "target_total_chapters", 0) or 0),
                    ),
                )

            normalized_until = int(run_until_chapter or getattr(project, "target_total_chapters", 0) or 0)
            if accepted_max >= normalized_until:
                reason = "target_total_reached" if accepted_max >= int(project.target_total_chapters or 0) else "run_until_reached"
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason=reason,
                        next_chapter=accepted_max + 1,
                        run_until_chapter=normalized_until,
                        target_total_chapters=int(project.target_total_chapters or 0),
                    ),
                )

            next_chapter = accepted_max + 1
            target = resolve_generation_run_target(
                project,
                next_chapter=next_chapter,
                run_until_chapter=normalized_until,
                max_chapters=max_chapters,
            )
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=target.effective_max_chapters,
                source="auto_continue",
                preloaded_plans=plans,
            )
            if workset.requested_chapters <= 0:
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason=workset.reason or "no_remaining_chapters",
                        next_chapter=next_chapter,
                        run_until_chapter=target.run_until_chapter,
                        target_total_chapters=target.target_total_chapters,
                        workset_reason=workset.reason,
                    ),
                )

        next_task_id = self.create_continue_generation_task(
            project_id=project_id,
            runtime_config=runtime_config,
            requested_chapters=workset.requested_chapters,
            max_chapters=target.effective_max_chapters,
            auto_continue=True,
            run_until_chapter=target.run_until_chapter,
            title=str(getattr(project, "title", "") or ""),
            subtitle=f"自动续跑 · {str(getattr(project, 'genre', '') or '')}",
            message="前一批完成，无阻断，自动继续生成。",
        )
        reason = "future_arc_materialized" if workset.reason == "future_arc_materialization_required" else "chapter_completed_no_blocker"
        return self._record_decision(
            project_id=project_id,
            parent_task_id=parent_task_id,
            decision=AutoContinueDecision(
                decision="continue",
                reason=reason,
                next_task_id=next_task_id,
                next_chapter=workset.chapter_numbers[0] if workset.chapter_numbers else next_chapter,
                run_until_chapter=target.run_until_chapter,
                target_total_chapters=target.target_total_chapters,
                requested_chapters=workset.requested_chapters,
                workset_reason=workset.reason,
            ),
        )

    def _terminal_block_reason(self, result: Any) -> str:
        if bool(getattr(result, "paused", False)):
            return "user_pause_reached"
        if bool(getattr(result, "cancelled", False)):
            return "cancelled"
        if list(getattr(result, "failed_chapters", []) or []):
            return "failed_chapters_blocker"
        if list(getattr(result, "paused_chapters", []) or []):
            return "pending_review_blocker"
        status = str(getattr(result, "status", "") or "").strip()
        if status and status != "completed":
            return f"{status}_blocker"
        return ""

    def _record_decision(
        self,
        *,
        project_id: str,
        parent_task_id: str,
        decision: AutoContinueDecision,
    ) -> AutoContinueDecision:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    task_id=parent_task_id,
                    scope="task",
                    event_family="audit_action",
                    event_type=DecisionEventType.AUTO_CONTINUE_DECISION,
                    actor_type="system",
                    summary=f"Auto-continue decision: {decision.decision} ({decision.reason})",
                    payload={
                        "decision": decision.decision,
                        "reason": decision.reason,
                        "next_task_id": decision.next_task_id,
                        "next_chapter": decision.next_chapter,
                        "run_until_chapter": decision.run_until_chapter,
                        "target_total_chapters": decision.target_total_chapters,
                        "requested_chapters": decision.requested_chapters,
                        "workset_reason": decision.workset_reason,
                    },
                    related_object_type="generation_task",
                    related_object_id=parent_task_id,
                )
            )
            session.commit()
        return decision
```

- [ ] **Step 5: Verify controller tests pass**

Run: `pytest tests/test_generation_auto_continue.py -q`

Expected: `3 passed`.

- [ ] **Step 6: Commit Task 2**

```bash
git add forwin/generation/auto_continue.py forwin/governance.py tests/test_generation_auto_continue.py
git commit -m "feat: add generation auto-continue controller"
```

### Task 3: API Request Contracts

**Files:**
- Modify: `forwin/api_schema/genesis.py`
- Modify: `forwin/api_schema/project.py`
- Modify: `forwin/project_ops/genesis.py`
- Modify: `forwin/project_ops/generation.py`
- Test: `tests/test_project_operation_guards.py`

- [ ] **Step 1: Add failing API contract tests**

Add to `tests/test_project_operation_guards.py`:

```python
def test_continue_generation_passes_auto_continue_target_to_task_creation(self) -> None:
    project = self._create_project(project_id="proj-continue-auto-target")
    with self.session_factory() as session:
        project_row = session.get(Project, project.id)
        project_row.creation_status = "writing"
        project_row.target_total_chapters = 60
        arc = ArcPlanVersion(
            id="arc-continue-auto-target",
            project_id=project.id,
            arc_synopsis="测试弧线",
            status="active",
            arc_number=3,
            chapter_start=25,
            chapter_end=36,
        )
        session.add(arc)
        session.flush()
        for chapter_number in range(25, 37):
            session.add(
                ChapterPlan(
                    id=f"plan-continue-auto-target-{chapter_number}",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=chapter_number,
                    title=f"第{chapter_number}章",
                    status="planned",
                )
            )
        session.commit()

    captured: dict[str, object] = {}

    def capture_task_creation(**kwargs):
        captured.update(kwargs)
        return "task-continue-auto-target"

    with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
        response = api_module.continue_project_generation(
            project.id,
            ProjectContinueGenerationRequest(run_until_chapter=36),
        )

    self.assertEqual(response.task_id, "task-continue-auto-target")
    self.assertTrue(captured["auto_continue"])
    self.assertEqual(captured["run_until_chapter"], 36)
    self.assertEqual(captured["requested_chapters"], 12)


def test_continue_generation_auto_continue_false_preserves_short_batch(self) -> None:
    project = self._create_project(project_id="proj-continue-auto-disabled")
    with self.session_factory() as session:
        project_row = session.get(Project, project.id)
        project_row.creation_status = "writing"
        project_row.target_total_chapters = 60
        arc = ArcPlanVersion(
            id="arc-continue-auto-disabled",
            project_id=project.id,
            arc_synopsis="测试弧线",
            status="active",
            arc_number=1,
            chapter_start=1,
            chapter_end=12,
        )
        session.add(arc)
        session.flush()
        for chapter_number in range(1, 13):
            session.add(
                ChapterPlan(
                    id=f"plan-continue-auto-disabled-{chapter_number}",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=chapter_number,
                    title=f"第{chapter_number}章",
                    status="planned",
                )
            )
        session.commit()

    captured: dict[str, object] = {}

    def capture_task_creation(**kwargs):
        captured.update(kwargs)
        return "task-continue-auto-disabled"

    with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
        api_module.continue_project_generation(
            project.id,
            ProjectContinueGenerationRequest(auto_continue=False, max_chapters=3),
        )

    self.assertFalse(captured["auto_continue"])
    self.assertEqual(captured["max_chapters"], 3)
    self.assertEqual(captured["requested_chapters"], 3)
```

- [ ] **Step 2: Run the failing API tests**

Run: `pytest tests/test_project_operation_guards.py::ProjectOperationGuardTests::test_continue_generation_passes_auto_continue_target_to_task_creation tests/test_project_operation_guards.py::ProjectOperationGuardTests::test_continue_generation_auto_continue_false_preserves_short_batch -q`

Expected: Pydantic or missing kwargs failures.

- [ ] **Step 3: Add request schema fields**

Modify `forwin/api_schema/genesis.py`:

```python
class StartWritingRequest(BaseModel):
    auto_continue: bool | None = None
    run_until_chapter: int | None = Field(default=None, ge=1)
    max_chapters: int | None = Field(default=None, ge=1)


class StartWritingResponse(BaseModel):
    ok: bool
```

Add `StartWritingRequest` to `__all__`.

Modify `forwin/api_schema/project.py`:

```python
class ProjectContinueGenerationRequest(BaseModel):
    max_chapters: int | None = Field(default=None, ge=1)
    auto_continue: bool | None = None
    run_until_chapter: int | None = Field(default=None, ge=1)
    operation_mode: str | None = None
```

- [ ] **Step 4: Wire schema fields into project ops**

Modify imports in `forwin/project_ops/genesis.py` to include `StartWritingRequest`.

Change the function signature:

```python
def start_project_writing(
    project_id: str,
    req: StartWritingRequest | None = None,
    *,
    get_session,
```

Inside the function before task creation:

```python
auto_continue = True if req is None or req.auto_continue is None else bool(req.auto_continue)
run_until_chapter = req.run_until_chapter if req is not None else None
max_chapters = req.max_chapters if req is not None else None
```

Pass the values into `create_continue_generation_task`:

```python
task_id = create_continue_generation_task(
    project_id=project.id,
    runtime_config=runtime_config,
    requested_chapters=handoff_result.active_chapter_plan_count,
    max_chapters=max_chapters,
    auto_continue=auto_continue,
    run_until_chapter=run_until_chapter,
    title=project.title,
    subtitle=f"启动写作 · {project.genre}",
    message="Genesis 完成，准备进入写作主链。",
)
```

Modify `forwin/project_ops/generation.py` inside `continue_project_generation`:

```python
max_chapters = req.max_chapters if req is not None else None
auto_continue = True if req is None or req.auto_continue is None else bool(req.auto_continue)
run_until_chapter = req.run_until_chapter if req is not None else None
```

Pass the values into `create_continue_generation_task`:

```python
task_id = create_continue_generation_task(
    project_id=project_id,
    runtime_config=runtime_config,
    requested_chapters=workset.requested_chapters,
    max_chapters=max_chapters,
    auto_continue=auto_continue,
    run_until_chapter=run_until_chapter,
    title=project.title,
    subtitle=f"继续生成 · {project.genre}",
    message="准备继续生成剩余章节。",
)
```

- [ ] **Step 5: Verify API contract tests pass**

Run the two tests from Step 2 again.

Expected: both pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add forwin/api_schema/genesis.py forwin/api_schema/project.py forwin/project_ops/genesis.py forwin/project_ops/generation.py tests/test_project_operation_guards.py
git commit -m "feat: expose generation auto-continue request options"
```

### Task 4: Task Completion Auto-Continue Hook

**Files:**
- Modify: `forwin/api_core/generation.py`
- Test: `tests/test_generation_auto_continue.py`
- Test: `tests/test_project_operation_guards.py`

- [ ] **Step 1: Add failing completion-hook test**

Add to `tests/test_generation_auto_continue.py`:

```python
def test_completion_handler_schedules_next_task_after_success(monkeypatch) -> None:
    from forwin.api_core import generation as generation_api

    scheduled: list[dict[str, object]] = []

    class RuntimeConfig:
        pass

    def fake_create_continue_generation_task(**kwargs):
        scheduled.append(kwargs)
        return "task-auto-next"

    class ControllerStub:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def after_task_completion(self, result, **kwargs):
            fake_create_continue_generation_task(
                project_id=result.project_id,
                runtime_config=kwargs["runtime_config"],
                requested_chapters=1,
                max_chapters=1,
                auto_continue=True,
                run_until_chapter=2,
            )

    class Result:
        project_id = "project-auto"
        status = "completed"
        completed_chapters = [1]
        failed_chapters = []
        paused_chapters = []

    monkeypatch.setattr(generation_api, "GenerationAutoContinueController", ControllerStub)

    handler = generation_api._make_generation_completion_handler(
        task_id="task-prev",
        root_event_id="root-event",
        runtime_config=RuntimeConfig(),
        auto_continue=True,
        run_until_chapter=2,
        max_chapters=None,
        create_continue_generation_task=fake_create_continue_generation_task,
    )
    handler(Result())

    assert scheduled[0]["project_id"] == "project-auto"
    assert scheduled[0]["run_until_chapter"] == 2
```

- [ ] **Step 2: Run the failing completion-hook test**

Run: `pytest tests/test_generation_auto_continue.py::test_completion_handler_schedules_next_task_after_success -q`

Expected: `_make_generation_completion_handler` does not accept the new keyword arguments.

- [ ] **Step 3: Import the controller and extend completion handler signature**

Modify imports in `forwin/api_core/generation.py`:

```python
from forwin.generation.auto_continue import GenerationAutoContinueController
```

Change `_make_generation_completion_handler` signature:

```python
def _make_generation_completion_handler(
    *,
    task_id: str,
    root_event_id: str = "",
    prior_handler=None,
    runtime_config: Config | None = None,
    auto_continue: bool = False,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
    create_continue_generation_task=None,
):
```

After the existing `RUN_COMPLETED` decision event commit, add:

```python
        if auto_continue and create_continue_generation_task is not None:
            GenerationAutoContinueController(
                session_factory=_get_session,
                create_continue_generation_task=create_continue_generation_task,
            ).after_task_completion(
                result,
                parent_task_id=task_id,
                run_until_chapter=run_until_chapter,
                max_chapters=max_chapters,
                auto_continue=auto_continue,
                runtime_config=runtime_config,
            )
```

- [ ] **Step 4: Extend continue task creation signature**

Modify `_create_continue_generation_task`:

```python
def _create_continue_generation_task(
    *,
    project_id: str,
    runtime_config: Config,
    requested_chapters: int,
    max_chapters: int | None = None,
    auto_continue: bool = True,
    run_until_chapter: int | None = None,
    title: str = "",
    subtitle: str = "",
    message: str = "",
) -> str:
```

Pass these values into the completion handler:

```python
_make_generation_completion_handler(
    task_id=task_id,
    root_event_id=root_event_id,
    prior_handler=_maybe_enqueue_auto_publish_jobs,
    runtime_config=runtime_config,
    auto_continue=auto_continue,
    run_until_chapter=run_until_chapter,
    max_chapters=max_chapters,
    create_continue_generation_task=_create_continue_generation_task,
),
```

Also pass equivalent values from `_create_generation_task` with `auto_continue=False` so legacy direct generation does not unexpectedly chain.

- [ ] **Step 5: Verify completion hook test passes**

Run: `pytest tests/test_generation_auto_continue.py::test_completion_handler_schedules_next_task_after_success -q`

Expected: pass.

- [ ] **Step 6: Run targeted task persistence tests**

Run: `pytest tests/test_generation_task_persistence.py tests/test_generation_auto_continue.py -q`

Expected: pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add forwin/api_core/generation.py tests/test_generation_auto_continue.py
git commit -m "feat: auto-continue after generation task completion"
```

### Task 5: MCP And UI Contract

**Files:**
- Modify: `forwin/mcp/client.py`
- Modify: `forwin/mcp/http.py`
- Modify: `forwin/ui_assets/home/app_genesis.js`
- Test: `tests/test_mcp_server.py`
- Test: `tests/browser/test_mock_book_creation_generation_regression.py`

- [ ] **Step 1: Add failing MCP test**

Add to `tests/test_mcp_server.py`:

```python
def test_continue_generation_via_mcp_passes_auto_continue_options(self) -> None:
    project_id = "proj-mcp-auto-continue"
    project = self._create_project(project_id=project_id, target_total_chapters=60)
    with self.session_factory() as session:
        project_row = session.get(Project, project.id)
        project_row.creation_status = "writing"
        arc = ArcPlanVersion(
            id="arc-mcp-auto-continue",
            project_id=project.id,
            arc_number=1,
            status="active",
            chapter_start=1,
            chapter_end=12,
            arc_synopsis="测试弧线",
        )
        session.add(arc)
        session.flush()
        for chapter_number in range(1, 13):
            session.add(
                ChapterPlan(
                    id=f"plan-mcp-auto-continue-{chapter_number}",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=chapter_number,
                    title=f"第{chapter_number}章",
                    status="planned",
                )
            )
        session.commit()

    captured: dict[str, object] = {}

    def capture_task_creation(**kwargs):
        captured.update(kwargs)
        task_id = "task-mcp-auto-continue"
        task = api_module._create_task_record(
            message=str(kwargs.get("message") or ""),
            requested_chapters=int(kwargs.get("requested_chapters") or 0),
        )
        task["project_id"] = project_id
        api_module._persist_generation_task(task_id, task)
        return task_id

    with patch("forwin.api._create_continue_generation_task", new=capture_task_creation):
        result = self._load_model(
            MutationResult,
            self._call_tool(
                "project_continue_generation",
                {
                    "project_id": project_id,
                    "run_until_chapter": 24,
                    "auto_continue": True,
                },
            ),
        )

    self.assertIsNotNone(result.task)
    self.assertEqual(captured["run_until_chapter"], 24)
    self.assertTrue(captured["auto_continue"])
```

- [ ] **Step 2: Run the failing MCP test**

Run: `pytest tests/test_mcp_server.py::MCPServerTests::test_continue_generation_via_mcp_passes_auto_continue_options -q`

Expected: MCP tool rejects unknown parameters or client drops them.

- [ ] **Step 3: Update MCP client and tools**

Modify `forwin/mcp/client.py`:

```python
async def project_start_writing(
    self,
    *,
    project_id: str,
    auto_continue: bool | None = None,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
) -> MutationResult:
    request_json: dict[str, Any] = {}
    if auto_continue is not None:
        request_json["auto_continue"] = bool(auto_continue)
    if run_until_chapter is not None:
        request_json["run_until_chapter"] = int(run_until_chapter)
    if max_chapters is not None:
        request_json["max_chapters"] = int(max_chapters)
    payload = await self._request_json(
        "POST",
        f"/api/projects/{project_id}/start-writing",
        json=request_json,
    )
```

Modify `project_continue_generation` similarly:

```python
async def project_continue_generation(
    self,
    *,
    project_id: str,
    max_chapters: int | None = None,
    auto_continue: bool | None = None,
    run_until_chapter: int | None = None,
) -> MutationResult:
```

Add `auto_continue` and `run_until_chapter` to `request_json` when provided.

Modify `forwin/mcp/http.py` function signatures:

```python
async def project_start_writing(
    project_id: str,
    auto_continue: bool | None = None,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
) -> MutationResult:
```

```python
async def project_continue_generation(
    project_id: str,
    max_chapters: int | None = None,
    auto_continue: bool | None = None,
    run_until_chapter: int | None = None,
) -> MutationResult:
```

Update descriptions to say normal generation auto-continues until target unless blocked.

- [ ] **Step 4: Update UI copy without changing payload**

Modify the confirmation text in `forwin/ui_assets/home/app_genesis.js`:

```javascript
if (!window.confirm('启动写作后，系统会从 Genesis 根蓝图物化 Arc 骨架与章节计划，并默认自动续跑到目标章节；遇到 review、修复、预算或人工 gate 才会停。继续吗？')) return;
```

Keep the request body absent so `tests/browser/test_mock_book_creation_generation_regression.py` can continue asserting `{}` for the captured payload.

- [ ] **Step 5: Verify MCP and browser tests**

Run:

```bash
pytest tests/test_mcp_server.py::MCPServerTests::test_continue_generation_via_mcp_passes_auto_continue_options tests/browser/test_mock_book_creation_generation_regression.py -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add forwin/mcp/client.py forwin/mcp/http.py forwin/ui_assets/home/app_genesis.js tests/test_mcp_server.py tests/browser/test_mock_book_creation_generation_regression.py
git commit -m "feat: expose auto-continue controls through mcp"
```

### Task 6: End-To-End Regression And Audit Checks

**Files:**
- Modify: `tests/test_project_operation_guards.py`
- Modify: `tests/test_generation_auto_continue.py`

- [ ] **Step 1: Add regression for one start request reaching future arcs**

Add to `tests/test_project_operation_guards.py`:

```python
def test_start_writing_defaults_to_auto_continue_until_project_target(self) -> None:
    project = self._create_project(project_id="proj-start-auto-continue")
    with self.session_factory() as session:
        project_row = session.get(Project, project.id)
        project_row.creation_status = "genesis_ready"
        project_row.target_total_chapters = 24
        session.commit()

    class FakeGenesisService:
        class Handoff:
            def start_writing(self, *, session, updater, command):
                project = session.get(Project, command.project_id)
                arc = ArcPlanVersion(
                    id="arc-start-auto-continue",
                    project_id=project.id,
                    version=1,
                    arc_number=1,
                    arc_synopsis="测试弧线",
                    status="active",
                    chapter_start=1,
                    chapter_end=12,
                    planned_target_size=12,
                )
                future_arc = ArcPlanVersion(
                    id="arc-start-auto-continue-future",
                    project_id=project.id,
                    version=1,
                    arc_number=2,
                    arc_synopsis="后续弧线",
                    status="planned",
                    chapter_start=13,
                    chapter_end=24,
                    planned_target_size=12,
                )
                session.add_all([arc, future_arc])
                for chapter_number in range(1, 13):
                    session.add(
                        ChapterPlan(
                            id=f"plan-start-auto-continue-{chapter_number}",
                            project_id=project.id,
                            arc_plan_id=arc.id,
                            chapter_number=chapter_number,
                            title=f"第{chapter_number}章",
                            status="planned",
                        )
                    )
                project.creation_status = "writing"
                session.add(project)
                session.flush()
                return SimpleNamespace(
                    active_chapter_plan_count=12,
                    project_status="writing",
                )

        def __init__(self):
            self.handoff = self.Handoff()

    captured: dict[str, object] = {}

    def capture_task_creation(**kwargs):
        captured.update(kwargs)
        return "task-start-auto-continue"

    response = api_project_ops.start_project_writing(
        project.id,
        get_session=self.session_factory,
        config=api_module._config,
        saved_runtime_config_or_default=lambda: Config(
            database_url=api_module._config.database_url,
            minimax_api_key="saved-key",
        ),
        build_genesis_service=lambda _runtime_config: FakeGenesisService(),
        close_genesis_service=lambda _service: None,
        require_genesis_project=lambda _project: None,
        active_genesis_revision=lambda _session, _project: SimpleNamespace(id="revision-start-writing"),
        project_has_active_generation_task=lambda _project_id, *, session=None: False,
        generation_task_conflict_message=lambda _project_id: "conflict",
        create_continue_generation_task=capture_task_creation,
    )

    self.assertEqual(response.task_id, "task-start-auto-continue")
    self.assertTrue(captured["auto_continue"])
    self.assertIsNone(captured["run_until_chapter"])
    self.assertEqual(captured["requested_chapters"], 12)
```

- [ ] **Step 2: Add audit payload regression**

Add to `tests/test_generation_auto_continue.py`:

```python
def test_controller_audit_payload_contains_target_fields() -> None:
    engine, Session = _session_factory("auto-continue-audit-payload")
    try:
        with Session.begin() as session:
            project = _project(session, total=3)
            _arc(session, project_id=project.id, arc_id="arc-1", number=1, status="active", start=1, end=3)
            for number in range(1, 4):
                _chapter(session, project_id=project.id, arc_id="arc-1", number=number, status="accepted")

        controller = GenerationAutoContinueController(
            session_factory=Session,
            create_continue_generation_task=lambda **kwargs: "unexpected",
        )
        controller.after_task_completion(
            ResultStub(project_id="project-auto", completed_chapters=[1, 2, 3]),
            parent_task_id="task-prev",
            run_until_chapter=3,
            max_chapters=None,
            auto_continue=True,
        )

        with Session() as session:
            event = session.query(DecisionEvent).filter_by(event_type="auto_continue_decision").one()
            payload = json.loads(event.payload_json)

        assert payload["decision"] == "stop"
        assert payload["reason"] == "target_total_reached"
        assert payload["run_until_chapter"] == 3
        assert payload["target_total_chapters"] == 3
    finally:
        engine.dispose()
```

Add `import json` at the top of the test file.

- [ ] **Step 3: Run regression tests**

Run:

```bash
pytest tests/test_generation_run_target.py tests/test_generation_auto_continue.py tests/test_project_operation_guards.py::ProjectOperationGuardTests::test_start_writing_defaults_to_auto_continue_until_project_target -q
```

Expected: pass.

- [ ] **Step 4: Commit Task 6**

```bash
git add tests/test_project_operation_guards.py tests/test_generation_auto_continue.py
git commit -m "test: cover generation auto-continue audit behavior"
```

### Task 7: Final Verification

**Files:**
- No new files unless earlier tasks reveal a narrowly scoped fix.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
pytest tests/test_generation_run_target.py tests/test_generation_auto_continue.py tests/test_continue_generation_workset.py tests/test_project_operation_guards.py tests/test_mcp_server.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run syntax and diff checks**

Run:

```bash
python3 -m compileall forwin/generation forwin/api_core forwin/project_ops forwin/mcp
git diff --check
```

Expected: compileall succeeds and `git diff --check` prints no whitespace errors.

- [ ] **Step 3: Smoke-check the current 60 chapter task state**

Use MCP, not raw database access:

```text
task_active_generation_check(project_id="60b878eac68a4c2aadad7fd0703b15ff")
project_get(project_id="60b878eac68a4c2aadad7fd0703b15ff")
```

Expected: do not start a new task if one is active. If the current task already finished and `can_resume=true`, do not manually continue as part of this implementation unless the user explicitly asks; this plan changes future behavior.

- [ ] **Step 4: Final commit if any verification-only fixes were needed**

If Task 7 required fixes, stage only the files changed by those fixes and commit with this message:

```bash
git add forwin tests
git commit -m "fix: stabilize generation auto-continue verification"
```

If no fixes were needed, do not create an empty commit.
