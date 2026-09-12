from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from forwin.application.projects.reviews import get_chapter_review
from forwin.canon import (
    CanonAdmissionOutcome,
    CanonQualityGateOutcome,
)
from forwin.canon import quality_preparation as quality_gates_module
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.checker.hard_floor import HardFloorResult
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline import ChapterPipeline
from forwin.generation.pipeline_core import project_chapters as project_chapters_module
from forwin.models.audit import DecisionEvent
from forwin.models.base import Base, get_engine, get_session_factory
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.candidate import CandidateReviewRequest
from forwin.review.decision.rules.repair_v2 import decide_repair_v2
from forwin.review.decision.types import Decision, DecisionInput, PlanLayerHealth
from forwin.review.repair import service as repair_service_module
from forwin.review.repair.control import RepairControl
from forwin.review.repair.plan_patch import RepairPlanPatchResult
from forwin.review.repair.service import (
    _attempts_for_repair_phase,
    _review_from_canon_gate_block,
)
from forwin.review.telemetry import ReviewTelemetry
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_test_url


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _noop_decision_refs(*args, **kwargs):
    return []


@pytest.fixture(autouse=True)
def _isolate_canon_repair_from_hard_floor(monkeypatch):
    monkeypatch.setattr(
        project_chapters_module,
        "run_hard_floor",
        lambda **_kwargs: HardFloorResult(passed=True),
    )


def _build_pipeline(
    database_url: str,
    *,
    max_rewrites: int = 3,
) -> ChapterPipeline:
    policy_payload = RuntimePolicy.for_profile("standard").model_dump(mode="python")
    policy_payload["review"]["max_rewrites"] = max_rewrites
    policy = RuntimePolicy.model_validate(policy_payload).with_user_settings(
        manual_checkpoints=False,
        band_checkpoint_action="continue",
    )
    infrastructure = InfrastructureConfig(
        database_url=database_url,
        artifact_root="/tmp/forwin-canon-repair-tests",
        retrieval_backend="qdrant",
        qdrant_url=":memory:",
        embedding_backend="hash",
        minimax_api_key="",
        minimax_model="fake-model",
    )
    return RuntimeContainer.from_config(
        infrastructure,
        policy=policy,
        role="generation_worker",
    ).build_chapter_pipeline()


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


class _Attempt:
    def __init__(self, repair_scope: str, repair_phase: str):
        self.repair_scope = repair_scope
        self.repair_phase = repair_phase


def _one_chapter_arc(arc_synopsis: str) -> dict[str, object]:
    return {
        "arc_synopsis": arc_synopsis,
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


def _writer_output(chapter_number: int, marker: str = "ok") -> WriterOutput:
    return WriterOutput(
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        body=f"正文{marker}" * 900,
        char_count=1800,
        end_of_chapter_summary=marker,
        state_changes=[],
        new_events=[],
        thread_beats=[],
        time_advance=None,
    )


def _draft_blocking_review(
    summary: str = "canon repair still blocked",
) -> ReviewVerdict:
    return ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="canon_repair_regression",
                severity="error",
                description=summary,
                reviewer="test",
                issue_type="placeholder_leakage",
                target_scope="draft",
                evidence_refs=["signal-1"],
                blocking=True,
            )
        ],
        review_summary=summary,
    )


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


def test_canon_repair_budget_includes_prior_review_repair_attempts():
    from tests.test_rc_repair_control import (
        _RepairHarness,
        _repair_attempt,
        _run_repair_loop,
        _hard_failure_review,
    )

    harness = _RepairHarness(max_rewrites=1)
    _output, review, forced = _run_repair_loop(
        harness,
        attempts=[_repair_attempt(phase="review_repair")],
        review=_hard_failure_review(),
        repair_phase="canon_repair",
    )
    assert review.repair_exhausted
    assert not forced
    assert not any(event.event_type == "repair_started" for event in harness.events)


