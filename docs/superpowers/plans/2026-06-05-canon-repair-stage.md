# Canon Repair Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a canon repair phase so a `warn` or `pass` chapter that later fails canon admission gets automatic repair attempts before pausing for review.

**Architecture:** Keep the existing initial review repair behavior, but extract the repair loop so it can run for a named phase. Add phase metadata to rewrite attempts, return rich canon apply outcomes, and route repairable canon admission blocks into a new `canon_repair` phase with a fresh per-phase budget.

**Tech Stack:** Python, SQLAlchemy, Alembic, FastAPI/Pydantic schemas, pytest.

---

## File Structure

- Modify `forwin/models/phase.py`: add `repair_phase` and `phase_attempt_no` columns to `ChapterRewriteAttempt`.
- Create `forwin/migrations/versions/0019_chapter_rewrite_attempt_phase.py`: migrate the two new attempt fields.
- Modify `forwin/state/updater.py`: let `save_chapter_rewrite_attempt()` persist phase metadata with backward-compatible defaults.
- Modify `forwin/state/repo.py`: add a phase-aware attempt list helper while keeping existing total-history helper unchanged.
- Modify `forwin/api_schema/review.py`: add additive attempt phase fields to `ChapterRewriteAttemptInfo`.
- Modify `forwin/project_ops/reviews.py`: serialize the new attempt fields in chapter review responses.
- Modify `forwin/review_engine/issue_taxonomy.py`: add canon admission synthetic issue kinds that route to the requested repair scope.
- Modify `forwin/orchestrator_loop_core/quality_gates.py`: return a rich canon apply outcome with the saved gate result and blocked path.
- Modify `forwin/orchestrator_loop_core/repair_loop.py`: extract a phase-aware repair loop and add a canon block review builder.
- Modify `forwin/orchestrator_loop_core/project_chapters.py`: route repairable canon blocks into `canon_repair` before marking `needs_review`.
- Modify `forwin/orchestrator_loop_core/service.py`: export and bind `_run_repair_loop_for_phase`, `_run_canon_repair_for_block`, and `_review_from_canon_gate_block`.
- Create `tests/test_canon_repair_stage.py`: focused regression tests for phase metadata, budget reset, warn-to-canon-fail repair, exhaustion, success, and system block reporting.

---

### Task 1: Add Rewrite Attempt Phase Storage

**Files:**
- Modify: `forwin/models/phase.py`
- Modify: `forwin/state/updater.py`
- Modify: `forwin/state/repo.py`
- Modify: `forwin/api_schema/review.py`
- Modify: `forwin/project_ops/reviews.py`
- Create: `forwin/migrations/versions/0019_chapter_rewrite_attempt_phase.py`
- Test: `tests/test_canon_repair_stage.py`

- [ ] **Step 1: Write the failing serialization test**

Create `tests/test_canon_repair_stage.py` with this initial test. This test intentionally constructs the model directly so it fails before the model/schema fields exist.

```python
from __future__ import annotations

import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.models.base import Base
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import ChapterPlan, ChapterRewriteAttempt
from forwin.models.project import Project
from forwin.project_ops.reviews import get_chapter_review


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _noop_decision_refs(*args, **kwargs):
    return []


def test_rewrite_attempt_phase_fields_are_serialized_in_review_detail():
    Session = _session_factory()
    session = Session()
    try:
        project = Project(id="p", title="测试项目", genre="玄幻", premise="premise")
        plan = ChapterPlan(
            id="cp1",
            project_id="p",
            chapter_number=1,
            title="第一章",
            one_line="开场",
            goals_json="[]",
            status="needs_review",
            repair_attempt_count=2,
        )
        draft = ChapterDraft(
            id="d1",
            chapter_plan_id="cp1",
            version=1,
            body_text="正文" * 100,
            summary="summary",
            char_count=200,
        )
        review = ChapterReview(
            id="r1",
            draft_id="d1",
            verdict="warn",
            issues_json="[]",
            review_meta_json=json.dumps({"review_summary": "warn"}, ensure_ascii=False),
        )
        attempt = ChapterRewriteAttempt(
            id="a1",
            project_id="p",
            chapter_number=1,
            attempt_no=2,
            repair_phase="canon_repair",
            phase_attempt_no=1,
            trigger_review_id="r1",
            repair_scope="draft",
            design_patch_json="{}",
            source_draft_id="d1",
            result_draft_id="d1",
            result_verdict="warn",
            result_review_id="r1",
        )
        session.add_all([project, plan, draft, review, attempt])
        session.commit()
    finally:
        session.close()

    detail = get_chapter_review(
        "p",
        1,
        get_session=Session,
        decision_refs_for_chapter_review=_noop_decision_refs,
    )

    assert detail.rewrite_attempts[0].attempt_no == 2
    assert detail.rewrite_attempts[0].repair_phase == "canon_repair"
    assert detail.rewrite_attempts[0].phase_attempt_no == 1
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_rewrite_attempt_phase_fields_are_serialized_in_review_detail -q
```

Expected: FAIL with a constructor or attribute error for `repair_phase` or `phase_attempt_no`.

- [ ] **Step 3: Add model columns**

In `forwin/models/phase.py`, update `ChapterRewriteAttempt` after `attempt_no`:

```python
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    repair_phase: Mapped[str] = mapped_column(String, default="review_repair")
    phase_attempt_no: Mapped[int] = mapped_column(Integer, default=0)
```

Also update the table indexes:

```python
    __table_args__ = (
        Index(
            "ix_chapter_rewrite_attempts_project_chapter_attempt",
            "project_id",
            "chapter_number",
            "attempt_no",
        ),
        Index(
            "ix_chapter_rewrite_attempts_project_chapter_phase",
            "project_id",
            "chapter_number",
            "repair_phase",
            "phase_attempt_no",
        ),
    )
```

