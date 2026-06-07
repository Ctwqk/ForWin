# P1 Long-Run Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix P1 long-run correctness risks by trimming retrieved memories under context budget, splitting trope usage into planned/accepted stages, and exposing P0/P1 pressure counters.

**Architecture:** Keep the existing broker, experience persistence, and pressure-report boundaries. Retrieval trimming remains local to `RetrievalBroker`; trope stages are modeled as a new column plus stage-aware helper APIs; accepted usage is recorded from the accepted chapter path without changing planning contracts.

**Tech Stack:** Python 3.13, SQLAlchemy ORM, Alembic migrations, pytest, existing ForWin protocol and orchestration modules.

---

## File Structure

- Modify `forwin/retrieval/broker_core/broker.py`
  - Add type-aware memory eviction in `_trim_pack`.
  - Add memory before/after/pruned fields to `last_observability_summary`.
- Modify `forwin/orchestrator_loop_core/project_chapters.py`
  - Include `pruned_memories` in `CONTEXT_PRUNED` emission.
  - Record accepted trope usage after accepted chapter status is marked.
- Modify `forwin/experience/trope_cooldown.py`
  - Add `usage_stage` normalization.
  - Filter recent cooldown reads to accepted usage by default.
  - Make usage writes idempotent by project/chapter/template/stage.
  - Add a helper to record accepted chapter usage from `experience_plan_json`.
- Modify `forwin/experience/persistence.py`
  - Mark band-plan scheduled usage as `planned`.
- Modify `forwin/models/phase.py`
  - Add `TropeUsageRecord.usage_stage`.
- Create `forwin/migrations/versions/0020_trope_usage_stage.py`
  - Add and backfill `usage_stage`; create lookup index.
- Modify `scripts/pulp_pressure_test.py`
  - Add read-only summary counters for planned/accepted trope usage and P0/P1 risks.
- Test files:
  - `tests/test_retrieval_typed_budget.py`
  - `tests/test_trope_cooldown.py`
  - `tests/test_pulp_pressure_test.py`

## Execution Setup

- [ ] **Step 1: Create isolated worktree**

```bash
git status --short
git worktree add .worktrees/p1-longrun-correctness -b codex/p1-longrun-correctness
cd .worktrees/p1-longrun-correctness
```

Expected: worktree exists on `codex/p1-longrun-correctness`, with only planned docs inherited from `master`.

- [ ] **Step 2: Use repo virtualenv**

```bash
/home/kikuhiko/ForWin/.venv/bin/python --version
/home/kikuhiko/ForWin/.venv/bin/pytest --version
```

Expected: Python and pytest commands run successfully.

---

### Task 1: Trim Retrieved Memories Under Context Budget

**Files:**
- Modify: `forwin/retrieval/broker_core/broker.py`
- Test: `tests/test_retrieval_typed_budget.py`

- [ ] **Step 1: Write failing broker memory trim tests**

Append tests that build a real `ChapterContextPack` with oversized memory summaries and a tiny budget:

```python
from forwin.protocol.context import ChapterContextPack


def _memory(summary: str, memory_type: str) -> SimpleNamespace:
    return SimpleNamespace(summary=summary, memory_type=memory_type, chapter_number=1)


def _pack_with_memories(memories: list[SimpleNamespace]) -> ChapterContextPack:
    return ChapterContextPack(
        project_id="project-1",
        chapter_number=5,
        chapter_plan_title="标题",
        chapter_plan_one_line="一句话",
        chapter_goals=[],
        active_entities=[],
        active_threads=[],
        active_relations=[],
        previous_chapter_summaries=[],
        retrieved_memories=memories,
    )


def test_trim_pack_prunes_low_priority_memories_before_obligations() -> None:
    broker = RetrievalBroker(context_budget_chars=350)
    pack = _pack_with_memories(
        [
            _memory("recent " + "x" * 180, "recent"),
            _memory("promise " + "x" * 180, "promise"),
            _memory("enemy " + "x" * 180, "enemy"),
        ]
    )

    trimmed = broker._trim_pack(pack)

    assert [item.memory_type for item in trimmed.retrieved_memories] == ["promise", "enemy"]


def test_finalize_context_summary_reports_memory_pruning() -> None:
    broker = RetrievalBroker(context_budget_chars=350)
    base_pack = _pack_with_memories(
        [
            _memory("recent " + "x" * 180, "recent"),
            _memory("promise " + "x" * 180, "promise"),
            _memory("enemy " + "x" * 180, "enemy"),
        ]
    )
    trimmed = broker._trim_pack(base_pack)

    broker._finalize_context_summary(
        base_pack=base_pack,
        pack=trimmed,
        memories=list(base_pack.retrieved_memories),
    )

    assert broker.last_observability_summary["memories_count_before"] == 3
    assert broker.last_observability_summary["memories_count_after"] == 2
    assert broker.last_observability_summary["pruned_memories"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_retrieval_typed_budget.py -q
```

