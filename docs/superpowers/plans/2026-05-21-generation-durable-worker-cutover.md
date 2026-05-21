# Generation Durable Worker Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut generation execution over from API daemon threads to external DB-lease workers only.

**Architecture:** API/MCP/automation paths enqueue `GenerationTask` rows in `queued` state and persist a small execution payload. A separate worker CLI claims queued or expired tasks, reconstructs runtime config from env plus the persisted non-secret snapshot, honors resume/pause/cancel state, runs generation, and enqueues auto-continue follow-up tasks through the same queue boundary.

**Tech Stack:** Python, FastAPI internals, SQLAlchemy ORM/Alembic, PostgreSQL `SKIP LOCKED`, Pydantic, pytest.

---

## File Structure

- Create `forwin/generation/task_payload.py`
  - Defines the persisted task execution payload model.
  - Serializes non-secret runtime config fields.
  - Rebuilds a worker runtime `Config` from the worker environment plus payload overrides.
- Create `forwin/generation/worker_cli.py`
  - Runs the polling loop used by `forwin generation-worker`.
  - Keeps CLI argument parsing in `forwin/cli.py` small.
- Create `forwin/migrations/versions/0017_generation_task_execution_payload.py`
  - Adds `generation_tasks.execution_payload_json`.
- Modify `forwin/models/task.py`
  - Adds the ORM column and changes default generation status from `starting` to `queued`.
- Modify `forwin/models/base.py`
  - Adds SQLite/bootstrap upgrade for `execution_payload_json`.
- Modify `forwin/api_core/tasks.py`
  - Persists and loads `execution_payload`.
  - Creates new task records in `queued` state.
  - Allows queued generation tasks to be paused before workers claim them.
- Modify `forwin/api_core/generation.py`
  - Removes generation daemon thread starts.
  - Writes execution payloads for initial and continue tasks.
  - Keeps root decision events and task id return semantics.
- Modify `forwin/generation/task_lease.py`
  - Claims only `queued` and expired `running` tasks.
  - Excludes cancel-requested and pause-requested tasks from claims.
- Modify `forwin/generation/continue_workset.py`
  - Adds `resume_from_chapter` filtering.
- Modify `forwin/orchestrator_loop_core/run_control.py`
  - Passes resume filtering into continue workset resolution.
- Modify `forwin/api_runtime.py`
  - Allows continue execution to receive `resume_from_chapter`.
- Modify `forwin/generation/worker.py`
  - Reads execution payloads.
  - Reconstructs runtime config.
  - Passes pause/cancel predicates and completion handlers.
  - Uses resume points behaviorally.
- Modify `forwin/cli.py`
  - Adds `generation-worker` subcommand.
- Modify focused tests under `tests/`
  - Adds cutover tests and updates old `starting` assertions to `queued`.

---

### Task 1: Persist Generation Execution Payload

**Files:**
- Create: `forwin/generation/task_payload.py`
- Create: `forwin/migrations/versions/0017_generation_task_execution_payload.py`
- Modify: `forwin/models/task.py`
- Modify: `forwin/models/base.py`
- Modify: `forwin/api_core/tasks.py`
- Test: `tests/test_generation_task_payload.py`
- Test: `tests/test_generation_task_persistence.py`

- [ ] **Step 1: Write failing payload model tests**

Add `tests/test_generation_task_payload.py`:

```python
from __future__ import annotations

import json

from forwin.config import Config
from forwin.generation.task_payload import (
    GenerationTaskExecutionPayload,
    build_worker_config_from_payload,
    execution_payload_from_config,
)


def test_execution_payload_serializes_non_secret_runtime_overrides() -> None:
    config = Config(
        minimax_api_key="sk-secret",
        minimax_base_url="https://llm.example.test/v1",
        minimax_model="model-a",
        quality_profile="pulp",
        operation_mode="blackbox",
        publisher_session_secret="publisher-secret",
        codex_bridge_token="codex-secret",
    )

    payload = execution_payload_from_config(
        mode="continue",
        runtime_config=config,
        root_event_id="event-root",
        auto_continue=True,
        run_until_chapter=50,
        max_chapters=5,
    )
    raw = payload.model_dump(mode="json")

    assert raw["mode"] == "continue"
    assert raw["root_event_id"] == "event-root"
    assert raw["auto_continue"] is True
    assert raw["run_until_chapter"] == 50
    assert raw["max_chapters"] == 5
    assert raw["runtime_overrides"]["minimax_base_url"] == "https://llm.example.test/v1"
    assert raw["runtime_overrides"]["minimax_model"] == "model-a"
    assert raw["runtime_overrides"]["quality_profile"] == "pulp"
    assert "minimax_api_key" not in json.dumps(raw)
    assert "publisher-secret" not in json.dumps(raw)
    assert "codex-secret" not in json.dumps(raw)


def test_worker_config_uses_worker_secret_and_payload_generation_settings() -> None:
    base = Config(
        minimax_api_key="sk-worker",
        minimax_model="worker-default",
        operation_mode="blackbox",
    )
    payload = GenerationTaskExecutionPayload(
        mode="continue",
        runtime_overrides={
            "minimax_model": "queued-model",
            "quality_profile": "pulp",
            "operation_mode": "blackbox",
        },
        root_event_id="root-1",
    )

    config = build_worker_config_from_payload(
        base,
        payload,
        task_id="task-1",
    )

    assert config.minimax_api_key == "sk-worker"
    assert config.minimax_model == "queued-model"
    assert config.quality_profile == "pulp"
    assert config.governance_task_id == "task-1"
    assert config.governance_causal_root_id == "root-1"
```

- [ ] **Step 2: Run the payload tests to verify they fail**

Run:

