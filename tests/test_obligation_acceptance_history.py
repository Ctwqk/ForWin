from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.models.audit import DecisionEvent
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation
from tests import test_canon_quality_acceptance_evidence as quality_fixtures
from tests.test_canon_quality_acceptance_evidence import _commit

quality_session = quality_fixtures.quality_session


def _activate(session, *, origin=1, obligation_id="promise"):
    repo = NarrativeObligationRepository(session)
    item = repo.create_obligation(
        NarrativeObligation(
            id=obligation_id,
            project_id="book",
            origin_chapter_number=origin,
            origin_draft_id=f"draft-{origin}",
            status="planned",
            obligation_type="motivation_gap",
            summary="Explain why Lin helped",
            deadline_chapter=3,
            payoff_test="Give a concrete reason",
        )
    )
    commit = _commit(session, origin)
    repo.activate_planned_for_chapter(
        "book",
        origin_chapter_number=origin,
        acceptance_id=commit.id,
        draft_id=f"draft-{origin}",
    )
    return item, commit


@pytest.mark.parametrize("operation", ["resolve", "expire", "waive"])
def test_later_lifecycle_change_restores_earlier_unresolved_prefix_without_deleting_history(
    quality_session, operation
):
    session = quality_session
    item, first = _activate(session)
    second = _commit(session, 2)
    third = _commit(session, 3)
    session.get(Project, "book").book_revision = 3
    repo = NarrativeObligationRepository(session)
    if operation == "resolve":
        repo.mark_obligation_resolved(
            item.id,
            verifier_result={"status": "pass"},
            evidence_refs=["chapter:3"],
            resolution_chapter=3,
        )
    elif operation == "expire":
        repo.expire_obligation(item.id, reason="deadline passed", chapter_number=3)
        repo.block_expired_obligation(item.id, chapter_number=3)
    else:
        repo.waive_obligation(item.id, reason="operator exception", actor="operator")
    assert repo.list_active_for_context("book", chapter_number=4) == []
    events_before = list(session.scalars(select(DecisionEvent.id)))
    repo.restore_prefix(
        project_id="book",
        active_commit_ids={1: first.id, 2: second.id, 3: third.id},
        from_chapter=2,
    )
    restored = repo.list_active_for_context("book", chapter_number=2)
    assert [entry.id for entry in restored] == [item.id]
    assert restored[0].status == "active"
    assert restored[0].resolution_chapter == 0
    assert restored[0].resolution_evidence_refs == []
    assert restored[0].waive_reason == ""
    assert list(session.scalars(select(DecisionEvent.id))) == events_before
    assert session.get(NarrativeObligationRow, item.id) is not None


def test_changed_suffix_cannot_inherit_old_resolution_and_can_replay_new_resolution(
    quality_session,
):
    session = quality_session
    item, first = _activate(session)
    second = _commit(session, 2)
    third = _commit(session, 3)
    repo = NarrativeObligationRepository(session)
    repo.mark_obligation_resolved(
        item.id,
        verifier_result={"status": "pass", "source": "old"},
        evidence_refs=["old-chapter:3"],
        resolution_chapter=3,
    )
    repo.restore_prefix(
        project_id="book",
        active_commit_ids={1: first.id, 2: second.id, 3: third.id},
        from_chapter=2,
    )
    assert repo.list_active_for_context("book", chapter_number=3)[0].must_resolve_now
    # A new candidate chapter 3 must produce its own resolution; old history stays.
    replacement = _commit(session, 3, revision=2)
    repo.set_candidate_acceptance(
        project_id="book",
        chapter_number=3,
        acceptance_id=replacement.id,
        draft_id="draft-3",
    )
    repo.mark_obligation_resolved(
        item.id,
        verifier_result={"status": "pass", "source": "new"},
        evidence_refs=["new-chapter:3"],
        resolution_chapter=3,
    )
    assert repo.list_active_for_context("book", chapter_number=4) == []
    events = [
        json.loads(row.payload_json) for row in session.scalars(select(DecisionEvent))
    ]
    assert any(event.get("effect_acceptance_id") == third.id for event in events)
    assert any(event.get("effect_acceptance_id") == replacement.id for event in events)