- [ ] **Step 4: Add the Alembic migration**

Create `forwin/migrations/versions/0019_chapter_rewrite_attempt_phase.py`:

```python
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019_chapter_rewrite_attempt_phase"
down_revision = "0018_publisher_bindings_covers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chapter_rewrite_attempts",
        sa.Column("repair_phase", sa.String(), nullable=False, server_default="review_repair"),
    )
    op.add_column(
        "chapter_rewrite_attempts",
        sa.Column("phase_attempt_no", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_chapter_rewrite_attempts_project_chapter_phase",
        "chapter_rewrite_attempts",
        ["project_id", "chapter_number", "repair_phase", "phase_attempt_no"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chapter_rewrite_attempts_project_chapter_phase",
        table_name="chapter_rewrite_attempts",
    )
    op.drop_column("chapter_rewrite_attempts", "phase_attempt_no")
    op.drop_column("chapter_rewrite_attempts", "repair_phase")
```

- [ ] **Step 5: Persist phase fields in the updater**

In `forwin/state/updater.py`, extend `save_chapter_rewrite_attempt()` parameters:

```python
        repair_phase: str = "review_repair",
        phase_attempt_no: int | None = None,
```

Place them after `attempt_no`. In the `ChapterRewriteAttempt(...)` constructor, add:

```python
            repair_phase=str(repair_phase or "review_repair"),
            phase_attempt_no=max(0, int(phase_attempt_no if phase_attempt_no is not None else attempt_no)),
```

Keep all current call sites valid by using the defaults.

- [ ] **Step 6: Add a phase-aware repository helper**

In `forwin/state/repo.py`, add this method below `list_chapter_rewrite_attempts()`:

```python
    def list_chapter_rewrite_attempts_for_phase(
        self,
        project_id: str,
        chapter_number: int,
        repair_phase: str,
    ) -> list[ChapterRewriteAttempt]:
        return self.session.execute(
            select(ChapterRewriteAttempt)
            .where(
                ChapterRewriteAttempt.project_id == project_id,
                ChapterRewriteAttempt.chapter_number == chapter_number,
                ChapterRewriteAttempt.repair_phase == str(repair_phase or "review_repair"),
            )
            .order_by(
                ChapterRewriteAttempt.phase_attempt_no.asc(),
                ChapterRewriteAttempt.created_at.asc(),
            )
        ).scalars().all()
```

- [ ] **Step 7: Add additive API fields**

In `forwin/api_schema/review.py`, update `ChapterRewriteAttemptInfo`:

```python
class ChapterRewriteAttemptInfo(BaseModel):
    attempt_no: int
    repair_phase: str = "review_repair"
    phase_attempt_no: int = 0
    repair_scope: str = ""
```

In `forwin/project_ops/reviews.py`, update `ChapterRewriteAttemptInfo(...)` construction:

```python
                    repair_phase=str(getattr(item, "repair_phase", "") or "review_repair"),
                    phase_attempt_no=int(getattr(item, "phase_attempt_no", 0) or 0),
```

Place these fields immediately after `attempt_no=...`.

- [ ] **Step 8: Run the focused test**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_rewrite_attempt_phase_fields_are_serialized_in_review_detail -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add forwin/models/phase.py forwin/state/updater.py forwin/state/repo.py forwin/api_schema/review.py forwin/project_ops/reviews.py forwin/migrations/versions/0019_chapter_rewrite_attempt_phase.py tests/test_canon_repair_stage.py
git commit -m "feat: track rewrite attempt repair phase"
```

---

### Task 2: Make the Existing Repair Loop Phase-Aware

**Files:**
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Test: `tests/test_canon_repair_stage.py`

- [ ] **Step 1: Write the failing phase-budget unit test**

Append this test to `tests/test_canon_repair_stage.py`. It proves that global history is preserved while `canon_repair` sees a fresh phase history.

```python
from forwin.orchestrator_loop_core.repair_loop import _attempts_for_repair_phase


class _Attempt:
    def __init__(self, repair_scope: str, repair_phase: str):
        self.repair_scope = repair_scope
        self.repair_phase = repair_phase


def test_attempts_for_repair_phase_filters_history_without_deleting_total_history():
    attempts = [
        _Attempt("draft", "review_repair"),
        _Attempt("draft", "review_repair"),
        _Attempt("chapter_plan", "review_repair"),
        _Attempt("draft", "canon_repair"),
    ]

    phase_attempts = _attempts_for_repair_phase(attempts, "canon_repair")

    assert len(attempts) == 4
    assert [item.repair_scope for item in phase_attempts] == ["draft"]
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_attempts_for_repair_phase_filters_history_without_deleting_total_history -q
```

Expected: FAIL with `ImportError` because `_attempts_for_repair_phase` does not exist.

- [ ] **Step 3: Add phase helper functions**

In `forwin/orchestrator_loop_core/repair_loop.py`, near the top after imports, add:

```python
REVIEW_REPAIR_PHASE = "review_repair"
CANON_REPAIR_PHASE = "canon_repair"


def _attempt_repair_phase(attempt: object) -> str:
    return str(getattr(attempt, "repair_phase", "") or REVIEW_REPAIR_PHASE)


def _attempts_for_repair_phase(
    attempts: list[object],
    repair_phase: str,
) -> list[object]:
    normalized_phase = str(repair_phase or REVIEW_REPAIR_PHASE)
    return [attempt for attempt in attempts if _attempt_repair_phase(attempt) == normalized_phase]
```

Add these names to `__all__` at the bottom:

```python
    "REVIEW_REPAIR_PHASE",
    "CANON_REPAIR_PHASE",
    "_attempts_for_repair_phase",
