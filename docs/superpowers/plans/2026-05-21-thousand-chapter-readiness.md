# Thousand-Chapter Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the P0-P2 thousand-chapter readiness path: long-run policy, real pressure metrics, pulp beat verification, failure containment, extraction fallback, durable resume, typed retrieval budgets, and trope cooldown.

**Architecture:** Keep `Project.target_total_chapters` as the only total target. Add execution policy, deterministic verification, and maintenance/deferred events around the current generation loop before moving to lease-backed task execution. Extend current BookState, narrative obligation, memory index, and experience planning paths without adding Saga/Volume or new legacy compatibility.

**Tech Stack:** Python 3.12+, FastAPI/Pydantic, SQLAlchemy/PostgreSQL, existing ForWin models and migrations, pytest, current ForWin MCP/API workflow.

---

## Worktree And Legacy Constraints

- Execute in an isolated worktree when implementation begins, using `superpowers:using-git-worktrees`.
- Do not stage or revert existing legacy-removal work unless the task explicitly owns that file.
- Do not add runtime dependencies on `legacy_entity_id`, world v4 projection writes, `state.location`, `creation_status="legacy"`, old env aliases, or old API constructor shapes.
- If a file is actively owned by legacy removal Phase 2 or Phase 3, sequence the thousand-chapter task after that phase or isolate the edit in a narrow patch.

## File Structure

- Create `forwin/long_run_policy.py`: parse and normalize long-run execution policy.
- Modify `forwin/api_schema/project.py`: project create/extend limits and automation payload fields.
- Modify `forwin/project_payloads/runtime_maps.py`: normalize long-run policy inside project automation.
- Modify `forwin/production/policy.py`: expose policy to production scheduler without reusing legacy quota aliases.
- Modify `forwin/mcp/client.py`: align target limits with API schema.
- Modify `forwin/ui_assets/home/app_library.js`: align browser validation and copy with API limits.
- Create `forwin/maintenance/deferred.py`: record recoverable maintenance events.
- Modify `forwin/governance.py`: add explicit decision event types for deferred maintenance and pulp beat verification.
- Modify `forwin/orchestrator_loop_core/project_chapters.py`: stop same-run cascade and defer recoverable failures.
- Create `forwin/checker/pulp_beat.py`: deterministic pulp beat verifier.
- Modify `forwin/checker/hard_floor.py`: include pulp beat metadata and policy threshold results.
- Rewrite `scripts/pulp_pressure_test.py`: read-only report collector.
- Modify `forwin/writer/chapter_writer.py`: three-window structured extraction fallback.
- Modify `forwin/models/task.py` and add migration: lease/resume columns for generation tasks.
- Create `forwin/generation/task_lease.py`: claim, heartbeat, lease expiry, and resume point logic.
- Modify `forwin/api_core/tasks.py` and `forwin/api_core/generation.py`: serialize lease fields and use claim/resume helpers.
- Create `forwin/retrieval/typed_budget.py`: retrieval bucket config and packing helpers.
- Modify `forwin/retrieval/broker_core/broker.py`: collect typed memory buckets under a hard budget.
- Create `forwin/experience/trope_cooldown.py`: cooldown state and selection helpers.
- Modify `forwin/models/phase.py` or add a focused model file for trope usage persistence.
- Modify `forwin/experience/band_scheduler.py`: apply persistent cooldown when choosing templates.

## Task 1: Entry Contract And LongRunPolicy

**Files:**
- Create: `forwin/long_run_policy.py`
- Modify: `forwin/api_schema/project.py`
- Modify: `forwin/project_payloads/runtime_maps.py`
- Modify: `forwin/production/policy.py`
- Modify: `forwin/mcp/client.py`
- Modify: `forwin/ui_assets/home/app_library.js`
- Test: `tests/test_long_run_policy.py`
- Test: `tests/test_project_schema_long_run.py`

- [ ] **Step 1: Write failing tests for policy normalization**

Create `tests/test_long_run_policy.py`:

```python
from __future__ import annotations

from forwin.long_run_policy import LongRunMode, LongRunPolicy, normalize_long_run_policy


def test_normalize_defaults_to_daily_serial() -> None:
    policy = normalize_long_run_policy({})

    assert policy == LongRunPolicy()
    assert policy.mode == LongRunMode.daily_serial
    assert policy.batch_size == 1
    assert policy.stop_on_chapter_failure is True
    assert policy.defer_observation_failures is False
    assert policy.payoff_gap_limit == 2
    assert policy.resume_policy == "manual_after_failed_chapter"


def test_normalize_factory_batch_clamps_batch_size() -> None:
    policy = normalize_long_run_policy(
        {
            "mode": "factory_batch",
            "batch_size": 999,
            "defer_observation_failures": True,
            "payoff_gap_limit": 5,
            "resume_policy": "auto_after_infrastructure_failure",
        }
    )

    assert policy.mode == LongRunMode.factory_batch
    assert policy.batch_size == 50
    assert policy.defer_observation_failures is True
    assert policy.payoff_gap_limit == 5
    assert policy.resume_policy == "auto_after_infrastructure_failure"
```

- [ ] **Step 2: Write failing tests for API/MCP limits**

Create `tests/test_project_schema_long_run.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from forwin.api_schema.project import ProjectCreateRequest, ProjectExtendGenerationRequest


def test_project_create_accepts_thousand_chapter_target() -> None:
    req = ProjectCreateRequest(title="长篇", premise="p", target_total_chapters=1000)

    assert req.target_total_chapters == 1000


def test_project_create_rejects_above_contract_limit() -> None:
    with pytest.raises(ValidationError):
        ProjectCreateRequest(title="太长", premise="p", target_total_chapters=5001)


def test_project_extend_accepts_factory_sized_extension() -> None:
    req = ProjectExtendGenerationRequest(additional_chapters=500)

    assert req.additional_chapters == 500
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_long_run_policy.py tests/test_project_schema_long_run.py -q
```