def test_activation_binds_only_the_accepted_draft_and_suffix_obligation_reverts_to_planned(
    quality_session,
):
    session = quality_session
    repo = NarrativeObligationRepository(session)
    repo.create_obligation(
        NarrativeObligation(
            id="other-draft",
            project_id="book",
            origin_chapter_number=2,
            origin_draft_id="other",
            status="planned",
            obligation_type="motivation_gap",
            summary="Unaccepted debt",
            deadline_chapter=3,
            payoff_test="Show reason",
        )
    )
    item, second = _activate(session, origin=2)
    assert session.get(NarrativeObligationRow, "other-draft").status == "planned"
    repo.restore_prefix(
        project_id="book", active_commit_ids={2: second.id}, from_chapter=2
    )
    assert session.get(NarrativeObligationRow, item.id).status == "planned"
    assert repo.list_active_for_context("book", chapter_number=3) == []


def test_missing_old_origin_or_tampered_current_row_is_unknown_not_reconstructed(
    quality_session,
):
    session = quality_session
    item, first = _activate(session)
    repo = NarrativeObligationRepository(session)
    row = session.get(NarrativeObligationRow, item.id)
    row.summary = "unrecorded mutation"
    session.flush()
    with pytest.raises(ValueError, match="before|after|changed|provenance"):
        repo.restore_prefix(
            project_id="book", active_commit_ids={1: first.id}, from_chapter=2
        )
    assert row.summary == "unrecorded mutation"


def test_legacy_resolved_row_without_origin_evidence_fails_closed(quality_session):
    session = quality_session
    first = _commit(session)
    session.add(
        NarrativeObligationRow(
            id="legacy",
            project_id="book",
            origin_chapter_number=1,
            origin_draft_id="draft-1",
            status="resolved",
            resolution_chapter=3,
        )
    )
    session.flush()
    with pytest.raises(ValueError, match="provenance|origin|history"):
        NarrativeObligationRepository(session).restore_prefix(
            project_id="book", active_commit_ids={1: first.id}, from_chapter=2
        )
    assert session.get(NarrativeObligationRow, "legacy").status == "resolved"


def test_lifecycle_update_and_before_image_rollback_together(quality_session):
    session = quality_session
    item, _first = _activate(session)
    _commit(session, 2)
    session.commit()
    count = len(list(session.scalars(select(DecisionEvent.id))))
    with pytest.raises(RuntimeError, match="abort"), session.begin_nested():
        NarrativeObligationRepository(session).mark_obligation_resolved(
            item.id,
            verifier_result={"status": "pass"},
            evidence_refs=["chapter:2"],
            resolution_chapter=2,
        )
        raise RuntimeError("abort")
    assert session.get(NarrativeObligationRow, item.id).status == "active"
    assert len(list(session.scalars(select(DecisionEvent.id)))) == count


def test_candidate_acceptance_overlay_keeps_new_origin_visible_without_a_canon_record(
    quality_session,
):
    from forwin.models.project import ChapterPlan

    session = quality_session
    repo = NarrativeObligationRepository(session)
    repo.create_obligation(
        NarrativeObligation(
            id="candidate-debt",
            project_id="book",
            origin_chapter_number=2,
            origin_draft_id="draft-2",
            status="planned",
            obligation_type="motivation_gap",
            summary="Candidate debt",
            deadline_chapter=3,
            payoff_test="Reason",
        )
    )
    repo.set_candidate_acceptance(
        project_id="book",
        chapter_number=2,
        acceptance_id="future-acceptance",
        draft_id="draft-2",
    )
    repo.activate_planned_for_chapter(
        "book",
        origin_chapter_number=2,
        acceptance_id="future-acceptance",
        draft_id="draft-2",
    )
    assert session.get(ChapterPlan, "chapter-2").active_commit_id is None
    assert [
        row.id for row in repo.list_active_for_context("book", chapter_number=3)
    ] == ["candidate-debt"]