```

- [ ] **Step 4: Extract the while loop into `_run_repair_loop_for_phase()`**

In `forwin/orchestrator_loop_core/repair_loop.py`, create this method immediately after `_review_and_maybe_rewrite()`. Move the existing `while True:` body from `_review_and_maybe_rewrite()` into it. Preserve the existing local variable names.

Use this signature:

```python
def _run_repair_loop_for_phase(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    checker: ContinuityChecker,
    project_id: str,
    chapter_plan: ChapterPlan,
    current_context,
    current_output: WriterOutput,
    current_draft: ChapterDraft,
    current_review: ReviewVerdict,
    current_review_row: ChapterReview,
    current_writer_trace_id: str,
    current_review_trace_id: str,
    current_review_event,
    repair_phase: str,
) -> tuple[WriterOutput, ReviewVerdict, bool]:
```

At the top of each loop iteration, replace the existing attempt lookup block with:

```python
        existing_attempts = repo.list_chapter_rewrite_attempts(project_id, chapter_plan.chapter_number)
        phase_attempts = _attempts_for_repair_phase(existing_attempts, repair_phase)
        repair_v2_input = DecisionInput(
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=current_review,
            signals=[],
            open_obligations=[],
            operation_mode=self.config.operation_mode,
            attempts_completed=len(phase_attempts),
            prior_scope_history=[
                str(getattr(attempt, "repair_scope", "") or "")
                for attempt in phase_attempts
            ],
            budget=None,
            target_total_chapters=0,
            plan_layer_health=PlanLayerHealth(),
        )
```

Replace `attempt_no = len(existing_attempts) + 1` with:

```python
        attempt_no = len(existing_attempts) + 1
        phase_attempt_no = len(phase_attempts) + 1
```

Every call to `updater.save_chapter_rewrite_attempt(...)` inside the extracted loop must pass:

```python
                repair_phase=repair_phase,
                phase_attempt_no=phase_attempt_no,
```

Every assignment `chapter_plan.repair_attempt_count = attempt_no` stays unchanged so the existing field remains total attempts.

- [ ] **Step 5: Call the extracted method from `_review_and_maybe_rewrite()`**

In `_review_and_maybe_rewrite()`, replace the old `while True:` block with:

```python
    return self._run_repair_loop_for_phase(
        session=session,
        repo=repo,
        updater=updater,
        checker=checker,
        project_id=project_id,
        chapter_plan=chapter_plan,
        current_context=context,
        current_output=current_output,
        current_draft=current_draft,
        current_review=current_review,
        current_review_row=current_review_row,
        current_writer_trace_id=current_writer_trace_id,
        current_review_trace_id=current_review_trace_id,
        current_review_event=current_review_event,
        repair_phase=REVIEW_REPAIR_PHASE,
    )
```

- [ ] **Step 6: Run existing repair regressions**

Run:

```bash
python3 -m pytest tests/test_phase05_regressions.py::Phase05RegressionTest::test_blackbox_repair_exhaustion_pauses_for_manual_review -q
python3 -m pytest tests/test_phase05_regressions.py::Phase05RegressionTest::test_blackbox_force_accept_after_repair_exhaustion -q
python3 -m pytest tests/test_canon_repair_stage.py -q
```

Expected: all PASS. The existing tests must still see total attempt counts of 4 and 6.

- [ ] **Step 7: Commit Task 2**

```bash
git add forwin/orchestrator_loop_core/repair_loop.py tests/test_canon_repair_stage.py
git commit -m "refactor: make chapter repair loop phase aware"
```

---

### Task 3: Return Rich Canon Apply Outcomes

**Files:**
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_canon_repair_stage.py`

- [ ] **Step 1: Write the failing adapter test**

Append this test to `tests/test_canon_repair_stage.py`:

```python
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.orchestrator_loop_core.quality_gates import CanonApplyOutcome


def test_canon_apply_outcome_preserves_gate_result_and_block_path():
    gate = CanonAdmissionGateResult(
        project_id="p",
        chapter_number=2,
        draft_id="d1",
        review_id="r1",
        commit_allowed=False,
        verdict="fail",
        admission_mode="blocked",
        required_repair_scope="draft",
        gate_summary="canon quality gate strict: commit_allowed=False",
    )

    outcome = CanonApplyOutcome(
        blocked_path="frozen/path.json",
        block_kind="canon_quality",
        canon_gate_result=gate,
    )

    assert outcome.blocked
    assert outcome.repairable_scope == "draft"
    assert outcome.canon_gate_result is gate
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_canon_apply_outcome_preserves_gate_result_and_block_path -q
```

Expected: FAIL with `ImportError` because `CanonApplyOutcome` does not exist.

- [ ] **Step 3: Add canon outcome dataclasses**

In `forwin/orchestrator_loop_core/quality_gates.py`, add imports:

```python
from dataclasses import dataclass
```

Add these dataclasses near the top of the file after imports:

```python
@dataclass(frozen=True)
class CanonQualityGateOutcome:
    blocked_path: str = ""
    gate_result: CanonAdmissionGateResult | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_path)


@dataclass(frozen=True)
class CanonApplyOutcome:
    blocked_path: str = ""
    block_kind: str = ""
    canon_gate_result: CanonAdmissionGateResult | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_path or self.block_kind)

    @property
    def repairable_scope(self) -> str:
        if self.block_kind != "canon_quality" or self.canon_gate_result is None:
            return ""
        return str(self.canon_gate_result.required_repair_scope or "")
```

- [ ] **Step 4: Change `_apply_canon_quality_gate()` return type**

In `forwin/orchestrator_loop_core/quality_gates.py`, change `_apply_canon_quality_gate()` to return `CanonQualityGateOutcome`.

Replace:

```python
    if gate_result.commit_allowed:
        return ""
```

with:

```python
    if gate_result.commit_allowed:
        return CanonQualityGateOutcome(gate_result=gate_result)
```

Replace the final return:

