from __future__ import annotations

from datetime import datetime

from forwin.context.assembler_core import _build_canon_quality_context
from forwin.llm_eval.cases import sample_context
from forwin.models import Project
from forwin.models.audit import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.writer.prompt_core import _canon_quality_context_section
from forwin.audit.events import DecisionEventType
from tests.postgres import postgres_test_url


def test_historical_rewrite_context_freezes_next_accepted_chapter_outcome() -> None:
    engine = get_engine(postgres_test_url("historical_rewrite_anchor"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory.begin() as session:
            project = Project(
                title="锈轨纪事",
                premise="测试历史章节重写。",
                genre="科幻",
                target_total_chapters=80,
            )
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=80,
                arc_synopsis="测试卷。",
            )
            session.add(arc)
            session.flush()
            current = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=74,
                title="第74章",
                status="needs_review",
            )
            accepted = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=75,
                title="第75章　险途返程",
                status="accepted",
            )
            session.add_all([current, accepted])
            session.flush()
            draft = ChapterDraft(
                chapter_plan_id=accepted.id,
                version=1,
                body_text="轨车在返程下坡遭接管，最终把蓝铜送回第九工坊。",
                summary="返程下坡遭远程接管，轨车仍在窗口关闭前将蓝铜送回第九工坊。",
                char_count=28,
            )
            session.add(draft)
            session.flush()
            review = ChapterReview(draft_id=draft.id, verdict="pass")
            session.add(review)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=accepted.id,
                    chapter_number=75,
                    candidate_draft_id=draft.id,
                    review_id=review.id,
                    status="accepted",
                    canon_status="canon",
                )
            )

        with session_factory() as session:
            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=74,
                target_total_chapters=80,
                chapter_title="第74章",
            )

        anchor = next(
            item
            for item in context["invariant_constraints"]
            if item["invariant_key"] == "accepted_future_chapter:75"
        )
        assert anchor["kind"] == "accepted_future_anchor"
        assert anchor["current_value"] == {
            "chapter_number": 75,
            "title": "第75章　险途返程",
            "summary": "返程下坡遭远程接管，轨车仍在窗口关闭前将蓝铜送回第九工坊。",
        }
    finally:
        engine.dispose()


def test_writer_prompt_marks_accepted_future_anchor_as_immutable() -> None:
    context = sample_context()
    context.canon_quality_context = {
        "invariant_constraints": [
            {
                "invariant_key": "accepted_future_chapter:75",
                "kind": "accepted_future_anchor",
                "label": "第75章已接受结果",
                "current_value": {
                    "chapter_number": 75,
                    "title": "第75章　险途返程",
                    "summary": "返程下坡遭远程接管，轨车仍在窗口关闭前将蓝铜送回第九工坊。",
                },
                "constraints": {"immutable_definition": True},
            }
        ]
    }

    section = _canon_quality_context_section(context)

    assert section is not None
    assert "已接受后续章节的冻结锚点" in section
    assert "不得提前完成、重排、否定或改写" in section
    assert "将蓝铜送回第九工坊" in section


def test_historical_rewrite_prompt_injects_latest_operator_retry_reason() -> None:
    engine = get_engine(postgres_test_url("historical_rewrite_operator_reason"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    stale_reason = "旧的重试说明不得进入新一轮改写。"
    retry_reason = "承接第73章已启动的59:59许可；不得重做制动测试或远程拖刹揭示。"
    try:
        with session_factory.begin() as session:
            project = Project(
                title="锈轨纪事",
                premise="测试人工重试约束。",
                genre="科幻",
                target_total_chapters=80,
            )
            session.add(project)
            session.flush()
            session.add(
                DecisionEvent(
                    project_id=project.id,
                    chapter_number=74,
                    scope="chapter",
                    event_family="audit_action",
                    event_type=DecisionEventType.RETRY_ATTEMPT,
                    actor_type="api",
                    reason=stale_reason,
                    created_at=datetime(2026, 9, 6, 12, 0, 0),
                )
            )
            session.add(
                DecisionEvent(
                    project_id=project.id,
                    chapter_number=74,
                    scope="chapter",
                    event_family="audit_action",
                    event_type=DecisionEventType.RETRY_ATTEMPT,
                    actor_type="api",
                    reason=retry_reason,
                    created_at=datetime(2026, 9, 6, 12, 1, 0),
                )
            )

        with session_factory() as session:
            quality = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=74,
                target_total_chapters=80,
                chapter_title="第74章",
            )

        context = sample_context()
        context.canon_quality_context = quality
        section = _canon_quality_context_section(context)

        assert section is not None
        assert "操作员定向重写约束" in section
        assert retry_reason in section
        assert stale_reason not in section
    finally:
        engine.dispose()