```bash
python3 -m pytest tests/test_generation_task_payload.py -q
```

Expected: FAIL because `forwin.generation.task_payload` does not exist.

- [ ] **Step 3: Implement the payload helper**

Create `forwin/generation/task_payload.py`:

```python
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.api_runtime import copy_config
from forwin.config import Config


ExecutionMode = Literal["initial", "continue"]


SECRET_CONFIG_FIELDS = {
    "database_url",
    "minimax_api_key",
    "minio_access_key",
    "minio_secret_key",
    "publisher_extension_api_key",
    "publisher_session_secret",
    "http_basic_password",
    "codex_bridge_token",
}


class GenerationTaskExecutionPayload(BaseModel):
    mode: ExecutionMode
    premise: str = ""
    genre: str = ""
    num_chapters: int = 0
    auto_continue: bool = True
    root_event_id: str = ""
    model_profile_id: str = ""
    run_until_chapter: int = 0
    max_chapters: int = 0
    runtime_overrides: dict[str, Any] = Field(default_factory=dict)


def runtime_overrides_from_config(config: Config) -> dict[str, Any]:
    raw = config.model_dump(mode="json")
    return {
        key: value
        for key, value in raw.items()
        if key not in SECRET_CONFIG_FIELDS
    }


def execution_payload_from_config(
    *,
    mode: ExecutionMode,
    runtime_config: Config,
    root_event_id: str = "",
    premise: str = "",
    genre: str = "",
    num_chapters: int = 0,
    auto_continue: bool = True,
    run_until_chapter: int | None = None,
    max_chapters: int | None = None,
    model_profile_id: str = "",
) -> GenerationTaskExecutionPayload:
    return GenerationTaskExecutionPayload(
        mode=mode,
        premise=str(premise or ""),
        genre=str(genre or ""),
        num_chapters=int(num_chapters or 0),
        auto_continue=bool(auto_continue),
        root_event_id=str(root_event_id or ""),
        model_profile_id=str(model_profile_id or ""),
        run_until_chapter=int(run_until_chapter or 0),
        max_chapters=int(max_chapters or 0),
        runtime_overrides=runtime_overrides_from_config(runtime_config),
    )


def payload_to_json(payload: GenerationTaskExecutionPayload) -> str:
    return payload.model_dump_json()


def payload_from_json(raw: str | None) -> GenerationTaskExecutionPayload:
    if not raw:
        return GenerationTaskExecutionPayload(mode="continue")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return GenerationTaskExecutionPayload(mode="continue")
    if not isinstance(payload, dict):
        return GenerationTaskExecutionPayload(mode="continue")
    return GenerationTaskExecutionPayload.model_validate(payload)


def build_worker_config_from_payload(
    base_config: Config,
    payload: GenerationTaskExecutionPayload,
    *,
    task_id: str,
) -> Config:
    overrides = dict(payload.runtime_overrides)
    overrides["governance_task_id"] = str(task_id or "")
    overrides["governance_causal_root_id"] = str(payload.root_event_id or "")
    for key in SECRET_CONFIG_FIELDS:
        overrides.pop(key, None)
    return copy_config(base_config, **overrides)
```

- [ ] **Step 4: Add ORM column and migration**

Modify `forwin/models/task.py`:

```python
class GenerationTask(Base):
    __tablename__ = "generation_tasks"
    # ...

    status: Mapped[str] = mapped_column(String, default="queued")
    # ...
    execution_payload_json: Mapped[str] = mapped_column(Text, default="{}")
```

Create `forwin/migrations/versions/0017_generation_task_execution_payload.py`:

```python
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0017_generation_task_execution_payload"
down_revision = "0016_project_progression_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("execution_payload_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "execution_payload_json")
```

Modify `forwin/models/base.py` in `_upgrade_generation_task_leases()`:

```python
        ("execution_payload_json", "TEXT NOT NULL DEFAULT '{}'"),
```

- [ ] **Step 5: Persist execution payload through task helpers**

Modify `forwin/api_core/tasks.py`:

```python
def _generation_task_from_row(row: GenerationTask) -> dict[str, Any]:
    return {
        # existing fields
        "execution_payload": _json_load_object(getattr(row, "execution_payload_json", "{}") or "{}"),
    }
```

Update `_apply_generation_task_to_row()`:

```python
    row.execution_payload_json = json.dumps(
        task.get("execution_payload", {}) or {},
        ensure_ascii=False,
    )
```

Update `_create_task_record()`:

```python
        "status": "queued",
        "execution_payload": {},
```

Update `_task_is_pausable()` to include queued tasks:

```python
        and str(task.get("status", "")) in {"queued", "running"}
```

- [ ] **Step 6: Add persistence assertions**

Add to `tests/test_generation_task_persistence.py`:

```python
    def test_task_record_defaults_to_queued_for_worker_cutover(self) -> None:
        task = api_module._create_task_record(title="Queue 默认", requested_chapters=1)

        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["current_stage"], "queued")
        self.assertEqual(task["execution_payload"], {})

    def test_generation_task_execution_payload_round_trips_to_database(self) -> None:
        task = api_module._create_task_record(title="payload", requested_chapters=1)
        task["execution_payload"] = {
            "mode": "continue",
            "root_event_id": "event-1",
            "auto_continue": True,
            "runtime_overrides": {"quality_profile": "pulp"},
        }

        api_module._persist_generation_task("task-payload-1", task)
        loaded = api_module._get_generation_task_or_404("task-payload-1")

        self.assertEqual(loaded["execution_payload"]["mode"], "continue")
        self.assertEqual(loaded["execution_payload"]["root_event_id"], "event-1")
        self.assertEqual(
            loaded["execution_payload"]["runtime_overrides"]["quality_profile"],
            "pulp",
        )
```