```python
    return frozen_path or "canon-quality-gate-blocked"
```

with:

```python
    return CanonQualityGateOutcome(
        blocked_path=frozen_path or "canon-quality-gate-blocked",
        gate_result=gate_result,
    )
```

- [ ] **Step 5: Change `_apply_canon_candidate()` return type**

In `_apply_canon_candidate()`, change the return annotation to:

```python
) -> CanonApplyOutcome:
```

Replace:

```python
        quality_blocked_path = self._apply_canon_quality_gate(
            session=session,
            repo=repo,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
            verdict=verdict,
        )
        if quality_blocked_path:
            return quality_blocked_path
```

with:

```python
        quality_outcome = self._apply_canon_quality_gate(
            session=session,
            repo=repo,
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
            verdict=verdict,
        )
        if quality_outcome.blocked:
            return CanonApplyOutcome(
                blocked_path=quality_outcome.blocked_path,
                block_kind="canon_quality",
                canon_gate_result=quality_outcome.gate_result,
            )
```

For world v4 blocks, replace `return v4_blocked_path` with:

```python
            return CanonApplyOutcome(
                blocked_path=v4_blocked_path,
                block_kind="world_v4",
            )
```

At the successful end of `_apply_canon_candidate()`, replace `return None` with:

```python
        return CanonApplyOutcome()
```

For the existing exception handler that saves a frozen path and returns a path, return:

```python
        return CanonApplyOutcome(
            blocked_path=frozen_path or "canon-apply-error",
            block_kind="canon_apply_error",
        )
```

- [ ] **Step 6: Add a compatibility adapter in `project_chapters.py`**

In `forwin/orchestrator_loop_core/project_chapters.py`, add this helper near the top-level helper functions:

```python
def _coerce_canon_apply_outcome(value: object):
    from forwin.orchestrator_loop_core.quality_gates import CanonApplyOutcome

    if isinstance(value, CanonApplyOutcome):
        return value
    if value:
        return CanonApplyOutcome(blocked_path=str(value), block_kind="legacy_block")
    return CanonApplyOutcome()
```

Then replace:

```python
            frozen_path = self._apply_canon_candidate(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_num,
                writer_output=writer_output,
                verdict=verdict,
            )
            if frozen_path:
                frozen_artifacts.append(frozen_path)
```

with:

```python
            canon_outcome = _coerce_canon_apply_outcome(
                self._apply_canon_candidate(
                    session=session,
                    repo=repo,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    writer_output=writer_output,
                    verdict=verdict,
                )
            )
            if canon_outcome.blocked:
                frozen_path = canon_outcome.blocked_path
                if frozen_path:
                    frozen_artifacts.append(frozen_path)
```

Leave the existing `needs_review` behavior in place for this task. Task 5 changes it for repairable canon blocks.

- [ ] **Step 7: Run focused and existing tests**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_canon_apply_outcome_preserves_gate_result_and_block_path -q
python3 -m pytest tests/test_hard_floor.py -q
python3 -m pytest tests/test_phase05_regressions.py::Phase05RegressionTest::test_blackbox_repair_exhaustion_pauses_for_manual_review -q
```

Expected: all PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add forwin/orchestrator_loop_core/quality_gates.py forwin/orchestrator_loop_core/project_chapters.py tests/test_canon_repair_stage.py
git commit -m "refactor: return canon apply outcomes"
```

---

### Task 4: Build Canon Block Reviews and Scope Routing

**Files:**
- Modify: `forwin/review_engine/issue_taxonomy.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Test: `tests/test_canon_repair_stage.py`

- [ ] **Step 1: Write the failing synthetic review test**

Append this test to `tests/test_canon_repair_stage.py`:

```python
from forwin.orchestrator_loop_core.repair_loop import _review_from_canon_gate_block
from forwin.review_engine.rules.repair_v2 import decide_repair_v2
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def test_canon_gate_block_review_routes_to_required_draft_scope():
    gate = CanonAdmissionGateResult(
        project_id="p",
        chapter_number=2,
        draft_id="d1",
        review_id="r1",
        commit_allowed=False,
        verdict="fail",
        admission_mode="blocked",
        required_repair_scope="draft",
        gate_summary="canon quality gate strict: commit_allowed=False",
        deterministic_issue_refs=["signal-1"],
    )

    review = _review_from_canon_gate_block(gate)
    decision = decide_repair_v2(
        DecisionInput(
            project_id="p",
            chapter_number=2,
            review=review,
            signals=[],
            open_obligations=[],
            operation_mode="blackbox",
            attempts_completed=0,
            prior_scope_history=[],
            budget=None,
            target_total_chapters=0,
            plan_layer_health=PlanLayerHealth(),
        )
    )

    assert review.verdict == "fail"
    assert review.recommended_action == "rewrite"
    assert review.issues[0].issue_type == "canon_admission_draft_block"
    assert decision.outcome == "local_repair"
    assert decision.sub_action["scope"] == "draft"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_canon_gate_block_review_routes_to_required_draft_scope -q
```

Expected: FAIL with `ImportError` for `_review_from_canon_gate_block`.

- [ ] **Step 3: Add canon synthetic issue taxonomy**

In `forwin/review_engine/issue_taxonomy.py`, add these entries to `ISSUE_TO_SCOPE`:

```python
    "canon_admission_draft_block": "draft",
    "canon_admission_chapter_plan_block": "chapter_plan",
    "canon_admission_band_block": "band_plan",
    "canon_admission_arc_block": "arc_plan",
    "canon_admission_book_block": "book_plan",
```

- [ ] **Step 4: Add canon scope normalization and review builder**

In `forwin/orchestrator_loop_core/repair_loop.py`, add this helper near the phase helpers:

```python
_CANON_SCOPE_TO_REPAIR_SCOPE = {
    "draft": "draft",
    "chapter": "chapter_plan",
    "chapter_plan": "chapter_plan",
    "band": "band_plan",
    "band_plan": "band_plan",
    "arc": "arc_plan",
    "arc_plan": "arc_plan",
    "book": "book_plan",
    "book_plan": "book_plan",
}