Expected: import failure for `forwin.long_run_policy` and validation failure for 1000 chapter targets.

- [ ] **Step 4: Implement `LongRunPolicy`**

Create `forwin/long_run_policy.py`:

```python
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class LongRunMode(StrEnum):
    daily_serial = "daily_serial"
    factory_batch = "factory_batch"
    soak_test = "soak_test"


ResumePolicy = Literal[
    "manual_after_failed_chapter",
    "auto_after_infrastructure_failure",
]


class LongRunPolicy(BaseModel):
    mode: LongRunMode = LongRunMode.daily_serial
    batch_size: int = Field(default=1, ge=1, le=50)
    stop_on_chapter_failure: bool = True
    defer_observation_failures: bool = False
    payoff_gap_limit: int = Field(default=2, ge=1, le=10)
    resume_policy: ResumePolicy = "manual_after_failed_chapter"


def normalize_long_run_policy(raw: Any) -> LongRunPolicy:
    payload = raw if isinstance(raw, dict) else {}
    try:
        return LongRunPolicy.model_validate(payload)
    except Exception:
        cleaned = dict(payload)
        mode = str(cleaned.get("mode") or "").strip()
        if mode not in {item.value for item in LongRunMode}:
            cleaned["mode"] = LongRunMode.daily_serial.value
        try:
            cleaned["batch_size"] = min(50, max(1, int(cleaned.get("batch_size", 1) or 1)))
        except (TypeError, ValueError):
            cleaned["batch_size"] = 1
        try:
            cleaned["payoff_gap_limit"] = min(10, max(1, int(cleaned.get("payoff_gap_limit", 2) or 2)))
        except (TypeError, ValueError):
            cleaned["payoff_gap_limit"] = 2
        if cleaned.get("resume_policy") not in {
            "manual_after_failed_chapter",
            "auto_after_infrastructure_failure",
        }:
            cleaned["resume_policy"] = "manual_after_failed_chapter"
        cleaned["stop_on_chapter_failure"] = bool(cleaned.get("stop_on_chapter_failure", True))
        cleaned["defer_observation_failures"] = bool(cleaned.get("defer_observation_failures", False))
        return LongRunPolicy.model_validate(cleaned)
```

- [ ] **Step 5: Wire policy into automation schema and normalization**

In `forwin/api_schema/project.py`, add import and field:

```python
from forwin.long_run_policy import LongRunPolicy
```

Inside `ProjectAutomationSettings`:

```python
    long_run_policy: LongRunPolicy = Field(default_factory=LongRunPolicy)
```

In `forwin/project_payloads/runtime_maps.py`, import and normalize:

```python
from forwin.long_run_policy import normalize_long_run_policy
```

Add to the return payload:

```python
            "long_run_policy": normalize_long_run_policy(payload.get("long_run_policy")).model_dump(mode="json"),
```

- [ ] **Step 6: Update entry limits**

In `forwin/api_schema/project.py`:

```python
class ProjectCreateRequest(BaseModel):
    title: str
    premise: str
    genre: str = "玄幻"
    setting_summary: str = ""
    target_total_chapters: int = Field(default=50, ge=1, le=5000)
```

In `ProjectCreateResponse`, `ProjectSummary`, and `ProjectDetail`, change display defaults from `3` to `50`.

In `ProjectExtendGenerationRequest`:

```python
class ProjectExtendGenerationRequest(BaseModel):
    additional_chapters: int = Field(default=50, ge=1, le=500)
```

In `forwin/mcp/client.py`, change the client-side validation:

```python
        if target_total_chapters < 1 or target_total_chapters > 5000:
            raise ValueError("target_total_chapters must be between 1 and 5000")
```

In `forwin/ui_assets/home/app_library.js`, change the create validation branch to reject values above `5000`, and adjust the visible error message to say `1 到 5000`.

- [ ] **Step 7: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_long_run_policy.py tests/test_project_schema_long_run.py tests/test_production_policy.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add forwin/long_run_policy.py forwin/api_schema/project.py forwin/project_payloads/runtime_maps.py forwin/production/policy.py forwin/mcp/client.py forwin/ui_assets/home/app_library.js tests/test_long_run_policy.py tests/test_project_schema_long_run.py
git commit -m "feat: add long-run generation policy"
```

## Task 2: Failure Containment And Deferred Maintenance

**Files:**
- Create: `forwin/maintenance/__init__.py`
- Create: `forwin/maintenance/deferred.py`
- Modify: `forwin/governance.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_hard_floor.py`
- Test: `tests/test_deferred_maintenance.py`

- [ ] **Step 1: Add failing tests for same-run stop**

Extend `tests/test_hard_floor.py` with a two-chapter test:

```python
def test_project_chapter_loop_stops_after_hard_floor_failure(monkeypatch) -> None:
    hard_floor = HardFloorResult(
        passed=False,
        fail_reasons=["chapter_length"],
        checks={"chapter_length": False},
        metadata={"body_char_count": 10},
    )
    monkeypatch.setattr(project_chapters, "run_hard_floor", lambda **kwargs: hard_floor, raising=False)

    chapter_loop = FakeChapterLoop(hard_floor_gate_enabled=True)
    session = FakeSession()
    repo = FakeRepo()
    updater = FakeUpdater()

    result = WritingOrchestrator._run_project_chapters(
        chapter_loop,
        session=session,
        repo=repo,
        updater=updater,
        checker=SimpleNamespace(),
        project_id="project-1",
        chapter_numbers=[1, 2],
        requested_chapters=2,
    )

    assert result.failed_chapters == [1]
    assert result.completed_chapters == []
    assert [call[1] for call in updater.status_calls] == [1]
