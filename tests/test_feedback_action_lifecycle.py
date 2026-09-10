from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.audience.actions import ActionMapper
from forwin.audience.feedback import FeedbackCooldown
from forwin.models import FeedbackActionRecord, Project
from forwin.models.base import Base
from forwin.state.repo import StateRepository


def aggregate_view(
    *,
    key="pacing:slow",
    signal_type="pacing",
    direction="too_slow",
    qualified=True,
    project_id="book",
):
    return {
        "project_id": project_id,
        "aggregate_id": "aggregate-" + key,
        "aggregation_version": "test-v1",
        "evidence_sha256": "a" * 64,
        "signal_key": key,
        "signal_type": signal_type,
        "direction": direction,
        "target_name": "调查线索",
        "signal_level": "confirmed",
        "max_severity": 3,
        "hit_comment_count": 7,
        "known_author_count": 7,
        "total_comment_count": 12,
        "analyzed_comment_count": 12,
        "unknown_author_comment_count": 0,
        "source_qualified": qualified,
        "qualification_reasons": [] if qualified else ["publication_unknown"],
        "source_scope": {
            "chapter_start": 1,
            "chapter_end": 3,
            "canon_commit_ids": ["canon"],
            "publication_ids": ["publication"],
            "platforms": ["fanqie"],
        },
        "evidence_comment_ids": ["comment"],
        "evidence_analysis_ids": ["analysis"],
    }


@pytest.fixture
def action_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="book", title="Book", premise="Premise"))
        session.commit()
        yield session
    engine.dispose()


def test_mapping_consumes_frozen_qualification_and_opposite_directions():
    mapper = ActionMapper()
    actions = mapper.map_actions(
        [
            aggregate_view(),
            aggregate_view(key="pacing:fast", direction="too_fast")
            | {"target_name": "另一支线"},
            aggregate_view(key="unknown", direction="unknown"),
            aggregate_view(key="unqualified", qualified=False),
        ]
    )
    assert len(actions) == 2
    assert len({action.action_type for action in actions}) == 2
    assert {action.direction for action in actions} == {"too_slow", "too_fast"}
    assert all(
        action.aggregate_evidence["evidence_sha256"] == "a" * 64 for action in actions
    )


def test_proposal_selection_cooldown_and_hint_expiry_are_distinct(action_session):
    session = action_session
    mapper = ActionMapper()
    cooldown = FeedbackCooldown(cooldown_chapters=1)
    actions = mapper.map_actions([aggregate_view()])
    records = mapper.record_actions(
        session, project_id="book", chapter_number=4, actions=actions, cooldown=cooldown
    )
    record = records[0]
    assert record.status == "proposed"
    assert cooldown.is_cooled(session, "book", record.signal_key, 4)
    assert StateRepository(session).get_audience_hints("book", 5) is None
    mapper.select_actions(
        session,
        project_id="book",
        chapter_number=4,
        action_ids=[record.id],
        cooldown=cooldown,
    )
    assert record.status == "selected"
    assert record.cooldown_until_chapter == 5
    assert (record.target_chapter_start, record.target_chapter_end) == (5, 9)
    assert record.hint_expires_at_chapter == 7
    assert not cooldown.is_cooled(session, "book", record.signal_key, 4)
    assert cooldown.is_cooled(session, "book", record.signal_key, 5)
    hints = StateRepository(session).get_audience_hints("book", 6)
    assert [item.action_id for item in hints.items] == [record.id]
    assert StateRepository(session).get_audience_hints("book", 8) is None
    assert json.loads(record.prompt_inclusions_json) == []
    assert json.loads(record.plan_application_json) == {}
    assert json.loads(record.body_observation_json) == {}
    assert json.loads(record.effect_observation_json) == {}
    assert (
        mapper.record_actions(
            session,
            project_id="book",
            chapter_number=4,
            actions=actions,
            cooldown=cooldown,
        )[0].id
        == record.id
    )
    assert len(list(session.scalars(select(FeedbackActionRecord)))) == 1


def test_prediction_has_observation_hint_but_no_plan_command(action_session):
    mapper = ActionMapper()
    actions = mapper.map_actions(
        [
            aggregate_view(
                key="prediction:motive", signal_type="prediction", direction="predicts"
            )
        ]
    )
    records = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=actions,
        cooldown=FeedbackCooldown(),
    )
    mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        action_ids=[records[0].id],
        cooldown=FeedbackCooldown(),
    )
    hints = StateRepository(action_session).get_audience_hints("book", 5)
    assert len(hints.prediction_hints) == 1
    assert "调查线索" in hints.prediction_hints[0]
    assert "plan_hint" not in json.loads(records[0].action_payload_json)