def _canon_repair_scope(raw_scope: object) -> str:
    return _CANON_SCOPE_TO_REPAIR_SCOPE.get(str(raw_scope or "").strip(), "")


def _canon_issue_type_for_scope(repair_scope: str) -> str:
    return {
        "draft": "canon_admission_draft_block",
        "chapter_plan": "canon_admission_chapter_plan_block",
        "band_plan": "canon_admission_band_block",
        "arc_plan": "canon_admission_arc_block",
        "book_plan": "canon_admission_book_block",
    }.get(repair_scope, "")


def _review_from_canon_gate_block(gate_result) -> ReviewVerdict:
    repair_scope = _canon_repair_scope(getattr(gate_result, "required_repair_scope", ""))
    issue_type = _canon_issue_type_for_scope(repair_scope)
    issue = ContinuityIssue(
        rule_name="canon_admission_block",
        severity="error",
        description=str(getattr(gate_result, "gate_summary", "") or "canon admission blocked commit"),
        reviewer="canon_quality_gate",
        issue_type=issue_type or "canon_admission_unrouted_block",
        target_scope=repair_scope or "operator",
        evidence_refs=[
            str(ref)
            for ref in getattr(gate_result, "deterministic_issue_refs", []) or []
            if str(ref or "")
        ],
        source_layer="canon_admission",
        blocking_origin="canon_quality_gate",
        blocking=True,
        original_result=gate_result.model_dump(mode="json") if hasattr(gate_result, "model_dump") else {},
    )
    repair_instruction = None
    if repair_scope in {"draft", "chapter_plan", "band_plan"}:
        repair_instruction = RepairInstruction(
            repair_scope=repair_scope,  # type: ignore[arg-type]
            failure_type="mixed",
            must_fix=[issue.description],
            must_preserve=[],
            scope_reason="canon admission required repair",
            design_patch={
                "canon_required_repair_scope": repair_scope,
                "canon_gate_summary": str(getattr(gate_result, "gate_summary", "") or ""),
            },
            evidence_refs=list(issue.evidence_refs),
        )
    return ReviewVerdict(
        verdict="fail",
        issues=[issue],
        recommended_action="rewrite" if repair_scope else "pause_for_review",
        review_summary=str(getattr(gate_result, "gate_summary", "") or "canon admission blocked commit"),
        reviewer_mode="canon_repair",
        repair_instruction=repair_instruction,
        residual_review_issues=[issue],
    )
```

Add `_canon_repair_scope`, `_review_from_canon_gate_block`, and `CANON_REPAIR_PHASE` to `__all__`.

- [ ] **Step 5: Bind helpers on the orchestrator service**

In `forwin/orchestrator_loop_core/service.py`, add imports from `repair_loop` for:

```python
CANON_REPAIR_PHASE
_run_repair_loop_for_phase
_review_from_canon_gate_block
```

Then bind:

```python
WritingOrchestrator._run_repair_loop_for_phase = _run_repair_loop_for_phase
WritingOrchestrator._review_from_canon_gate_block = _review_from_canon_gate_block
```

Only bind `CANON_REPAIR_PHASE` if the service file already binds constants; otherwise import it in `project_chapters.py` directly.

- [ ] **Step 6: Run the synthetic review test**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_canon_gate_block_review_routes_to_required_draft_scope -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add forwin/review_engine/issue_taxonomy.py forwin/orchestrator_loop_core/repair_loop.py forwin/orchestrator_loop_core/service.py tests/test_canon_repair_stage.py
git commit -m "feat: route canon admission blocks to repair scopes"
```

---

### Task 5: Route Repairable Canon Blocks Into `canon_repair`

**Files:**
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Test: `tests/test_canon_repair_stage.py`

- [ ] **Step 1: Write the failing end-to-end regression**

Append this test to `tests/test_canon_repair_stage.py`. It drives the orchestrator through `review=warn`, a repairable canon block, one canon repair attempt, and final canon success.

