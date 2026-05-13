from __future__ import annotations

from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import ArcPlanVersion, CandidateDraftRecord, ChapterDraft, ChapterPlan, ChapterReview, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.writer import WriterOutput


def test_service_collects_and_persists_blocking_signals() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="质量门禁", premise="六十份档案倒计时", genre="悬疑", target_total_chapters=1)
            session.add(project)
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="档案签名人：相关人员。倒计时还有59分钟。",
                end_of_chapter_summary="测试",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=True,
            )
            session.commit()

        with session_factory() as session:
            open_signals = CanonQualityRepository(session).list_open_signals(project.id, before_chapter=2)
            assert any(signal.signal_type == "placeholder_leakage" for signal in result.signals)
            assert any(signal.signal_type == "final_countdown_unresolved" for signal in result.signals)
            assert len(open_signals) >= 2
    finally:
        engine.dispose()


def test_service_passes_final_title_and_summary_into_completion_gate() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_final_summary"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="终章门禁", premise="白塔记忆重置倒计时", genre="悬疑", target_total_chapters=12)
            session.add(project)
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=12,
                title="倒计时：最后一日",
                body="父亲投影闪烁后，林澈确认白塔和记忆重置的真相，然后走向旧轨深处。",
                end_of_chapter_summary="记忆芯片损坏，林澈被困在封闭的第五层。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=12,
                writer_output=output,
                draft_id="draft-final",
                persist=False,
            )

            assert any(signal.signal_type == "final_hook_unresolved" for signal in result.signals)
    finally:
        engine.dispose()


def test_service_blocks_gender_drift_from_committed_previous_chapters() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_identity_gender"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="身份门禁", premise="主角：林澈。", genre="悬疑", target_total_chapters=99)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=12,
                arc_synopsis="身份连续性 arc",
            )
            session.add(arc)
            session.flush()
            plan8 = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=8,
                title="洛庭若的棋局",
                status="accepted",
            )
            session.add(plan8)
            session.flush()
            draft8 = ChapterDraft(
                chapter_plan_id=plan8.id,
                version=1,
                body_text="沈宴秋出手相助，两人逃入地下旧轨第五层。她从一开始就是洛庭若派来接近林澈的人。",
                summary="沈宴秋出手相助，她从一开始就是洛庭若派来接近林澈的人。",
            )
            session.add(draft8)
            session.flush()
            review8 = ChapterReview(draft_id=draft8.id, verdict="pass")
            session.add(review8)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan8.id,
                    chapter_number=8,
                    candidate_draft_id=draft8.id,
                    review_id=review8.id,
                    version=1,
                    status="canon_committed",
                    canon_status="canon",
                )
            )
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=12,
                title="倒计时：最后一日",
                body="林澈得知，沈宴秋是自己叔叔。这个自称是他叔叔的男人把钥匙交给他。",
                end_of_chapter_summary="沈宴秋是自己叔叔。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=12,
                writer_output=output,
                draft_id="draft-12",
                persist=False,
            )

            assert any(signal.signal_type == "identity_gender_conflict" for signal in result.signals)
    finally:
        engine.dispose()


def test_service_treats_last_materialized_chapter_as_final_when_target_total_is_stale() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_final_materialized"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(
                title="终章门禁",
                premise="白塔记忆重置倒计时",
                genre="悬疑",
                target_total_chapters=0,
            )
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=12,
                arc_synopsis="终章 arc",
            )
            session.add(arc)
            session.flush()
            session.add(
                ChapterPlan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=12,
                    title="倒计时：最后一日",
                )
            )
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=12,
                title="倒计时：最后一日",
                body="林澈逃离时发现潮汐钟楼钥匙已断裂，追兵逼近。",
                end_of_chapter_summary="林澈获得芯片，但被白塔巡检员伏击，钥匙断裂，追兵逼近。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=12,
                writer_output=output,
                draft_id="draft-final",
                persist=False,
            )

            assert any(signal.signal_type == "final_hook_unresolved" for signal in result.signals)
    finally:
        engine.dispose()


def test_service_does_not_treat_last_materialized_arc_chapter_as_book_final_without_final_label() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_not_final_materialized"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="中段项目", premise="白塔记忆重置倒计时", genre="悬疑", target_total_chapters=0)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=8,
                arc_synopsis="中段 arc",
            )
            session.add(arc)
            session.flush()
            session.add(
                ChapterPlan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=8,
                    title="旧轨夹击",
                )
            )
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=8,
                title="旧轨夹击",
                body="林澈发现白塔巡检员追来，转身没入夜色中。",
                end_of_chapter_summary="林澈被追兵逼近，下一步必须进入地下旧轨。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=8,
                writer_output=output,
                draft_id="draft-mid",
                persist=False,
            )

            assert not any(signal.signal_type.startswith("final_") for signal in result.signals)
    finally:
        engine.dispose()
