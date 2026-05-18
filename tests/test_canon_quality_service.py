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
            project = Project(title="终章门禁", premise="核心系统记忆重置倒计时", genre="悬疑", target_total_chapters=12)
            session.add(project)
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=12,
                title="倒计时：最后一日",
                body="父亲投影闪烁后，陆明确认核心系统和记忆重置的真相，然后走向旧轨深处。",
                end_of_chapter_summary="记忆芯片损坏，陆明被困在封闭的第五层。",
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
            project = Project(title="身份门禁", premise="主角：陆明。", genre="悬疑", target_total_chapters=99)
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
                title="周砚的棋局",
                status="accepted",
            )
            session.add(plan8)
            session.flush()
            draft8 = ChapterDraft(
                chapter_plan_id=plan8.id,
                version=1,
                body_text="韩青出手相助，两人逃入地下检修线第五层。她从一开始就是周砚派来接近陆明的人。",
                summary="韩青出手相助，她从一开始就是周砚派来接近陆明的人。",
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
                body="陆明得知，韩青是自己叔叔。这个自称是他叔叔的男人把钥匙交给他。",
                end_of_chapter_summary="韩青是自己叔叔。",
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


def test_service_does_not_persist_false_gender_from_committed_object_pronoun() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_identity_object_pronoun"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="身份门禁", premise="主角：陆明。", genre="悬疑", target_total_chapters=99)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=30,
                chapter_end=36,
                arc_synopsis="身份连续性 arc",
            )
            session.add(arc)
            session.flush()
            plan34 = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=34,
                title="核心系统核心层的审判",
                status="accepted",
            )
            session.add(plan34)
            session.flush()
            draft34 = ChapterDraft(
                chapter_plan_id=plan34.id,
                version=1,
                body_text=(
                    "韩青坐在靠墙的金属椅上，手腕被磁力束带扣在扶手上，"
                    "抬头看见他的表情没有惊讶，只有一种疲惫。"
                ),
                summary="韩青被关在羁押室。",
            )
            session.add(draft34)
            session.flush()
            review34 = ChapterReview(draft_id=draft34.id, verdict="pass")
            session.add(review34)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan34.id,
                    chapter_number=34,
                    candidate_draft_id=draft34.id,
                    review_id=review34.id,
                    version=1,
                    status="canon_committed",
                    canon_status="canon",
                )
            )
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=35,
                title="倒计时的终止条件",
                body="韩青抬起头，她说自己愿意承担代价，但陆明拒绝牺牲她的记忆。",
                end_of_chapter_summary="韩青愿意授权，她仍然清醒。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=35,
                writer_output=output,
                draft_id="draft-35",
                persist=False,
            )

            assert not [signal for signal in result.signals if signal.signal_type == "identity_gender_conflict"]
    finally:
        engine.dispose()


def test_service_rebuilds_recent_countdown_context_when_ledger_is_missing() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_countdown_body_fallback"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(
                title="终章倒计时门禁",
                premise="主角：陆明。主线倒计时必须单调减少。",
                genre="悬疑",
                target_total_chapters=36,
            )
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=3,
                chapter_start=25,
                chapter_end=36,
                arc_synopsis="终段 arc",
            )
            session.add(arc)
            session.flush()
            plan35 = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=35,
                title="倒计时的终止条件",
                status="accepted",
            )
            session.add(plan35)
            session.flush()
            draft35 = ChapterDraft(
                chapter_plan_id=plan35.id,
                version=1,
                body_text=(
                    "陆明穿过检修通道，终端屏幕上，记忆重置倒计时闪烁着猩红的数字：08:12。"
                    "他回头看向韩青。倒计时：07:03。"
                ),
                summary="陆明确认记忆重置倒计时进入最后阶段。",
            )
            session.add(draft35)
            session.flush()
            review35 = ChapterReview(draft_id=draft35.id, verdict="pass")
            session.add(review35)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan35.id,
                    chapter_number=35,
                    candidate_draft_id=draft35.id,
                    review_id=review35.id,
                    version=1,
                    status="canon_committed",
                    canon_status="canon",
                )
            )
            session.flush()

            output = WriterOutput(
                project_id=project.id,
                chapter_number=36,
                title="终章归档",
                body=(
                    "倒计时悬浮在视野右上角——28:00，27:59，27:58。"
                    "陆明启动核心系统关闭协议。倒计时归零的那一刻，整座城市的灯光同时熄灭。"
                ),
                end_of_chapter_summary="陆明关闭核心系统，记忆重置周期结束。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=36,
                writer_output=output,
                draft_id="draft-36",
                persist=False,
                mode="deterministic",
            )

            countdown_conflicts = [
                signal
                for signal in result.signals
                if signal.signal_type == "countdown_non_monotonic"
                and signal.subject_key == "countdown:memory_reset"
            ]
            assert countdown_conflicts
            assert countdown_conflicts[0].payload["previous_minutes"] == 7
    finally:
        engine.dispose()