```python
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.config import Config
from forwin.models.phase import ChapterPlan
from forwin.orchestrator_loop_core.quality_gates import CanonApplyOutcome
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from tests.postgres import postgres_test_url


def _writer_output(chapter_number: int, title: str, body: str = "正文") -> WriterOutput:
    return WriterOutput(
        chapter_number=chapter_number,
        title=title,
        body=body * 900,
        char_count=len(body * 900),
        end_of_chapter_summary="summary",
        state_changes=[],
        new_events=[],
        thread_beats=[],
        time_advance=None,
    )


class _WarnThenPassReviewHub:
    def __init__(self):
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ReviewVerdict(
                verdict="warn",
                recommended_action="pause_for_review",
                review_summary="soft warning",
            )
        return ReviewVerdict(
            verdict="pass",
            recommended_action="continue",
            review_summary="repaired",
        )


def test_warn_review_canon_block_runs_canon_repair_before_accepting(tmp_path):
    from forwin.models.phase import ChapterRewriteAttempt

    orchestrator = WritingOrchestrator(
        Config(
            database_url=postgres_test_url("canon-repair-stage-success"),
            artifact_root=str(tmp_path / "artifacts"),
            minimax_api_key="",
            minimax_model="fake-model",
            chapter_review_form_mode="off",
            operation_mode="blackbox",
        )
    )
    try:
        orchestrator.config.operation_mode = "blackbox"
        orchestrator.config.freeze_failed_candidates = False
        orchestrator.config.hard_floor_gate_enabled = False
        orchestrator.review_hub = _WarnThenPassReviewHub()
        orchestrator.writer.write_chapter = lambda context: _writer_output(context.chapter_number, "第一章")
        orchestrator._write_chapter_with_attention_fallback = lambda **kwargs: _writer_output(
            kwargs["chapter_number"],
            "第一章 修复",
        )
        orchestrator._apply_repair_patch = lambda **kwargs: ({}, kwargs["context"], {}, {}, "")

        gate = CanonAdmissionGateResult(
            project_id="p",
            chapter_number=1,
            draft_id="d1",
            review_id="r1",
            commit_allowed=False,
            verdict="fail",
            admission_mode="blocked",
            required_repair_scope="draft",
            gate_summary="canon quality gate strict: commit_allowed=False",
            deterministic_issue_refs=["signal-1"],
        )
        apply_calls = {"count": 0}

        def fake_apply_canon_candidate(**kwargs):
            apply_calls["count"] += 1
            if apply_calls["count"] == 1:
                return CanonApplyOutcome(
                    blocked_path="canon-quality-gate-blocked",
                    block_kind="canon_quality",
                    canon_gate_result=gate,
                )
            return CanonApplyOutcome()

        orchestrator._apply_canon_candidate = fake_apply_canon_candidate
        orchestrator.story_planner.create_book_plan = lambda **kwargs: {
            "title": "测试书",
            "premise": "premise",
            "chapter_count": 1,
            "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
            "characters": [],
            "locations": [],
            "factions": [],
            "relations": [],
            "plot_threads": [],
            "initial_time": {"label": "开始", "description": "开始"},
        }

        result = orchestrator.run("p", "g", 1)

        session = orchestrator.SessionLocal()
        try:
            attempts = session.execute(select(ChapterRewriteAttempt)).scalars().all()
        finally:
            session.close()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert result.status in {"paused", "completed"}
    assert apply_calls["count"] == 2
    assert len(attempts) == 1
    assert attempts[0].repair_phase == "canon_repair"
    assert attempts[0].attempt_no == 1
    assert attempts[0].phase_attempt_no == 1
    assert attempts[0].repair_scope == "draft"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_warn_review_canon_block_runs_canon_repair_before_accepting -q
```

Expected: FAIL because the current outer loop marks `needs_review` after the first canon block and records zero attempts.

- [ ] **Step 3: Add `_run_canon_repair_for_block()`**

In `forwin/orchestrator_loop_core/repair_loop.py`, add:

```python
def _run_canon_repair_for_block(
    self,
    *,
    session: Session,
    repo: StateRepository,
    updater: StateUpdater,
    checker: ContinuityChecker,
    project_id: str,
    chapter_plan: ChapterPlan,
    context,
    writer_output: WriterOutput,
    gate_result,
) -> tuple[WriterOutput, ReviewVerdict, bool]:
    latest_draft, _latest_review = self._latest_draft_and_review_for_chapter(
        session=session,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
    )
    if latest_draft is None:
        synthetic_review = _review_from_canon_gate_block(gate_result)
        writer_output, latest_draft, synthetic_review_row = self._persist_draft_and_review(
            session=session,
            updater=updater,
            chapter_plan=chapter_plan,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=writer_output,
            review=synthetic_review,
        )
    else:
        synthetic_review = _review_from_canon_gate_block(gate_result)
        synthetic_review_row = updater.save_review(latest_draft.id, synthetic_review)

    current_review_event = self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_plan.chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
        scope="chapter",
        summary=f"第{chapter_plan.chapter_number}章 canon gate promoted review to fail。",
        related_object_type="chapter_review",
        related_object_id=synthetic_review_row.id,
        payload=self._review_event_payload(synthetic_review),
    )
    return self._run_repair_loop_for_phase(
        session=session,
        repo=repo,
        updater=updater,
        checker=checker,
        project_id=project_id,
        chapter_plan=chapter_plan,
        current_context=context,
        current_output=writer_output,
        current_draft=latest_draft,
        current_review=synthetic_review,
        current_review_row=synthetic_review_row,
        current_writer_trace_id="",
        current_review_trace_id="",
        current_review_event=current_review_event,
        repair_phase=CANON_REPAIR_PHASE,
    )
```

Add `_run_canon_repair_for_block` to `__all__` and bind it in `forwin/orchestrator_loop_core/service.py`:

```python
WritingOrchestrator._run_canon_repair_for_block = _run_canon_repair_for_block
```

- [ ] **Step 4: Route repairable canon outcomes in `project_chapters.py`**

In `project_chapters.py`, wrap the canon application block that starts at the
`applying_canon` progress event in a local retry loop. The structure should be:

```python
            while True:
                self._emit_progress(
                    "stage_changed",
                    stage="applying_canon",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                canon_outcome = _coerce_canon_apply_outcome(
                    self._apply_canon_candidate(
                        session=session,
                        repo=repo,
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        writer_output=writer_output,
                        verdict=verdict,
                    )
                )
                if not canon_outcome.blocked:
                    break

                frozen_path = canon_outcome.blocked_path
                if frozen_path:
                    frozen_artifacts.append(frozen_path)
                repair_scope = ""
                if canon_outcome.block_kind == "canon_quality" and canon_outcome.canon_gate_result is not None:
                    repair_scope = _canon_repair_scope(
                        canon_outcome.canon_gate_result.required_repair_scope
                    )
                if repair_scope and self.config.operation_mode == "blackbox":
                    writer_output, verdict, force_accept_applied = self._run_canon_repair_for_block(
                        session=session,
                        repo=repo,
                        updater=updater,
                        checker=checker,
                        project_id=project_id,
                        chapter_plan=chapter_plan,
                        context=context,
                        writer_output=writer_output,
                        gate_result=canon_outcome.canon_gate_result,
                    )
                    repair_attempt_count = len(
                        repo.list_chapter_rewrite_attempts(project_id, chapter_num)
                    )
                    residual_review_issues = self._review_issue_payloads(verdict)
                    canon_risk_level = self._review_canon_risk(verdict)
                    session.commit()
                    continue

                break
```

