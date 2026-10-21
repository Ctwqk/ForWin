from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CanonQualitySignalRow, QualityAnalysisRunRow
from forwin.protocol.writer import WriterOutput
from tests.helpers.book_state_seed import character_node, commit_chapter
from tests.postgres import postgres_test_url


class FakeFormClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        return self.payload


def _seed_character_prior(session: Session, project_id: str) -> None:
    commit_chapter(
        session,
        project_id,
        0,
        [character_node(project_id, f"char-{project_id}", "林青", state={"alive_status": "alive"})],
        story_minute=0,
    )


def test_service_asks_book_state_characters_and_persists_no_state() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_form_projection"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            _seed_character_prior(session, project.id)
            quote = "林青倒下，再无呼吸。"
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="第一章",
                body=quote,
                end_of_chapter_summary="林青死亡。",
            )

            client = FakeFormClient(_payload(project.id, 1, character_quote=quote))
            result = analyze_writer_output_quality(
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
            session.commit()

        with session_factory() as session:
            signal_rows = session.query(CanonQualitySignalRow).filter_by(project_id=project.id).all()
        prompt = "\n".join(str(message.get("content", "")) for message in client.calls[0]["messages"])
        assert result.mode == "chapter_review_form"
        assert result.blocking is False
        assert result.raw_analyzer_results[0]["metadata"]["source_mode"] == "chapter_review_form"
        assert "character_transitions" not in result.raw_analyzer_results[0]
        assert '"prior_life_state": "alive"' in prompt
        assert signal_rows == []
    finally:
        engine.dispose()


def test_service_blocks_unasked_character_answer() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_form_rejects_unasked_character"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            quote = "林青倒下，再无呼吸。"

            result = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="第一章",
                    body=quote,
                    end_of_chapter_summary="林青死亡。",
                ),
                draft_id="draft-1",
                persist=True,
                mode="primary",
                llm_client=FakeFormClient(_payload(project.id, 1, character_quote=quote)),
            )
            session.commit()

        assert result.blocking is True
        assert result.signals[0].signal_type == "form_schema_invalid"
        assert result.summary == "Unasked characters answers: 林青"
    finally:
        engine.dispose()


def test_service_rejects_subject_misattribution() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_form_rejects_subject"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            _seed_character_prior(session, project.id)
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
                mode="primary",
                llm_client=FakeFormClient(payload),
            )
            session.commit()

        assert result.blocking is True
        assert result.signals[0].signal_type == "form_answer_rejected"
    finally:
        engine.dispose()


def test_service_supersedes_stale_chapter_signal_when_quote_later_validates() -> None:
    engine = get_engine(postgres_test_url("canon_quality_service_supersedes_stale_quote_signal"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="表单质量", premise="主角：林青。", genre="悬疑", target_total_chapters=3)
            session.add(project)
            session.flush()
            _seed_character_prior(session, project.id)
            quote = "死了。2014年，心脏病发作"

            rejected = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="第一章",
                    body="林青看着苏皖只是摇头，没有继续回答。",
                    end_of_chapter_summary="未确认状态。",
                ),
                draft_id="draft-old",
                persist=True,
                mode="primary",
                llm_client=FakeFormClient(_payload(project.id, 1, character_quote=quote)),
            )
            assert rejected.blocking is False
            assert rejected.signals[0].signal_type == "form_answer_rejected"
            assert rejected.signals[0].severity == "warning"

            accepted = analyze_writer_output_quality(
                session=session,
                project_id=project.id,
                chapter_number=1,
                writer_output=WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="第一章",
                    body=f"林青听见苏皖说：“{quote}。”",
                    end_of_chapter_summary="林青死亡。",
                ),
                draft_id="draft-new",
                persist=True,
                mode="primary",
                llm_client=FakeFormClient(_payload(project.id, 1, character_quote=quote)),
            )
            session.commit()

        with session_factory() as session:
            signal_rows = session.query(CanonQualitySignalRow).filter_by(project_id=project.id).all()
            open_rows = [row for row in signal_rows if row.status == "open"]
            assert accepted.blocking is False
            assert open_rows == []
            assert {row.status for row in signal_rows} == {"superseded"}
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
                mode="primary",
                llm_client=None,
            )

            assert result.blocking is True
            assert result.signals[0].signal_type == "form_llm_unavailable"
            assert result.review_issues[0]["source_mode"] == "chapter_review_form"
            assert session.query(QualityAnalysisRunRow).count() == 0
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