Expected: FAIL because `_trim_pack` does not remove memories and summary lacks the new keys.

- [ ] **Step 3: Add type-aware memory eviction**

In `forwin/retrieval/broker_core/broker.py`, add helpers near `_trim_pack`:

```python
    @staticmethod
    def _memory_eviction_rank(memory: object) -> tuple[int, int]:
        memory_type = str(getattr(memory, "memory_type", "") or "").strip()
        priority = {
            "recent": 0,
            "relationship": 1,
            "world": 1,
            "enemy": 2,
            "wealth_status": 2,
            "promise": 2,
        }.get(memory_type, 1)
        return priority, int(getattr(memory, "chapter_number", 0) or 0)

    def _drop_lowest_priority_memory(self, memories: list[object]) -> tuple[list[object], object | None]:
        if not memories:
            return memories, None
        remove_index = min(
            range(len(memories)),
            key=lambda index: (self._memory_eviction_rank(memories[index]), -index),
        )
        removed = memories[remove_index]
        return memories[:remove_index] + memories[remove_index + 1 :], removed
```

Then add the memory branch in `_trim_pack` after `active_relations` and before the entity floor:

```python
            memories = list(getattr(pack, "retrieved_memories", []) or [])
            if memories:
                next_memories, removed = self._drop_lowest_priority_memory(memories)
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(update={"retrieved_memories": next_memories})
                continue
```

Update `_finalize_context_summary`:

```python
            "memories_count_before": len(getattr(base_pack, "retrieved_memories", []) or memories or []),
            "memories_count_after": len(getattr(pack, "retrieved_memories", []) or []),
            "memories_count": len(getattr(pack, "retrieved_memories", []) or memories or []),
            "pruned_memories": max(
                0,
                len(getattr(base_pack, "retrieved_memories", []) or memories or [])
                - len(getattr(pack, "retrieved_memories", []) or []),
            ),
```

- [ ] **Step 4: Include memory pruning in context-pruned event**

In `forwin/orchestrator_loop_core/project_chapters.py`, expand the pruning key set:

```python
                    for key in (
                        "pruned_entities",
                        "pruned_threads",
                        "pruned_relations",
                        "pruned_memories",
                    )
```

- [ ] **Step 5: Run focused tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_retrieval_typed_budget.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit retrieval memory trimming**

```bash
git add forwin/retrieval/broker_core/broker.py forwin/orchestrator_loop_core/project_chapters.py tests/test_retrieval_typed_budget.py
git commit -m "fix: trim retrieved memories under context budget"
```

---

### Task 2: Split Planned And Accepted Trope Usage

**Files:**
- Modify: `forwin/models/phase.py`
- Modify: `forwin/experience/trope_cooldown.py`
- Modify: `forwin/experience/persistence.py`
- Create: `forwin/migrations/versions/0020_trope_usage_stage.py`
- Test: `tests/test_trope_cooldown.py`

- [ ] **Step 1: Write failing trope stage tests**

Extend `tests/test_trope_cooldown.py`:

