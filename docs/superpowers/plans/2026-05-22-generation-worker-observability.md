# Generation Worker Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the durable generation worker to ForWin's existing DecisionEvent, PerformanceSpan, and stdlib logging systems without creating a second logging backend.

**Architecture:** Add a focused worker observability adapter that builds `OperationContext` values and calls `ObservabilityService`; keep `DecisionEvent` for task-level audit facts, `PerformanceSpan` for timing, and stdlib logging for process-level loop state. Extend lease claim return data so worker code can distinguish queued claims from expired lease reclaims, then pass `component="worker"` through the existing task runtime spans.

**Tech Stack:** Python, SQLAlchemy, pytest, stdlib `dataclasses`, ForWin `ObservabilityService`, `DecisionEvent`, `PerformanceSpan`.

---

## Existing Observability Contract

`Design-docs/V3_8.md` is the governing logging design:

- `DecisionEvent` is the business, audit, and governance event ledger.
- `DecisionEvent` must not become a low-level debug log.
- `PromptTrace` remains the LLM trace path and should not receive worker lifecycle data.
- Raw prompt, raw response, API keys, publisher secrets, cookies, and bridge tokens must not be written into worker logs or DecisionEvents.
- Empty polling and process lifecycle details belong in stdlib logs.

This implementation keeps those boundaries. Worker claim, reclaim, heartbeat ownership failure, and execution failure are task-level events. Empty poll, startup, stop, and loop exception are process logs. Worker spans reuse `PerformanceSpan`.

## File Structure

- Modify `forwin/governance.py`
  - Add worker DecisionEvent type constants and register them in `KNOWN_DECISION_EVENT_TYPES`.
- Modify `forwin/generation/task_lease.py`
  - Return `GenerationTaskClaimResult` from `claim_generation_task()` with `claim_kind` and prior lease metadata.
- Create `forwin/generation/worker_observability.py`
  - Worker-scoped helper for DecisionEvents, worker spans, and non-fatal observability failure handling.
- Modify `forwin/api_runtime.py`
  - Add `component: str = "api"` to runtime entry points and use it for `task.operation` / `task.cleanup` spans.
- Modify `forwin/generation/worker.py`
  - Consume claim result metadata, record claim/reclaim events, wrap execution in worker spans, record heartbeat and execution failures, and pass `component="worker"` to runtime calls.
- Modify `forwin/generation/worker_cli.py`
  - Add startup, empty-poll, task-executed, stop, and loop-exception stdlib logs.
- Modify `tests/test_generation_task_lease.py`
  - Update claim return expectations and worker runtime component assertions.
- Create `tests/test_generation_worker_observability.py`
  - Focused DB-backed worker observability tests.
- Modify `tests/test_generation_worker_cli.py`
  - Add stdlib log assertions and no-claim no-DecisionEvent assertion.
- Modify `tests/test_observability_phase_f_runtime_integration.py`
  - Assert default API component and explicit worker component for runtime spans.

## Task 1: Lease Claim Metadata

**Files:**
- Modify: `forwin/generation/task_lease.py`
- Modify: `forwin/generation/worker.py`
- Modify: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Write failing claim metadata tests**

Append these tests to `tests/test_generation_task_lease.py`:

```python
def test_queued_claim_reports_claim_kind_and_no_previous_owner() -> None:
    engine = get_engine(postgres_test_url("generation-task-queued-claim-kind"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-queued-kind",
                    task_kind="generation",
                    status="queued",
                    project_id="project-1",
                )
            )

        with Session.begin() as session:
            claim = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)

        assert claim is not None
        assert claim.task.id == "task-queued-kind"
        assert claim.claim_kind == "queued"
        assert claim.previous_lease_owner == ""
        assert claim.previous_lease_expires_at is None
    finally:
        engine.dispose()


def test_expired_claim_reports_previous_lease_metadata() -> None:
    engine = get_engine(postgres_test_url("generation-task-expired-claim-kind"))
    init_db(engine)
    Session = get_session_factory(engine)
    expired = datetime.now(timezone.utc) - timedelta(minutes=10)
    try:
        with Session.begin() as session:
            session.add(
                GenerationTask(
                    id="task-expired-kind",
                    task_kind="generation",
                    status="running",
                    project_id="project-1",
                    lease_owner="old-worker",
                    lease_expires_at=expired,
                    heartbeat_at=expired,
                )
            )

        with Session.begin() as session:
            claim = claim_generation_task(session, worker_id="worker-2", lease_seconds=300)

        assert claim is not None
        assert claim.task.id == "task-expired-kind"
        assert claim.task.lease_owner == "worker-2"
        assert claim.claim_kind == "expired_running"
        assert claim.previous_lease_owner == "old-worker"
        assert claim.previous_lease_expires_at == expired
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run the new tests and verify failure**

Run:

```bash
python3 -m pytest \
  tests/test_generation_task_lease.py::test_queued_claim_reports_claim_kind_and_no_previous_owner \
  tests/test_generation_task_lease.py::test_expired_claim_reports_previous_lease_metadata \
  -q
```

Expected: FAIL because `claim_generation_task()` returns `GenerationTask`, not an object with `.task` and `.claim_kind`.

- [ ] **Step 3: Add the claim result model and return metadata**

In `forwin/generation/task_lease.py`, add imports:

```python
from dataclasses import dataclass
from typing import Literal
```

Add this model above `utcnow()`:

```python
@dataclass(frozen=True)
class GenerationTaskClaimResult:
    task: GenerationTask
    claim_kind: Literal["queued", "expired_running"]
    previous_lease_owner: str = ""
    previous_lease_expires_at: datetime | None = None
