from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.preparation import CanonPreparationService
from forwin.generation.gate_delegation import GateResolution
from forwin.generation.pipeline_core.chapter_review_gate import (
    evaluate_candidate_gate,
)
from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.generation.pipeline_core.result import RunResult
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from forwin.protocol.review import (
    ContinuityIssue,
    FinalResidualDecision,
    RepairVerification,
    ReviewVerdict,
)
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.writer.execution import WriterExecutionResult
from tests.postgres import postgres_test_url


class SpyDelegate:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("ineligible candidates must not reach gate delegation")


class ApprovingDelegate:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> GateResolution:
        self.calls += 1
        return GateResolution(
            resolved=True,
            approved=True,
            decision="approve",
            delegate="spark",
        )


def test_fail_verdict_never_reaches_gate_delegation() -> None:
    delegate = SpyDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )
    verdict = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="canon_conflict",
                reviewer="canon",
                severity="error",
                description="hard canon conflict",
            )
        ],
        recommended_action="manual_review",
    )

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=verdict,
        delegate=delegate,
    )

    assert outcome.pause_required is True
    assert outcome.should_apply_canon is False
    assert delegate.calls == 0


@pytest.mark.parametrize(
    "verdict",
    [
        ReviewVerdict(
            verdict="warn",
            final_residual_decision=FinalResidualDecision(
                decision="manual_review_required",
            ),
        ),
        ReviewVerdict(
            verdict="warn",
            final_residual_decision=FinalResidualDecision(
                decision="force_accept",
                canon_risk="high",
            ),
        ),
        ReviewVerdict(
            verdict="pass",
            repair_verification=RepairVerification(
                fixed_all_must_fix=False,
                preserved_all_must_preserve=True,
            ),
        ),
        ReviewVerdict(
            verdict="pass",
            residual_review_issues=[
                ContinuityIssue(
                    rule_name="blocking_residual",
                    severity="warning",
                    description="still blocking",
                    blocking=True,
                )
            ],
        ),
    ],
)
def test_canon_ineligible_pass_or_warn_never_reaches_gate_delegation(
    verdict: ReviewVerdict,
) -> None:
    delegate = SpyDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=verdict,
        delegate=delegate,
    )

    assert outcome.pause_required is True
    assert outcome.should_apply_canon is False
    assert delegate.calls == 0


@pytest.mark.parametrize("verdict", ["pass", "warn"])
def test_spark_only_approves_canon_eligible_review_opportunities(verdict: str) -> None:
    delegate = ApprovingDelegate()
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark"
    )

    outcome = evaluate_candidate_gate(
        policy=policy,
        verdict=ReviewVerdict(verdict=verdict, issues=[]),
        delegate=delegate,
    )

    assert delegate.calls == 1
    assert outcome.gate_approved is True
    assert outcome.pause_required is False
    assert outcome.should_apply_canon is True


