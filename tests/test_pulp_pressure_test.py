from __future__ import annotations

import json

from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.task import GenerationTask
from scripts import pulp_pressure_test
from tests.postgres import postgres_test_url


def test_pressure_report_uses_real_chapter_rows(tmp_path, monkeypatch) -> None:
    database_url = postgres_test_url("pulp-pressure-report")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            project = Project(
                id="project-pressure",
                title="P",
                premise="p",
                genre="都市",
                creation_status="writing",
                target_total_chapters=30,
            )
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                id="arc-1",
                project_id=project.id,
                arc_synopsis="arc",
                status="active",
            )
            session.add(arc)
            session.flush()
            session.add(
                ChapterPlan(
                    id="plan-1",
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    status="accepted",
                    one_line="summary",
                )
            )
            session.add(
                GenerationTask(
                    id="task-1",
                    task_kind="generation",
                    project_id=project.id,
                    status="completed",
                    requested_chapters=1,
                    completed_chapters_json="[1]",
                )
            )

        monkeypatch.setenv("DATABASE_URL", database_url)
        output = tmp_path / "report"

        assert (
            pulp_pressure_test.main(
                ["--project-id", "project-pressure", "--chapters", "1", "--output", str(output)]
            )
            == 0
        )

        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert summary["chapter_count"] == 1
        assert "future versions can replace" not in (
            output / "README.md"
        ).read_text(encoding="utf-8").lower()
    finally:
        engine.dispose()
