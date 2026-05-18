from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CharacterStateTransitionRow
from forwin.protocol.writer import WriterOutput


class FakeFormClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        return self.payload


def test_service_persists_validated_form_projection() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_form_projection"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            quote = "林青倒下，再无呼吸。"
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body=quote,
                end_of_chapter_summary="林青死亡。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=True,
                llm_client=FakeFormClient(_payload(project.id, 1, character_quote=quote)),
                return_raw_analyzer_results=True,
            )
            session.commit()

        with session_factory() as session:
            rows = session.query(CharacterStateTransitionRow).filter_by(project_id=project.id).all()
            assert result.mode == "chapter_review_form"
            assert result.blocking is False
            assert result.raw_analyzer_results[0]["metadata"]["source_mode"] == "chapter_review_form"
            assert rows[0].character_name == "林青"
            assert "chapter_review_form" in rows[0].payload_json
    finally:
        engine.dispose()


def test_service_rejects_subject_misattribution_without_state_write() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_form_rejects_subject"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            quote = "林青和委员会高层的合谋导致家族成员死亡。"
            payload = _payload(
                project.id,
                1,
                character_quote=quote,
                subject_of_quote="家族成员",
            )
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body=quote,
                end_of_chapter_summary="家族成员死亡。",
            )

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                draft_id="draft-1",
                persist=True,
                llm_client=FakeFormClient(payload),
            )
            session.commit()

        with session_factory() as session:
            rows = session.query(CharacterStateTransitionRow).filter_by(project_id=project.id).all()
            assert result.blocking is True
            assert result.signals[0].signal_type == "form_answer_rejected"
            assert rows == []
    finally:
        engine.dispose()


def test_service_blocks_when_form_llm_unavailable() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_form_missing_llm"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
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
            assert result.review_issues[0]["source_mode"] == "chapter_review_form"
    finally:
        engine.dispose()


def _payload(
    project_id: str,
    chapter_number: int,
    *,
    character_quote: str,
    subject_of_quote: str = "林青",
) -> dict:
    return {
        "project_id": project_id,
        "chapter_number": chapter_number,
        "form_schema_version": FORM_SCHEMA_VERSION,
        "characters": [
            {
                "name": "林青",
                "appears_in_chapter": True,
                "life_state": {
                    "value": "dead",
                    "evidence_quote": character_quote,
                    "subject_of_quote": subject_of_quote,
                    "confidence": 0.95,
                },
                "custody_state": {"value": "unknown"},
                "participation": {"value": "mentioned_only"},
            }
        ],
        "countdowns": [],
        "obligations": [],
        "open_signals": [],
        "new_observations": {},
        "chapter_summary": "表单测试。",
    }
