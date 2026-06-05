from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.postgres import postgres_test_url
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.config import Config
from forwin.models import base as base_module
from forwin.models.base import Base, get_engine, get_session_factory
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.orchestrator_loop_core import quality_gates as quality_gates_module
from forwin.orchestrator_loop_core import repair_loop as repair_loop_module
from forwin.orchestrator_loop_core.quality_gates import (
    CanonApplyOutcome,
    CanonQualityGateOutcome,
)
from forwin.orchestrator_loop_core.repair_loop import (
    _attempts_for_repair_phase,
    _review_from_canon_gate_block,
)
from forwin.project_ops.reviews import get_chapter_review
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review_engine.rules.repair_v2 import decide_repair_v2
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _noop_decision_refs(*args, **kwargs):
    return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, object] | None] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params)


class _FakeEngine:
    def __init__(self, conn: _RecordingConnection) -> None:
        self.conn = conn

    def begin(self):
        return self

    def __enter__(self) -> _RecordingConnection:
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _RecordingAlembicOp:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.add_column_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.create_index_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def add_column(self, *args, **kwargs) -> None:
        self.add_column_calls.append((args, kwargs))

    def create_index(self, *args, **kwargs) -> None:
        self.create_index_calls.append((args, kwargs))

    def execute(self, statement) -> None:
        self.statements.append(str(statement))


def _rewrite_attempt_phase_migration():
    return importlib.import_module(
        "forwin.migrations.versions.0019_chapter_rewrite_attempt_phase"
    )