After this loop, keep the existing `if canon_outcome.blocked:` status handling. That remaining branch now covers non-repairable canon blocks and legacy block outcomes only.

- [ ] **Step 5: Run the success regression**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py::test_warn_review_canon_block_runs_canon_repair_before_accepting -q
```

Expected: PASS. The test must show exactly one `canon_repair` attempt and two canon apply calls.

- [ ] **Step 6: Commit Task 5**

```bash
git add forwin/orchestrator_loop_core/repair_loop.py forwin/orchestrator_loop_core/service.py forwin/orchestrator_loop_core/project_chapters.py tests/test_canon_repair_stage.py
git commit -m "feat: run canon repair before review pause"
```

---

### Task 6: Add Budget Reset, Exhaustion, and System Block Regressions

**Files:**
- Modify: `tests/test_canon_repair_stage.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Modify: `forwin/api_core/tasks.py`
- Modify: `forwin/api_core/runtime.py`

- [ ] **Step 1: Add budget reset regression**

Append this test. It asserts previous `review_repair` attempts do not consume `canon_repair` phase budget.

```python
def test_canon_repair_budget_ignores_prior_review_repair_attempts():
    attempts = [
        _Attempt("draft", "review_repair"),
        _Attempt("draft", "review_repair"),
        _Attempt("chapter_plan", "review_repair"),
        _Attempt("band_plan", "review_repair"),
    ]

    canon_attempts = _attempts_for_repair_phase(attempts, "canon_repair")
    review_attempts = _attempts_for_repair_phase(attempts, "review_repair")

    assert len(review_attempts) == 4
    assert canon_attempts == []
```

- [ ] **Step 2: Add canon repair exhaustion regression**

Append this test. Also add `DecisionEvent` to the imports from `forwin.models.governance` if the file does not already import it.

```python
class _WarnThenFailReviewHub:
    def __init__(self):
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ReviewVerdict(
                verdict="warn",
                recommended_action="pause_for_review",
                review_summary="soft warning",
            )
        return ReviewVerdict(
            verdict="fail",
            recommended_action="rewrite",
            review_summary="still blocked",
        )


def test_canon_repair_exhaustion_pauses_after_phase_budget(tmp_path):
    from forwin.models.phase import ChapterRewriteAttempt

    orchestrator = WritingOrchestrator(
        Config(
            database_url=postgres_test_url("canon-repair-stage-exhaustion"),
            artifact_root=str(tmp_path / "artifacts"),
            minimax_api_key="",
            minimax_model="fake-model",
            chapter_review_form_mode="off",
            operation_mode="blackbox",
        )
    )
    try:
        orchestrator.config.freeze_failed_candidates = False
        orchestrator.config.hard_floor_gate_enabled = False
        orchestrator.review_hub = _WarnThenFailReviewHub()
        orchestrator.writer.write_chapter = lambda context: _writer_output(context.chapter_number, "第一章")
        orchestrator._write_chapter_with_attention_fallback = lambda **kwargs: _writer_output(
            kwargs["chapter_number"],
            "第一章 修复",
        )
        orchestrator._apply_repair_patch = lambda **kwargs: ({}, kwargs["context"], {}, {}, "")
        orchestrator.story_planner.create_book_plan = lambda **kwargs: {
            "title": "测试书",
            "premise": "premise",
            "chapter_count": 1,
            "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
            "characters": [],
            "locations": [],
            "factions": [],
            "relations": [],
            "plot_threads": [],
            "initial_time": {"label": "开始", "description": "开始"},
        }
        gate = CanonAdmissionGateResult(
            project_id="p",
            chapter_number=1,
            draft_id="d1",
            review_id="r1",
            commit_allowed=False,
            verdict="fail",
            admission_mode="blocked",
            required_repair_scope="draft",
            gate_summary="canon quality gate strict: commit_allowed=False",
            deterministic_issue_refs=["signal-1"],
        )
        orchestrator._apply_canon_candidate = lambda **kwargs: CanonApplyOutcome(
            blocked_path="canon-quality-gate-blocked",
            block_kind="canon_quality",
            canon_gate_result=gate,
        )

        result = orchestrator.run("p", "g", 1)

        session = orchestrator.SessionLocal()
        try:
            attempts = session.execute(
                select(ChapterRewriteAttempt).order_by(ChapterRewriteAttempt.attempt_no.asc())
            ).scalars().all()
            plan = session.execute(select(ChapterPlan)).scalar_one()
        finally:
            session.close()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert result.status == "needs_review"
    assert len(attempts) >= 2
    assert all(item.repair_phase == "canon_repair" for item in attempts)
    assert attempts[0].phase_attempt_no == 1
    assert attempts[-1].phase_attempt_no == len(attempts)
    assert plan.status == "needs_review"
    assert plan.canon_risk_level == "high"
```

- [ ] **Step 3: Add non-repairable system block regression**

Append this test. It uses a canon quality block with `required_repair_scope=None`, so the orchestrator must not run `canon_repair`.