```

- [ ] **Step 2: Add failing test for deferred maintenance recorder**

Create `tests/test_deferred_maintenance.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from forwin.maintenance.deferred import DeferredMaintenanceRecord, record_deferred_maintenance


class UpdaterSpy:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def save_decision_event(self, event) -> None:
        self.events.append(event.model_dump(mode="json"))


def test_record_deferred_maintenance_saves_decision_event() -> None:
    updater = UpdaterSpy()
    record = DeferredMaintenanceRecord(
        project_id="project-1",
        chapter_number=7,
        task_type="memory_index_upsert",
        reason="qdrant timeout",
        payload={"error_class": "TimeoutError"},
    )

    record_deferred_maintenance(updater, record)

    assert updater.events[0]["project_id"] == "project-1"
    assert updater.events[0]["chapter_number"] == 7
    assert updater.events[0]["event_type"] == "deferred_maintenance_recorded"
    assert updater.events[0]["payload"]["task_type"] == "memory_index_upsert"
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_hard_floor.py::test_project_chapter_loop_stops_after_hard_floor_failure tests/test_deferred_maintenance.py -q
```

Expected: loop still continues or recorder import fails.

- [ ] **Step 4: Add event type and deferred recorder**

In `forwin/governance.py`, add a decision event type:

```python
    DEFERRED_MAINTENANCE_RECORDED = "deferred_maintenance_recorded"
```

Create `forwin/maintenance/__init__.py`:

```python
from .deferred import DeferredMaintenanceRecord, record_deferred_maintenance

__all__ = ["DeferredMaintenanceRecord", "record_deferred_maintenance"]
```

Create `forwin/maintenance/deferred.py`:

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forwin.governance import DecisionEventInfo, DecisionEventType


class DeferredMaintenanceRecord(BaseModel):
    project_id: str
    chapter_number: int = 0
    task_type: str
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


def record_deferred_maintenance(updater, record: DeferredMaintenanceRecord) -> None:  # noqa: ANN001
    updater.save_decision_event(
        DecisionEventInfo(
            project_id=record.project_id,
            chapter_number=record.chapter_number,
            scope="chapter" if record.chapter_number else "project",
            event_family="runtime_observation",
            event_type=DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
            actor_type="system",
            summary=f"Deferred maintenance recorded: {record.task_type}",
            reason=record.reason,
            payload={"task_type": record.task_type, **record.payload},
        )
    )
```

- [ ] **Step 5: Stop after hard-floor failure**

In `forwin/orchestrator_loop_core/project_chapters.py`, change the hard-floor failure branch:

```python
                    failed_chapters.append(chapter_num)
                    break
```

Replace the existing `continue` after hard-floor failure with `break`.

- [ ] **Step 6: Defer memory index upsert failure**

In `forwin/orchestrator_loop_core/project_chapters.py`, import:

```python
from forwin.maintenance.deferred import DeferredMaintenanceRecord, record_deferred_maintenance
```

In the memory upsert `except Exception as exc` block, replace `raise` with:

```python
                if bool(getattr(self.config, "quality_profile", "") == "pulp"):
                    record_deferred_maintenance(
                        updater,
                        DeferredMaintenanceRecord(
                            project_id=project_id,
                            chapter_number=chapter_num,
                            task_type="memory_index_upsert",
                            reason=str(exc),
                            payload={"error_class": exc.__class__.__name__, "error_summary": str(exc)},
                        ),
                    )
                else:
                    raise
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_hard_floor.py tests/test_deferred_maintenance.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add forwin/governance.py forwin/maintenance tests/test_hard_floor.py tests/test_deferred_maintenance.py forwin/orchestrator_loop_core/project_chapters.py
git commit -m "fix: stop generation after fatal chapter failure"
```

## Task 3: PulpBeatVerifier And Pressure Collector

**Files:**
- Create: `forwin/checker/pulp_beat.py`
- Modify: `forwin/checker/hard_floor.py`
- Rewrite: `scripts/pulp_pressure_test.py`
- Test: `tests/test_pulp_beat_verifier.py`
- Test: `tests/test_pulp_pressure_test.py`

- [ ] **Step 1: Write failing PulpBeatVerifier tests**

Create `tests/test_pulp_beat_verifier.py`:

```python
from __future__ import annotations

from forwin.checker.pulp_beat import verify_pulp_beats


def test_verify_pulp_beats_detects_core_payoff() -> None:
    body = "众人嘲笑他没资格。林远当场拿出合同，老板脸色大变，当众道歉，还赔偿三十万。门外忽然传来新的威胁。"

    result = verify_pulp_beats(body)

    assert result.pressure_present is True
    assert result.protagonist_action_present is True
    assert result.visible_payoff_present is True
    assert result.audience_reaction_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.next_hook_present is True


def test_verify_pulp_beats_flags_missing_payoff() -> None:
    result = verify_pulp_beats("他走在路上，想了很多前情，夜色越来越深。")

    assert result.visible_payoff_present is False
    assert "visible_payoff_present" in result.missing_fields
```

- [ ] **Step 2: Write failing pressure collector test**

Create `tests/test_pulp_pressure_test.py` with a seeded project and one accepted chapter. Use a temp output directory:

```python
from __future__ import annotations

import json

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from scripts import pulp_pressure_test
from tests.postgres import postgres_test_url


def test_pressure_report_uses_real_chapter_rows(tmp_path, monkeypatch) -> None:
    database_url = postgres_test_url("pulp-pressure-report")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(id="project-pressure", title="P", premise="p", genre="都市", creation_status="writing", target_total_chapters=30)
            session.add(project)
            arc = ArcPlanVersion(id="arc-1", project_id=project.id, arc_synopsis="arc", status="active")
            session.add(arc)
            session.add(ChapterPlan(id="plan-1", project_id=project.id, arc_plan_id=arc.id, chapter_number=1, title="第一章", status="accepted", summary="summary"))
            session.add(GenerationTask(id="task-1", task_kind="generation", project_id=project.id, status="completed", requested_chapters=1, completed_chapters_json="[1]"))

        monkeypatch.setenv("DATABASE_URL", database_url)
        output = tmp_path / "report"

        assert pulp_pressure_test.main(["--project-id", "project-pressure", "--chapters", "1", "--output", str(output)]) == 0

        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert summary["chapter_count"] == 1
        assert "future versions can replace" not in (output / "README.md").read_text(encoding="utf-8").lower()
    finally:
        engine.dispose()
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_pulp_beat_verifier.py tests/test_pulp_pressure_test.py -q
```