def test_rewrite_attempt_phase_fields_are_serialized_in_review_detail():
    Session = _session_factory()
    session = Session()
    try:
        project = Project(id="p", title="测试项目", genre="玄幻", premise="premise")
        arc = ArcPlanVersion(id="arc1", project_id="p", arc_synopsis="arc")
        plan = ChapterPlan(
            id="cp1",
            project_id="p",
            arc_plan_id="arc1",
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
        session.add_all([project, arc, plan, draft, review, attempt])
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


def test_runtime_upgrade_helper_backfills_rewrite_attempt_phase_columns():
    conn = _RecordingConnection()

    base_module._upgrade_chapter_rewrite_attempt_phase(conn)

    statements = "\n".join(conn.statements)
    assert (
        "ALTER TABLE chapter_rewrite_attempts "
        "ADD COLUMN IF NOT EXISTS repair_phase VARCHAR NOT NULL DEFAULT 'review_repair'"
    ) in statements
    assert (
        "ALTER TABLE chapter_rewrite_attempts "
        "ADD COLUMN IF NOT EXISTS phase_attempt_no INTEGER NOT NULL DEFAULT 0"
    ) in statements
    assert "UPDATE chapter_rewrite_attempts" in statements
    assert "SET phase_attempt_no = attempt_no" in statements
    assert "WHERE phase_attempt_no = 0" in statements
    assert (
        "CREATE INDEX IF NOT EXISTS ix_chapter_rewrite_attempts_project_chapter_phase"
    ) in statements
    assert "chapter_rewrite_attempt_phase_v1" in base_module.POSTGRES_BASELINE_MIGRATIONS


def test_runtime_postgresql_upgrade_invokes_rewrite_attempt_phase_helper(monkeypatch):
    called = False

    def _record_call(conn):
        nonlocal called
        called = True

    monkeypatch.setattr(base_module, "_upgrade_chapter_rewrite_attempt_phase", _record_call)

    base_module._upgrade_postgresql_database(_FakeEngine(_RecordingConnection()))

    assert called


def test_alembic_revision_fits_version_table():
    migration = _rewrite_attempt_phase_migration()

    assert migration.revision == "0019_rewrite_attempt_phase"
    assert len(migration.revision) <= 32


def test_alembic_upgrade_uses_idempotent_sql_and_backfills_phase_attempt(monkeypatch):
    migration = _rewrite_attempt_phase_migration()
    fake_op = _RecordingAlembicOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    statements = "\n".join(fake_op.statements)
    assert "ADD COLUMN IF NOT EXISTS repair_phase" in statements
    assert "ADD COLUMN IF NOT EXISTS phase_attempt_no" in statements
    assert "CREATE INDEX IF NOT EXISTS ix_chapter_rewrite_attempts_project_chapter_phase" in statements
    assert (
        "UPDATE chapter_rewrite_attempts SET phase_attempt_no = attempt_no "
        "WHERE phase_attempt_no = 0"
    ) in statements
    assert fake_op.add_column_calls == []
    assert fake_op.create_index_calls == []


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


def test_force_accept_flags_latest_attempt_in_active_repair_phase(monkeypatch):
    review_attempt = SimpleNamespace(
        repair_scope="draft",
        repair_phase="review_repair",
        forced_accept_applied=False,
    )
    canon_attempt = SimpleNamespace(
        repair_scope="draft",
        repair_phase="canon_repair",
        forced_accept_applied=False,
    )

    class _Repo:
        def list_chapter_rewrite_attempts(self, _project_id, _chapter_number):
            return [review_attempt, canon_attempt]

    class _Session:
        def add(self, _row) -> None:
            return None

    class _Orchestrator:
        config = SimpleNamespace(operation_mode="blackbox")

        def _pause_requested(self) -> bool:
            return False

        def _record_engine_decision_event(self, **_kwargs) -> None:
            return None

        def _record_decision_event(self, **_kwargs):
            return SimpleNamespace(id="force-event")

        def _review_meta_json(self, review: ReviewVerdict) -> str:
            return json.dumps(
                review.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
            )

    monkeypatch.setattr(
        repair_loop_module,
        "decide_repair_v2",
        lambda _input: Decision(
            outcome="manual_review",
            reason="repair budget exhausted",
            rule_id="test-repair-exhausted",
            missing_evidence=[],
            routed_from="test",
            sub_action={},
        ),
    )

    class _FinalGateEngine:
        def __init__(self, _rules) -> None:
            return None

        def decide(self, _input) -> Decision:
            return Decision(
                outcome="manual_review",
                reason="force accept",
                rule_id="test-force-accept",
                missing_evidence=[],
                routed_from="test",
                sub_action={
                    "final_gate_decision": "force_accept",
                    "forceable": True,
                    "canon_risk": "low",
                    "residual_issues": [],
                    "requires_human": False,
                },
            )

    monkeypatch.setattr(repair_loop_module, "AutoDecisionEngine", _FinalGateEngine)

    _output, _review, forced_accept = repair_loop_module._run_repair_loop_for_phase(
        _Orchestrator(),
        session=_Session(),
        repo=_Repo(),
        updater=object(),
        checker=object(),
        project_id="p",
        chapter_plan=SimpleNamespace(chapter_number=1),
        current_context=object(),
        current_output=object(),
        current_draft=object(),
        current_review=ReviewVerdict(verdict="fail", issues=[]),
        current_review_row=SimpleNamespace(id="review-1", review_meta_json="{}"),
        current_writer_trace_id="writer-trace",
        current_review_trace_id="review-trace",
        current_review_event=SimpleNamespace(id="review-event"),
        repair_phase="review_repair",
    )

    assert forced_accept is True
    assert review_attempt.forced_accept_applied is True
    assert canon_attempt.forced_accept_applied is False


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
    assert outcome.blocked_path == "frozen/path.json"
    assert outcome.block_kind == "canon_quality"
    assert outcome.repairable_scope == "draft"
    assert outcome.canon_gate_result is gate


def test_canon_quality_gate_deferred_acceptance_short_circuits_before_admission_side_effects(
    monkeypatch,
):
    gate = CanonAdmissionGateResult(
        project_id="p",
        chapter_number=2,
        draft_id="d1",
        review_id="r1",
        commit_allowed=True,
        verdict="warn",
        admission_mode="with_obligation",
        gate_summary="canon quality gate strict: commit_allowed=True",
    )
    calls: list[str] = []

    monkeypatch.setattr(
        quality_gates_module,
        "analyze_writer_output_quality",
        lambda **_kwargs: calls.append("analysis")
        or SimpleNamespace(signals=[], raw_analyzer_results=[]),
    )

    def _evaluate_canon_admission(**_kwargs):
        calls.append("admission")
        return gate

    monkeypatch.setattr(quality_gates_module, "evaluate_canon_admission", _evaluate_canon_admission)

    class _ObligationRepo:
        def __init__(self, _session) -> None:
            calls.append("obligation_repo")
            return None

        def list_active_for_context(self, *_args, **_kwargs):
            return []

        def list_planned_for_chapter(self, *_args, **_kwargs):
            return []

        def list_patches_by_ids(self, _ids):
            return []

    class _CanonQualityRepo:
        def __init__(self, _session) -> None:
            calls.append("canon_quality_repo")
            return None

        def save_admission_run(self, gate_result, *, signals) -> None:
            calls.append("save_admission")

    class _Session:
        def get(self, _model, _id):
            return SimpleNamespace(target_total_chapters=0)

    class _Orchestrator:
        config = SimpleNamespace(
            canon_quality_gate="strict",
            chapter_review_form_mode="off",
            chapter_review_form_min_blocking_confidence=0.8,
        )
        llm_client = None

        def _latest_draft_and_review_for_chapter(self, **_kwargs):
            return SimpleNamespace(id="d1"), SimpleNamespace(id="r1")

        def _prepare_deferred_acceptance_if_needed(self, **_kwargs):
            calls.append("deferred_acceptance")
            return ["deferred patch failed"]

        def _record_decision_event(self, **kwargs) -> None:
            calls.append(f"event:{kwargs['event_type']}")
            return None

    monkeypatch.setattr(
        quality_gates_module,
        "NarrativeObligationRepository",
        _ObligationRepo,
    )
    monkeypatch.setattr(
        quality_gates_module,
        "CanonQualityRepository",
        _CanonQualityRepo,
    )

    outcome = quality_gates_module._apply_canon_quality_gate(
        _Orchestrator(),
        session=_Session(),
        repo=object(),
        updater=object(),
        project_id="p",
        chapter_number=2,
        writer_output=object(),
        verdict=SimpleNamespace(verdict="warn"),
    )

    assert isinstance(outcome, CanonQualityGateOutcome)
    assert outcome.blocked_path == "deferred-acceptance-blocked"
    assert outcome.gate_result is None
    assert calls == [
        "analysis",
        "deferred_acceptance",
        "event:canon_commit_blocked",
    ]


def test_apply_canon_candidate_exception_without_freeze_returns_unblocked_outcome(
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
    assert not outcome.blocked
    assert outcome.blocked_path == ""
    assert outcome.block_kind == ""
    assert session.rolled_back is True
    assert failed_rows[0]["canon_artifact_path"] == ""


def test_coerce_canon_apply_outcome_rejects_truthy_non_string_values():
    from forwin.orchestrator_loop_core.project_chapters import (
        _coerce_canon_apply_outcome,
    )

    existing = CanonApplyOutcome(blocked_path="path", block_kind="legacy_block")

    assert _coerce_canon_apply_outcome(existing) is existing
    assert not _coerce_canon_apply_outcome(None).blocked
    assert not _coerce_canon_apply_outcome("").blocked
    legacy = _coerce_canon_apply_outcome("legacy/path.json")
    assert legacy.blocked
    assert legacy.blocked_path == "legacy/path.json"
    assert legacy.block_kind == "legacy_block"
    with pytest.raises(TypeError):
        _coerce_canon_apply_outcome({"blocked_path": "legacy/path.json"})


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


def test_warn_review_canon_block_runs_canon_repair_before_accepting():
    class WarnThenPassReviewHub:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, **_kwargs) -> ReviewVerdict:
            self.calls += 1
            if self.calls == 1:
                return ReviewVerdict(
                    verdict="warn",
                    issues=[],
                    review_summary="soft review warning",
                )
            return ReviewVerdict(verdict="pass", issues=[])

    db_path = postgres_test_url("canon-repair-admission")
    orchestrator = WritingOrchestrator(
        Config(
            database_url=db_path,
            minimax_api_key="",
            minimax_model="fake-model",
            chapter_review_form_mode="off",
            operation_mode="blackbox",
            review_fail_max_rewrites=1,
            auto_band_checkpoint=False,
            manual_checkpoints_enabled=False,
        )
    )
    apply_calls = {"count": 0}
    try:
        orchestrator.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: {
            "arc_synopsis": "canon repair admission",
            "setting_summary": "无",
            "chapters": [
                {
                    "chapter_number": 1,
                    "title": "第一章",
                    "one_line": "开场",
                    "goals": ["推进主线"],
                }
            ],
            "characters": [],
            "locations": [],
            "factions": [],
            "relations": [],
            "plot_threads": [],
            "initial_time": {"label": "开始", "description": "开始"},
        }
        orchestrator.writer.write_chapter = lambda context: WriterOutput(
            chapter_number=context.chapter_number,
            title=f"第{context.chapter_number}章",
            body="正文" * 900,
            char_count=1800,
            end_of_chapter_summary="ok",
            state_changes=[],
            new_events=[],
            thread_beats=[],
            time_advance=None,
        )
        orchestrator.review_hub = WarnThenPassReviewHub()

        def apply_canon_candidate(**_kwargs):
            apply_calls["count"] += 1
            if apply_calls["count"] == 1:
                return CanonApplyOutcome(
                    blocked_path="frozen/canon-quality.json",
                    block_kind="canon_quality",
                    canon_gate_result=CanonAdmissionGateResult(
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
                    ),
                )
            return CanonApplyOutcome()

        orchestrator._apply_canon_candidate = apply_canon_candidate

        result = orchestrator.run("p", "g", 1)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            attempts = session.execute(
                select(ChapterRewriteAttempt).order_by(ChapterRewriteAttempt.attempt_no)
            ).scalars().all()
            plan = session.execute(select(ChapterPlan)).scalar_one()
        finally:
            session.close()
            engine.dispose()
    finally:
        orchestrator.llm_client.close()
        orchestrator.engine.dispose()

    assert result.status == "completed"
    assert apply_calls["count"] == 2
    assert len(attempts) == 1
    assert attempts[0].repair_phase == "canon_repair"
    assert attempts[0].attempt_no == 1
    assert attempts[0].phase_attempt_no == 1
    assert attempts[0].repair_scope == "draft"
    assert plan.status == "accepted"
    assert plan.repair_attempt_count == 1


def _canon_repair_decision_for_scope(raw_scope: object) -> tuple[ReviewVerdict, Decision]:
    gate = SimpleNamespace(
        required_repair_scope=raw_scope,
        gate_summary=f"canon quality gate strict: required_repair_scope={raw_scope}",
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
    return review, decision


@pytest.mark.parametrize(
    ("raw_scope", "expected_issue_type", "expected_outcome", "expected_scope"),
    [
        ("chapter_plan", "canon_admission_chapter_plan_block", "chapter_patch", "chapter_plan"),
        (" BAND ", "canon_admission_band_block", "band_patch", "band_plan"),
        ("arc", "canon_admission_arc_block", "arc_patch", "arc_plan"),
        ("book", "canon_admission_book_block", "book_patch", "book_plan"),
    ],
)
def test_canon_gate_block_review_routes_known_required_scopes(
    raw_scope,
    expected_issue_type,
    expected_outcome,
    expected_scope,
):
    review, decision = _canon_repair_decision_for_scope(raw_scope)

    assert review.verdict == "fail"
    assert review.recommended_action == "rewrite"
    assert review.issues[0].issue_type == expected_issue_type
    assert review.issues[0].target_scope == expected_scope
    assert decision.outcome == expected_outcome
    assert decision.sub_action["scope"] == expected_scope


@pytest.mark.parametrize("raw_scope", [None, "scene_plan"])
def test_canon_gate_block_review_unrouted_scope_requires_operator_review(raw_scope):
    review, decision = _canon_repair_decision_for_scope(raw_scope)

    assert review.verdict == "fail"
    assert review.recommended_action == "pause_for_review"
    assert review.issues[0].issue_type == "canon_admission_unrouted_block"
    assert review.issues[0].target_scope == "operator"
    assert decision.outcome == "manual_review"
    assert decision.sub_action["scope"] == "operator"