- [ ] **Step 7: Run payload and persistence tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_payload.py tests/test_generation_task_persistence.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

Run:

```bash
git add forwin/generation/task_payload.py forwin/models/task.py forwin/models/base.py forwin/migrations/versions/0017_generation_task_execution_payload.py forwin/api_core/tasks.py tests/test_generation_task_payload.py tests/test_generation_task_persistence.py
git commit -m "feat: persist generation task execution payload"
```

---

### Task 2: Make API Generation Creation Enqueue-Only

**Files:**
- Modify: `forwin/api_core/generation.py`
- Modify: `forwin/api_system_routes.py`
- Modify: `tests/test_generation_task_persistence.py`
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing enqueue-only tests**

Add to `tests/test_generation_task_persistence.py`:

```python
    def test_create_generation_task_enqueues_without_starting_thread(self) -> None:
        config = Config(database_url=postgres_test_url("generation-tasks"), minimax_api_key="sk-test")

        with patch("forwin.api_core.generation.threading.Thread") as thread_cls:
            task_id = api_module._create_generation_task(
                premise="主角从县城崛起",
                genre="都市",
                num_chapters=2,
                runtime_config=config,
                title="线程切换测试",
                subtitle="都市 · 2 章",
            )

        thread_cls.assert_not_called()
        task = api_module._get_generation_task_or_404(task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["current_stage"], "queued")
        self.assertEqual(task["execution_payload"]["mode"], "initial")
        self.assertEqual(task["execution_payload"]["premise"], "主角从县城崛起")
        self.assertEqual(task["execution_payload"]["genre"], "都市")
        self.assertEqual(task["execution_payload"]["num_chapters"], 2)

    def test_create_continue_generation_task_enqueues_without_starting_thread(self) -> None:
        config = Config(database_url=postgres_test_url("generation-tasks"), minimax_api_key="sk-test")
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Project(
                    id="project-enqueue-only",
                    title="继续入队测试",
                    premise="测试",
                    genre="玄幻",
                    creation_status="writing",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with patch("forwin.api_core.generation.threading.Thread") as thread_cls:
            task_id = api_module._create_continue_generation_task(
                project_id="project-enqueue-only",
                runtime_config=config,
                requested_chapters=3,
                max_chapters=3,
                auto_continue=False,
                run_until_chapter=8,
                title="继续入队测试",
                subtitle="继续生成",
            )

        thread_cls.assert_not_called()
        task = api_module._get_generation_task_or_404(task_id)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["project_id"], "project-enqueue-only")
        self.assertEqual(task["execution_payload"]["mode"], "continue")
        self.assertFalse(task["execution_payload"]["auto_continue"])
        self.assertEqual(task["execution_payload"]["run_until_chapter"], 8)
        self.assertEqual(task["execution_payload"]["max_chapters"], 3)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m pytest tests/test_generation_task_persistence.py::GenerationTaskPersistenceTests::test_create_generation_task_enqueues_without_starting_thread tests/test_generation_task_persistence.py::GenerationTaskPersistenceTests::test_create_continue_generation_task_enqueues_without_starting_thread -q
```

Expected: FAIL because `threading.Thread` is still called.

- [ ] **Step 3: Remove daemon execution from task factories**

Modify `forwin/api_core/generation.py`.

In `_create_generation_task()`, after `root_event_id` is created, replace the runtime thread creation with payload persistence:

```python
    runtime_config = copy_config(
        runtime_config,
        governance_task_id=task_id,
        governance_causal_root_id=root_event_id,
    )
    payload = execution_payload_from_config(
        mode="initial",
        runtime_config=runtime_config,
        root_event_id=root_event_id,
        premise=premise,
        genre=genre,
        num_chapters=num_chapters,
    )
    task_record["execution_payload"] = payload.model_dump(mode="json")
    _persist_generation_task(task_id, task_record)
    return task_id
```

Move the first `_persist_generation_task(task_id, task_record)` below payload construction so the row is written once with complete execution payload.

In `_create_continue_generation_task()`, do the same:

```python
    runtime_config = copy_config(
        runtime_config,
        governance_task_id=task_id,
        governance_causal_root_id=root_event_id,
    )
    payload = execution_payload_from_config(
        mode="continue",
        runtime_config=runtime_config,
        root_event_id=root_event_id,
        auto_continue=auto_continue,
        run_until_chapter=run_until_chapter,
        max_chapters=max_chapters,
    )
    task_record["execution_payload"] = payload.model_dump(mode="json")
    _persist_generation_task(task_id, task_record)
    return task_id
```

Import the helper:

```python
from forwin.generation.task_payload import execution_payload_from_config
```

Remove direct calls to:

```python
threading.Thread(
    target=_run_generation_with_config,
    ...
)
threading.Thread(
    target=_run_continue_project_with_config,
    ...
)
```

- [ ] **Step 4: Pass model profile id for direct generate requests**

Modify `forwin/api_system_routes.py` where `create_generation_task()` is called:

```python
            task_id = create_generation_task(
                premise=req.premise,
                genre=req.genre,
                num_chapters=req.num_chapters,
                runtime_config=runtime_config,
                project_id=normalized_project_id,
                title=task_title,
                subtitle=task_subtitle,
                model_profile_id=str(req.model_profile_id or "").strip(),
            )
```

Update `_create_generation_task()` signature in `forwin/api_core/generation.py`:

```python
    model_profile_id: str = "",
```

Pass it into `execution_payload_from_config()`.

- [ ] **Step 5: Update old `starting` expectations**

In `tests/test_mcp_server.py`, change the start-writing assertion:

```python
        self.assertEqual(started.task.status, "queued")
```

