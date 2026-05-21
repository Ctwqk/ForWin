from __future__ import annotations

import json

from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.governance import DecisionEvent
from forwin.models.observability import PerformanceSpan
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
            session.add_all(
                [
                    ChapterPlan(
                        id="plan-1",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=1,
                        title="第一章",
                        status="accepted",
                        one_line="summary",
                        experience_plan_json=json.dumps(
                            {
                                "selected_template_ids": ["trope-a"],
                                "planned_reward_tags": ["power"],
                                "visible_payoff": "到账",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    ChapterPlan(
                        id="plan-2",
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=2,
                        title="第二章",
                        status="accepted",
                        one_line="summary",
                        experience_plan_json=json.dumps(
                            {
                                "selected_template_ids": ["trope-a"],
                                "planned_reward_tags": ["power"],
                                "visible_payoff": "缺失",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ]
            )
            session.add(
                GenerationTask(
                    id="task-1",
                    task_kind="generation",
                    project_id=project.id,
                    status="completed",
                    requested_chapters=2,
                    completed_chapters_json="[1, 2]",
                )
            )
            session.add_all(
                [
                    DecisionEvent(
                        project_id=project.id,
                        chapter_number=1,
                        event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                        payload_json=json.dumps(
                            {
                                "pulp_beat": {
                                    "visible_payoff_present": True,
                                    "missing_fields": [],
                                }
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    DecisionEvent(
                        project_id=project.id,
                        chapter_number=2,
                        event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                        payload_json=json.dumps(
                            {
                                "pulp_beat": {
                                    "visible_payoff_present": False,
                                    "missing_fields": ["visible_payoff_present"],
                                }
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    DecisionEvent(
                        project_id=project.id,
                        chapter_number=2,
                        event_type=DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
                        payload_json=json.dumps(
                            {
                                "task_type": "structured_extraction",
                                "structured_extraction": "partial_degraded",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    PerformanceSpan(
                        project_id=project.id,
                        chapter_number=1,
                        span_name="llm.writer",
                        span_kind="llm",
                        duration_ms=1000,
                        metrics_json=json.dumps(
                            {
                                "prompt_char_count": 100,
                                "context_pack_char_count": 50,
                            }
                        ),
                    ),
                    PerformanceSpan(
                        project_id=project.id,
                        chapter_number=2,
                        span_name="llm.writer",
                        span_kind="llm",
                        duration_ms=2000,
                        metrics_json=json.dumps(
                            {
                                "prompt_char_count": 120,
                                "context_pack_char_count": 70,
                            }
                        ),
                    ),
                ]
            )

        monkeypatch.setenv("DATABASE_URL", database_url)
        output = tmp_path / "report"

        assert (
            pulp_pressure_test.main(
                    ["--project-id", "project-pressure", "--chapters", "2", "--output", str(output)]
            )
            == 0
        )

        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        assert summary["chapter_count"] == 2
        assert summary["avg_llm_calls_per_chapter"] == 1
        assert summary["p95_wall_time_seconds"] == 2
        assert summary["prompt_char_count_slope"] == 20
        assert summary["context_pack_char_count_slope"] == 20
        assert summary["visible_payoff_missing_rate"] == 0.5
        assert summary["canon_extraction_failure_rate"] == 1
        assert summary["repeat_trope_template_rate"] == 0.5
        assert summary["repeat_trope_category_rate"] == 0.5
        assert "future versions can replace" not in (
            output / "README.md"
        ).read_text(encoding="utf-8").lower()
    finally:
        engine.dispose()