def test_historical_action_never_becomes_qualified_or_starts_new_cooldown(
    action_session,
):
    record = FeedbackActionRecord(
        id="old",
        project_id="book",
        signal_key="old",
        signal_type="risk",
        action_type="old",
        triggered_at_chapter=4,
        cooldown_until_chapter=100,
        notes="旧建议",
    )
    action_session.add(record)
    action_session.flush()
    assert StateRepository(action_session).get_audience_hints("book", 5) is None
    assert FeedbackCooldown().is_cooled(action_session, "book", "old", 5)


@pytest.mark.parametrize(
    "signal_type,direction,change",
    [
        ("pacing", "too_slow", "decrease"),
        ("pacing", "too_fast", "decrease"),
        ("confusion", "unclear", "decrease"),
        ("risk", "concern", "decrease"),
        ("character_heat", "positive", "increase"),
        ("character_heat", "negative", "decrease"),
        ("relationship_interest", "want_more", "observe"),
        ("relationship_interest", "want_less", "observe"),
        ("prediction", "predicts", "observe"),
    ],
)
def test_mapper_owns_desired_signal_change(
    action_session, signal_type, direction, change
):
    mapper = ActionMapper()
    actions = mapper.map_actions(
        [aggregate_view(signal_type=signal_type, direction=direction)]
    )
    record = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=actions,
        cooldown=FeedbackCooldown(),
    )[0]
    assert json.loads(record.action_payload_json)["desired_signal_change"] == change


def test_selection_serializes_same_signal_and_does_not_extend_selection(action_session):
    mapper = ActionMapper()
    view = aggregate_view()
    second = view | {"aggregate_id": "later-aggregate"}
    first_record = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions([view]),
        cooldown=FeedbackCooldown(),
    )[0]
    second_record = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions([second]),
        cooldown=FeedbackCooldown(),
    )[0]
    pack = mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        action_ids=[first_record.id, second_record.id],
        cooldown=FeedbackCooldown(),
    )
    assert [item.action_id for item in pack.items] == [first_record.id]
    assert second_record.status == "proposed"
    mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=5,
        action_ids=[first_record.id],
        cooldown=FeedbackCooldown(),
    )
    assert first_record.selected_at_chapter == 4
    assert first_record.cooldown_until_chapter == 7


def test_cooldown_uses_latest_selection_not_proposal_creation_time(action_session):
    from datetime import UTC, datetime

    action_session.add_all(
        [
            FeedbackActionRecord(
                id="earlier-proposal",
                project_id="book",
                signal_key="key",
                status="selected",
                source_qualified=True,
                cooldown_until_chapter=12,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            FeedbackActionRecord(
                id="later-proposal",
                project_id="book",
                signal_key="key",
                status="selected",
                source_qualified=True,
                cooldown_until_chapter=8,
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ]
    )
    action_session.flush()
    assert not FeedbackCooldown().is_cooled(action_session, "book", "key", 10)


def test_delayed_selection_does_not_make_hint_available_before_selection(
    action_session,
):
    mapper = ActionMapper()
    record = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions([aggregate_view()]),
        cooldown=FeedbackCooldown(),
    )[0]
    mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=5,
        action_ids=[record.id],
        cooldown=FeedbackCooldown(),
    )
    assert StateRepository(action_session).get_audience_hints("book", 5) is None
    assert (
        StateRepository(action_session).get_audience_hints("book", 6).items[0].action_id
        == record.id
    )


@pytest.mark.parametrize(
    "signal_type,first,second",
    [
        ("pacing", "too_slow", "too_fast"),
        ("character_heat", "positive", "negative"),
        ("relationship_interest", "want_more", "want_less"),
        ("confusion", "unclear", "clear"),
    ],
)
@pytest.mark.parametrize("second_window", [(1, 3), (3, 7)])
def test_conflicting_confirmed_directions_only_produce_observation(
    action_session, signal_type, first, second, second_window
):
    mapper = ActionMapper()
    views = [
        aggregate_view(key=direction, signal_type=signal_type, direction=direction)
        for direction in (first, second)
    ]
    views[1]["source_scope"] |= {
        "chapter_start": second_window[0],
        "chapter_end": second_window[1],
    }
    records = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions(views),
        cooldown=FeedbackCooldown(),
    )
    assert len(records) == 2
    for record in records:
        payload = json.loads(record.action_payload_json)
        assert record.action_type == "observe_conflicting_directions"
        assert payload["decision_reason"] == "conflicting_directions"
        assert payload["desired_signal_change"] == "observe"
        assert {item["aggregate_id"] for item in payload["conflicting_aggregates"]} == {
            view["aggregate_id"] for view in views
        }
        assert "plan_hint" not in payload
        assert json.loads(record.aggregate_evidence_json)["signal_level"] == "confirmed"


