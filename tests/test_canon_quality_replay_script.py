from __future__ import annotations

from pathlib import Path

from forwin.models import ArcPlanVersion, ChapterDraft, ChapterPlan, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from scripts.replay_canon_quality_for_project import replay_project


def test_replay_script_writes_markdown_report(tmp_path: Path) -> None:
    engine = get_engine(postgres_test_url("canon_quality_replay"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="回放测试", premise="六十份档案", genre="悬疑", target_total_chapters=1)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(project_id=project.id, arc_synopsis="测试", status="active")
            session.add(arc)
            session.flush()
            plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                one_line="测试",
                status="accepted",
            )
            session.add(plan)
            session.flush()
            session.add(
                ChapterDraft(
                    chapter_plan_id=plan.id,
                    version=1,
                    body_text="签名人：相关人员。倒计时还有59分钟。",
                    summary="测试",
                    char_count=20,
                    llm_raw_response="{}",
                )
            )
            session.commit()
            project_id = project.id

        output_path = tmp_path / "report.md"
        signal_count = replay_project(
            session_factory=session_factory,
            project_id=project_id,
            from_chapter=1,
            to_chapter=1,
            output_path=output_path,
            persist=False,
        )

        text = output_path.read_text(encoding="utf-8")
        assert signal_count >= 1
        assert "placeholder_leakage" in text
    finally:
        engine.dispose()