Keep:

```python
        self.assertEqual(started.task.current_stage, "queued")
```

- [ ] **Step 6: Run focused API/MCP tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_persistence.py tests/test_mcp_server.py::MCPServerTests::test_project_start_writing_materializes_arc_and_starts_generation_task -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add forwin/api_core/generation.py forwin/api_system_routes.py tests/test_generation_task_persistence.py tests/test_mcp_server.py
git commit -m "feat: enqueue generation tasks without api threads"
```

---

### Task 3: Tighten Lease Claim Semantics

**Files:**
- Modify: `forwin/generation/task_lease.py`
- Modify: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Add claim-guard tests**

Add to `tests/test_generation_task_lease.py`:

```python
def test_claim_generation_task_does_not_claim_non_expired_running_task() -> None:
    engine = get_engine(postgres_test_url("generation-task-non-expired"))
    init_db(engine)
    Session = get_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-running-owned",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="worker-1",
                    lease_expires_at=now + timedelta(minutes=5),
                    heartbeat_at=now,
                )
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)

        assert task is None
    finally:
        engine.dispose()


def test_claim_generation_task_skips_paused_or_cancel_requested_queued_tasks() -> None:
    engine = get_engine(postgres_test_url("generation-task-claim-flags"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add_all(
                [
                    GenerationTask(
                        id="task-paused-before-claim",
                        task_kind="generation",
                        status="queued",
                        project_id="project-1",
                        pause_requested=True,
                    ),
                    GenerationTask(
                        id="task-cancel-before-claim",
                        task_kind="generation",
                        status="queued",
                        project_id="project-2",
                        cancel_requested=True,
                    ),
                ]
            )

        with Session.begin() as session:
            task = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)

        assert task is None
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run tests to verify the new flag test fails**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py::test_claim_generation_task_skips_paused_or_cancel_requested_queued_tasks -q
```

Expected: FAIL because claim currently does not filter pause/cancel flags.

- [ ] **Step 3: Update claim query**

Modify `forwin/generation/task_lease.py`:

```python
            select(GenerationTask)
            .where(
                GenerationTask.deleted_at.is_(None),
                GenerationTask.task_kind == "generation",
                GenerationTask.cancel_requested.is_(False),
                GenerationTask.pause_requested.is_(False),
                or_(
                    GenerationTask.status == "queued",
                    (
                        (GenerationTask.status == "running")
                        & (
                            GenerationTask.lease_expires_at.is_(None)
                            | (GenerationTask.lease_expires_at < now)
                        )
                    ),
                ),
            )
```

Remove `GenerationTask.status == "starting"` from the claimable statuses.

- [ ] **Step 4: Run lease tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add forwin/generation/task_lease.py tests/test_generation_task_lease.py
git commit -m "fix: restrict generation task lease claims"
```

---

### Task 4: Make Resume Points Affect Continue Worksets

**Files:**
- Modify: `forwin/generation/continue_workset.py`
- Modify: `forwin/orchestrator_loop_core/run_control.py`
- Modify: `forwin/api_runtime.py`
- Modify: `forwin/generation/worker.py`
- Test: `tests/test_continue_generation_workset.py`
- Test: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Add resume-aware workset test**

Add to `tests/test_continue_generation_workset.py` near existing workset tests:

```python
def test_continue_workset_filters_chapters_before_resume_point() -> None:
    engine = get_engine(postgres_test_url("continue-workset-resume"))
    init_db(engine)
    Session = get_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            project = Project(
                id="project-workset-resume",
                title="Resume",
                premise="测试",
                genre="玄幻",
                creation_status="writing",
                created_at=now,
                updated_at=now,
            )
            arc = ArcPlanVersion(
                id="arc-workset-resume",
                project_id=project.id,
                version=1,
                arc_number=1,
                status="active",
                chapter_start=1,
                chapter_end=5,
                created_at=now,
            )
            session.add_all([project, arc])
            session.add_all(
                [
                    ChapterPlan(
                        id=f"plan-workset-resume-{number}",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=number,
                        title=f"第{number}章",
                        summary="测试",
                        status="planned",
                        created_at=now,
                        updated_at=now,
                    )
                    for number in range(1, 6)
                ]
            )

        with Session() as session:
            workset = build_continue_generation_workset(
                session,
                "project-workset-resume",
                resume_from_chapter=3,
                max_chapters=2,
            )

        assert workset.chapter_numbers == (3, 4)
        assert workset.requested_chapters == 2
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run the resume workset test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_continue_generation_workset.py::test_continue_workset_filters_chapters_before_resume_point -q
```

Expected: FAIL because `resume_from_chapter` is not accepted.

- [ ] **Step 3: Add resume filtering to workset builder**

Modify `forwin/generation/continue_workset.py` signature:

```python
def build_continue_generation_workset(
    session: Session,
    project_id: str,
    *,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
    include_failed: bool = True,
    source: str = "direct_continue",
    preloaded_plans: list[ChapterPlan] | None = None,
) -> ContinueGenerationWorkset:
```

Normalize the resume point:

```python
    resume_floor = int(resume_from_chapter or 0)
```

Filter active and materialized candidates:

```python
            and (resume_floor <= 0 or int(plan.chapter_number or 0) >= resume_floor)
```

Filter future arc predictions:

```python
        predicted = [
            number for number in _future_arc_chapter_numbers(future_arc)
            if resume_floor <= 0 or number >= resume_floor
        ]
```

- [ ] **Step 4: Thread resume through runtime and orchestrator**

Modify `forwin/api_runtime.py`:

```python
def run_continue_project_with_config(
    task_id: str,
    project_id: str,
    config: Config,
    update_task: TaskUpdater,
    logger: logging.Logger,
    should_abort: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
    completion_handler: Callable[[object], None] | None = None,
) -> None:
```

Change the operation lambda:

```python
        lambda: orchestrator.continue_project(
            project_id,
            max_chapters=max_chapters,
            resume_from_chapter=resume_from_chapter,
        ),
```

Modify `forwin/orchestrator_loop_core/run_control.py`:

```python
def continue_project(
    self,
    project_id: str,
    max_chapters: int | None = None,
    resume_from_chapter: int | None = None,
) -> RunResult:
```

Pass resume to workset resolution:

```python
        workset = build_continue_generation_workset(
            session,
            project_id,
            max_chapters=max_chapters,
            resume_from_chapter=resume_from_chapter,
            source="orchestrator_continue",
        )
```

Repeat the same argument when rebuilding the workset after future arc
materialization.

- [ ] **Step 5: Make worker default continue executor pass resume**

Modify `forwin/generation/worker.py`:

```python
        run_continue_project_with_config(
            task.id,
            str(task.project_id or ""),
            worker_config,
            update_task,
            logger,
            should_abort=_db_task_flag(session_factory, task.id, "cancel_requested"),
            should_pause=_db_task_flag(session_factory, task.id, "pause_requested"),
            max_chapters=int(task.max_chapters or 0) or None,
            resume_from_chapter=resume_from_chapter,
            completion_handler=completion_handler,
        )
```

Add the predicate helper:

```python
def _db_task_flag(
    session_factory: Callable[[], Any],
    task_id: str,
    attr: str,
) -> Callable[[], bool]:
    def _read() -> bool:
        with session_factory() as session:
            row = session.get(GenerationTask, task_id)
            return bool(getattr(row, attr, False)) if row is not None else True

    return _read
```

- [ ] **Step 6: Add executor resume assertion**

Extend `tests/test_generation_task_lease.py` with a monkeypatched runtime call:

```python
def test_default_continue_executor_passes_resume_to_runtime(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-resume-runtime"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[dict[str, object]] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-worker-resume-runtime",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                    completed_chapters_json="[1, 2]",
                    max_chapters=3,
                    execution_payload_json='{"mode":"continue","runtime_overrides":{}}',
                )
            )

        def fake_run_continue_project_with_config(*args, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            "forwin.api_runtime.run_continue_project_with_config",
            fake_run_continue_project_with_config,
        )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-resume",
            config=Config(minimax_api_key="sk-test"),
        )

        assert result.resume_from_chapter == 3
        assert calls[0]["resume_from_chapter"] == 3
    finally:
        engine.dispose()
```

- [ ] **Step 7: Run resume tests**

Run:

```bash
python3 -m pytest tests/test_continue_generation_workset.py::test_continue_workset_filters_chapters_before_resume_point tests/test_generation_task_lease.py::test_default_continue_executor_passes_resume_to_runtime -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
git add forwin/generation/continue_workset.py forwin/orchestrator_loop_core/run_control.py forwin/api_runtime.py forwin/generation/worker.py tests/test_continue_generation_workset.py tests/test_generation_task_lease.py
git commit -m "feat: honor generation task resume points"
```

---

### Task 5: Rebuild Worker Execution from Persisted Payload

**Files:**
- Modify: `forwin/generation/worker.py`
- Test: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Add payload-driven worker tests**

Add to `tests/test_generation_task_lease.py`:

```python
def test_worker_uses_initial_payload_for_new_generation(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-initial-payload"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[tuple[object, ...]] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-initial-payload",
                    task_kind="generation",
                    status="queued",
                    project_id="",
                    requested_chapters=2,
                    execution_payload_json=(
                        '{"mode":"initial","premise":"县城开局","genre":"都市",'
                        '"num_chapters":2,"runtime_overrides":{"quality_profile":"pulp"}}'
                    ),
                )
            )

        def fake_run_generation_with_config(*args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(
            "forwin.api_runtime.run_generation_with_config",
            fake_run_generation_with_config,
        )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-initial",
            config=Config(minimax_api_key="sk-test"),
        )

        assert result.claimed is True
        assert calls
        assert calls[0][1] == "县城开局"
        assert calls[0][2] == "都市"
        assert calls[0][3] == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py::test_worker_uses_initial_payload_for_new_generation -q
```

Expected: FAIL because the default new executor uses `task.message` as premise.

- [ ] **Step 3: Read payload and build worker config in worker executors**

Modify `forwin/generation/worker.py` imports:

```python
from forwin.generation.task_payload import (
    build_worker_config_from_payload,
    payload_from_json,
)
```

In `_default_continue_executor()`, parse payload and config:

```python
        payload = payload_from_json(getattr(task, "execution_payload_json", "{}"))
        worker_config = build_worker_config_from_payload(
            config,
            payload,
            task_id=task.id,
        )
```

Use `worker_config` in `run_continue_project_with_config()`.

In `_default_new_executor()`, parse payload and use payload fields:

```python
        payload = payload_from_json(getattr(task, "execution_payload_json", "{}"))
        worker_config = build_worker_config_from_payload(
            config,
            payload,
            task_id=task.id,
        )
        run_generation_with_config(
            task.id,
            payload.premise,
            payload.genre,
            int(payload.num_chapters or task.requested_chapters or 0),
            worker_config,
            update_task,
            logger,
            should_abort=_db_task_flag(session_factory, task.id, "cancel_requested"),
            should_pause=_db_task_flag(session_factory, task.id, "pause_requested"),
            completion_handler=completion_handler,
        )
```

- [ ] **Step 4: Keep worker failure updates scoped to owned leases**

In `run_one_generation_task()`, keep the existing lease-owner check before
marking failed:

```python
            if row is not None and row.lease_owner == worker_id:
                row.status = "failed"
                row.current_stage = "failed"
                row.error_message = "generation_worker_execution_failed"
                session.add(row)
```

If this block has drifted, restore this exact ownership guard.

- [ ] **Step 5: Run worker payload tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add forwin/generation/worker.py tests/test_generation_task_lease.py
git commit -m "feat: execute generation tasks from persisted payload"
```

---

### Task 6: Preserve Auto-Continue Through Worker Completion

**Files:**
- Modify: `forwin/generation/worker.py`
- Test: `tests/test_generation_auto_continue.py`
- Test: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Add worker completion auto-continue test**

Add to `tests/test_generation_task_lease.py`:

```python
def test_worker_continue_executor_passes_completion_handler(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-completion-handler"))
    init_db(engine)
    Session = get_session_factory(engine)
    seen_completion_handlers: list[object] = []
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-worker-completion",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                    max_chapters=2,
                    run_until_chapter=10,
                    execution_payload_json=(
                        '{"mode":"continue","auto_continue":true,'
                        '"run_until_chapter":10,"max_chapters":2,'
                        '"runtime_overrides":{}}'
                    ),
                )
            )

        def fake_run_continue_project_with_config(*args, **kwargs):
            seen_completion_handlers.append(kwargs.get("completion_handler"))

        monkeypatch.setattr(
            "forwin.api_runtime.run_continue_project_with_config",
            fake_run_continue_project_with_config,
        )

        run_one_generation_task(
            session_factory=Session,
            worker_id="worker-completion",
            config=Config(minimax_api_key="sk-test"),
        )

        assert callable(seen_completion_handlers[0])
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run the completion handler test to verify it fails**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py::test_worker_continue_executor_passes_completion_handler -q
```

Expected: FAIL because the worker default executor does not pass a completion handler.

- [ ] **Step 3: Add a worker completion handler factory**

Modify `forwin/generation/worker.py`:

```python
def _worker_completion_handler(
    *,
    session_factory: Callable[[], Any],
    task_id: str,
    payload,
    worker_config: Config,
) -> Callable[[object], None]:
    from forwin.generation.auto_continue import GenerationAutoContinueController

    def _create_next_task(**kwargs: Any) -> str:
        from forwin.api_core.generation import _create_continue_generation_task

        return _create_continue_generation_task(**kwargs)

    def _handle(result: object) -> None:
        if not bool(getattr(payload, "auto_continue", True)):
            return
        controller = GenerationAutoContinueController(
            session_factory=session_factory,
            create_continue_generation_task=_create_next_task,
        )
        controller.after_task_completion(
            result,
            parent_task_id=task_id,
            run_until_chapter=int(getattr(payload, "run_until_chapter", 0) or 0) or None,
            max_chapters=int(getattr(payload, "max_chapters", 0) or 0) or None,
            auto_continue=bool(getattr(payload, "auto_continue", True)),
            runtime_config=worker_config,
        )

    return _handle