def test_nonoverlapping_opposite_windows_preserve_each_direction():
    first = aggregate_view()
    second = aggregate_view(key="fast", direction="too_fast")
    second["source_scope"] |= {"chapter_start": 4, "chapter_end": 7}
    assert (
        len(
            {
                action.action_type
                for action in ActionMapper().map_actions([first, second])
            }
        )
        == 2
    )


@pytest.mark.parametrize("qualified", [False, True])
def test_malformed_frozen_json_is_unknown_and_never_rewritten(
    action_session, qualified
):
    from forwin.audience.actions import action_is_qualified

    row = FeedbackActionRecord(
        id="broken",
        project_id="book",
        signal_key="key",
        source_qualified=qualified,
        status="selected",
        aggregate_evidence_json="{broken",
        hint_valid_from_chapter=5,
        hint_expires_at_chapter=7,
        target_chapter_start=5,
        target_chapter_end=9,
        selected_at_chapter=4,
    )
    action_session.add(row)
    action_session.flush()
    assert action_is_qualified(row) is False
    assert StateRepository(action_session).get_audience_hints("book", 5) is None
    assert row.aggregate_evidence_json == "{broken"


def test_foreign_project_snapshot_cannot_create_action(action_session):
    mapper = ActionMapper()
    with pytest.raises(ValueError, match="identity"):
        mapper.record_actions(
            action_session,
            project_id="book",
            chapter_number=4,
            actions=mapper.map_actions([aggregate_view(project_id="foreign")]),
            cooldown=FeedbackCooldown(),
        )
    assert list(action_session.scalars(select(FeedbackActionRecord))) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "foreign"),
        ("signal_key", "different"),
        ("signal_type", "risk"),
        ("direction", "too_fast"),
    ],
)
def test_action_rejects_mismatched_frozen_identity(action_session, field, value):
    from forwin.audience.actions import action_is_qualified

    mapper = ActionMapper()
    row = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions([aggregate_view()]),
        cooldown=FeedbackCooldown(),
    )[0]
    evidence = json.loads(row.aggregate_evidence_json)
    evidence[field] = value
    row.aggregate_evidence_json = json.dumps(evidence)
    assert action_is_qualified(row) is False


def test_aggregation_pass_keeps_cooled_opposite_as_conflict_evidence(
    action_session, monkeypatch
):
    from forwin.audience.feedback import SignalAggregator, run_feedback_aggregation_pass
    from forwin.models import SignalWindowAggregate

    mapper = ActionMapper()
    slow = aggregate_view()
    fast = aggregate_view(key="fast", direction="too_fast")
    fast["evidence_sha256"] = "b" * 64
    selected = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=3,
        actions=mapper.map_actions([slow]),
        cooldown=FeedbackCooldown(),
    )[0]
    mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=3,
        action_ids=[selected.id],
        cooldown=FeedbackCooldown(),
    )
    rows = []
    for view in (slow, fast):
        row = SignalWindowAggregate(
            id=view["aggregate_id"],
            project_id="book",
            signal_key=view["signal_key"],
            signal_type=view["signal_type"],
            target_name=view["target_name"],
            direction=view["direction"],
            signal_level="confirmed",
            aggregation_version=view["aggregation_version"],
            evidence_sha256=view["evidence_sha256"],
            source_qualified=True,
            max_severity=3,
            provenance_json=json.dumps(view),
        )
        action_session.add(row)
        rows.append(row)
    action_session.flush()
    monkeypatch.setattr(SignalAggregator, "aggregate", lambda *_args, **_kwargs: rows)
    run_feedback_aggregation_pass(action_session, "book", 4)
    new_fast = action_session.scalars(
        select(FeedbackActionRecord).where(FeedbackActionRecord.signal_key == "fast")
    ).one()
    assert (
        json.loads(new_fast.action_payload_json).get("decision_reason")
        == "conflicting_directions"
    )
    assert "plan_hint" not in json.loads(new_fast.action_payload_json)


def test_risk_watchlist_is_visible_observation_without_plan_or_writer_repair_command(
    action_session,
):
    mapper = ActionMapper()
    view = aggregate_view(signal_type="risk", direction="concern") | {
        "signal_level": "watchlist",
        "known_author_count": 1,
        "hit_comment_count": 3,
    }
    record = mapper.record_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        actions=mapper.map_actions([view]),
        cooldown=FeedbackCooldown(),
    )[0]
    hints = mapper.select_actions(
        action_session,
        project_id="book",
        chapter_number=4,
        action_ids=[record.id],
        cooldown=FeedbackCooldown(),
    )
    payload = json.loads(record.action_payload_json)
    assert record.action_type == "observe_risk_watchlist"
    assert record.status == "selected"
    assert payload["decision_reason"] == "watchlist_observation"
    assert payload["desired_signal_change"] == "observe"
    assert "plan_hint" not in payload
    assert "尚未形成多作者共识" in hints.risk_flags[0]
    assert "既定计划" in hints.risk_flags[0]