def test_acceptance_activation_cannot_omit_or_misattribute_draft(quality_session):
    session = quality_session
    repo = NarrativeObligationRepository(session)
    commit = _commit(session)
    with pytest.raises(ValueError, match="draft|ownership"):
        repo.activate_planned_for_chapter(
            "book", origin_chapter_number=1, acceptance_id=commit.id
        )
    with pytest.raises(ValueError, match="draft|ownership"):
        repo.set_candidate_acceptance(
            project_id="book",
            chapter_number=1,
            acceptance_id="future",
            draft_id="draft-2",
        )


def _historical_form(*, body="Body 2", value="fulfilled", quote="Body 2"):
    import hashlib

    body_hash = hashlib.sha256(body.encode()).hexdigest()
    return {
        "review": {
            "project_id": "book",
            "chapter_number": 2,
            "draft_id": "draft-2",
            "blocking": False,
            "form": {"obligations": [{"id": "promise"}]},
            "answers": {
                "obligations": [
                    {
                        "id": "promise",
                        "addressed": {
                            "value": value,
                            "confidence": 0.95,
                            "evidence_quote": quote,
                        },
                    }
                ]
            },
            "validation_report": {"rejected": []},
        },
        "checks": [
            {
                "dimension": dimension,
                "status": "pass",
                "evidence_refs": [f"body:{body_hash}#0:{len(body)}"],
            }
            for dimension in (
                "possession",
                "knowledge",
                "life_state",
                "time",
                "place",
                "obligations",
            )
        ],
    }


def test_fresh_reviewed_fulfillment_resolves_only_with_exact_body_and_candidate_identity(
    quality_session,
):
    session = quality_session
    item, _ = _activate(session)
    repo = NarrativeObligationRepository(session)
    repo.set_candidate_acceptance(
        project_id="book",
        chapter_number=2,
        acceptance_id="future-acceptance",
        draft_id="draft-2",
    )
    assert repo.apply_reviewed_resolutions(
        project_id="book",
        chapter_number=2,
        chapter_body="Body 2",
        historical_form=_historical_form(),
    ) == [item.id]
    assert repo.list_active_for_context("book", chapter_number=3) == []
    row = session.get(NarrativeObligationRow, item.id)
    assert row.resolution_evidence_refs_json.startswith('["body:')
    event = session.scalar(
        select(DecisionEvent).where(
            DecisionEvent.id
            == json.loads(row.metadata_json)["_canon_obligation_provenance"][
                "head_event_id"
            ]
        )
    )
    assert json.loads(event.payload_json)["effect_acceptance_id"] == "future-acceptance"


@pytest.mark.parametrize(
    "value,quote", [("unaddressed", "Body 2"), ("fulfilled", "invented payoff")]
)
def test_candidate_does_not_inherit_resolution_without_valid_fresh_fulfillment(
    quality_session, value, quote
):
    session = quality_session
    item, _ = _activate(session)
    repo = NarrativeObligationRepository(session)
    repo.set_candidate_acceptance(
        project_id="book",
        chapter_number=2,
        acceptance_id="future-acceptance",
        draft_id="draft-2",
    )
    if value == "fulfilled":
        with pytest.raises(ValueError, match="evidence|quote"):
            repo.apply_reviewed_resolutions(
                project_id="book",
                chapter_number=2,
                chapter_body="Body 2",
                historical_form=_historical_form(value=value, quote=quote),
            )
    else:
        assert (
            repo.apply_reviewed_resolutions(
                project_id="book",
                chapter_number=2,
                chapter_body="Body 2",
                historical_form=_historical_form(value=value, quote=quote),
            )
            == []
        )
    assert session.get(NarrativeObligationRow, item.id).status == "active"


def _clone_session(session):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from forwin.models.base import Base

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    clone = Session(engine)
    for table in Base.metadata.sorted_tables:
        rows = [dict(row) for row in session.execute(select(table)).mappings()]
        if rows:
            clone.execute(table.insert(), rows)
    clone.flush()
    return clone