Expected: verifier import fails and pressure report still emits the old synthetic-report copy.

- [ ] **Step 4: Implement PulpBeatVerifier**

Create `forwin/checker/pulp_beat.py`:

```python
from __future__ import annotations

from pydantic import BaseModel, Field


class PulpBeatResult(BaseModel):
    pressure_present: bool = False
    protagonist_action_present: bool = False
    visible_payoff_present: bool = False
    audience_reaction_present: bool = False
    enemy_or_obstacle_damage_present: bool = False
    new_gain_or_status_shift_present: bool = False
    next_hook_present: bool = False
    missing_fields: list[str] = Field(default_factory=list)


PRESSURE_WORDS = ("嘲笑", "看不起", "羞辱", "威胁", "逼迫", "驱赶", "扣钱")
ACTION_WORDS = ("当场", "出手", "拿出", "开口", "反击", "证明", "亮出")
PAYOFF_WORDS = ("到账", "赔偿", "合同", "资格", "名额", "升职", "奖励")
AUDIENCE_WORDS = ("众人", "全场", "同事", "邻居", "直播间", "村里")
DAMAGE_WORDS = ("道歉", "跪下", "开除", "赔钱", "封杀", "脸色大变", "失去资格")
GAIN_WORDS = ("赔偿", "到账", "资格", "名额", "合同", "职位", "资源")
HOOK_WORDS = ("忽然", "没想到", "就在这时", "门外", "电话响起", "新的威胁")


def _has_any(body: str, words: tuple[str, ...]) -> bool:
    return any(word in body for word in words)


def verify_pulp_beats(body: str) -> PulpBeatResult:
    text = str(body or "")
    result = PulpBeatResult(
        pressure_present=_has_any(text, PRESSURE_WORDS),
        protagonist_action_present=_has_any(text, ACTION_WORDS),
        visible_payoff_present=_has_any(text, PAYOFF_WORDS),
        audience_reaction_present=_has_any(text, AUDIENCE_WORDS),
        enemy_or_obstacle_damage_present=_has_any(text, DAMAGE_WORDS),
        new_gain_or_status_shift_present=_has_any(text, GAIN_WORDS),
        next_hook_present=_has_any(text[-240:], HOOK_WORDS),
    )
    missing = [
        field
        for field in (
            "pressure_present",
            "protagonist_action_present",
            "visible_payoff_present",
            "audience_reaction_present",
            "enemy_or_obstacle_damage_present",
            "new_gain_or_status_shift_present",
            "next_hook_present",
        )
        if not getattr(result, field)
    ]
    return result.model_copy(update={"missing_fields": missing})
```

- [ ] **Step 5: Add hard-floor metadata**

In `forwin/checker/hard_floor.py`, import and call:

```python
from .pulp_beat import verify_pulp_beats
```

Inside `run_hard_floor`, after ending hook:

```python
    pulp_beats = verify_pulp_beats(body)
    checks["pulp_visible_payoff"] = pulp_beats.visible_payoff_present
    if getattr(config, "quality_profile", "") == "pulp" and not pulp_beats.visible_payoff_present:
        warning_reasons.append("pulp_visible_payoff")
```

Add to metadata:

```python
            "pulp_beat": pulp_beats.model_dump(mode="json"),
```

- [ ] **Step 6: Rewrite pressure collector as read-only DB collector**

In `scripts/pulp_pressure_test.py`, keep `ChapterMetric`, add DB loading with `Config.from_env`, `get_engine`, `get_session_factory`, and query `Project`, `ChapterPlan`, `GenerationTask`, `DecisionEvent`, `PromptTrace`, and `PerformanceSpan`. Replace the old README text with:

```python
        "# Pulp Pressure Test Report\n\n"
        "This report was generated from existing ForWin project/task/chapter telemetry.\n"
        "It does not start generation or mutate project state.\n"
```

Implement `collect_rows(session, project_id, chapters)` so each returned row uses real chapter status and task data:

```python
def collect_rows(session, project_id: str, chapters: int) -> list[ChapterMetric]:
    plans = {
        int(plan.chapter_number or 0): plan
        for plan in session.query(ChapterPlan)
        .filter(ChapterPlan.project_id == project_id)
        .order_by(ChapterPlan.chapter_number.asc())
        .all()
    }
    rows: list[ChapterMetric] = []
    for chapter_number in range(1, chapters + 1):
        plan = plans.get(chapter_number)
        rows.append(
            ChapterMetric(
                chapter_number=chapter_number,
                verdict=str(getattr(plan, "status", "") or "") if plan else "missing_plan",
                chapter_length=int(getattr(plan, "char_count", 0) or 0) if plan else None,
            )
        )
    return rows
```

- [ ] **Step 7: Run tests**

Run:

```bash
python3 -m pytest tests/test_pulp_beat_verifier.py tests/test_hard_floor.py tests/test_pulp_pressure_test.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add forwin/checker/pulp_beat.py forwin/checker/hard_floor.py scripts/pulp_pressure_test.py tests/test_pulp_beat_verifier.py tests/test_pulp_pressure_test.py tests/test_hard_floor.py
git commit -m "feat: collect pulp pressure metrics"
```

## Task 4: Three-Window Structured Extraction

