from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from forwin.context.providers.feedback_provider import FeedbackContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.experience.arc_experience_planner import ArcExperiencePlanningService
from forwin.experience.service import (
    AudienceCalibrationProfile,
    ExperiencePlanningService,
    load_long_window_audience_trends,
)
from forwin.genesis.arc_activation_review import build_arc_activation_review_pack
from forwin.models import (
    ArcPlanVersion,
    CandidateDraftRecord,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    CommentSignalCandidate,
    FeedbackActionRecord,
    Project,
    PublisherRawComment,
    SignalWindowAggregate,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon import CanonCommitRecord
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.stage_analysis import PacingStrategist
from forwin.protocol.context import (
    AudienceHintView,
    ChapterContextPack,
    ReaderFeedbackView,
    ReviewContextPack,
    SignalSummaryView,
)
from forwin.protocol.writer import WriterOutput
from forwin.review.context_builder import build_review_context_pack
from forwin.review.llm_webnovel import LLMWebNovelReviewer
from forwin.simulation.world import WorldSimulator, build_reader_feedback_snapshot
from forwin.state.repo import StateRepository
from tests.postgres import postgres_test_url


def _strong_feedback() -> ReaderFeedbackView:
    return ReaderFeedbackView(
        comment_count=20,
        dominant_sentiment="risk:confirmed",
        feedback_summary="多名读者认为剧情逻辑崩坏。",
        confirmed_signals=[
            SignalSummaryView(
                signal_key="risk:plot:主线",
                signal_type="risk",
                target_name="主线",
                level="confirmed",
                hit_count=12,
                max_severity=4,
            )
        ],
        reader_tier=3,
    )


def _strong_hints() -> AudienceHintView:
    return AudienceHintView(items=[
        {"action_id": "pacing", "category": "pacing", "text": "立即压缩主线"},
        {"action_id": "clarity", "category": "clarity", "text": "重写世界规则"},
        {"action_id": "heat", "category": "character_heat", "text": "增加配角戏份"},
        {"action_id": "risk", "category": "risk", "text": "阻断当前正文"},
    ])


def _chapter_context(**updates) -> ChapterContextPack:
    base = ChapterContextPack(
        project_id="project-1",
        project_title="隔离测试",
        premise="主角追查旧案",
        genre="悬疑",
        setting_summary="港城",
        chapter_number=5,
        chapter_plan_title="第五章",
        chapter_plan_one_line="主角找到新的账册证据。",
        chapter_goals=["推进旧案"],
    )
    return base.model_copy(update=updates)


def test_writer_feedback_provider_quarantines_existing_action_hints() -> None:
    class Repo:
        def get_audience_hints(self, _project_id: str, *, before_chapter: int):
            assert before_chapter == 5
            return SimpleNamespace(
                pacing_hints=["立即压缩主线"],
                clarity_hints=["重写世界规则"],
                character_heat_changes=["增加配角戏份"],
                risk_flags=["阻断当前正文"],
            )

    draft = ContextDraft()
    FeedbackContextProvider().contribute(
        ContextRequest(
            project_id="project-1",
            chapter_plan=SimpleNamespace(chapter_number=5),
            repo=Repo(),
        ),
        draft,
    )

    assert draft.data == {
        "audience_hints_raw": None,
        "audience_hints": None,
        "reader_feedback": None,
    }


def test_review_inputs_are_identical_with_no_feedback_direct_feedback_or_backfill() -> None:
    no_feedback = build_review_context_pack(context=_chapter_context())
    direct_feedback = build_review_context_pack(
        context=_chapter_context(
            reader_feedback=_strong_feedback(),
            audience_hints=_strong_hints(),
        )
    )
    backfilled_feedback = build_review_context_pack(
        context=_chapter_context(),
        repo=SimpleNamespace(
            get_recent_reader_feedback=lambda *_args, **_kwargs: _strong_feedback()
        ),
    )

    assert direct_feedback == no_feedback
    assert backfilled_feedback == no_feedback


def test_llm_review_payload_ignores_direct_feedback_but_keeps_story_evidence() -> None:
    reviewer = LLMWebNovelReviewer(enabled=False)
    output = WriterOutput(
        project_id="project-1",
        chapter_number=5,
        title="第五章",
        body="主角核对账册，确认旧案证据仍然成立。",
        end_of_chapter_summary="账册证据得到确认。",
    )
    base = ReviewContextPack(
        project_id="project-1",
        project_title="隔离测试",
        chapter_number=5,
        chapter_plan_title="第五章",
        chapter_plan_one_line="主角找到新的账册证据。",
        chapter_goals=["推进旧案"],
        canon_invariants=[
            {
                "invariant_key": "book_state_rule:ledger",
                "current_value": "账册已封存",
            }
        ],
    )
    with_feedback = base.model_copy(
        update={
            "reader_feedback": _strong_feedback(),
            "audience_hints": _strong_hints(),
        }
    )

    baseline_payload = reviewer._llm_payload(base, output)
    feedback_payload = reviewer._llm_payload(with_feedback, output)

    assert feedback_payload == baseline_payload
    assert feedback_payload["world"]["canon_invariants"] == base.canon_invariants
    assert any(
        item["evidence_id"] == "canon_invariant:book_state_rule:ledger"
        for item in feedback_payload["evidence_index"]
    )


def test_arc_experience_fallback_ignores_feedback_trends() -> None:
    service = ArcExperiencePlanningService()
    project = Project(id="project-1", title="隔离测试", premise="旧案", genre="悬疑")
    structure = ArcStructureDraftData(
        phase_layout=["setup", "pressure", "payoff"],
        key_beats=["发现账册"],
        thread_priorities=[],
        hotspot_candidates=[],
        compression_candidates=[],
    )

    baseline = service.plan_arc_experience(
        project=project,
        structure=structure,
        chapter_plans=[],
        audience_trends=[],
    )
    with_feedback = service.plan_arc_experience(
        project=project,
        structure=structure,
        chapter_plans=[],
        audience_trends=[
            "主线:risk:confirmed",
            "规则:confusion:confirmed",
            "配角:relationship_interest:confirmed",
        ],
    )

    assert with_feedback == baseline


@pytest.fixture()
def feedback_sessions():
    engine = get_engine(postgres_test_url("feedback-quarantine"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        yield Session
    finally:
        engine.dispose()


def _add_project(session, *, title: str) -> Project:
    project = Project(title=title, premise="主角追查旧案", genre="悬疑")
    session.add(project)
    session.flush()
    return project


def _add_strong_feedback_rows(session, project: Project) -> None:
    comment = PublisherRawComment(
        project_id=project.id,
        platform_id="fanqie",
        remote_comment_id=f"comment-{project.id}",
        work_name=project.title,
        chapter_title="第一章",
        author_id="reader-1",
        body_text="剧情逻辑崩坏，完全不能继续。",
        raw_payload_json="{}",
    )
    session.add(comment)
    session.flush()
    session.add(
        CommentSignalCandidate(
            project_id=project.id,
            source_comment_id=comment.id,
            signal_type="risk",
            target_type="plot",
            target_name="主线",
            severity=4,
            confidence=0.99,
            evidence_span=comment.body_text,
            signal_level="confirmed",
            chapter_number=1,
        )
    )
    session.add_all(
        [
            SignalWindowAggregate(
                project_id=project.id,
                signal_key="risk:plot:主线",
                signal_type="risk",
                target_type="plot",
                target_name="主线",
                window_type=window_type,
                window_chapter_start=1,
                window_chapter_end=4,
                hit_comment_count=12,
                unique_user_count=8,
                total_comment_count=12,
                reader_estimate=960,
                max_severity=4,
                avg_confidence=0.99,
                signal_level="confirmed",
            )
            for window_type in ("medium", "long")
        ]
    )
    session.add(
        FeedbackActionRecord(
            project_id=project.id,
            signal_key="risk:plot:主线",
            signal_type="risk",
            action_type="pause_for_review",
            triggered_at_chapter=3,
            cooldown_until_chapter=6,
            notes="阻断当前正文",
        )
    )


def test_existing_feedback_rows_remain_observable_but_do_not_change_automation(
    feedback_sessions,
) -> None:
    Session = feedback_sessions
    with Session.begin() as session:
        baseline_project = _add_project(session, title="无评论基线")
        feedback_project = _add_project(session, title="已有评论")
        _add_strong_feedback_rows(session, feedback_project)
        baseline_id = baseline_project.id
        feedback_id = feedback_project.id

    with Session() as session:
        observed = build_reader_feedback_snapshot(
            session,
            "已有评论",
            project_id=feedback_id,
            chapter_number=4,
        )
        assert observed["comment_count"] == 1
        assert load_long_window_audience_trends(session, feedback_id) == [
            "主线:risk:confirmed"
        ]
        repo = StateRepository(session)
        observed_hints = repo.get_audience_hints(feedback_id, before_chapter=5)
        # Historical observations remain queryable but cannot become qualified input.
        assert observed_hints is None

        baseline_draft = ContextDraft()
        FeedbackContextProvider().contribute(
            ContextRequest(
                project_id=baseline_id,
                chapter_plan=SimpleNamespace(chapter_number=5),
                repo=repo,
            ),
            baseline_draft,
        )
        feedback_draft = ContextDraft()
        FeedbackContextProvider().contribute(
            ContextRequest(
                project_id=feedback_id,
                chapter_plan=SimpleNamespace(chapter_number=5),
                repo=repo,
            ),
            feedback_draft,
        )
        assert feedback_draft.data == baseline_draft.data

        baseline_pacing = PacingStrategist().analyze(
            session=session,
            project_id=baseline_id,
            chapter_number=4,
        )
        feedback_pacing = PacingStrategist().analyze(
            session=session,
            project_id=feedback_id,
            chapter_number=4,
        )
        assert feedback_pacing == baseline_pacing

        baseline_world = WorldSimulator().simulate(
            session=session,
            project_id=baseline_id,
            chapter_number=4,
        )
        feedback_world = WorldSimulator().simulate(
            session=session,
            project_id=feedback_id,
            chapter_number=4,
        )
        assert feedback_world == baseline_world

        class CapturingWorldLLM:
            def __init__(self) -> None:
                self.prompts = []

            def chat(self, prompt, **_kwargs):
                self.prompts.append(prompt)
                return json.dumps(
                    {
                        "pressure_level": "rising",
                        "pressure_summary": "旧案的连锁反应继续扩大。",
                        "notable_shifts": ["账册证据引发追查"],
                    },
                    ensure_ascii=False,
                )

        baseline_llm = CapturingWorldLLM()
        feedback_llm = CapturingWorldLLM()
        baseline_llm_world = WorldSimulator(llm_client=baseline_llm).simulate(
            session=session,
            project_id=baseline_id,
            chapter_number=4,
        )
        feedback_llm_world = WorldSimulator(llm_client=feedback_llm).simulate(
            session=session,
            project_id=feedback_id,
            chapter_number=4,
        )
        assert feedback_llm_world == baseline_llm_world
        assert feedback_llm.prompts == baseline_llm.prompts

        baseline_calibration = ExperiencePlanningService().build_audience_calibration_profile(
            session=session,
            project_id=baseline_id,
        )
        feedback_calibration = ExperiencePlanningService().build_audience_calibration_profile(
            session=session,
            project_id=feedback_id,
        )
        assert feedback_calibration == baseline_calibration == AudienceCalibrationProfile()

        baseline_activation = build_arc_activation_review_pack(
            session,
            project_id=baseline_id,
            arc_number=2,
            chapter_start=5,
        )
        feedback_activation = build_arc_activation_review_pack(
            session,
            project_id=feedback_id,
            arc_number=2,
            chapter_start=5,
        )
        assert feedback_activation.audience_signals == []
        assert baseline_activation.audience_signals == []


def test_story_based_pacing_blocker_remains_without_feedback(feedback_sessions) -> None:
    Session = feedback_sessions
    with Session.begin() as session:
        project = _add_project(session, title="真实阻断")
        arc = ArcPlanVersion(
            project_id=project.id,
            arc_number=1,
            chapter_start=1,
            chapter_end=1,
            arc_synopsis="旧案开局",
        )
        session.add(arc)
        session.flush()
        plan = ChapterPlan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=1,
            title="第一章",
            one_line="主角找到证据",
            status="accepted",
        )
        session.add(plan)
        session.flush()
        draft = ChapterDraft(
            chapter_plan_id=plan.id,
            version=1,
            body_text="过短",
            summary="证据出现。",
            char_count=2,
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="pass")
        session.add(review)
        session.flush()
        candidate = CandidateDraftRecord(
            project_id=project.id,
            chapter_plan_id=plan.id,
            chapter_number=1,
            candidate_draft_id=draft.id,
            review_id=review.id,
            body_hash=hashlib.sha256(draft.body_text.encode()).hexdigest(),
            status="accepted",
            canon_status="canon",
        )
        session.add(candidate)
        session.flush()
        commit = CanonCommitRecord(
            idempotency_key=f"{plan.id}:accepted",
            project_id=project.id,
            chapter_plan_id=plan.id,
            chapter_number=1,
            candidate_id=candidate.id,
            chapter_title=plan.title,
            status="committed",
        )
        session.add(commit)
        session.flush()
        plan.active_commit_id = commit.id
        candidate.canon_commit_id = commit.id
        project_id = project.id

    with Session() as session:
        assessment = PacingStrategist().analyze(
            session=session,
            project_id=project_id,
            chapter_number=1,
        )

    assert assessment.risk_level == "high"
    assert assessment.verdict == "compressed"
    assert "近期章节长度偏短" in assessment.summary