```

Change the function signature:

```python
def claim_generation_task(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> GenerationTaskClaimResult | None:
```

Replace the mutation and return block at the end of `claim_generation_task()` with:

```python
    previous_status = str(row.status or "")
    previous_lease_owner = str(row.lease_owner or "")
    previous_lease_expires_at = row.lease_expires_at
    claim_kind: Literal["queued", "expired_running"] = (
        "expired_running" if previous_status == "running" else "queued"
    )
    row.status = "running"
    row.current_stage = "running"
    row.lease_owner = str(worker_id or "").strip()
    row.lease_expires_at = expires
    row.heartbeat_at = now
    row.started_at = row.started_at or now
    row.finished_at = None
    session.add(row)
    return GenerationTaskClaimResult(
        task=row,
        claim_kind=claim_kind,
        previous_lease_owner=previous_lease_owner if claim_kind == "expired_running" else "",
        previous_lease_expires_at=previous_lease_expires_at if claim_kind == "expired_running" else None,
    )
```

- [ ] **Step 4: Update existing direct claim call sites**

In `tests/test_generation_task_lease.py`, every direct claim assignment should read:

```python
claim = claim_generation_task(session, worker_id="worker-1", lease_seconds=300)
task = claim.task if claim is not None else None
```

Use the same pattern for other worker ids. Keep assertions against `task` unless the test is explicitly asserting `claim.claim_kind`.

In `forwin/generation/worker.py`, change the claim block to:

```python
        claim = claim_generation_task(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return GenerationWorkerResult(message="no_claimable_generation_task")
        task = claim.task
```

Keep the existing `task_id`, `project_id`, and `resume_from_chapter` assignments after `task = claim.task`.

- [ ] **Step 5: Run lease and worker cutover tests**

Run:

```bash
python3 -m pytest tests/test_generation_task_lease.py tests/test_generation_worker_cutover.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/generation/task_lease.py forwin/generation/worker.py tests/test_generation_task_lease.py
git commit -m "feat: expose generation task claim metadata"
```

## Task 2: Worker DecisionEvent Types

**Files:**
- Modify: `forwin/governance.py`
- Create: `tests/test_generation_worker_observability.py`

- [ ] **Step 1: Write failing event type registration test**

Create `tests/test_generation_worker_observability.py` with:

```python
from __future__ import annotations

from forwin.governance import DecisionEventType, ensure_decision_event_type


def test_generation_worker_decision_event_types_are_registered() -> None:
    assert ensure_decision_event_type("generation_worker_claimed") == (
        DecisionEventType.GENERATION_WORKER_CLAIMED
    )
    assert ensure_decision_event_type("generation_worker_reclaimed") == (
        DecisionEventType.GENERATION_WORKER_RECLAIMED
    )
    assert ensure_decision_event_type("generation_worker_heartbeat_failed") == (
        DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED
    )
    assert ensure_decision_event_type("generation_worker_execution_failed") == (
        DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED
    )
```

- [ ] **Step 2: Run the new test and verify failure**

Run:

```bash
python3 -m pytest tests/test_generation_worker_observability.py::test_generation_worker_decision_event_types_are_registered -q
```

Expected: FAIL because the enum values do not exist.

- [ ] **Step 3: Add event constants and register them**

In `forwin/governance.py`, add these enum values near the task operation events:

```python
    GENERATION_WORKER_CLAIMED = "generation_worker_claimed"
    GENERATION_WORKER_RECLAIMED = "generation_worker_reclaimed"
    GENERATION_WORKER_HEARTBEAT_FAILED = "generation_worker_heartbeat_failed"
    GENERATION_WORKER_EXECUTION_FAILED = "generation_worker_execution_failed"
```

Add the same values to `KNOWN_DECISION_EVENT_TYPES` near the existing task operation entries:

```python
    DecisionEventType.GENERATION_WORKER_CLAIMED,
    DecisionEventType.GENERATION_WORKER_RECLAIMED,
    DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED,
    DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED,
```

- [ ] **Step 4: Run the registration test**

Run:

```bash
python3 -m pytest tests/test_generation_worker_observability.py::test_generation_worker_decision_event_types_are_registered -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/governance.py tests/test_generation_worker_observability.py
git commit -m "feat: register generation worker events"
```

## Task 3: Worker Observability Helper

**Files:**
- Create: `forwin/generation/worker_observability.py`
- Modify: `tests/test_generation_worker_observability.py`

- [ ] **Step 1: Add helper tests for claim and reclaim events**

Append to `tests/test_generation_worker_observability.py`:

```python
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from forwin.config import Config
from forwin.generation.task_lease import GenerationTaskClaimResult
from forwin.generation.worker_observability import (
    generation_worker_span,
    record_worker_claim,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.observability import PerformanceSpan
from forwin.models.project import Project
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


def _seed_project_task(Session, *, task_id: str, project_id: str = "project-worker-obs") -> None:
    with Session.begin() as session:
        session.add(
            Project(
                id=project_id,
                title="Worker Observability",
                premise="premise",
                genre="玄幻",
            )
        )
        session.add(
            GenerationTask(
                id=task_id,
                task_kind="generation",
                status="running",
                project_id=project_id,
                lease_owner="worker-1",
            )
        )


def _task_events(Session, task_id: str) -> list[DecisionEvent]:
    with Session() as session:
        return list(
            session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.task_id == task_id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            ).scalars()
        )


def test_record_worker_claim_writes_project_scoped_decision_event() -> None:
    database_url = postgres_test_url("generation-worker-claim-event")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-claim-event")
        with Session() as session:
            task = session.get(GenerationTask, "task-worker-claim-event")
            assert task is not None
            claim = GenerationTaskClaimResult(task=task, claim_kind="queued")
            record_worker_claim(
                session_factory=Session,
                config=Config(database_url=database_url, minimax_api_key=""),
                worker_id="worker-1",
                claim=claim,
                resume_from_chapter=4,
                lease_seconds=300,
            )

        events = _task_events(Session, "task-worker-claim-event")
        assert [event.event_type for event in events] == ["generation_worker_claimed"]
        assert events[0].scope == "task"
        assert events[0].actor_type == "worker"
        assert events[0].actor_id == "worker-1"
        payload = json.loads(events[0].payload_json)
        assert payload["worker_id"] == "worker-1"
        assert payload["claim_kind"] == "queued"
        assert payload["resume_from_chapter"] == 4
        assert payload["lease_seconds"] == 300
        assert "previous_lease_owner" not in payload
    finally:
        engine.dispose()


def test_record_worker_reclaim_includes_previous_lease_metadata() -> None:
    database_url = postgres_test_url("generation-worker-reclaim-event")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    try:
        _seed_project_task(Session, task_id="task-worker-reclaim-event")
        with Session() as session:
            task = session.get(GenerationTask, "task-worker-reclaim-event")
            assert task is not None
            task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            claim = GenerationTaskClaimResult(
                task=task,
                claim_kind="expired_running",
                previous_lease_owner="old-worker",
                previous_lease_expires_at=expired,
            )
            record_worker_claim(
                session_factory=Session,
                config=Config(database_url=database_url, minimax_api_key=""),
                worker_id="worker-2",
                claim=claim,
                resume_from_chapter=9,
                lease_seconds=300,
            )

        event = _task_events(Session, "task-worker-reclaim-event")[0]
        assert event.event_type == "generation_worker_reclaimed"
        payload = json.loads(event.payload_json)
        assert payload["claim_kind"] == "expired_running"
        assert payload["previous_lease_owner"] == "old-worker"
        assert payload["previous_lease_expires_at"] == expired.isoformat()
    finally:
        engine.dispose()
```

- [ ] **Step 2: Add tests for worker span and swallowed observability failures**

Append to `tests/test_generation_worker_observability.py`:

```python
def test_generation_worker_span_records_performance_span() -> None:
    database_url = postgres_test_url("generation-worker-span")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-span")
        with generation_worker_span(
            session_factory=Session,
            config=Config(database_url=database_url, minimax_api_key=""),
            span_name="generation_worker.claim",
            task_id="task-worker-span",
            project_id="project-worker-obs",
            worker_id="worker-1",
            tags={"claim_kind": "queued"},
            metrics={"claimed": 1, "lease_seconds": 300},
        ) as span:
            span.metric("resume_from_chapter", 3)

        with Session() as session:
            row = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.task_id == "task-worker-span")
            ).scalar_one()
        assert row.span_name == "generation_worker.claim"
        assert row.span_kind == "worker"
        assert row.component == "worker"
        assert row.stage == "generation_worker.claim"
        assert json.loads(row.tags_json)["worker_id"] == "worker-1"
        assert json.loads(row.metrics_json)["resume_from_chapter"] == 3
    finally:
        engine.dispose()


