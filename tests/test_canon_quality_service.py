from __future__ import annotations

from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import Project
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