def test_spark_approval_runs_real_chapter_pipeline_through_canon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = postgres_test_url("spark-gate-real-canon-flow")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    policy = RuntimePolicy.for_profile("standard").with_user_settings(
        gate_delegate="spark",
        review_interval_chapters=1,
        manual_checkpoints=False,
        band_checkpoint_action="continue",
    )
    policy = policy.model_copy(
        update={"canon": policy.canon.model_copy(update={"hard_floor": False})}
    )
    try:
        with Session.begin() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Spark Canon Flow",
                premise="Delegation may approve a pause, never Canon itself.",
                genre="test",
                target_total_chapters=2,
                runtime_policy=policy,
            )
            project.automation_json = '{"primary_publish_platform":"qidian"}'
            arc = updater.create_arc_plan(project.id, "Arc one")
            chapter_one = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="Chapter one",
                one_line="Approve the pause then enter Canon",
                goals=["Prove the production call path"],
            )
            updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=2,
                title="Chapter two",
                one_line="Remain pending",
                goals=["Stop after chapter one"],
            )
            project_id = project.id

        writer_output = WriterOutput(
            project_id=project_id,
            chapter_number=1,
            title="Chapter one",
            body="A complete candidate passes review and awaits Canon.",
            char_count=53,
            end_of_chapter_summary="The candidate is ready.",
        )
        verdict = ReviewVerdict(verdict="pass", issues=[])

        class PersistingRepair:
            @staticmethod
            def review_candidate(**kwargs):
                session = kwargs["session"]
                chapter_plan = kwargs["chapter_plan"]
                output = kwargs["writer_output"]
                draft = ChapterDraft(
                    chapter_plan_id=chapter_plan.id,
                    version=1,
                    body_text=output.body,
                    summary=output.end_of_chapter_summary,
                    char_count=output.char_count,
                )
                session.add(draft)
                session.flush()
                review = ChapterReview(
                    draft_id=draft.id,
                    verdict="pass",
                    issues_json="[]",
                    review_meta_json='{"verdict":"pass"}',
                )
                session.add(review)
                session.flush()
                CandidateDraftRepository(session).create_reviewed_version(
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    draft=draft,
                    review=review,
                    writer_output=output,
                    plan_revision=candidate_plan_revision(chapter_plan),
                    policy_version=1,
                )
                kwargs["updater"].mark_chapter_status(project_id, 1, "drafted")
                return output, verdict, False

        class RecordingPreparation:
            def __init__(self) -> None:
                self.calls = 0

            def prepare(self, *, request, **kwargs):
                self.calls += 1
                return CanonPreparationService().prepare_from_approved(
                    session=kwargs["session"],
                    candidate_id=request.candidate_id,
                    approved_book_state_changes=ApprovedGraphDeltaSet(
                        project_id=project_id,
                        chapter_number=1,
                        graph_deltas=[],
                        approved_by=["book_state_review"],
                    ),
                    entity_admission_plan=EntityAdmissionPlan(
                        project_id=project_id,
                        chapter_number=1,
                        candidate_fingerprint=writer_output_admission_fingerprint(
                            request.writer_output
                        ),
                    ),
                    acceptance_mode=request.acceptance_mode,
                    repair_attempt_count=request.repair_attempt_count,
                    residual_review_issues=request.residual_review_issues,
                    canon_risk_level=request.canon_risk_level or "low",
                )

        preparation = RecordingPreparation()
        delegation_calls: list[str] = []

        def abort_after_first_acceptance() -> bool:
            with Session() as session:
                chapter = session.get(ChapterPlan, chapter_one.id)
                return bool(chapter is not None and chapter.status == "accepted")

        stage = SimpleNamespace(
            policy=policy,
            retrieval_broker=SimpleNamespace(
                last_observability_summary={},
                build_chapter_context=lambda *_args: SimpleNamespace(),
            ),
            repair=PersistingRepair(),
            repair_execution=SimpleNamespace(),
            canon_preparation=preparation,
            llm_client=None,
            artifact_store=None,
            trace_recorder=None,
            canon_admission=CanonAdmissionService(session_factory=Session),
            _project_policy=lambda _session, _project: policy,
            _make_state_helpers=lambda session: (
                StateRepository(session),
                StateUpdater(session),
                SimpleNamespace(),
            ),
            _abort_requested=abort_after_first_acceptance,
            _pause_requested=lambda: False,
            _strict_progression_block=lambda **_kwargs: ("", "", ""),
            _manual_boundary_checkpoint=lambda *_args, **_kwargs: None,
            _audit_current_plan_before_write=lambda **kwargs: kwargs["context"],
            writer_execution=SimpleNamespace(
                execute=lambda _request: WriterExecutionResult(output=writer_output)
            ),
            _resolve_gate_delegation=lambda **kwargs: (
                delegation_calls.append(kwargs["gate_kind"])
                or GateResolution(
                    resolved=True,
                    approved=True,
                    decision="approve",
                    delegate="spark",
                )
            ),
            _emit_progress=lambda *_args, **_kwargs: None,
            _recover_post_canon_before_chapter=lambda **_kwargs: None,
            _run_phase3_pass=lambda **_kwargs: {"run_ids": {}},
            _run_post_canon_order_controls=lambda **_kwargs: {},
            _audit_future_plans_after_acceptance=lambda **_kwargs: None,
            _record_generation_audit_report_if_due=lambda **_kwargs: None,
            _cancelled_result=lambda project_id, requested, **kwargs: RunResult(
                project_id=project_id,
                requested_chapters=requested,
                completed_chapters=list(kwargs.get("completed_chapters", [])),
                cancelled=True,
            ),
        )
        monkeypatch.setattr(
            "forwin.generation.pipeline_core.project_chapters.save_accepted_trope_usage_for_chapter",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "forwin.generation.pipeline_core.chapter_execution_support.defer_structured_extraction_if_needed",
            lambda **_kwargs: None,
        )

        with Session() as session:
            result = ChapterExecutionStage._run_project_chapters(
                stage,
                session=session,
                repo=StateRepository(session),
                updater=StateUpdater(session),
                checker=SimpleNamespace(),
                project_id=project_id,
                chapter_numbers=[1, 2],
                requested_chapters=2,
            )

        assert result.completed_chapters == [1]
        assert delegation_calls == ["chapter_review_interval"]
        assert preparation.calls == 1
        with Session() as session:
            chapter = session.get(ChapterPlan, chapter_one.id)
            candidate = CandidateDraftRepository(session).latest_for_chapter(
                project_id=project_id,
                chapter_number=1,
            )
            assert chapter is not None and chapter.status == "accepted"
            assert candidate is not None and candidate.status == "accepted"
            assert session.scalar(select(func.count(CanonCommitRecord.id))) == 1
            assert session.scalar(select(func.count(CandidateDraftRecord.id))) == 1
    finally:
        engine.dispose()