def test_service_blocks_unbridged_custody_regression_from_recent_canon() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_custody_regression"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="羁押连续性", premise="主角：陆明。", genre="悬疑", target_total_chapters=36)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=3,
                chapter_start=25,
                chapter_end=36,
                arc_synopsis="终段 arc",
            )
            session.add(arc)
            session.flush()
            plan30 = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=30,
                title="档案署的清算",
                status="accepted",
            )
            session.add(plan30)
            session.flush()
            draft30 = ChapterDraft(
                chapter_plan_id=plan30.id,
                version=1,
                body_text="陆明利用父亲留下的硬件后门救出被关押的韩青，两人逃出审讯室。",
                summary="陆明救出韩青，两人发现紧急终止协议需要共同进入核心系统底层核心机房。",
            )
            session.add(draft30)
            session.flush()
            review30 = ChapterReview(draft_id=draft30.id, verdict="pass")
            session.add(review30)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan30.id,
                    chapter_number=30,
                    candidate_draft_id=draft30.id,
                    review_id=review30.id,
                    version=1,
                    status="canon_committed",
                    canon_status="canon",
                )
            )
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=31,
                title="旧城集体记忆震荡",
                body="韩青被关在底层牢房里，双手被束缚带固定在管道上。陆明在走廊外寻找开门方式。",
                end_of_chapter_summary="陆明发现韩青仍被关在底层牢房。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=31,
                writer_output=output,
                draft_id="draft-31",
                persist=False,
                mode="deterministic",
            )

            assert any(signal.signal_type == "custody_state_regression" for signal in result.signals)
            assert any(
                signal.signal_type == "custody_state_regression" and signal.severity == "error"
                for signal in result.signals
            )
    finally:
        engine.dispose()


def test_service_supersedes_stale_same_chapter_signals_on_reanalysis() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_supersedes_same_chapter_signals"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="质量重跑", premise="主角：陆明。", genre="悬疑", target_total_chapters=99)
            session.add(project)
            session.flush()
            bad_output = WriterOutput(
                project_id=project.id,
                chapter_number=12,
                title="第十二章",
                body="档案签名人：相关人员。倒计时还有59分钟。",
                end_of_chapter_summary="测试",
            )
            analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=12,
                writer_output=bad_output,
                draft_id="draft-bad",
                persist=True,
            )
            session.commit()

        with session_factory() as session:
            fixed_output = WriterOutput(
                project_id=project.id,
                chapter_number=12,
                title="第十二章",
                body="陆明确认档案签名来自周清和，随后关闭核心系统记忆重置倒计时，危机解除。",
                end_of_chapter_summary="陆明关闭记忆重置倒计时。",
            )
            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=12,
                writer_output=fixed_output,
                draft_id="draft-fixed",
                persist=True,
            )
            session.commit()

        with session_factory() as session:
            open_signals = CanonQualityRepository(session).list_open_signals(project.id, before_chapter=13)

        assert not [signal for signal in result.signals if signal.signal_type == "placeholder_leakage"]
        assert not [signal for signal in open_signals if signal.signal_type == "placeholder_leakage"]
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
                premise="核心系统记忆重置倒计时",
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
                body="陆明逃离时发现钟塔钥匙已断裂，追兵逼近。",
                end_of_chapter_summary="陆明获得芯片，但被系统巡检员伏击，钥匙断裂，追兵逼近。",
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


def test_service_does_not_treat_last_materialized_arc_chapter_as_book_final_when_target_total_remains() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_not_final_with_target"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(
                title="六十章项目",
                premise="核心系统记忆重置倒计时",
                genre="悬疑",
                target_total_chapters=60,
            )
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=1,
                chapter_end=12,
                arc_synopsis="第一 arc",
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
                body="陆明中止了一轮重置，又看见父亲投影留下新的警告。",
                end_of_chapter_summary="核心系统的局部重置被中止，但父亲提示更深层的重置才刚开始。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=12,
                writer_output=output,
                draft_id="draft-12",
                persist=False,
            )

            assert not any(signal.signal_type.startswith("final_") for signal in result.signals)
    finally:
        engine.dispose()


def test_service_does_not_treat_last_materialized_arc_chapter_as_book_final_without_final_label() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_not_final_materialized"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="中段项目", premise="核心系统记忆重置倒计时", genre="悬疑", target_total_chapters=0)
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
                body="陆明发现系统巡检员追来，转身没入夜色中。",
                end_of_chapter_summary="陆明被追兵逼近，下一步必须进入地下检修线。",
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