```python
import json

from forwin.experience.trope_cooldown import save_accepted_trope_usage_for_chapter


def test_planned_trope_usage_does_not_count_as_default_recent_usage() -> None:
    engine = get_engine(postgres_test_url("trope-usage-stages"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="power-a",
                category="power",
                usage_stage="planned",
            )
            save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="justice-a",
                category="justice",
                usage_stage="accepted",
            )

        with Session.begin() as session:
            template_ids, categories = recent_trope_usage(session, project_id="project-1")
            planned_template_ids, planned_categories = recent_trope_usage(
                session,
                project_id="project-1",
                usage_stage="planned",
            )

        assert template_ids == ["justice-a"]
        assert categories == ["justice"]
        assert planned_template_ids == ["power-a"]
        assert planned_categories == ["power"]
    finally:
        engine.dispose()


def test_save_trope_usage_is_idempotent_by_project_chapter_template_stage() -> None:
    engine = get_engine(postgres_test_url("trope-usage-idempotent"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            first = save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="power-a",
                category="power",
                usage_stage="accepted",
            )
            second = save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="power-a",
                category="power",
                usage_stage="accepted",
            )

        assert first.id == second.id
    finally:
        engine.dispose()


def test_save_accepted_trope_usage_for_chapter_extracts_plan_templates() -> None:
    engine = get_engine(postgres_test_url("trope-accepted-helper"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            save_accepted_trope_usage_for_chapter(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="",
                chapter_number=3,
                experience_plan_json=json.dumps(
                    {
                        "selected_template_ids": ["power-a", "justice-a"],
                        "planned_reward_tags": ["power", "justice"],
                    },
                    ensure_ascii=False,
                ),
            )

        with Session.begin() as session:
            template_ids, categories = recent_trope_usage(session, project_id="project-1")

        assert template_ids == ["justice-a", "power-a"]
        assert categories == ["justice", "power"]
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_trope_cooldown.py -q
```

Expected: FAIL because `usage_stage` and the accepted helper do not exist.

- [ ] **Step 3: Add model column**

In `forwin/models/phase.py`, update `TropeUsageRecord.__table_args__` and fields:

```python
        Index("ix_trope_usage_project_stage_created", "project_id", "usage_stage", "created_at"),
```

```python
    usage_stage: Mapped[str] = mapped_column(
        String,
        default="accepted",
        server_default="accepted",
    )
```

- [ ] **Step 4: Add migration**

Create `forwin/migrations/versions/0020_trope_usage_stage.py`:

```python
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020_trope_usage_stage"
down_revision = "0019_rewrite_attempt_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE trope_usage_records "
            "ADD COLUMN IF NOT EXISTS usage_stage VARCHAR NOT NULL DEFAULT 'accepted'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE trope_usage_records SET usage_stage = 'accepted' "
            "WHERE usage_stage IS NULL OR usage_stage = ''"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_trope_usage_project_stage_created "
            "ON trope_usage_records (project_id, usage_stage, created_at)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_trope_usage_project_stage_created"))
    op.execute(sa.text("ALTER TABLE trope_usage_records DROP COLUMN IF EXISTS usage_stage"))
```

- [ ] **Step 5: Add stage-aware trope helpers**

In `forwin/experience/trope_cooldown.py`, add:

```python
import json
from typing import Any


def _normalize_usage_stage(value: str | None) -> str:
    stage = str(value or "accepted").strip()
    return stage if stage in {"planned", "accepted"} else "accepted"
```

Change `recent_trope_usage` to accept and filter `usage_stage`:

```python
def recent_trope_usage(
    session: Session,
    *,
    project_id: str,
    limit: int = 24,
    usage_stage: str = "accepted",
) -> tuple[list[str], list[str]]:
    stage = _normalize_usage_stage(usage_stage)
    rows = (
        session.execute(
            select(TropeUsageRecord)
            .where(
                TropeUsageRecord.project_id == project_id,
                TropeUsageRecord.usage_stage == stage,
            )
            .order_by(TropeUsageRecord.created_at.desc(), TropeUsageRecord.id.desc())
            .limit(max(1, int(limit or 24)))
        )
        .scalars()
        .all()
    )
    return [row.template_id for row in rows], [row.category for row in rows]
```