**Files:**
- Modify: `forwin/writer/chapter_writer.py`
- Modify: `forwin/governance.py`
- Test: `tests/test_chapter_writer_extraction_windows.py`

- [ ] **Step 1: Write failing test for tail fallback**

Create `tests/test_chapter_writer_extraction_windows.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from forwin.writer.chapter_writer import ChapterWriter


def test_extraction_retry_uses_tail_window(monkeypatch) -> None:
    seen_bodies: list[str] = []
    writer = ChapterWriter(llm_client=SimpleNamespace())
    body = "开头铺垫" * 500 + "中段推进" * 500 + "章末他当场获得三十万赔偿，敌人失去资格。"

    def fake_chat_json(prompt: str, **kwargs):
        seen_bodies.append(prompt)
        if len(seen_bodies) == 1:
            raise RuntimeError("primary failed")
        return {"new_events": [{"summary": "获得赔偿"}]}

    monkeypatch.setattr(writer, "_chat_json", fake_chat_json)
    result = writer._extract_structured_part(
        label="state_event_extraction",
        prompt_builder=lambda context, title, chapter_body: chapter_body,
        context=SimpleNamespace(),
        chapter_title="第一章",
        chapter_body=body,
        primary_temperature=0.25,
        primary_max_tokens=100,
        retry_temperature=0.2,
        retry_max_tokens=100,
    )

    assert result["new_events"][0]["summary"] == "获得赔偿"
    assert any("三十万赔偿" in item for item in seen_bodies[1:])
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python3 -m pytest tests/test_chapter_writer_extraction_windows.py -q
```

Expected: tail content is not present because retry uses the first 1800 characters only.

- [ ] **Step 3: Add fallback window helpers**

In `forwin/writer/chapter_writer.py`, add methods on `ChapterWriter`:

```python
    @staticmethod
    def _structured_fallback_windows(chapter_body: str) -> list[str]:
        body = str(chapter_body or "")
        if len(body) <= 1800:
            return [body]
        midpoint = len(body) // 2
        windows = [
            body[:1200],
            body[max(0, midpoint - 600) : min(len(body), midpoint + 600)],
            body[-1600:],
        ]
        deduped: list[str] = []
        for window in windows:
            if window and window not in deduped:
                deduped.append(window)
        return deduped
```

Update `_extract_structured_part` retry block to iterate windows:

```python
            last_error = exc
            for shortened_body in self._structured_fallback_windows(chapter_body):
                try:
                    return self._chat_json(
                        prompt_builder(context, chapter_title, shortened_body),
                        temperature=retry_temperature,
                        max_tokens=retry_max_tokens,
                        timeout_seconds=self.scene_call_timeout_seconds,
                        max_attempts=1,
                        retry_on_timeout=False,
                        stage_key=label,
                    )
                except Exception as repair_exc:  # noqa: BLE001
                    last_error = repair_exc
            logger.warning("%s degraded to empty metadata after retry: %s", label, last_error)
            return {"_generation_meta": {label: "degraded", f"{label}_error": str(last_error)}}
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m pytest tests/test_chapter_writer_extraction_windows.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add forwin/writer/chapter_writer.py tests/test_chapter_writer_extraction_windows.py
git commit -m "fix: preserve tail facts in structured extraction retry"
```

## Task 5: Durable Generation Lease And Resume

**Files:**
- Modify: `forwin/models/task.py`
- Add migration: `forwin/migrations/versions/0012_generation_task_leases.py`
- Create: `forwin/generation/task_lease.py`
- Modify: `forwin/api_core/tasks.py`
- Modify: `forwin/api_schema/tasks.py`
- Test: `tests/test_generation_task_lease.py`
- Test: `tests/test_task_recovery_resume.py`

- [ ] **Step 1: Write failing lease tests**

Create `tests/test_generation_task_lease.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forwin.generation.task_lease import claim_generation_task, heartbeat_generation_task
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


def test_claim_generation_task_sets_lease_fields() -> None:
    engine = get_engine(postgres_test_url("generation-task-lease"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(GenerationTask(id="task-lease", task_kind="generation", status="queued", project_id="project-1"))

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)

        assert task is not None
        assert task.id == "task-lease"
        assert task.status == "running"
        assert task.lease_owner == "worker-1"
        assert task.lease_expires_at is not None
        assert task.heartbeat_at is not None
    finally:
        engine.dispose()


def test_expired_running_task_can_be_reclaimed() -> None:
    engine = get_engine(postgres_test_url("generation-task-reclaim"))
    init_db(engine)
    Session = get_session_factory(engine)
    expired = datetime.now(timezone.utc) - timedelta(minutes=10)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-expired",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="old-worker",
                    lease_expires_at=expired,
                    heartbeat_at=expired,
                )
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)

        assert task is not None
        assert task.lease_owner == "worker-2"
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py -q
```

Expected: `GenerationTask` has no lease fields and helper import fails.

- [ ] **Step 3: Add model fields and migration**

In `forwin/models/task.py`, add fields:

```python
    lease_owner: Mapped[str] = mapped_column(String, default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resume_from_chapter: Mapped[int] = mapped_column(Integer, default=0)
    run_until_chapter: Mapped[int] = mapped_column(Integer, default=0)
    max_chapters: Mapped[int] = mapped_column(Integer, default=0)
```