def test_force_accept_flags_latest_attempt_in_active_repair_phase(monkeypatch):
    review_attempt = SimpleNamespace(
        repair_scope="draft",
        repair_phase="review_repair",
        source_draft_id="draft-1",
        result_draft_id="draft-2",
        forced_accept_applied=False,
    )
    canon_attempt = SimpleNamespace(
        repair_scope="draft",
        repair_phase="canon_repair",
        source_draft_id="draft-2",
        result_draft_id="draft-3",
        forced_accept_applied=False,
    )

    class _Repo:
        def list_chapter_rewrite_attempts(self, _project_id, _chapter_number):
            return [review_attempt, canon_attempt]

    class _Session:
        def add(self, _row) -> None:
            return None

    class _Pipeline:
        policy = RuntimePolicy.for_profile("standard")

        def __init__(self):
            self.control = RepairControl(
                progress=None, should_pause=self._pause_requested
            )
            self.telemetry = ReviewTelemetry(
                SimpleNamespace(
                    record_event=self._record_decision_event,
                    record_rule_decision=self._record_rule_decision_event,
                )
            )

        def _pause_requested(self) -> bool:
            return False

        def _record_rule_decision_event(self, **_kwargs) -> None:
            return None

        def _record_decision_event(self, **_kwargs):
            return SimpleNamespace(id="force-event")

        def _review_meta_json(self, review: ReviewVerdict) -> str:
            return json.dumps(
                review.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
            )

    monkeypatch.setattr(
        repair_service_module,
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
                    "final_residual_decision": "force_accept",
                    "forceable": True,
                    "canon_risk": "low",
                    "residual_issues": [],
                    "requires_human": False,
                },
            )

    monkeypatch.setattr(repair_service_module, "AutoDecisionEngine", _FinalGateEngine)

    _output, _review, forced_accept = repair_service_module._run_repair_loop_for_phase(
        _Pipeline(),
        session=_Session(),
        repo=_Repo(),
        updater=object(),
        checker=object(),
        project_id="p",
        chapter_plan=SimpleNamespace(chapter_number=1),
        current_context=object(),
        current_output=object(),
        current_draft=SimpleNamespace(id="draft-1"),
        current_review=ReviewVerdict(verdict="fail", issues=[]),
        current_review_row=SimpleNamespace(id="review-1", review_meta_json="{}"),
        current_review_trace_id="review-trace",
        current_review_event=SimpleNamespace(id="review-event"),
        repair_phase="review_repair",
    )

    assert forced_accept is True
    assert _review.verdict == "warn"
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

    outcome = CanonAdmissionOutcome(
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
        lambda **_kwargs: (
            calls.append("analysis")
            or SimpleNamespace(signals=[], raw_analyzer_results=[])
        ),
    )

    def _evaluate_canon_admission(**_kwargs):
        calls.append("admission")
        return gate

    monkeypatch.setattr(
        quality_gates_module, "evaluate_canon_admission", _evaluate_canon_admission
    )

    class _ObligationRepo:
        def __init__(self, _session) -> None:
            calls.append("obligation_repo")

        def list_active_for_context(self, *_args, **_kwargs):
            return []

        def list_planned_for_chapter(self, *_args, **_kwargs):
            return []

        def list_patches_by_ids(self, _ids):
            return []

    class _CanonQualityRepo:
        def __init__(self, _session) -> None:
            calls.append("canon_quality_repo")

        def save_admission_run(self, gate_result, *, signals):
            calls.append("save_admission")
            return SimpleNamespace(id="quality-admission-run")

    class _Session:
        def get(self, _model, _id):
            return SimpleNamespace(target_total_chapters=0)

    class _Pipeline:
        policy = RuntimePolicy.for_profile("standard")
        llm_client = None

        def _record_decision_event(self, **kwargs) -> None:
            calls.append(f"event:{kwargs['event_type']}")

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
    monkeypatch.setattr(
        quality_gates_module,
        "latest_draft_and_review_for_chapter",
        lambda **_kwargs: (SimpleNamespace(id="d1"), SimpleNamespace(id="r1")),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "prepare_deferred_acceptance",
        lambda **_kwargs: (
            calls.append("deferred_acceptance") or ["deferred patch failed"]
        ),
    )

    outcome = quality_gates_module.CanonQualityPreparer().evaluate(
        policy=_Pipeline().policy,
        llm_client=_Pipeline().llm_client,
        artifact_store=getattr(_Pipeline(), "artifact_store", None),
        recorder=SimpleNamespace(
            record_event=getattr(
                _Pipeline(), "_record_decision_event", lambda **_kwargs: None
            ),
            record_rule_decision=getattr(
                _Pipeline(), "_record_rule_decision_event", lambda **_kwargs: None
            ),
            save_prompt_trace=getattr(
                _Pipeline(), "save_prompt_trace", lambda **_kwargs: ""
            ),
        ),
        session=_Session(),
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


def test_canon_quality_gate_passes_draft_resolved_obligation_ids(monkeypatch):
    gate = CanonAdmissionGateResult(
        project_id="p",
        chapter_number=18,
        draft_id="d18",
        review_id="r18",
        commit_allowed=True,
        verdict="warn",
        admission_mode="with_obligation",
        gate_summary="canon quality gate strict: commit_allowed=True",
    )
    captured: dict[str, object] = {}
    calls: list[str] = []
    from forwin.canon_quality.chapter_review_form.form_builder import build_form
    from forwin.canon_quality.chapter_review_form.form_schema import (
        ChapterReviewAnswers,
    )
    from forwin.narrative_obligations.types import NarrativeObligation
    body = "Ari confirms the retired engineer is Nox."
    obligation = NarrativeObligation(id="obl-due", project_id="p", origin_chapter_number=1,
        obligation_type="identity_ambiguity", status="active", summary="Identify the retired engineer",
        payoff_test="Confirm the engineer is Nox", deadline_chapter=18, linked_plan_patch_ids=["patch-1"])
    form = build_form(project_id="p", chapter_number=18, chapter_text=body, obligations=[obligation])
    assessment = {"value": "fulfilled", "evidence_quote": body, "subject_of_quote": "Nox", "confidence": .95,
                  "explanation": "Ari confirms that Nox is the retired engineer."}
    answers = ChapterReviewAnswers.model_validate({"project_id": "p", "chapter_number": 18,
        "form_schema_version": form.form_schema_version, "reviewed_body_sha256": form.reviewed_body_sha256, "obligations": [{"id": "obl-due",
        "addressed": assessment, "payoff_evidence": assessment,
        "subject_matches": {**assessment, "value": "true"},
        "condition_results": [{"condition": obligation.payoff_test, "assessment": assessment}]}]})
    monkeypatch.setattr(quality_gates_module, "analyze_writer_output_quality",
        lambda **_kwargs: SimpleNamespace(signals=[], raw_analyzer_results=[], form=form, answers=answers))

    def _evaluate_canon_admission(**kwargs):
        captured.update(kwargs)
        return gate

    monkeypatch.setattr(
        quality_gates_module, "evaluate_canon_admission", _evaluate_canon_admission
    )

    class _ObligationRepo:
        def __init__(self, _session) -> None:
            return None

        def list_for_admission(self, *_args, **_kwargs):
            return [obligation]

        def list_planned_for_chapter(self, *_args, **_kwargs):
            return []

        def list_patches_by_ids(self, _ids):
            return []

    class _CanonQualityRepo:
        def __init__(self, _session) -> None:
            return None

        def save_admission_run(self, gate_result, *, signals):
            calls.append("save_admission")
            return SimpleNamespace(id="quality-admission-run")

    class _Session:
        def get(self, _model, _id):
            return SimpleNamespace(target_total_chapters=100)

    class _Pipeline:
        policy = RuntimePolicy.for_profile("standard")
        llm_client = None

        def _record_decision_event(self, **_kwargs) -> None:
            calls.append("event")

    monkeypatch.setattr(
        quality_gates_module, "NarrativeObligationRepository", _ObligationRepo
    )
    monkeypatch.setattr(
        quality_gates_module, "CanonQualityRepository", _CanonQualityRepo
    )
    monkeypatch.setattr(
        quality_gates_module,
        "latest_draft_and_review_for_chapter",
        lambda **_kwargs: (SimpleNamespace(id="d18"), SimpleNamespace(id="r18")),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "prepare_deferred_acceptance",
        lambda **_kwargs: [],
    )

    outcome = quality_gates_module.CanonQualityPreparer().evaluate(
        policy=_Pipeline().policy,
        llm_client=_Pipeline().llm_client,
        artifact_store=getattr(_Pipeline(), "artifact_store", None),
        recorder=SimpleNamespace(
            record_event=getattr(
                _Pipeline(), "_record_decision_event", lambda **_kwargs: None
            ),
            record_rule_decision=getattr(
                _Pipeline(), "_record_rule_decision_event", lambda **_kwargs: None
            ),
            save_prompt_trace=getattr(
                _Pipeline(), "save_prompt_trace", lambda **_kwargs: ""
            ),
        ),
        session=_Session(),
        updater=object(),
        project_id="p",
        chapter_number=18,
        writer_output=SimpleNamespace(body="Ari confirms the retired engineer is Nox."),
        verdict=SimpleNamespace(verdict="pass"),
    )

    assert isinstance(outcome, CanonQualityGateOutcome)
    assert outcome.gate_result is gate
    assert captured["resolved_obligation_ids"] == ["obl-due"]
    assert calls == ["save_admission", "event"]
    assert outcome.obligation_resolution_plan.resolved_obligation_ids == ["obl-due"]


def test_canon_admission_exception_pauses_chapter_instead_of_accepting(monkeypatch):
    class PassReviewHub:
        def review(self, **_kwargs) -> ReviewVerdict:
            return ReviewVerdict(
                verdict="pass",
                issues=[],
                review_summary="accepted by test reviewer",
            )

    db_path = postgres_test_url("canon-apply-exception-no-freeze")
    pipeline = _build_pipeline(db_path)
    try:
        pipeline.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: (
            _one_chapter_arc("canon apply exception")
        )
        pipeline.writer.write_chapter = lambda context: _writer_output(
            context.chapter_number
        )
        pipeline.candidate_review.draft_review = PassReviewHub()

        def fail_canon_quality_gate(*_args, **_kwargs):
            raise RuntimeError("canon apply failed")

        monkeypatch.setattr(
            quality_gates_module.CanonQualityPreparer,
            "evaluate",
            fail_canon_quality_gate,
        )

        result = pipeline.run("p", "g", 1)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            plan = session.execute(select(ChapterPlan)).scalar_one()
        finally:
            session.close()
            engine.dispose()
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()

    assert result.status == "needs_review"
    assert result.completed_chapters == []
    assert result.paused_chapters == [1]
    assert plan.status == "needs_review"


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


@pytest.mark.parametrize("canon_changed", [False, True])
def test_warn_review_canon_block_runs_canon_repair_before_accepting(
    monkeypatch, canon_changed
):
    from forwin.state.updater import StateUpdater

    create_project = StateUpdater.create_project

    def create_serial_project(updater, *args, **kwargs):
        project = create_project(updater, *args, **kwargs)
        project.automation_json = '{"primary_publish_platform":"qidian"}'
        return project

    monkeypatch.setattr(StateUpdater, "create_project", create_serial_project)

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
    pipeline = _build_pipeline(db_path, max_rewrites=1)
    apply_calls = {"count": 0}
    from forwin.retrieval.requirements import RequiredContextError
    from forwin.retrieval.source_identity import CanonBaselineChanged
    from forwin.review.repair.plan_patch import RepairPlanPatchService

    patch_apply = RepairPlanPatchService.apply
    patch_errors, writer_calls = [], []

    def apply_after_repair_commit(owner, request):
        original = request.context
        # The real RepairService already committed REPAIR_STARTED. The old
        # capability must remain dead; the repair owner must explicitly rebind.
        with pytest.raises(RequiredContextError, match="transaction ended"):
            original.required_context_hydrator(original, [])
        if canon_changed:
            project = request.session.get(Project, request.project_id)
            project.book_revision += 1
            request.session.flush()
        try:
            result = patch_apply(owner, request)
        except Exception as exc:
            patch_errors.append(exc)
            raise
        assert result.context.canon_read_baseline == original.canon_read_baseline
        assert (
            result.context.required_context_hydrator
            is not original.required_context_hydrator
        )
        result.context.required_context_hydrator(result.context, [])
        return result

    monkeypatch.setattr(RepairPlanPatchService, "apply", apply_after_repair_commit)
    try:
        pipeline.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: {
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
        pipeline.writer.write_chapter = lambda context: (
            writer_calls.append(context)
            or WriterOutput(
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
        )
        pipeline.candidate_review.draft_review = WarnThenPassReviewHub()

        def evaluate_canon_candidate(**_kwargs):
            apply_calls["count"] += 1
            if apply_calls["count"] == 1:
                return CanonQualityGateOutcome(
                    blocked_path="frozen/canon-quality.json",
                    gate_result=CanonAdmissionGateResult(
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
            return CanonQualityGateOutcome()

        pipeline.canon_preparation.quality_preparer.evaluate = evaluate_canon_candidate

        result = pipeline.run("p", "g", 1)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            attempts = (
                session.execute(
                    select(ChapterRewriteAttempt).order_by(
                        ChapterRewriteAttempt.attempt_no
                    )
                )
                .scalars()
                .all()
            )
            plan = session.execute(select(ChapterPlan)).scalar_one()
        finally:
            session.close()
            engine.dispose()
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()

    if canon_changed:
        assert result.status == "failed"
        assert len(writer_calls) == 1
        assert len(patch_errors) == 1
        assert isinstance(patch_errors[0], CanonBaselineChanged)
        assert attempts == []
        assert plan.status == "failed"
        return
    assert len(writer_calls) == 2
    assert not patch_errors
    assert result.status == "completed"
    assert result.frozen_artifacts == []
    assert apply_calls["count"] == 2
    assert len(attempts) == 1
    assert attempts[0].repair_phase == "canon_repair"
    assert attempts[0].attempt_no == 1
    assert attempts[0].phase_attempt_no == 1
    assert attempts[0].repair_scope == "draft"
    assert plan.status == "accepted"
    assert plan.repair_attempt_count == 1


def test_repairable_canon_block_exhaustion_pauses_with_canon_repair_attempts(
    monkeypatch,
):
    class WarnThenFailReviewHub:
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
            return _draft_blocking_review(
                f"canon quality gate strict: repair attempt {self.calls - 1} still blocked"
            )

    db_path = postgres_test_url("canon-repair-exhaustion-task6")
    pipeline = _build_pipeline(db_path, max_rewrites=1)
    try:
        pipeline.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: (
            _one_chapter_arc("canon repair exhaustion")
        )
        pipeline.writer.write_chapter = lambda context: _writer_output(
            context.chapter_number
        )
        pipeline.candidate_review.draft_review = WarnThenFailReviewHub()
        pipeline.writer.write_chapter = lambda context: _writer_output(
            int(context.chapter_number),
            marker=f"repair-{pipeline.candidate_review.draft_review.calls}",
        )
        monkeypatch.setattr(
            pipeline.repair_plan_patch,
            "apply",
            lambda request: RepairPlanPatchResult(
                {"repair_scope": request.repair_scope}, request.context, {}, {}, ""
            ),
        )

        def evaluate_canon_candidate(**_kwargs):
            return CanonQualityGateOutcome(
                blocked_path="frozen/canon-quality.json",
                gate_result=CanonAdmissionGateResult(
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

        pipeline.canon_preparation.quality_preparer.evaluate = evaluate_canon_candidate

        result = pipeline.run("p", "g", 1)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            attempts = (
                session.execute(
                    select(ChapterRewriteAttempt).order_by(
                        ChapterRewriteAttempt.attempt_no
                    )
                )
                .scalars()
                .all()
            )
            plan = session.execute(select(ChapterPlan)).scalar_one()
            latest_attempt_review = session.execute(
                select(ChapterReview).where(
                    ChapterReview.id == attempts[-1].result_review_id
                )
            ).scalar_one()
        finally:
            session.close()
            engine.dispose()
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()

    latest_review_meta = json.loads(latest_attempt_review.review_meta_json or "{}")
    assert result.status == "needs_review"
    assert len(attempts) == 1
    assert {attempt.repair_phase for attempt in attempts} == {"canon_repair"}
    assert [attempt.phase_attempt_no for attempt in attempts] == list(
        range(1, len(attempts) + 1)
    )
    assert plan.status == "needs_review"
    assert plan.canon_risk_level == "high"
    assert latest_review_meta["repair_exhausted"] is True


@pytest.mark.parametrize(
    ("raw_scope", "expected_payload_scope"),
    [
        (None, ""),
        ("arc", "arc"),
        ("book", "book"),
    ],
)
def test_non_repairable_canon_quality_block_records_system_block_without_repair(
    raw_scope,
    expected_payload_scope,
):
    class WarnReviewHub:
        def review(self, **_kwargs) -> ReviewVerdict:
            return ReviewVerdict(
                verdict="warn",
                issues=[],
                review_summary="soft review warning",
            )

    db_path = postgres_test_url("canon-system-block-task6")
    pipeline = _build_pipeline(db_path, max_rewrites=1)
    try:
        pipeline.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: (
            _one_chapter_arc("canon system block")
        )
        pipeline.writer.write_chapter = lambda context: _writer_output(
            context.chapter_number
        )
        pipeline.candidate_review.draft_review = WarnReviewHub()
        pipeline.repair.repair_canon_block = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-repairable canon block should not run canon repair")
        )
        pipeline.canon_preparation.quality_preparer.evaluate = lambda **_kwargs: (
            CanonQualityGateOutcome(
                blocked_path="frozen/canon-quality.json",
                gate_result=CanonAdmissionGateResult(
                    project_id="p",
                    chapter_number=1,
                    draft_id="d1",
                    review_id="r1",
                    commit_allowed=False,
                    verdict="fail",
                    admission_mode="blocked",
                    required_repair_scope=raw_scope,
                    gate_summary=f"canon quality gate strict: required_repair_scope={raw_scope}",
                    deterministic_issue_refs=["signal-1"],
                ),
            )
        )

        result = pipeline.run("p", "g", 1)

        engine = get_engine(db_path)
        session = get_session_factory(engine)()
        try:
            attempts = session.execute(select(ChapterRewriteAttempt)).scalars().all()
            plan = session.execute(select(ChapterPlan)).scalar_one()
            events = session.execute(select(DecisionEvent)).scalars().all()
        finally:
            session.close()
            engine.dispose()
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()

    system_block_events = [
        event
        for event in events
        if (json.loads(event.payload_json or "{}").get("outcome") == "system_block")
    ]
    assert result.status == "needs_review"
    assert result.system_block_chapters == [1]
    assert attempts == []
    assert plan.status == "needs_review"
    assert plan.repair_attempt_count == 0
    assert system_block_events
    system_block_event = system_block_events[-1]
    system_block_payload = json.loads(system_block_event.payload_json or "{}")
    assert (
        system_block_payload["reason"]
        == f"canon quality gate strict: required_repair_scope={raw_scope}"
    )
    assert system_block_payload["required_repair_scope"] == expected_payload_scope
    assert "canon quality gate" in system_block_event.summary
    assert "canon quality gate" in system_block_event.reason


@pytest.mark.parametrize(
    ("initial_force_accept", "canon_repair_force_accept"),
    [
        (True, False),
        (False, True),
    ],
)
def test_failed_canon_repair_after_force_accept_pauses_without_reapplying_canon(
    initial_force_accept,
    canon_repair_force_accept,
):
    db_path = postgres_test_url("canon-repair-force-accept-fail")
    pipeline = _build_pipeline(db_path, max_rewrites=1)
    apply_calls = {"count": 0}
    repair_calls = {"count": 0}
    try:
        pipeline.arc_director.plan_arc = lambda _premise, _genre, _num_chapters: {
            "arc_synopsis": "canon repair force accept fail",
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
        pipeline.writer.write_chapter = lambda context: WriterOutput(
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

        pipeline.candidate_review.draft_review = SimpleNamespace(
            review=lambda **_kwargs: ReviewVerdict(verdict="pass", issues=[])
        )

        def force_accepted_review(**kwargs):
            evaluation = pipeline.candidate_review.evaluate(
                CandidateReviewRequest(
                    session=kwargs["session"],
                    repo=kwargs["repo"],
                    checker=kwargs["checker"],
                    project_id=kwargs["project_id"],
                    chapter_number=kwargs["chapter_plan"].chapter_number,
                    context=kwargs["context"],
                    output=kwargs["writer_output"],
                )
            )
            persisted = pipeline.candidate_review.persist(
                session=kwargs["session"],
                updater=kwargs["updater"],
                chapter_plan=kwargs["chapter_plan"],
                project_id=kwargs["project_id"],
                evaluation=evaluation,
            )
            return persisted.output, evaluation.review, initial_force_accept

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

        def evaluate_canon_candidate(**_kwargs):
            apply_calls["count"] += 1
            if apply_calls["count"] > 1:
                raise AssertionError(
                    "canon should not be retried after failed canon repair"
                )
            return CanonQualityGateOutcome(
                blocked_path="frozen/canon-quality.json",
                gate_result=gate,
            )

        def failed_canon_repair(**kwargs):
            repair_calls["count"] += 1
            return (
                kwargs["writer_output"],
                ReviewVerdict(
                    verdict="fail",
                    issues=[],
                    repair_exhausted=True,
                ),
                canon_repair_force_accept,
            )

        pipeline.repair.review_candidate = force_accepted_review
        pipeline.canon_preparation.quality_preparer.evaluate = evaluate_canon_candidate
        pipeline.repair.repair_canon_block = failed_canon_repair

        result = pipeline.run("p", "g", 1)
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()

    assert result.status == "needs_review"
    assert apply_calls["count"] == 1
    assert repair_calls["count"] == 1


def _canon_repair_decision_for_scope(
    raw_scope: object,
) -> tuple[ReviewVerdict, Decision]:
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
        (
            "chapter_plan",
            "canon_admission_chapter_plan_block",
            "chapter_patch",
            "chapter_plan",
        ),
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