def test_worker_observability_event_failure_is_non_fatal() -> None:
    class BrokenObservability:
        def event(self, *args, **kwargs):
            raise RuntimeError("event sink unavailable")

    database_url = postgres_test_url("generation-worker-event-nonfatal")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-event-nonfatal")
        with Session() as session:
            task = session.get(GenerationTask, "task-worker-event-nonfatal")
            assert task is not None
            claim = GenerationTaskClaimResult(task=task, claim_kind="queued")

        record_worker_claim(
            session_factory=Session,
            config=Config(database_url=database_url, minimax_api_key=""),
            worker_id="worker-1",
            claim=claim,
            resume_from_chapter=1,
            lease_seconds=300,
            observability_factory=lambda **_kwargs: BrokenObservability(),
        )

        assert _task_events(Session, "task-worker-event-nonfatal") == []
    finally:
        engine.dispose()
```

- [ ] **Step 3: Run the helper tests and verify failure**

Run:

```bash
python3 -m pytest \
  tests/test_generation_worker_observability.py::test_record_worker_claim_writes_project_scoped_decision_event \
  tests/test_generation_worker_observability.py::test_record_worker_reclaim_includes_previous_lease_metadata \
  tests/test_generation_worker_observability.py::test_generation_worker_span_records_performance_span \
  tests/test_generation_worker_observability.py::test_worker_observability_event_failure_is_non_fatal \
  -q
```

Expected: FAIL because `forwin.generation.worker_observability` does not exist.

- [ ] **Step 4: Create the helper module**

Create `forwin/generation/worker_observability.py`:

```python
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from forwin.config import Config
from forwin.generation.task_lease import GenerationTaskClaimResult
from forwin.governance import DecisionEventType
from forwin.observability import NullObservability, OperationContext, ObservabilityService
from forwin.observability.ports import ObservabilityPort, SpanHandle