```

Use the handler in both default executors:

```python
        completion_handler = _worker_completion_handler(
            session_factory=session_factory,
            task_id=task.id,
            payload=payload,
            worker_config=worker_config,
        )
```

Pass `completion_handler=completion_handler` into runtime calls.

- [ ] **Step 4: Initialize API task dependencies for CLI workers before this handler is used**

This handler imports `_create_continue_generation_task`, which uses `api_state`.
Task 7 initializes `api_state._config`, `api_state._SessionFactory`, and related
runtime singletons in the worker CLI before polling. Keep this import inside
`_create_next_task()` so unit tests can monkeypatch runtime calls without
starting API state.

- [ ] **Step 5: Run worker completion tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py::test_worker_continue_executor_passes_completion_handler tests/test_generation_auto_continue.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git add forwin/generation/worker.py tests/test_generation_task_lease.py
git commit -m "feat: run auto continue from generation worker completion"
```

---

### Task 7: Add External Generation Worker CLI

**Files:**
- Create: `forwin/generation/worker_cli.py`
- Modify: `forwin/cli.py`
- Test: `tests/test_generation_worker_cli.py`

- [ ] **Step 1: Write CLI loop tests**

Create `tests/test_generation_worker_cli.py`:

```python
from __future__ import annotations

from forwin.config import Config
from forwin.generation.worker import GenerationWorkerResult
from forwin.generation.worker_cli import run_generation_worker_loop


def test_generation_worker_loop_once_exits_when_no_task() -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    exit_code = run_generation_worker_loop(
        session_factory=lambda: None,
        config=Config(minimax_api_key="sk-test"),
        worker_id="worker-test",
        lease_seconds=300,
        poll_interval=0,
        once=True,
        run_once=fake_run_once,
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["worker_id"] == "worker-test"


def test_generation_worker_loop_polls_until_stop_after_claim() -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return GenerationWorkerResult(claimed=True, task_id="task-1", executed=True)
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    exit_code = run_generation_worker_loop(
        session_factory=lambda: None,
        config=Config(minimax_api_key="sk-test"),
        worker_id="worker-test",
        lease_seconds=300,
        poll_interval=0,
        once=False,
        max_loops=2,
        run_once=fake_run_once,
    )

    assert exit_code == 0
    assert len(calls) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3 -m pytest tests/test_generation_worker_cli.py -q
```