Change `save_trope_usage` to accept `usage_stage`, check for existing row, and set the field:

```python
def save_trope_usage(
    session: Session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
    chapter_number: int,
    template_id: str,
    category: str,
    usage_stage: str = "accepted",
) -> TropeUsageRecord:
    stage = _normalize_usage_stage(usage_stage)
    normalized_template_id = str(template_id or "").strip()
    existing = session.execute(
        select(TropeUsageRecord).where(
            TropeUsageRecord.project_id == project_id,
            TropeUsageRecord.chapter_number == int(chapter_number or 0),
            TropeUsageRecord.template_id == normalized_template_id,
            TropeUsageRecord.usage_stage == stage,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = TropeUsageRecord(
        project_id=project_id,
        arc_id=arc_id,
        band_id=band_id,
        chapter_number=int(chapter_number or 0),
        template_id=normalized_template_id,
        category=category,
        usage_stage=stage,
    )
    session.add(row)
    return row
```

Add accepted plan extraction helper:

```python
def save_accepted_trope_usage_for_chapter(
    session: Session,
    *,
    project_id: str,
    arc_id: str,
    band_id: str,
    chapter_number: int,
    experience_plan_json: str,
) -> list[TropeUsageRecord]:
    plan = _json_loads(experience_plan_json, {})
    if not isinstance(plan, dict):
        return []
    template_ids = _plan_values(
        plan,
        "selected_template_ids",
        "selected_trope_ids",
        "template_ids",
        "trope_ids",
        "active_band_template_ids",
    )
    categories = _plan_values(
        plan,
        "planned_reward_tags",
        "selected_trope_categories",
        "reward_tags",
    )
    rows: list[TropeUsageRecord] = []
    for index, template_id in enumerate(template_ids):
        category = categories[index] if index < len(categories) else ""
        rows.append(
            save_trope_usage(
                session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_number=chapter_number,
                template_id=template_id,
                category=category,
                usage_stage="accepted",
            )
        )
    return rows
```

Also add `_plan_values` and `_json_loads` helpers that mirror `scripts/pulp_pressure_test.py` list extraction.

- [ ] **Step 6: Mark scheduled band usage as planned**

In `forwin/experience/persistence.py`, pass:

```python
                usage_stage="planned",
```

- [ ] **Step 7: Run trope tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_trope_cooldown.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit trope stage model and helpers**

```bash
git add forwin/models/phase.py forwin/experience/trope_cooldown.py forwin/experience/persistence.py forwin/migrations/versions/0020_trope_usage_stage.py tests/test_trope_cooldown.py
git commit -m "fix: split planned and accepted trope usage"
```

---

### Task 3: Record Accepted Trope Usage In Chapter Acceptance Path