Create migration `forwin/migrations/versions/0012_generation_task_leases.py`:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_generation_task_leases"
down_revision = "0011_no_legacy_char_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_tasks", sa.Column("lease_owner", sa.String(), nullable=False, server_default=""))
    op.add_column("generation_tasks", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("generation_tasks", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.add_column("generation_tasks", sa.Column("resume_from_chapter", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("generation_tasks", sa.Column("run_until_chapter", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("generation_tasks", sa.Column("max_chapters", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_generation_tasks_lease", "generation_tasks", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_generation_tasks_lease", table_name="generation_tasks")
    op.drop_column("generation_tasks", "max_chapters")
    op.drop_column("generation_tasks", "run_until_chapter")
    op.drop_column("generation_tasks", "resume_from_chapter")
    op.drop_column("generation_tasks", "heartbeat_at")
    op.drop_column("generation_tasks", "lease_expires_at")
    op.drop_column("generation_tasks", "lease_owner")
```

- [ ] **Step 4: Implement lease helpers**

Create `forwin/generation/task_lease.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forwin.models.task import GenerationTask


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_generation_task(session: Session, *, worker_id: str, lease_seconds: int = 300) -> GenerationTask | None:
    now = utcnow()
    expires = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    row = (
        session.execute(
            select(GenerationTask)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.task_kind == "generation",
                or_(
                    GenerationTask.status == "queued",
                    GenerationTask.status == "starting",
                    (GenerationTask.status == "running") & (GenerationTask.lease_expires_at < now),
                ),
            )
            .order_by(GenerationTask.created_at.asc(), GenerationTask.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    row.status = "running"
    row.current_stage = "running"
    row.lease_owner = worker_id
    row.lease_expires_at = expires
    row.heartbeat_at = now
    row.started_at = row.started_at or now
    row.finished_at = None
    session.add(row)
    return row


def heartbeat_generation_task(session: Session, *, task_id: str, worker_id: str, lease_seconds: int = 300) -> bool:
    now = utcnow()
    row = session.get(GenerationTask, task_id)
    if row is None or row.lease_owner != worker_id or row.status != "running":
        return False
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    session.add(row)
    return True
```

- [ ] **Step 5: Serialize lease fields**

In `forwin/api_core/tasks.py`, add fields to `_generation_task_from_row`:

```python
        "lease_owner": str(getattr(row, "lease_owner", "") or ""),
        "lease_expires_at": getattr(row, "lease_expires_at", None),
        "heartbeat_at": getattr(row, "heartbeat_at", None),
        "resume_from_chapter": int(getattr(row, "resume_from_chapter", 0) or 0),
        "run_until_chapter": int(getattr(row, "run_until_chapter", 0) or 0),
        "max_chapters": int(getattr(row, "max_chapters", 0) or 0),
```

Add matching assignments in `_apply_generation_task_to_row`.

In `forwin/api_schema/tasks.py`, add response fields:

```python
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    resume_from_chapter: int = 0
    run_until_chapter: int = 0
    max_chapters: int = 0
```

- [ ] **Step 6: Run tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py tests/test_generation_auto_continue.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add forwin/models/task.py forwin/migrations/versions/0012_generation_task_leases.py forwin/generation/task_lease.py forwin/api_core/tasks.py forwin/api_schema/tasks.py tests/test_generation_task_lease.py
git commit -m "feat: add generation task leases"
```

## Task 6: Typed Retrieval Budgets

**Files:**
- Create: `forwin/retrieval/typed_budget.py`
- Modify: `forwin/retrieval/broker_core/broker.py`
- Test: `tests/test_retrieval_typed_budget.py`

- [ ] **Step 1: Write failing typed budget test**

Create `tests/test_retrieval_typed_budget.py`:

```python
from __future__ import annotations

from forwin.retrieval.typed_budget import RetrievalBudget, bucket_memory_results


def test_bucket_memory_results_respects_per_type_quota() -> None:
    memories = [
        {"summary": "recent 1", "memory_type": "recent"},
        {"summary": "recent 2", "memory_type": "recent"},
        {"summary": "enemy 1", "memory_type": "enemy"},
        {"summary": "wealth 1", "memory_type": "wealth_status"},
    ]
    budget = RetrievalBudget(recent=1, enemy=1, wealth_status=1, promise=1, world=1)

    result = bucket_memory_results(memories, budget)

    assert [item["summary"] for item in result["recent"]] == ["recent 1"]
    assert [item["summary"] for item in result["enemy"]] == ["enemy 1"]
    assert [item["summary"] for item in result["wealth_status"]] == ["wealth 1"]
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python3 -m pytest tests/test_retrieval_typed_budget.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement typed budget helpers**

Create `forwin/retrieval/typed_budget.py`:

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetrievalBudget(BaseModel):
    recent: int = Field(default=3, ge=0, le=12)
    promise: int = Field(default=2, ge=0, le=12)
    enemy: int = Field(default=2, ge=0, le=12)
    wealth_status: int = Field(default=2, ge=0, le=12)
    relationship: int = Field(default=1, ge=0, le=12)
    world: int = Field(default=3, ge=0, le=12)


def _memory_type(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("memory_type") or item.get("type") or "recent"
    else:
        value = getattr(item, "memory_type", "") or getattr(item, "type", "") or "recent"
    normalized = str(value or "recent").strip()
    if normalized in {"wealth", "status", "item"}:
        return "wealth_status"
    if normalized in {"obligation", "promise"}:
        return "promise"
    if normalized in {"enemy", "obstacle"}:
        return "enemy"
    if normalized in {"relationship", "faction"}:
        return "relationship"
    if normalized == "world":
        return "world"
    return "recent"


def bucket_memory_results(memories: list[Any], budget: RetrievalBudget) -> dict[str, list[Any]]:
    buckets = {name: [] for name in budget.model_fields}
    limits = budget.model_dump()
    for memory in memories:
        key = _memory_type(memory)
        if len(buckets[key]) < int(limits[key]):
            buckets[key].append(memory)
    return buckets
```

- [ ] **Step 4: Wire broker to request larger raw memory set and trim by type**

In `forwin/retrieval/broker_core/broker.py`, import:

```python
from forwin.retrieval.typed_budget import RetrievalBudget, bucket_memory_results
```

In `RetrievalBroker.__init__`, add:

```python
        retrieval_budget: RetrievalBudget | None = None,
```

and store:

```python
        self.retrieval_budget = retrieval_budget or RetrievalBudget()
```

In `_pick_memories`, request a raw limit equal to the sum of bucket limits:

```python
        raw_limit = max(self.max_memories, sum(self.retrieval_budget.model_dump().values()))
        memories = self.memory_index.search(project_id=base_pack.project_id, query=query, limit=raw_limit)
        eligible = [memory for memory in memories if memory.chapter_number < base_pack.chapter_number]
        buckets = bucket_memory_results(eligible, self.retrieval_budget)
        selected = []
        for key in ("recent", "promise", "enemy", "wealth_status", "relationship", "world"):
            selected.extend(buckets[key])
        return selected[:raw_limit]
```

- [ ] **Step 5: Run tests**

Run:

```bash
python3 -m pytest tests/test_retrieval_typed_budget.py tests/test_context_recency_truncation.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add forwin/retrieval/typed_budget.py forwin/retrieval/broker_core/broker.py tests/test_retrieval_typed_budget.py
git commit -m "feat: add typed retrieval budgets"
```

## Task 7: Persistent Trope Cooldown

**Files:**
- Create: `forwin/experience/trope_cooldown.py`
- Modify: `forwin/models/phase.py`
- Add migration: `forwin/migrations/versions/0013_trope_usage_records.py`
- Modify: `forwin/experience/service.py`
- Modify: `forwin/experience/band_scheduler.py`
- Modify: `forwin/experience/persistence.py`
- Modify: `forwin/planning/band_plan_service.py`
- Test: `tests/test_trope_cooldown.py`

- [ ] **Step 1: Write failing cooldown test**

Create `tests/test_trope_cooldown.py`:

```python
from __future__ import annotations

from forwin.experience.trope_cooldown import TropeCooldownPolicy, select_available_templates
from forwin.protocol.trope_library import TropeTemplate


def _template(template_id: str, category: str, cost: int = 1) -> TropeTemplate:
    return TropeTemplate(template_id=template_id, display_name=template_id, category=category, cost_weight=cost)


def test_select_available_templates_filters_recent_template_and_category() -> None:
    templates = [_template("power-a", "power"), _template("power-b", "power"), _template("justice-a", "justice")]
    selected = select_available_templates(
        templates,
        recent_template_ids=["power-a"],
        recent_categories=["justice"],
        policy=TropeCooldownPolicy(template_band_gap=3, category_band_gap=1),
    )

    assert [item.template_id for item in selected] == ["power-b"]
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python3 -m pytest tests/test_trope_cooldown.py -q
```

Expected: import failure.

- [ ] **Step 3: Add cooldown helpers**

Create `forwin/experience/trope_cooldown.py`:

```python
from __future__ import annotations

from pydantic import BaseModel, Field

from forwin.protocol.trope_library import TropeTemplate


class TropeCooldownPolicy(BaseModel):
    template_band_gap: int = Field(default=3, ge=0, le=20)
    category_band_gap: int = Field(default=1, ge=0, le=20)


def select_available_templates(
    templates: list[TropeTemplate],
    *,
    recent_template_ids: list[str],
    recent_categories: list[str],
    policy: TropeCooldownPolicy,
) -> list[TropeTemplate]:
    blocked_templates = set(recent_template_ids[: policy.template_band_gap])
    blocked_categories = set(recent_categories[: policy.category_band_gap])
    available = [
        template
        for template in templates
        if template.template_id not in blocked_templates
        and str(template.category) not in blocked_categories
    ]
    return available or [template for template in templates if template.template_id not in blocked_templates] or list(templates)
```

- [ ] **Step 4: Add usage persistence model and migration**

In `forwin/models/phase.py`, add:

```python
class TropeUsageRecord(Base):
    __tablename__ = "trope_usage_records"
    __table_args__ = (
        Index("ix_trope_usage_project_band", "project_id", "band_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"), nullable=False)
    arc_id: Mapped[str] = mapped_column(String, default="")
    band_id: Mapped[str] = mapped_column(String, default="")
    chapter_number: Mapped[int] = mapped_column(Integer, default=0)
    template_id: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
```

Create migration `forwin/migrations/versions/0013_trope_usage_records.py`:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_trope_usage_records"
down_revision = "0012_generation_task_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trope_usage_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False, server_default=""),
        sa.Column("band_id", sa.String(), nullable=False, server_default=""),
        sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_trope_usage_project_band", "trope_usage_records", ["project_id", "band_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_trope_usage_project_band", table_name="trope_usage_records")
    op.drop_table("trope_usage_records")
```

- [ ] **Step 5: Persist selected trope usage**

In `forwin/experience/trope_cooldown.py`, add repository helpers:

```python
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import TropeUsageRecord


def recent_trope_usage(session: Session, *, project_id: str, limit: int = 24) -> tuple[list[str], list[str]]:
    rows = (
        session.execute(
            select(TropeUsageRecord)
            .where(TropeUsageRecord.project_id == project_id)
            .order_by(TropeUsageRecord.created_at.desc(), TropeUsageRecord.id.desc())
            .limit(max(1, int(limit or 24)))
        )
        .scalars()
        .all()
    )
    return [row.template_id for row in rows], [row.category for row in rows]


def save_trope_usage(
    session: Session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
    chapter_number: int,
    template_id: str,
    category: str,
) -> TropeUsageRecord:
    row = TropeUsageRecord(
        project_id=project_id,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=chapter_number,
        template_id=template_id,
        category=category,
    )
    session.add(row)
    return row
```

In `forwin/experience/persistence.py`, add:

```python
    def save_trope_usage_records(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        schedule: BandDelightSchedule,
    ) -> None:
        from forwin.experience.trope_cooldown import save_trope_usage

        for item in schedule.scheduled_rewards:
            template_id = str(item.template_id or "").strip()
            if not template_id:
                continue
            save_trope_usage(
                session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=schedule.band_id,
                chapter_number=int(item.chapter_hint or 0),
                template_id=template_id,
                category=str(item.category or ""),
            )
```

- [ ] **Step 6: Add cooldown fields to calibration profile**

In `forwin/experience/service.py`, extend the dataclass:

```python
@dataclass(slots=True)
class AudienceCalibrationProfile:
    boost_reward_density: bool = False
    clarify_rule_legibility: bool = False
    protect_character_heat: bool = False
    hold_managed_ambiguity: bool = False
    recent_template_ids: list[str] | None = None
    recent_trope_categories: list[str] | None = None
```

- [ ] **Step 7: Wire scheduler selection**

In `forwin/experience/band_scheduler.py`, import helper:

```python
from forwin.experience.trope_cooldown import TropeCooldownPolicy, select_available_templates
```

Inside `template_for`, before selecting `under_ceiling`, filter candidates:

```python
            under_ceiling = select_available_templates(
                under_ceiling,
                recent_template_ids=list(getattr(calibration, "recent_template_ids", []) or []),
                recent_categories=list(getattr(calibration, "recent_trope_categories", []) or []),
                policy=TropeCooldownPolicy(),
            )
```

In `forwin/planning/band_plan_service.py`, after `self.persistence.save_band_experience_plan(...)`, add:

```python
        self.persistence.save_trope_usage_records(
            session=session,
            project_id=request.project_id,
            arc_id=request.arc_id,
            schedule=schedule,
        )
```

Before the scheduler call in `ensure_current_band_plan`, import and load recent
usage:

```python
        from forwin.experience.trope_cooldown import recent_trope_usage

        recent_template_ids, recent_categories = recent_trope_usage(
            session,
            project_id=request.project_id,
        )
        calibration.recent_template_ids = recent_template_ids
        calibration.recent_trope_categories = recent_categories
```

- [ ] **Step 8: Run tests**

Run:

```bash
python3 -m pytest tests/test_trope_cooldown.py tests/test_experience_planning_service.py -q
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add forwin/experience/trope_cooldown.py forwin/models/phase.py forwin/migrations/versions/0013_trope_usage_records.py forwin/experience/service.py forwin/experience/band_scheduler.py forwin/experience/persistence.py forwin/planning/band_plan_service.py tests/test_trope_cooldown.py
git commit -m "feat: add trope cooldown primitives"
```

## Task 8: Verification Pass And Pressure Report Docs

**Files:**
- Modify: `scripts/pulp_pressure_test.py`
- Modify: `docs/designs/thousand-chapter-readiness.md`
- Create: `docs/superpowers/reports/thousand-chapter-readiness-verification.md`

- [ ] **Step 1: Run focused P0 tests**

Run:

```bash
python3 -m pytest tests/test_long_run_policy.py tests/test_project_schema_long_run.py tests/test_hard_floor.py tests/test_deferred_maintenance.py tests/test_pulp_beat_verifier.py tests/test_pulp_pressure_test.py -q
```

Expected: all pass.

- [ ] **Step 2: Run focused P1/P2 tests**

Run:

```bash
python3 -m pytest tests/test_chapter_writer_extraction_windows.py tests/test_generation_task_lease.py tests/test_retrieval_typed_budget.py tests/test_trope_cooldown.py -q
```

Expected: all pass.

- [ ] **Step 3: Run compile and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin scripts
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 4: Run legacy inventory audit**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
```

Expected: exits 0. If it fails on files touched by this plan, remove the new legacy reference. If it fails on unrelated legacy-removal work already present in the worktree, record that exact failure in the verification report and do not edit unrelated files.

- [ ] **Step 5: Write verification report**

Create `docs/superpowers/reports/thousand-chapter-readiness-verification.md`:

```markdown
# Thousand-Chapter Readiness Verification

## Scope

Implemented P0-P2 primitives from `docs/superpowers/specs/2026-05-21-thousand-chapter-readiness-design.md`.

## Commands

| Command | Result |
|---|---|
| `python3 -m pytest tests/test_long_run_policy.py tests/test_project_schema_long_run.py tests/test_hard_floor.py tests/test_deferred_maintenance.py tests/test_pulp_beat_verifier.py tests/test_pulp_pressure_test.py -q` | PASS |
| `python3 -m pytest tests/test_chapter_writer_extraction_windows.py tests/test_generation_task_lease.py tests/test_retrieval_typed_budget.py tests/test_trope_cooldown.py -q` | PASS |
| `python3 -m compileall -q forwin scripts` | PASS |
| `git diff --check` | PASS |
| `python3 scripts/audit_legacy_inventory.py --check --strict-patterns` | PASS |

## Remaining Runtime Proof

- 30 chapter pressure report path is available.
- 100 chapter quality stability requires a real generated project run.
- 300 chapter unattended readiness requires controlled restart/reclaim exercise.
```

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/reports/thousand-chapter-readiness-verification.md docs/designs/thousand-chapter-readiness.md scripts/pulp_pressure_test.py
git commit -m "docs: record thousand chapter verification"
```

## Final Verification

Run:

```bash
python3 -m pytest tests/test_long_run_policy.py tests/test_project_schema_long_run.py tests/test_hard_floor.py tests/test_deferred_maintenance.py tests/test_pulp_beat_verifier.py tests/test_pulp_pressure_test.py tests/test_chapter_writer_extraction_windows.py tests/test_generation_task_lease.py tests/test_retrieval_typed_budget.py tests/test_trope_cooldown.py -q
python3 -m compileall -q forwin scripts
git diff --check
python3 scripts/audit_legacy_inventory.py --check --strict-patterns
```

Expected: all commands exit 0, except the legacy audit may report unrelated in-flight legacy-removal changes from the shared worktree. Do not claim that audit passed unless the command exits 0 in the execution worktree.
