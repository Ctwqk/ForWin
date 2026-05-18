from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models.canon_quality import CharacterStateTransitionRow
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.writer import WriterOutput


class FakeReviewClient:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        quote = "林青倒下，再无呼吸。"
        return {
            "project_id": self.project_id,
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
                    "participation": {"value": "present_acting"},
                }
            ],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "林青死亡。",
        }


def test_analyze_writer_output_quality_uses_form_only_and_persists_projection() -> None:
    engine = get_engine(postgres_test_url("chapter_review_form_api_flow"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单门禁", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="林青倒下，再无呼吸。",
                end_of_chapter_summary="林青死亡。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=True,
                llm_client=FakeReviewClient(project.id),
                return_raw_analyzer_results=True,
            )
            session.commit()

        with session_factory() as session:
            transitions = session.query(CharacterStateTransitionRow).filter_by(project_id=project.id).all()
            assert result.mode == "chapter_review_form"
            assert result.raw_analyzer_results[0]["analyzer"] == "ChapterReviewForm"
            assert transitions[0].payload_json
            assert "chapter_review_form" in transitions[0].payload_json
    finally:
        engine.dispose()


def test_analyze_writer_output_quality_blocks_when_form_llm_missing() -> None:
    engine = get_engine(postgres_test_url("chapter_review_form_api_missing_llm"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单门禁", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body="林青出现。",
                end_of_chapter_summary="林青出现。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=False,
                llm_client=None,
            )

            assert result.blocking is True
            assert result.signals[0].signal_type == "form_llm_unavailable"
    finally:
        engine.dispose()