**Files:**
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_trope_cooldown.py`

- [ ] **Step 1: Add a focused helper test for chapter-plan shaped input**

If Task 2 helper test does not already cover plan-shaped JSON, ensure `tests/test_trope_cooldown.py` includes:

```python
def test_save_accepted_trope_usage_for_chapter_ignores_empty_templates() -> None:
    engine = get_engine(postgres_test_url("trope-accepted-empty"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            rows = save_accepted_trope_usage_for_chapter(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="",
                chapter_number=3,
                experience_plan_json=json.dumps(
                    {
                        "selected_template_ids": ["", "power-a"],
                        "planned_reward_tags": ["", "power"],
                    },
                    ensure_ascii=False,
                ),
            )

        assert len(rows) == 1
    finally:
        engine.dispose()
```

- [ ] **Step 2: Wire accepted usage after accepted status**

In `forwin/orchestrator_loop_core/project_chapters.py`, add an import:

```python
from forwin.experience.trope_cooldown import save_accepted_trope_usage_for_chapter
```

After `updater.mark_chapter_status(... status="accepted" ...)` and before `_defer_structured_extraction_if_needed(...)`, add:

```python
            save_accepted_trope_usage_for_chapter(
                session,
                project_id=project_id,
                arc_id=str(getattr(chapter_plan, "arc_plan_id", "") or ""),
                band_id="",
                chapter_number=chapter_num,
                experience_plan_json=str(
                    getattr(chapter_plan, "experience_plan_json", "") or "{}"
                ),
            )
```

- [ ] **Step 3: Run focused tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_trope_cooldown.py tests/test_canon_repair_stage.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit accepted usage wiring**

```bash
git add forwin/orchestrator_loop_core/project_chapters.py tests/test_trope_cooldown.py
git commit -m "fix: record accepted trope usage after chapter acceptance"
```

---

### Task 4: Add P1 Pressure Counters

**Files:**
- Modify: `scripts/pulp_pressure_test.py`
- Test: `tests/test_pulp_pressure_test.py`

- [ ] **Step 1: Write failing pressure counter assertions**

In `tests/test_pulp_pressure_test.py`, import `TropeUsageRecord` and seed usage/event rows:

```python
from forwin.models.phase import TropeUsageRecord
```

Inside the existing `Session.begin()` seed block:

```python
            session.add_all(
                [
                    TropeUsageRecord(
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band-1",
                        chapter_number=1,
                        template_id="trope-a",
                        category="power",
                        usage_stage="planned",
                    ),
                    TropeUsageRecord(
                        project_id=project.id,
                        arc_id=arc.id,
                        band_id="band-1",
                        chapter_number=1,
                        template_id="trope-a",
                        category="power",
                        usage_stage="accepted",
                    ),
                ]
            )
```

Add three more events:

```python
                    DecisionEvent(
                        project_id=project.id,
                        chapter_number=2,
                        event_type=DecisionEventType.CANON_COMMIT_FAILED,
                        payload_json=json.dumps({"reason": "apply_failed"}, ensure_ascii=False),
                    ),
                    DecisionEvent(
                        project_id=project.id,
                        chapter_number=2,
                        event_type=DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED,
                        payload_json=json.dumps({"worker_id": "worker-1"}, ensure_ascii=False),
                    ),
                    DecisionEvent(
                        project_id=project.id,
                        chapter_number=2,
                        event_type=DecisionEventType.CONTEXT_PRUNED,
                        payload_json=json.dumps({"pruned_memories": 2}, ensure_ascii=False),
                    ),
```

Change the seeded task to include a failed chapter:

```python
                    failed_chapters_json="[3]",
```

Assert summary counters:

```python
        assert summary["planned_trope_usage_count"] == 1
        assert summary["accepted_trope_usage_count"] == 1
        assert summary["canon_commit_failed_count"] == 1
        assert summary["generation_worker_heartbeat_failed_count"] == 1
        assert summary["failed_chapter_stop_count"] == 1
        assert summary["context_memory_pruned_count"] == 2
```

- [ ] **Step 2: Run pressure test to verify it fails**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_pulp_pressure_test.py -q
```

Expected: FAIL because summary counters are absent.

- [ ] **Step 3: Add pressure counter reads**

In `scripts/pulp_pressure_test.py`, import:

```python
from forwin.models.phase import TropeUsageRecord
```

Add optional trope counts to `compute_summary`:

```python
def compute_summary(
    rows: list[ChapterMetric],
    *,
    events: list[DecisionEvent] | None = None,
    tasks: list[GenerationTask] | None = None,
    trope_usage_counts: dict[str, int] | None = None,
) -> dict[str, object]:
```

Normalize:

```python
    trope_usage_counts = trope_usage_counts or {}
```

Add summary fields:

```python
        "planned_trope_usage_count": int(trope_usage_counts.get("planned", 0)),
        "accepted_trope_usage_count": int(trope_usage_counts.get("accepted", 0)),
        "canon_commit_failed_count": _event_count(events, DecisionEventType.CANON_COMMIT_FAILED),
        "generation_worker_heartbeat_failed_count": _event_count(
            events,
            DecisionEventType.GENERATION_WORKER_HEARTBEAT_FAILED,
        ),
        "failed_chapter_stop_count": _failed_chapter_stop_count(tasks),
        "context_memory_pruned_count": _context_memory_pruned_count(events),
```

Update `write_reports` signature and call `compute_summary(..., trope_usage_counts=trope_usage_counts)`.

In `main()`, collect:

```python
            trope_usage_counts = _trope_usage_counts(session, args.project_id)
        write_reports(
            rows,
            args.output,
            events=events,
            tasks=tasks,
            trope_usage_counts=trope_usage_counts,
        )
```

Add helpers:

```python
def _trope_usage_counts(session, project_id: str) -> dict[str, int]:  # noqa: ANN001
    rows = (
        session.query(TropeUsageRecord.usage_stage, TropeUsageRecord.id)
        .filter(TropeUsageRecord.project_id == project_id)
        .all()
    )
    counts = {"planned": 0, "accepted": 0}
    for usage_stage, _row_id in rows:
        stage = str(usage_stage or "accepted")
        if stage not in counts:
            stage = "accepted"
        counts[stage] += 1
    return counts


def _event_count(events: list[DecisionEvent], event_type: str) -> int:
    return sum(1 for event in events if str(event.event_type or "") == str(event_type))


def _failed_chapter_stop_count(tasks: list[GenerationTask]) -> int:
    return sum(len(_json_list_ints(task.failed_chapters_json)) for task in tasks)


def _context_memory_pruned_count(events: list[DecisionEvent]) -> int:
    total = 0
    for event in events:
        if str(event.event_type or "") != str(DecisionEventType.CONTEXT_PRUNED):
            continue
        payload = _json_loads(event.payload_json, {})
        if not isinstance(payload, dict):
            continue
        try:
            total += int(payload.get("pruned_memories") or 0)
        except (TypeError, ValueError):
            continue
    return total
```

- [ ] **Step 4: Run pressure tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_pulp_pressure_test.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit pressure counters**

```bash
git add scripts/pulp_pressure_test.py tests/test_pulp_pressure_test.py
git commit -m "feat: report long-run pressure counters"
```

---

### Task 5: P1 Verification And Merge

**Files:**
- All P1 files from Tasks 1-4.

- [ ] **Step 1: Run focused test suite**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest \
  tests/test_retrieval_typed_budget.py \
  tests/test_trope_cooldown.py \
  tests/test_pulp_pressure_test.py \
  tests/test_band_plan_service.py \
  tests/test_canon_repair_stage.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run compile verification**

```bash
/home/kikuhiko/ForWin/.venv/bin/python -m compileall forwin scripts/pulp_pressure_test.py
```

Expected: command exits 0.

- [ ] **Step 3: Check worktree status**

```bash
git status --short
```

Expected: clean.

- [ ] **Step 4: Merge into master**

```bash
cd /home/kikuhiko/ForWin
git status --short
git merge --ff-only codex/p1-longrun-correctness
```

Expected: fast-forward merge succeeds.

- [ ] **Step 5: Verify on master**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest \
  tests/test_retrieval_typed_budget.py \
  tests/test_trope_cooldown.py \
  tests/test_pulp_pressure_test.py \
  tests/test_band_plan_service.py \
  tests/test_canon_repair_stage.py \
  -q
/home/kikuhiko/ForWin/.venv/bin/python -m compileall forwin scripts/pulp_pressure_test.py
```

Expected: both commands pass.

- [ ] **Step 6: Remove temporary worktree and branch**

```bash
git worktree remove .worktrees/p1-longrun-correctness
git branch -d codex/p1-longrun-correctness
```

Expected: temporary worktree and branch are removed after the merge.

## Self-Review

- Spec coverage: memory trimming, memory-prune observability, planned/accepted trope usage, accepted path recording, migration, idempotency, and pressure counters are covered by Tasks 1-4.
- Placeholder scan: no `TBD` or unresolved implementation placeholders remain.
- Type consistency: `usage_stage`, `save_accepted_trope_usage_for_chapter`, and pressure-counter helper names are consistent across tasks.
