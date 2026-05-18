from __future__ import annotations

from forwin.canon_quality.repository import CanonQualityRepository
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.canon_quality import CharacterStateTransitionRow, CountdownLedgerRow
from tests.postgres import postgres_test_url


def test_repository_excludes_superseded_legacy_rows_by_default() -> None:
    engine = get_engine(postgres_test_url("legacy_canon_supersede"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="Legacy Canon", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            session.add_all(
                [
                    CharacterStateTransitionRow(
                        project_id=project.id,
                        character_name="林青",
                        chapter_number=1,
                        transition_type="life_state",
                        from_state="unknown",
                        to_state="dead",
                        payload_json='{"source":"legacy_analyzer","superseded_by":"chapter_review_form_migration"}',
                    ),
                    CharacterStateTransitionRow(
                        project_id=project.id,
                        character_name="林青",
                        chapter_number=2,
                        transition_type="life_state",
                        from_state="alive",
                        to_state="wounded",
                        payload_json='{"source":"chapter_review_form"}',
                    ),
                    CountdownLedgerRow(
                        project_id=project.id,
                        countdown_key="main",
                        label="主倒计时",
                        chapter_number=1,
                        normalized_remaining_minutes=30,
                        raw_mention="三十分钟",
                        payload_json='{"source":"legacy_analyzer","superseded_by":"chapter_review_form_migration"}',
                    ),
                    CountdownLedgerRow(
                        project_id=project.id,
                        countdown_key="main",
                        label="主倒计时",
                        chapter_number=2,
                        normalized_remaining_minutes=20,
                        raw_mention="二十分钟",
                        payload_json='{"source":"chapter_review_form"}',
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            repo = CanonQualityRepository(session)
            transitions = repo.list_character_transitions(project.id)
            countdowns = repo.list_countdown_entries(project.id, include_details=True)
            all_transitions = repo.list_character_transitions(project.id, include_superseded=True)
            all_countdowns = repo.list_countdown_entries(project.id, include_details=True, include_superseded=True)

        assert [item["to_state"] for item in transitions] == ["wounded"]
        assert [item["normalized_remaining_minutes"] for item in countdowns] == [20]
        assert [item["payload"]["source"] for item in all_transitions] == ["legacy_analyzer", "chapter_review_form"]
        assert all_transitions[0]["payload"]["superseded_by"] == "chapter_review_form_migration"
        assert [item["payload"]["source"] for item in all_countdowns] == ["legacy_analyzer", "chapter_review_form"]
        assert all_countdowns[0]["payload"]["superseded_by"] == "chapter_review_form_migration"
    finally:
        engine.dispose()


def test_migration_marks_only_non_form_rows() -> None:
    from scripts.migrate_legacy_canon_to_form import is_form_sourced, mark_payload_superseded, summarize_rows

    form_payload = {"source": "chapter_review_form"}
    legacy_payload = {"source": "legacy_analyzer", "note": "keep"}

    assert is_form_sourced(form_payload) is True
    assert is_form_sourced(legacy_payload) is False
    assert mark_payload_superseded(form_payload) == form_payload
    assert mark_payload_superseded(legacy_payload) == {
        "source": "legacy_analyzer",
        "note": "keep",
        "superseded_by": "chapter_review_form_migration",
    }
    assert summarize_rows([{"payload": form_payload}, {"payload": legacy_payload}]) == {
        "form_sourced": 1,
        "legacy_sourced": 1,
        "already_superseded": 0,
        "total": 2,
    }