Expected: FAIL because `forwin.generation.worker_cli` does not exist.

- [ ] **Step 3: Implement worker CLI loop module**

Create `forwin/generation/worker_cli.py`:

```python
from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from typing import Any

from forwin.config import Config
from forwin.generation.worker import GenerationWorkerResult, run_one_generation_task


logger = logging.getLogger(__name__)


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_generation_worker_loop(
    *,
    session_factory: Callable[[], Any],
    config: Config,
    worker_id: str = "",
    lease_seconds: int = 300,
    poll_interval: float = 2.0,
    once: bool = False,
    max_loops: int = 0,
    run_once: Callable[..., GenerationWorkerResult] = run_one_generation_task,
) -> int:
    normalized_worker_id = str(worker_id or "").strip() or default_worker_id()
    loops = 0
    while True:
        loops += 1
        result = run_once(
            session_factory=session_factory,
            worker_id=normalized_worker_id,
            config=config,
            lease_seconds=lease_seconds,
        )
        if result.claimed:
            logger.info("Generation worker executed task %s", result.task_id)
        if once:
            return 0
        if max_loops > 0 and loops >= max_loops:
            return 0
        if not result.claimed:
            time.sleep(max(0.0, float(poll_interval or 0.0)))
```

- [ ] **Step 4: Add CLI command**