logger = logging.getLogger(__name__)


ObservabilityFactory = Callable[..., ObservabilityPort]


def record_worker_claim(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    worker_id: str,
    claim: GenerationTaskClaimResult,
    resume_from_chapter: int,
    lease_seconds: int,
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    task = claim.task
    event_type = (
        DecisionEventType.GENERATION_WORKER_RECLAIMED
        if claim.claim_kind == "expired_running"
        else DecisionEventType.GENERATION_WORKER_CLAIMED
    )
    summary = (
        "生成 worker 已接管过期 generation task lease。"
        if claim.claim_kind == "expired_running"
        else "生成 worker 已认领 generation task。"
    )
    payload = _claim_payload(
        worker_id=worker_id,
        claim=claim,
        resume_from_chapter=resume_from_chapter,
        lease_seconds=lease_seconds,
    )
    _record_worker_event(
        session_factory=session_factory,
        config=config,
        task_id=str(task.id or ""),
        project_id=str(task.project_id or ""),
        worker_id=worker_id,
        event_type=event_type,
        summary=summary,
        payload=payload,
        observability_factory=observability_factory,
    )


def record_worker_heartbeat_failed(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    lease_seconds: int,
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    _record_worker_event(
        session_factory=session_factory,
        config=config,
        task_id=task_id,
        project_id=project_id,
        worker_id=worker_id,
        event_type=DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED,
        summary="生成 worker heartbeat 失败，task lease ownership 已变化。",
        payload={
            "worker_id": str(worker_id or ""),
            "lease_seconds": max(30, int(lease_seconds or 300)),
        },
        observability_factory=observability_factory,
    )


def record_worker_execution_failed(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    exc: BaseException,
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    if not str(project_id or "").strip():
        logger.warning(
            "generation_worker_execution_failed task_id=%s worker_id=%s error_class=%s",
            task_id,
            worker_id,
            exc.__class__.__name__,
        )
        return
    obs = _build_observability(
        session_factory=session_factory,
        config=config,
        observability_factory=observability_factory,
    )
    ctx = _context(task_id=task_id, project_id=project_id, worker_id=worker_id)
    try:
        obs.error(
            ctx,
            event_type=DecisionEventType.GENERATION_WORKER_EXECUTION_FAILED,
            summary="生成 worker 执行 generation task 失败。",
            scope="task",
            payload={"worker_id": str(worker_id or "")},
            related_object_type="generation_task",
            related_object_id=str(task_id or ""),
        )
    except Exception:
        logger.debug("Ignoring generation worker execution failure observation.", exc_info=True)


@contextmanager
def generation_worker_span(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    span_name: str,
    task_id: str,
    project_id: str,
    worker_id: str,
    tags: dict[str, Any] | None = None,
    metrics: dict[str, int | float] | None = None,
    observability_factory: ObservabilityFactory | None = None,
) -> Iterator[SpanHandle]:
    obs = _build_observability(
        session_factory=session_factory,
        config=config,
        observability_factory=observability_factory,
    )
    ctx = _context(task_id=task_id, project_id=project_id, worker_id=worker_id, stage=span_name)
    span_tags = {"worker_id": str(worker_id or ""), **(tags or {})}
    try:
        span_cm = obs.span(
            ctx,
            span_name,
            span_kind="worker",
            component="worker",
            tags=span_tags,
            metrics=metrics or {},
        )
    except Exception:
        logger.debug("Ignoring generation worker span setup failure.", exc_info=True)
        span_cm = NullObservability().span(ctx, span_name)
    with span_cm as span:
        yield span


def _record_worker_event(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    task_id: str,
    project_id: str,
    worker_id: str,
    event_type: str,
    summary: str,
    payload: dict[str, Any],
    observability_factory: ObservabilityFactory | None = None,
) -> None:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        logger.info(
            "generation_worker_event event_type=%s task_id=%s worker_id=%s",
            event_type,
            task_id,
            worker_id,
        )
        return
    obs = _build_observability(
        session_factory=session_factory,
        config=config,
        observability_factory=observability_factory,
    )
    ctx = _context(task_id=task_id, project_id=normalized_project_id, worker_id=worker_id)
    try:
        obs.event(
            ctx,
            event_family="runtime_observation",
            event_type=event_type,
            summary=summary,
            scope="task",
            payload=payload,
            related_object_type="generation_task",
            related_object_id=str(task_id or ""),
        )
    except Exception:
        logger.debug("Ignoring generation worker event observation.", exc_info=True)


def _claim_payload(
    *,
    worker_id: str,
    claim: GenerationTaskClaimResult,
    resume_from_chapter: int,
    lease_seconds: int,
) -> dict[str, Any]:
    task = claim.task
    payload: dict[str, Any] = {
        "worker_id": str(worker_id or ""),
        "lease_expires_at": _isoformat(task.lease_expires_at),
        "resume_from_chapter": max(0, int(resume_from_chapter or 0)),
        "claim_kind": str(claim.claim_kind or ""),
        "lease_seconds": max(30, int(lease_seconds or 300)),
        "execution_mode": "continue" if str(task.project_id or "").strip() else "initial",
    }
    if claim.claim_kind == "expired_running":
        payload["previous_lease_owner"] = str(claim.previous_lease_owner or "")
        payload["previous_lease_expires_at"] = _isoformat(claim.previous_lease_expires_at)
    return {key: value for key, value in payload.items() if value not in ("", None)}


def _context(
    *,
    task_id: str,
    project_id: str,
    worker_id: str,
    stage: str = "generation_worker",
) -> OperationContext:
    return OperationContext(
        project_id=str(project_id or ""),
        task_id=str(task_id or ""),
        stage=stage,
        actor_type="worker",
        actor_id=str(worker_id or ""),
        operation_id=str(task_id or ""),
    )


def _build_observability(
    *,
    session_factory: Callable[[], Any],
    config: Config | None,
    observability_factory: ObservabilityFactory | None,
) -> ObservabilityPort:
    try:
        if observability_factory is not None:
            return observability_factory(session_factory=session_factory, config=config)
        return ObservabilityService(session_factory=session_factory, config=config)
    except Exception:
        logger.debug("Falling back to NullObservability for generation worker.", exc_info=True)
        return NullObservability()


def _isoformat(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""
```

- [ ] **Step 5: Run helper tests**

Run:

```bash
python3 -m pytest tests/test_generation_worker_observability.py -q
```

Expected: PASS for the registration and helper tests written so far.

- [ ] **Step 6: Commit**

```bash
git add forwin/generation/worker_observability.py tests/test_generation_worker_observability.py
git commit -m "feat: add generation worker observability helper"
```

## Task 4: Runtime Span Component

**Files:**
- Modify: `forwin/api_runtime.py`
- Modify: `forwin/generation/worker.py`
- Modify: `tests/test_observability_phase_f_runtime_integration.py`
- Modify: `tests/test_generation_task_lease.py`

- [ ] **Step 1: Add runtime component tests**

In `tests/test_observability_phase_f_runtime_integration.py`, update `test_run_orchestrator_task_records_operation_and_cleanup_spans()` after the span name assertion:

```python
        assert [row.component for row in rows] == ["api", "api"]
```

Append this test:

```python
def test_run_orchestrator_task_records_worker_component_when_requested() -> None:
    engine = get_engine(postgres_test_url("phase-f-runtime-worker-component"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            session.add(
                Project(
                    id=project_id,
                    title="Runtime Worker Component",
                    premise="premise",
                    genre="玄幻",
                )
            )
            session.commit()

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-runtime-worker-component"), minimax_api_key=""),
        )
        fake_llm = _FakeCloser()
        fake_engine = _FakeRuntimeEngine()
        orchestrator = SimpleNamespace(
            _SessionFactory=Session,
            services=SimpleNamespace(observability=obs),
            llm_client=fake_llm,
            engine=fake_engine,
        )
        result = SimpleNamespace(
            status="completed",
            project_id=project_id,
            failed_chapters=[],
            paused_chapters=[],
            frozen_artifacts=[],
        )

        run_orchestrator_task(
            "task-phase-f-runtime-worker-component",
            orchestrator,
            lambda: result,
            update_task=lambda *_args, **_kwargs: None,
            logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
            error_message="runtime failed",
            default_project_id=None,
            component="worker",
        )

        with Session() as session:
            rows = session.execute(
                select(PerformanceSpan)
                .where(PerformanceSpan.task_id == "task-phase-f-runtime-worker-component")
                .order_by(PerformanceSpan.created_at.asc(), PerformanceSpan.id.asc())
            ).scalars().all()

        assert [row.span_name for row in rows] == ["task.operation", "task.cleanup"]
        assert [row.component for row in rows] == ["worker", "worker"]
    finally:
        engine.dispose()
```

- [ ] **Step 2: Add worker executor passthrough assertions**

In `tests/test_generation_task_lease.py`, update `test_default_continue_executor_passes_resume_to_runtime()` after the resume assertion:

```python
        assert calls[0]["component"] == "worker"
```

Update `test_worker_continue_executor_passes_completion_handler()` after the completion handler assertion:

```python
        assert seen_completion_handlers[0] is not None
```

Update `test_worker_uses_initial_payload_for_new_generation()` so the fake captures kwargs:

```python
    calls: list[dict[str, object]] = []
```

Replace the fake function body with:

```python
        def fake_run_generation_with_config(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
```

Replace the final argument assertions with:

```python
        assert calls
        assert calls[0]["args"][1] == "县城开局"
        assert calls[0]["args"][2] == "都市"
        assert calls[0]["args"][3] == 2
        assert calls[0]["kwargs"]["component"] == "worker"
```

- [ ] **Step 3: Run the component tests and verify failure**

Run:

```bash
python3 -m pytest \
  tests/test_observability_phase_f_runtime_integration.py::test_run_orchestrator_task_records_operation_and_cleanup_spans \
  tests/test_observability_phase_f_runtime_integration.py::test_run_orchestrator_task_records_worker_component_when_requested \
  tests/test_generation_task_lease.py::test_default_continue_executor_passes_resume_to_runtime \
  tests/test_generation_task_lease.py::test_worker_uses_initial_payload_for_new_generation \
  -q
```

Expected: FAIL because runtime functions do not accept or forward `component`.

- [ ] **Step 4: Add component parameters to runtime functions**

In `forwin/api_runtime.py`, add this keyword parameter to `run_orchestrator_task()`:

```python
    component: str = "api",
```

Add this local variable after `observability = _task_observability(orchestrator)`:

```python
    span_component = str(component or "api").strip() or "api"
```

Change both span calls:

```python
        with observability.span(
            operation_ctx,
            "task.operation",
            span_kind="task",
            component=span_component,
        ) as span:
```

```python
            with observability.span(
                cleanup_ctx,
                "task.cleanup",
                span_kind="task",
                component=span_component,
            ):
```

Add `component: str = "api"` to `run_generation_with_config()` and `run_continue_project_with_config()`, then pass it through both calls to `run_orchestrator_task()`:

```python
        component=component,
```

- [ ] **Step 5: Pass worker component from worker executors**

In `forwin/generation/worker.py`, add this keyword to `run_continue_project_with_config()`:

```python
            component="worker",
```

Add this keyword to `run_generation_with_config()`:

```python
            component="worker",
```

- [ ] **Step 6: Run runtime and worker passthrough tests**

Run:

```bash
python3 -m pytest \
  tests/test_observability_phase_f_runtime_integration.py \
  tests/test_generation_task_lease.py::test_default_continue_executor_passes_resume_to_runtime \
  tests/test_generation_task_lease.py::test_worker_uses_initial_payload_for_new_generation \
  -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add forwin/api_runtime.py forwin/generation/worker.py tests/test_observability_phase_f_runtime_integration.py tests/test_generation_task_lease.py
git commit -m "feat: mark worker runtime spans"
```

## Task 5: Wire Worker Observability into Execution

**Files:**
- Modify: `forwin/generation/worker.py`
- Modify: `tests/test_generation_worker_observability.py`

- [ ] **Step 1: Add worker integration tests**

Append to `tests/test_generation_worker_observability.py`:

```python
from forwin.generation.worker import run_one_generation_task


def test_run_one_generation_task_records_claim_event_and_execute_span() -> None:
    database_url = postgres_test_url("generation-worker-integrated-claim")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-integrated-claim")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-integrated-claim")
            assert task is not None
            task.status = "queued"
            task.completed_chapters_json = "[1, 2]"
            session.add(task)

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-integrated",
            config=Config(database_url=database_url, minimax_api_key=""),
            execute_continue=lambda _task, _resume: None,
        )

        assert result.claimed is True
        events = _task_events(Session, "task-worker-integrated-claim")
        assert "generation_worker_claimed" in [event.event_type for event in events]
        with Session() as session:
            spans = session.execute(
                select(PerformanceSpan)
                .where(PerformanceSpan.task_id == "task-worker-integrated-claim")
                .order_by(PerformanceSpan.created_at.asc(), PerformanceSpan.id.asc())
            ).scalars().all()
        assert "generation_worker.claim" in [span.span_name for span in spans]
        assert "generation_worker.execute" in [span.span_name for span in spans]
        assert all(span.component == "worker" for span in spans)
    finally:
        engine.dispose()


def test_run_one_generation_task_records_reclaim_event() -> None:
    database_url = postgres_test_url("generation-worker-integrated-reclaim")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    try:
        _seed_project_task(Session, task_id="task-worker-integrated-reclaim")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-integrated-reclaim")
            assert task is not None
            task.status = "running"
            task.lease_owner = "old-worker"
            task.lease_expires_at = expired
            task.heartbeat_at = expired
            session.add(task)

        run_one_generation_task(
            session_factory=Session,
            worker_id="worker-reclaim",
            config=Config(database_url=database_url, minimax_api_key=""),
            execute_continue=lambda _task, _resume: None,
        )

        event = _task_events(Session, "task-worker-integrated-reclaim")[0]
        assert event.event_type == "generation_worker_reclaimed"
        payload = json.loads(event.payload_json)
        assert payload["previous_lease_owner"] == "old-worker"
        assert payload["previous_lease_expires_at"] == expired.isoformat()
    finally:
        engine.dispose()


def test_run_one_generation_task_records_heartbeat_failure_event() -> None:
    database_url = postgres_test_url("generation-worker-heartbeat-failed")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-heartbeat-failed")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-heartbeat-failed")
            assert task is not None
            task.status = "queued"
            session.add(task)

        def steal_lease(task: GenerationTask, _resume: int) -> None:
            with Session.begin() as session:
                row = session.get(GenerationTask, task.id)
                assert row is not None
                row.lease_owner = "other-worker"
                session.add(row)

        run_one_generation_task(
            session_factory=Session,
            worker_id="worker-heartbeat",
            config=Config(database_url=database_url, minimax_api_key=""),
            execute_continue=steal_lease,
        )

        assert "generation_worker_heartbeat_failed" in [
            event.event_type for event in _task_events(Session, "task-worker-heartbeat-failed")
        ]
    finally:
        engine.dispose()


def test_run_one_generation_task_records_execution_failed_event() -> None:
    database_url = postgres_test_url("generation-worker-execution-failed")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        _seed_project_task(Session, task_id="task-worker-execution-failed")
        with Session.begin() as session:
            task = session.get(GenerationTask, "task-worker-execution-failed")
            assert task is not None
            task.status = "queued"
            session.add(task)

        def fail_execution(_task: GenerationTask, _resume: int) -> None:
            raise RuntimeError("worker failed")

        try:
            run_one_generation_task(
                session_factory=Session,
                worker_id="worker-failed",
                config=Config(database_url=database_url, minimax_api_key=""),
                execute_continue=fail_execution,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("run_one_generation_task should propagate executor failure")

        event_types = [event.event_type for event in _task_events(Session, "task-worker-execution-failed")]
        assert "generation_worker_execution_failed" in event_types
        with Session() as session:
            row = session.get(GenerationTask, "task-worker-execution-failed")
            assert row is not None
            assert row.status == "failed"
            assert row.error_message == "generation_worker_execution_failed"
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run worker integration tests and verify failure**

Run:

```bash
python3 -m pytest \
  tests/test_generation_worker_observability.py::test_run_one_generation_task_records_claim_event_and_execute_span \
  tests/test_generation_worker_observability.py::test_run_one_generation_task_records_reclaim_event \
  tests/test_generation_worker_observability.py::test_run_one_generation_task_records_heartbeat_failure_event \
  tests/test_generation_worker_observability.py::test_run_one_generation_task_records_execution_failed_event \
  -q
```

Expected: FAIL because `run_one_generation_task()` does not call worker observability helpers.

- [ ] **Step 3: Import helper functions in worker**

In `forwin/generation/worker.py`, add:

```python
from forwin.generation.worker_observability import (
    generation_worker_span,
    record_worker_claim,
    record_worker_execution_failed,
    record_worker_heartbeat_failed,
)
```

- [ ] **Step 4: Record claim and claim span after the claim transaction commits**

In `run_one_generation_task()`, keep claim metadata assignment inside the `with session_factory.begin()` block, then add the worker observation immediately after that block:

```python
    task_id = str(task_id or "")
    project_id = str(project_id or "")
    record_worker_claim(
        session_factory=session_factory,
        config=config,
        worker_id=worker_id,
        claim=claim,
        resume_from_chapter=resume_from_chapter,
        lease_seconds=lease_seconds,
    )
    with generation_worker_span(
        session_factory=session_factory,
        config=config,
        span_name="generation_worker.claim",
        task_id=task_id,
        project_id=project_id,
        worker_id=worker_id,
        tags={"claim_kind": claim.claim_kind},
        metrics={
            "claimed": 1,
            "lease_seconds": max(30, int(lease_seconds or 300)),
            "resume_from_chapter": max(0, int(resume_from_chapter or 0)),
        },
    ):
        pass
```

- [ ] **Step 5: Wrap executor in worker execute span**

Replace the direct executor call:

```python
        executor(task, resume_from_chapter)
```

with:

```python
        with generation_worker_span(
            session_factory=session_factory,
            config=config,
            span_name="generation_worker.execute",
            task_id=task_id,
            project_id=project_id,
            worker_id=worker_id,
            tags={"execution_mode": "continue" if project_id else "initial"},
            metrics={"resume_from_chapter": max(0, int(resume_from_chapter or 0))},
        ):
            executor(task, resume_from_chapter)
```

- [ ] **Step 6: Record execution failure before marking the task failed**

In the `except Exception:` block, bind the exception:

```python
    except Exception as exc:
```

Add this before the DB update:

```python
        record_worker_execution_failed(
            session_factory=session_factory,
            config=config,
            task_id=task_id,
            project_id=project_id,
            worker_id=worker_id,
            exc=exc,
        )
```

- [ ] **Step 7: Record heartbeat failure**

Replace the final heartbeat block with:

```python
    with session_factory.begin() as session:
        heartbeat_ok = heartbeat_generation_task(
            session,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
    if not heartbeat_ok:
        record_worker_heartbeat_failed(
            session_factory=session_factory,
            config=config,
            task_id=task_id,
            project_id=project_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
```

Keep heartbeat calls inside `_default_continue_executor()` and `_default_new_executor()` unchanged; those extend long-running leases during real runtime execution. This final heartbeat verifies ownership after the injected executor completes.

- [ ] **Step 8: Run worker observability tests**

Run:

```bash
python3 -m pytest tests/test_generation_worker_observability.py tests/test_generation_task_lease.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add forwin/generation/worker.py tests/test_generation_worker_observability.py
git commit -m "feat: record generation worker lifecycle observations"
```

## Task 6: Worker CLI Process Logs

**Files:**
- Modify: `forwin/generation/worker_cli.py`
- Modify: `tests/test_generation_worker_cli.py`

- [ ] **Step 1: Add CLI logging tests**

Replace `tests/test_generation_worker_cli.py` with:

```python
from __future__ import annotations

import logging

from sqlalchemy import select

from forwin.config import Config
from forwin.generation.worker import GenerationWorkerResult
from forwin.generation.worker_cli import run_generation_worker_loop
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from tests.postgres import postgres_test_url


def test_generation_worker_loop_once_exits_when_no_task(caplog) -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    caplog.set_level(logging.DEBUG, logger="forwin.generation.worker_cli")
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
    messages = [record.getMessage() for record in caplog.records]
    assert any("Generation worker starting" in message for message in messages)
    assert any("No claimable generation task" in message for message in messages)
    assert any("Generation worker stopping" in message for message in messages)


def test_generation_worker_loop_no_claim_does_not_write_decision_events(caplog) -> None:
    database_url = postgres_test_url("generation-worker-cli-no-claim")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)

    def fake_run_once(**_kwargs):
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    try:
        caplog.set_level(logging.DEBUG, logger="forwin.generation.worker_cli")
        exit_code = run_generation_worker_loop(
            session_factory=Session,
            config=Config(database_url=database_url, minimax_api_key="sk-test"),
            worker_id="worker-test",
            lease_seconds=300,
            poll_interval=0,
            once=True,
            run_once=fake_run_once,
        )

        assert exit_code == 0
        with Session() as session:
            count = len(session.execute(select(DecisionEvent)).scalars().all())
        assert count == 0
    finally:
        engine.dispose()


def test_generation_worker_loop_polls_until_stop_after_claim(caplog) -> None:
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return GenerationWorkerResult(
                claimed=True,
                task_id="task-1",
                project_id="project-1",
                resume_from_chapter=7,
                executed=True,
            )
        return GenerationWorkerResult(claimed=False, message="no_claimable_generation_task")

    caplog.set_level(logging.DEBUG, logger="forwin.generation.worker_cli")
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
    messages = [record.getMessage() for record in caplog.records]
    assert any("Generation worker executed task task-1" in message for message in messages)
    assert any("project_id=project-1" in message for message in messages)
    assert any("resume_from_chapter=7" in message for message in messages)


def test_generation_worker_loop_logs_exception_before_raising(caplog) -> None:
    def fake_run_once(**_kwargs):
        raise RuntimeError("loop failed")

    caplog.set_level(logging.ERROR, logger="forwin.generation.worker_cli")
    try:
        run_generation_worker_loop(
            session_factory=lambda: None,
            config=Config(minimax_api_key="sk-test"),
            worker_id="worker-test",
            lease_seconds=300,
            poll_interval=0,
            once=True,
            run_once=fake_run_once,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("run_generation_worker_loop should propagate loop failure")

    assert any("Generation worker loop failed" in record.getMessage() for record in caplog.records)
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_generation_worker_cli.py -q
```

Expected: FAIL because startup, empty-poll, stop, and exception logs are missing.

- [ ] **Step 3: Add process-level logs**

In `forwin/generation/worker_cli.py`, add this after `loops = 0`:

```python
    logger.info(
        "Generation worker starting worker_id=%s lease_seconds=%s poll_interval=%s once=%s",
        normalized_worker_id,
        lease_seconds,
        poll_interval,
        once,
    )
```

Wrap the loop body in `try` / `except` / `finally`:

```python
    try:
        while True:
            loops += 1
            result = run_once(
                session_factory=session_factory,
                worker_id=normalized_worker_id,
                config=config,
                lease_seconds=lease_seconds,
            )
            if result.claimed:
                logger.info(
                    "Generation worker executed task %s project_id=%s resume_from_chapter=%s",
                    result.task_id,
                    result.project_id,
                    result.resume_from_chapter,
                )
            else:
                logger.debug(
                    "No claimable generation task worker_id=%s message=%s",
                    normalized_worker_id,
                    result.message,
                )
            if once:
                return 0
            if max_loops > 0 and loops >= max_loops:
                return 0
            if not result.claimed:
                time.sleep(max(0.0, float(poll_interval or 0.0)))
    except Exception:
        logger.exception("Generation worker loop failed worker_id=%s", normalized_worker_id)
        raise
    finally:
        logger.info("Generation worker stopping worker_id=%s loops=%s", normalized_worker_id, loops)
```

Remove the old unwrapped `while True` block.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
python3 -m pytest tests/test_generation_worker_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/generation/worker_cli.py tests/test_generation_worker_cli.py
git commit -m "feat: log generation worker loop lifecycle"
```

## Task 7: Focused Verification

**Files:**
- Verify only; no planned code changes.

- [ ] **Step 1: Run focused worker and observability tests**

Run:

```bash
python3 -m pytest \
  tests/test_generation_task_lease.py \
  tests/test_generation_worker_observability.py \
  tests/test_generation_worker_cli.py \
  tests/test_generation_worker_cutover.py \
  tests/test_observability_phase_f_runtime_integration.py \
  tests/test_observability_phase_f_spans.py \
  tests/test_observability_v38.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run compile check**

Run:

```bash
python3 -m compileall forwin tests
```

Expected: command exits with status 0.

- [ ] **Step 3: Run legacy inventory audit**

Run:

```bash
python3 scripts/audit_legacy_inventory.py --strict
```

Expected: PASS. This change should not add a legacy compatibility path or alter the existing inventory.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit status 0.

- [ ] **Step 5: Commit verification-only adjustments if any test-required edits were made**

If focused verification forced an edit, commit exactly those changed files:

```bash
git status --short
git add forwin/generation/task_lease.py forwin/generation/worker_observability.py forwin/generation/worker.py forwin/generation/worker_cli.py forwin/api_runtime.py forwin/governance.py tests/test_generation_task_lease.py tests/test_generation_worker_observability.py tests/test_generation_worker_cli.py tests/test_observability_phase_f_runtime_integration.py
git commit -m "test: stabilize generation worker observability"
```

If `git status --short` is clean, skip this commit.

## Completion Checklist

- [ ] `claim_generation_task()` returns claim metadata and existing call sites consume `.task`.
- [ ] Worker DecisionEvent types are registered and validated by tests.
- [ ] Worker claim/reclaim/failure events use `ObservabilityService.event()` and task scope.
- [ ] Worker claim and execute spans use `PerformanceSpan` with `component="worker"`.
- [ ] `run_orchestrator_task()` keeps API default spans as `component="api"`.
- [ ] Worker runtime calls pass `component="worker"`.
- [ ] Empty worker polls produce stdlib debug logs only and do not write DecisionEvents.
- [ ] Successful heartbeat does not write a DecisionEvent.
- [ ] Heartbeat ownership failure writes `generation_worker_heartbeat_failed` when project id exists.
- [ ] Execution failure writes `generation_worker_execution_failed` and preserves current failed-task semantics.
- [ ] No full execution payload, prompt, response, API key, publisher secret, cookie, or token is logged.
- [ ] Focused tests, compile check, legacy audit, and `git diff --check` pass.