def test_restore_preserves_exact_timestamp_when_prior_mutations_share_database_tick(
    quality_session,
):
    from forwin.narrative_obligations import history

    session = quality_session
    # SQLite's CURRENT_TIMESTAMP has second resolution. Creation, activation and
    # resolution can share a timestamp even though their journal states differ.
    session.connection().connection.driver_connection.create_function(
        "current_timestamp", 0, lambda: "2026-09-09 10:00:00"
    )
    item, first = _activate(session)
    second = _commit(session, 2)
    NarrativeObligationRepository(session).mark_obligation_resolved(
        item.id,
        verifier_result={"status": "pass"},
        evidence_refs=["old:2"],
        resolution_chapter=2,
    )
    scratch = _clone_session(session)
    try:
        scratch.connection().connection.driver_connection.create_function(
            "current_timestamp", 0, lambda: "2026-09-09 10:00:01"
        )
        repo = NarrativeObligationRepository(scratch)
        repo.restore_prefix(
            project_id="book",
            active_commit_ids={1: first.id, 2: second.id},
            from_chapter=2,
        )
        row = scratch.get(NarrativeObligationRow, item.id)
        assert history.image(row)["updated_at"] == "2026-09-09T10:00:00"
        assert history.before_mutation(scratch, row)["status"] == "active"
        repo.set_candidate_acceptance(
            project_id="book",
            chapter_number=2,
            acceptance_id="future-acceptance",
            draft_id="draft-2",
        )
        assert repo.apply_reviewed_resolutions(
            project_id="book",
            chapter_number=2,
            chapter_body="Body 2",
            historical_form=_historical_form(),
        ) == [item.id]
        assert history.before_mutation(scratch, row)["status"] == "resolved"
    finally:
        scratch.close()
        scratch.get_bind().dispose()


@pytest.mark.parametrize("later_install_tick", [False, True])
def test_install_candidate_projection_preserves_old_branch_and_checks_actual_acceptance(
    quality_session,
    later_install_tick,
):
    session = quality_session
    if later_install_tick:
        session.connection().connection.driver_connection.create_function(
            "current_timestamp", 0, lambda: "2026-09-09 10:00:00"
        )
    item, first = _activate(session)
    second = _commit(session, 2)
    third = _commit(session, 3)
    repo = NarrativeObligationRepository(session)
    repo.mark_obligation_resolved(
        item.id,
        verifier_result={"status": "pass"},
        evidence_refs=["old:3"],
        resolution_chapter=3,
    )
    old_event_ids = set(session.scalars(select(DecisionEvent.id)))
    scratch = _clone_session(session)
    try:
        if later_install_tick:
            scratch.connection().connection.driver_connection.create_function(
                "current_timestamp", 0, lambda: "2026-09-09 10:00:00"
            )
        candidate_repo = NarrativeObligationRepository(scratch)
        candidate_repo.restore_prefix(
            project_id="book",
            active_commit_ids={1: first.id, 2: second.id, 3: third.id},
            from_chapter=2,
        )
        candidate_repo.set_candidate_acceptance(
            project_id="book",
            chapter_number=2,
            acceptance_id="accept-2-2",
            draft_id="draft-2",
        )
        candidate_repo.apply_reviewed_resolutions(
            project_id="book",
            chapter_number=2,
            chapter_body="Body 2",
            historical_form=_historical_form(),
        )
        with pytest.raises(ValueError, match="acceptance|ownership"):
            repo.install_candidate_projection(
                source_session=scratch,
                project_id="book",
                acceptance_ids={2: "accept-2-2"},
            )
        assert session.get(NarrativeObligationRow, item.id).resolution_chapter == 3
        replacement = _commit(session, 2, revision=2)
        if later_install_tick:
            session.connection().connection.driver_connection.create_function(
                "current_timestamp", 0, lambda: "2026-09-09 10:00:01"
            )
        repo.install_candidate_projection(
            source_session=scratch,
            project_id="book",
            acceptance_ids={2: replacement.id},
        )
        assert session.get(NarrativeObligationRow, item.id).resolution_chapter == 2
        assert old_event_ids < set(session.scalars(select(DecisionEvent.id)))
        repo.restore_prefix(
            project_id="book",
            active_commit_ids={1: first.id, 2: replacement.id, 3: third.id},
            from_chapter=2,
        )
        assert session.get(NarrativeObligationRow, item.id).status == "active"
    finally:
        scratch.close()
        scratch.get_bind().dispose()