Modify `forwin/cli.py` imports:

```python
import signal
```

Add command function:

```python
def cmd_generation_worker(args: argparse.Namespace) -> None:
    from forwin.api_core import state as api_state
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.runtime.container import RuntimeContainer
    from forwin.generation.worker_cli import default_worker_id, run_generation_worker_loop

    config = _get_config(args)
    engine = get_engine(config.database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    api_state._config = config
    api_state._engine = engine
    api_state._SessionFactory = Session
    api_state._runtime_container = RuntimeContainer.from_config(config)

    worker_id = args.worker_id or default_worker_id()
    exit_code = run_generation_worker_loop(
        session_factory=Session,
        config=config,
        worker_id=worker_id,
        lease_seconds=args.lease_seconds,
        poll_interval=args.poll_interval,
        once=args.once,
    )
    engine.dispose()
    if exit_code:
        sys.exit(exit_code)
```

Add parser section:

```python
    worker = sub.add_parser("generation-worker", help="运行 durable generation worker")
    worker.add_argument("--worker-id", default="", help="Worker id；默认 hostname:pid")
    worker.add_argument("--lease-seconds", type=int, default=300, help="任务 lease 秒数")
    worker.add_argument("--poll-interval", type=float, default=2.0, help="无任务时轮询间隔秒数")
    worker.add_argument("--once", action="store_true", help="只 claim 一次后退出")
```

Dispatch in `main()`:

```python
    elif args.command == "generation-worker":
        cmd_generation_worker(args)
```

- [ ] **Step 5: Run CLI tests**

Run:

```bash
python3 -m pytest tests/test_generation_worker_cli.py -q
python3 -m forwin.cli generation-worker --help >/tmp/forwin-generation-worker-help.txt
grep -q "generation-worker" /tmp/forwin-generation-worker-help.txt
```

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

Run:

```bash
git add forwin/generation/worker_cli.py forwin/cli.py tests/test_generation_worker_cli.py
git commit -m "feat: add durable generation worker cli"
```

---

### Task 8: Update Pause/Cancel and Active-Task UI Semantics for Queued Tasks

**Files:**
- Modify: `forwin/api_core/tasks.py`
- Modify: `forwin/api_core/project_helpers.py`
- Modify: `forwin/project_ops/common.py`
- Test: `tests/test_generation_task_persistence.py`
- Test: `tests/test_project_operation_guards.py`

- [ ] **Step 1: Add queued pause test**

Add to `tests/test_generation_task_persistence.py`:

```python
    def test_queued_generation_task_can_be_marked_pause_requested(self) -> None:
        task = api_module._create_task_record(title="queued pause", requested_chapters=1)
        task["project_id"] = "project-queued-pause"
        api_module._persist_generation_task("task-queued-pause", task)

        loaded = api_module._get_generation_task_or_404("task-queued-pause")
        self.assertTrue(api_module._task_is_pausable(loaded))

        api_module._update_task(
            "task-queued-pause",
            pause_requested=True,
            message="已请求暂停，等待 worker 跳过 claim",
        )
        paused = api_module._get_generation_task_or_404("task-queued-pause")

        self.assertTrue(paused["pause_requested"])
        self.assertEqual(paused["status"], "queued")
```

- [ ] **Step 2: Run the queued pause test**

Run:

```bash
python3 -m pytest tests/test_generation_task_persistence.py::GenerationTaskPersistenceTests::test_queued_generation_task_can_be_marked_pause_requested -q
```

Expected: PASS after Task 1; if it fails, update `_task_is_pausable()`.

- [ ] **Step 3: Update project overlay pause affordance**

Modify `forwin/project_ops/common.py`:

```python
            "can_pause": status in {"queued", "running"} and not pause_requested,
```

Modify `forwin/api_core/project_helpers.py` pause-protection check:

```python
    if task.get("pause_requested") and normalized.get("status") in {"queued", "running"}:
        normalized.pop("status", None)
```

- [ ] **Step 4: Run operation guard tests that cover generation control**

Run:

```bash
python3 -m pytest tests/test_generation_task_persistence.py tests/test_project_operation_guards.py::ProjectOperationGuardTests::test_continue_generation_passes_auto_continue_target_to_task_creation -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 8**

Run:

```bash
git add forwin/api_core/tasks.py forwin/api_core/project_helpers.py forwin/project_ops/common.py tests/test_generation_task_persistence.py
git commit -m "fix: expose queued generation tasks as pausable"
```

---

### Task 9: End-to-End Worker Cutover Regression

**Files:**
- Test: `tests/test_generation_worker_cutover.py`
- Modify: production files only if this test exposes missed wiring

- [ ] **Step 1: Add end-to-end enqueue-then-claim test**

Create `tests/test_generation_worker_cutover.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import forwin.api as api_module
from forwin.config import Config
from forwin.generation.worker import run_one_generation_task
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, Project
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


