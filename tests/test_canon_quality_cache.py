from __future__ import annotations

import json

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import ArcPlanVersion, ChapterPlan, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CanonQualitySignalRow, QualityAnalysisRunRow
from forwin.protocol.writer import WriterOutput
from tests.helpers.book_state_seed import character_node, clock_node, commit_chapter
from tests.postgres import postgres_test_url


class CountingFormClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.calls = 0
        self.payload = payload

    def complete_json(self, **_kwargs):
        self.calls += 1
        return self.payload


def test_quality_cache_reuses_exact_input_and_invalidates_content_or_plan_changes() -> None:
    engine = get_engine(postgres_test_url("canon-quality-analysis-cache"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(
                title="质量缓存",
                premise="主角：林青。",
                genre="悬疑",
                target_total_chapters=3,
            )
            session.add(project)
            session.flush()
            commit_chapter(
                session,
                project.id,
                0,
                [
                    character_node(project.id, f"char-{project.id}", "林青", state={"alive_status": "alive"}),
                    clock_node(project.id, f"clock-{project.id}", "倒计时", due_at=60),
                ],
                story_minute=0,
            )
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_synopsis="第一卷",
                chapter_start=1,
                chapter_end=3,
            )
            session.add(arc)
            session.flush()
            plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="第一章",
                one_line="林青遭遇致命事件。",
            )
            session.add(plan)
            session.flush()
            quote = "林青倒下，再无呼吸。"
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body=quote,
                end_of_chapter_summary="林青死亡。",
            )
            client = CountingFormClient(_payload(project.id, quote, clock_key=f"clock-{project.id}"))

            draft_review = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                persist=False,
                mode="primary",
                llm_client=client,
                return_raw_analyzer_results=True,
            )
            canon_gate = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=True,
                mode="primary",
                llm_client=client,
                return_raw_analyzer_results=True,
            )
            plan.one_line = "林青遭遇致命事件并留下线索。"
            session.flush()
            analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                persist=False,
                mode="primary",
                llm_client=client,
            )
            analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output.model_copy(update={"body": f"{quote}余波仍在扩散。"}),
                persist=False,
                mode="primary",
                llm_client=client,
            )
            session.commit()

            signal_rows = session.query(CanonQualitySignalRow).filter_by(project_id=project.id).all()
            runs = session.query(QualityAnalysisRunRow).all()
            assert client.calls == 3
            assert draft_review.summary == canon_gate.summary
            assert canon_gate.draft_id == "draft-1"
            assert [signal.signal_type for signal in canon_gate.signals] == ["form_countdown_inconsistency"]
            assert canon_gate.signals[0].payload["draft_id"] == "draft-1"
            assert [(row.signal_type, row.draft_id) for row in signal_rows] == [
                ("form_countdown_inconsistency", "draft-1")
            ]
            assert len(runs) == 3
            assert all(json.loads(row.result_json)["analysis"]["draft_id"] == "" for row in runs)
    finally:
        engine.dispose()


def _payload(project_id: str, quote: str, *, clock_key: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "chapter_number": 1,
        "form_schema_version": FORM_SCHEMA_VERSION,
        "characters": [
            {
                "name": "林青",
                "appears_in_chapter": True,
                "life_state": {
                    "value": "dead",
                    "evidence_quote": quote,
                    "subject_of_quote": "林青",
                    "confidence": 0.95,
                },
                "custody_state": {"value": "unknown"},
                "participation": {"value": "mentioned_only"},
            }
        ],
        "countdowns": [
            {
                "key": clock_key,
                "mentioned_in_chapter": True,
                "status_in_this_chapter": {"value": "active", "evidence_quote": quote, "confidence": 0.95},
                "consistent_with_prior": {"value": "false", "evidence_quote": quote, "confidence": 0.95},
            }
        ],
        "obligations": [],
        "open_signals": [],
        "new_observations": {},
        "chapter_summary": "表单测试。",
    }