def test_install_refuses_live_obligation_drift_before_mutating_any_projection(
    quality_session,
):
    session = quality_session
    item, first = _activate(session)
    second = _commit(session, 2)
    repo = NarrativeObligationRepository(session)
    scratch = _clone_session(session)
    try:
        candidate_repo = NarrativeObligationRepository(scratch)
        candidate_repo.restore_prefix(
            project_id="book",
            active_commit_ids={1: first.id, 2: second.id},
            from_chapter=2,
        )
        candidate_repo.set_candidate_acceptance(
            project_id="book",
            chapter_number=2,
            acceptance_id="accept-2-2",
            draft_id="draft-2",
        )
        candidate_repo.apply_reviewed_resolutions(
            project_id="book",
            chapter_number=2,
            chapter_body="Body 2",
            historical_form=_historical_form(),
        )
        replacement = _commit(session, 2, revision=2)
        repo.waive_obligation(item.id, reason="later operator update", actor="operator")
        before = set(session.scalars(select(DecisionEvent.id)))
        with pytest.raises(ValueError, match="drift|changed|stale"):
            repo.install_candidate_projection(
                source_session=scratch,
                project_id="book",
                acceptance_ids={2: replacement.id},
            )
        assert session.get(NarrativeObligationRow, item.id).status == "waived"
        assert set(session.scalars(select(DecisionEvent.id))) == before
    finally:
        scratch.close()
        scratch.get_bind().dispose()


def test_restoration_only_install_keeps_old_suffix_resolution_out_of_later_prefixes(
    quality_session,
):
    session = quality_session
    item, first = _activate(session)
    second = _commit(session, 2)
    third = _commit(session, 3)
    repo = NarrativeObligationRepository(session)
    repo.mark_obligation_resolved(
        item.id,
        verifier_result={"status": "pass", "source": "old suffix"},
        evidence_refs=["old:3"],
        resolution_chapter=3,
    )
    old_events = set(session.scalars(select(DecisionEvent.id)))
    scratch = _clone_session(session)
    try:
        candidate_repo = NarrativeObligationRepository(scratch)
        candidate_repo.restore_prefix(
            project_id="book",
            active_commit_ids={1: first.id, 2: second.id, 3: third.id},
            from_chapter=2,
        )
        for chapter in (2, 3):
            candidate_repo.set_candidate_acceptance(
                project_id="book",
                chapter_number=chapter,
                acceptance_id=f"accept-{chapter}-2",
                draft_id=f"draft-{chapter}",
            )
        new_second = _commit(session, 2, revision=2)
        new_third = _commit(session, 3, revision=2)
        repo.install_candidate_projection(
            source_session=scratch,
            project_id="book",
            acceptance_ids={2: new_second.id, 3: new_third.id},
        )
        row = session.get(NarrativeObligationRow, item.id)
        assert row.status == "active"
        new_events = [
            json.loads(event.payload_json)
            for event in session.scalars(select(DecisionEvent))
            if event.id not in old_events
        ]
        restored = next(
            event
            for event in new_events
            if event.get("operation") == "revision_restore"
        )
        assert restored["before"]["status"] == "active"
        assert restored["installation_previous_live_image"]["status"] == "resolved"
        assert restored["effect_acceptance_id"] == new_second.id
        repo.restore_prefix(
            project_id="book",
            active_commit_ids={1: first.id, 2: new_second.id, 3: new_third.id},
            from_chapter=2,
        )
        assert row.status == "active"
        assert row.resolution_chapter == 0
        assert old_events.issubset(set(session.scalars(select(DecisionEvent.id))))
    finally:
        scratch.close()
        scratch.get_bind().dispose()