def test_api_enqueued_continue_task_is_claimed_by_worker(monkeypatch) -> None:
    engine = get_engine(postgres_test_url("generation-worker-cutover-e2e"))
    init_db(engine)
    Session = get_session_factory(engine)
    old_session_factory = api_module._SessionFactory
    api_module._SessionFactory = Session
    calls: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            session.add(
                Project(
                    id="project-worker-cutover",
                    title="Worker Cutover",
                    premise="测试",
                    genre="玄幻",
                    creation_status="writing",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ArcPlanVersion(
                    id="arc-worker-cutover",
                    project_id="project-worker-cutover",
                    version=1,
                    arc_number=1,
                    status="active",
                    created_at=now,
                )
            )

        task_id = api_module._create_continue_generation_task(
            project_id="project-worker-cutover",
            runtime_config=Config(database_url=postgres_test_url("generation-worker-cutover-e2e"), minimax_api_key="sk-test"),
            requested_chapters=1,
            max_chapters=1,
            auto_continue=False,
            title="Worker Cutover",
            subtitle="继续生成",
        )
        queued = api_module._get_generation_task_or_404(task_id)
        assert queued["status"] == "queued"

        def fake_run_continue_project_with_config(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(
            "forwin.api_runtime.run_continue_project_with_config",
            fake_run_continue_project_with_config,
        )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-cutover",
            config=Config(database_url=postgres_test_url("generation-worker-cutover-e2e"), minimax_api_key="sk-test"),
        )

        assert result.claimed is True
        assert result.task_id == task_id
        assert calls
        with Session.begin() as session:
            row = session.get(GenerationTask, task_id)
            assert row is not None
            assert row.status == "running"
            assert row.lease_owner == "worker-cutover"
    finally:
        api_module._SessionFactory = old_session_factory
        engine.dispose()
```

- [ ] **Step 2: Run the end-to-end test**

Run:

```bash
python3 -m pytest tests/test_generation_worker_cutover.py -q
```

Expected: PASS.

- [ ] **Step 3: Add static guard against generation daemon threads**

Add to `tests/test_generation_worker_cutover.py`:

```python
def test_generation_api_no_longer_starts_daemon_generation_threads() -> None:
    source = Path("forwin/api_core/generation.py").read_text()

    assert "target=_run_generation_with_config" not in source
    assert "target=_run_continue_project_with_config" not in source
    assert "daemon=True" not in source
```

Add import:

```python
from pathlib import Path
```

- [ ] **Step 4: Run the static guard**

Run:

```bash
python3 -m pytest tests/test_generation_worker_cutover.py::test_generation_api_no_longer_starts_daemon_generation_threads -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 9**

Run:

```bash
git add tests/test_generation_worker_cutover.py
git commit -m "test: cover durable generation worker cutover"
```

---

### Task 10: Final Verification and Documentation Note

**Files:**
- Modify: `README.md` only if it already documents generation startup commands.
- Modify: no stale thousand-chapter design document in this cutover pass.

- [ ] **Step 1: Run focused regression suite**

Run:

```bash
python3 -m pytest \
  tests/test_generation_task_payload.py \
  tests/test_generation_task_persistence.py \
  tests/test_generation_task_lease.py \
  tests/test_generation_worker_cli.py \
  tests/test_generation_worker_cutover.py \
  tests/test_continue_generation_workset.py \
  tests/test_generation_auto_continue.py \
  tests/test_mcp_server.py::MCPServerTests::test_project_start_writing_materializes_arc_and_starts_generation_task \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run compile check**

Run:

```bash
python3 -m compileall forwin tests
```

Expected: PASS with no syntax errors.

- [ ] **Step 3: Run strict legacy audit**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --strict
```

Expected: PASS. The cutover must not add new legacy compatibility names.

- [ ] **Step 4: Inspect remaining generation thread references**

Run:

```bash
grep -RIn "target=_run_generation_with_config\\|target=_run_continue_project_with_config\\|daemon=True" forwin/api_core/generation.py forwin/generation forwin/cli.py
```

Expected: no `daemon=True` generation API execution reference. Other modules may still use daemon threads for automation or transport and are outside this cutover.

- [ ] **Step 5: Commit final docs only if README changed**

If README needed an operational note, commit it:

```bash
git add README.md
git commit -m "docs: document generation worker startup"
```

If README did not need changes, skip this commit.

- [ ] **Step 6: Final status**

Run:

```bash
git status --short --branch
git log --oneline -8
```

Expected: working tree clean except unrelated user changes, and recent commits match the task commits above.

---

## Self-Review

Spec coverage:

- API daemon-thread removal is covered by Tasks 2 and 9.
- DB lease as single execution authority is covered by Tasks 3, 5, 7, and 9.
- Worker CLI is covered by Task 7.
- Restart recovery plus worker claim is covered by existing recovery tests and Task 9; add a recovery-specific worker claim test during Task 9 if the existing test does not exercise claim after `_recover_interrupted_generation_tasks()`.
- Resume behavior is covered by Task 4.
- Auto-continue durability is covered by Task 6.
- Queued task visibility and pause semantics are covered by Task 8.
- Strict no-new-legacy verification is covered by Task 10.

Placeholder scan:

- No deferred implementation markers are used in this plan.
- Every code-changing task includes concrete code snippets and exact verification commands.

Type consistency:

- `GenerationTaskExecutionPayload` fields match the fields used by API payload creation and worker execution.
- `resume_from_chapter` is threaded consistently through workset builder, orchestrator, runtime, and worker.
- The worker CLI delegates to `run_one_generation_task()` and does not create a second execution path.