```python
def test_non_repairable_canon_block_records_system_block_without_repair(tmp_path):
    from forwin.models.governance import DecisionEvent
    from forwin.models.phase import ChapterRewriteAttempt

    orchestrator = WritingOrchestrator(
        Config(
            database_url=postgres_test_url("canon-repair-stage-system-block"),
            artifact_root=str(tmp_path / "artifacts"),
            minimax_api_key="",
            minimax_model="fake-model",
            chapter_review_form_mode="off",
            operation_mode="blackbox",
        )
    )
    try:
        orchestrator.config.freeze_failed_candidates = False
        orchestrator.config.hard_floor_gate_enabled = False
        orchestrator.review_hub = _WarnThenPassReviewHub()
        orchestrator.writer.write_chapter = lambda context: _writer_output(context.chapter_number, "第一章")
        orchestrator.story_planner.create_book_plan = lambda **kwargs: {
            "title": "测试书",
            "premise": "premise",
            "chapter_count": 1,
            "chapters": [{"chapter_number": 1, "title": "第一章", "one_line": "开场", "goals": ["推进主线"]}],
            "characters": [],
            "locations": [],
            "factions": [],
            "relations": [],
            "plot_threads": [],
            "initial_time": {"label": "开始", "description": "开始"},
        }
        gate = CanonAdmissionGateResult(
            project_id="p",
            chapter_number=1,
            draft_id="d1",
            review_id="r1",
            commit_allowed=False,
            verdict="fail",
            admission_mode="blocked",
            required_repair_scope=None,
            gate_summary="canon quality gate strict: commit_allowed=False; no automatic route",
        )
        orchestrator._apply_canon_candidate = lambda **kwargs: CanonApplyOutcome(
            blocked_path="canon-quality-gate-blocked",
            block_kind="canon_quality",
            canon_gate_result=gate,
        )

        result = orchestrator.run("p", "g", 1)

        session = orchestrator.SessionLocal()
        try:
            attempts = session.execute(select(ChapterRewriteAttempt)).scalars().all()
            plan = session.execute(select(ChapterPlan)).scalar_one()
            events = session.execute(select(DecisionEvent)).scalars().all()
        finally:
            session.close()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert result.status in {"needs_review", "paused"}
    assert len(attempts) == 0
    assert plan.status == "needs_review"
    assert plan.repair_attempt_count == 0
    assert any("canon quality gate" in str(event.reason or event.summary or "") for event in events)
```

- [ ] **Step 4: Run the new regressions and verify failures**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py -q
```

Expected before implementation polishing: the budget helper test passes; exhaustion or system-block tests may fail on status/message details.

- [ ] **Step 5: Fix status metadata for exhaustion**

When canon repair returns a final `ReviewVerdict` with `repair_exhausted=True`, ensure `project_chapters.py` marks:

```python
updater.mark_chapter_status(
    project_id,
    chapter_num,
    "needs_review",
    repair_attempt_count=repair_attempt_count,
    residual_review_issues=residual_review_issues,
    canon_risk_level="high",
)
```

After the status update, persist the final review meta so the API exposes exhaustion:

```python
current_review_row.review_meta_json = self._review_meta_json(current_review)
session.add(current_review_row)
```

- [ ] **Step 6: Fix task/status wording for non-repairable canon blocks**

The existing chapter status set does not include `system_block`, so keep `needs_review` as the chapter status for compatibility. Add a decision event payload with:

```python
{
    "outcome": "system_block",
    "reason": canon_outcome.canon_gate_result.gate_summary,
    "required_repair_scope": canon_outcome.repairable_scope,
}
```

In the task message code path that currently constructs `"需自动修复或重试章节"`, change zero-attempt non-repairable canon blocks to say:

```python
message = f"章节 {chapter_num} 遇到 canon system block，需处理系统阻断后重试"
```

Keep the old message for true repair exhaustion.

- [ ] **Step 7: Run all canon repair tests**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

```bash
git add tests/test_canon_repair_stage.py forwin/orchestrator_loop_core/project_chapters.py forwin/orchestrator_loop_core/repair_loop.py forwin/api_core/tasks.py forwin/api_core/runtime.py
git commit -m "test: cover canon repair budget and blocking outcomes"
```

---

### Task 7: Full Verification and Fifteen-Chapter Regression Check

**Files:**
- No planned source edits unless verification reveals a bug.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
python3 -m pytest tests/test_canon_repair_stage.py -q
python3 -m pytest tests/test_phase05_regressions.py::Phase05RegressionTest::test_blackbox_repair_exhaustion_pauses_for_manual_review -q
python3 -m pytest tests/test_phase05_regressions.py::Phase05RegressionTest::test_blackbox_force_accept_after_repair_exhaustion -q
python3 -m pytest tests/test_hard_floor.py -q
python3 -m pytest tests/test_canon_admission_gate.py -q
```

Expected: all PASS.

- [ ] **Step 2: Run project checks**

Run:

```bash
python3 -m pytest
python3 -m ruff check . || true
python3 -m mypy forwin || true
```

Expected: pytest PASS. Ruff/mypy may report existing issues because the project convention allows them to be non-blocking with `|| true`; record any new-looking errors in the final summary.

- [ ] **Step 3: Re-run the blocked fifteen-chapter scenario**

Create a fresh 15-chapter blackbox project for this regression check so the old project `8ac86975f5a345abb9c781e7246b48b9` remains available as pre-fix evidence.

Save the fresh project id in `PROJECT_ID`, then check the chapter 2 review endpoint:

```bash
curl -fsS "http://127.0.0.1:8899/api/projects/${PROJECT_ID}/chapters/2/review" | python3 -m json.tool
```

Expected after a retry on the same failure pattern:

```text
repair_attempt_count > 0
rewrite_attempts contains at least one item with repair_phase = canon_repair
phase_attempt_no starts at 1
needs_review occurs only after repair_exhausted=true, or the chapter proceeds if canon repair passes
```

- [ ] **Step 4: Inspect git status**

Run:

```bash
git status --short --branch
```

Expected: clean working tree after all implementation commits.

- [ ] **Step 5: Final implementation summary**

Report:

```text
Implemented canon_repair phase in /home/kikuhiko/ForWin.
Key behavior: warn/pass review can be upgraded by canon admission failure into automatic repair.
Budget behavior: canon_repair has fresh phase budget; historical attempts remain.
Verification: list exact pytest/ruff/mypy commands and outcomes.
```
