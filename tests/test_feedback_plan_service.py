from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from forwin.candidate_drafts import candidate_plan_revision
from forwin.models.base import Base
from forwin.models.capacity import ChapterCapacityReservation
from forwin.models.draft import ChapterDraft
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import FeedbackActionRecord, PublisherUploadJob
from forwin.models.task import GenerationTask
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository


def _service():
    assert importlib.util.find_spec("forwin.planning.feedback_plan") is not None, (
        "selected feedback needs a future-plan owner with eligibility, CAS and audit"
    )
    module = importlib.import_module("forwin.planning.feedback_plan")
    return module.FeedbackPlanService()


@pytest.fixture
def plan_case():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for book in ("book", "other"):
            session.add(
                Project(
                    id=book,
                    title="Same title",
                    premise="Fixed root",
                    target_total_chapters=10,
                    runtime_policy_json=RuntimePolicy.for_profile(
                        "standard"
                    ).model_dump_json(),
                    runtime_policy_version=1,
                )
            )
            session.add(
                ArcPlanVersion(
                    id=f"arc-{book}", project_id=book, arc_synopsis="Fixed arc"
                )
            )
            for number in range(1, 7):
                session.add(
                    ChapterPlan(
                        id=f"{book}-{number}",
                        project_id=book,
                        arc_plan_id=f"arc-{book}",
                        chapter_number=number,
                        title=f"Chapter {number}",
                        one_line="The original task",
                        goals_json='["Keep the existing goal"]',
                        task_contract_json='[{"task":"retain"}]',
                        experience_plan_json=ChapterExperiencePlan(
                            rule_anchors=["The existing rule"],
                            progress_markers=["Keep the original progress"],
                            active_subworld_ids=["world-1"],
                        ).model_dump_json(),
                    )
                )
        session.commit()
        yield session, session.get(ChapterPlan, "book-3")
    engine.dispose()


def _selected(session, **updates):
    values = {
        "id": "action",
        "project_id": "book",
        "signal_key": "confusion:rule",
        "signal_type": "confusion",
        "action_type": "clarify_rule_legibility",
        "triggered_at_chapter": 1,
        "cooldown_until_chapter": 4,
        "status": "selected",
        "source_qualified": True,
        "aggregate_id": "aggregate-1",
        "direction": "unclear",
        "target_chapter_start": 2,
        "target_chapter_end": 5,
        "hint_valid_from_chapter": 2,
        "hint_expires_at_chapter": 5,
        "selected_at_chapter": 1,
        "selected_at": datetime.now(UTC),
        "aggregate_evidence_json": json.dumps(
            {
                "aggregate_id": "aggregate-1",
                "project_id": "book",
                "signal_key": "confusion:rule",
                "signal_type": "confusion",
                "direction": "unclear",
                "aggregation_version": "test-frozen-v1",
                "evidence_sha256": "a" * 64,
                "source_qualified": True,
                "source_scope": [
                    {"platform_id": "qidian", "work_binding_id": "work-1"}
                ],
                "source_comment_ids": ["comment-1", "comment-2", "comment-3"],
            }
        ),
        "action_payload_json": json.dumps(
            {
                "hint": {
                    "category": "clarity_hints",
                    "text": "Clarify the existing rule's cost.",
                },
                "plan_hint": {
                    "field": "rule_anchors",
                    "text": "Clarify the existing rule's cost.",
                },
            }
        ),
        "plan_application_json": "{}",
        "body_observation_json": "{}",
        "effect_observation_json": "{}",
    }
    values.update(updates)
    row = FeedbackActionRecord(**values)
    session.add(row)
    session.commit()
    return row


def _apply(owner, session, chapter, action, *, revision=None):
    return owner.apply(
        session=session,
        project_id=chapter.project_id,
        action_id=action.id,
        chapter_number=chapter.chapter_number,
        expected_plan_revision=revision or candidate_plan_revision(chapter),
    )


def _applications(action):
    return json.loads(action.plan_application_json)["applications"]


def test_selected_action_changes_only_future_experience_and_records_real_revisions(
    plan_case,
):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    before_revision = candidate_plan_revision(chapter)
    original_plan = chapter.experience_plan_json
    result = _apply(owner, session, chapter, action)
    assert result.status == "applied"
    assert candidate_plan_revision(chapter) != before_revision
    plan = StateRepository(session).get_chapter_experience_plan("book", 3)
    assert plan.rule_anchors == [
        "The existing rule",
        "Clarify the existing rule's cost.",
    ]
    assert plan.progress_markers == ["Keep the original progress"]
    assert chapter.title == "Chapter 3"
    assert chapter.one_line == "The original task"
    assert chapter.goals_json == '["Keep the existing goal"]'
    assert chapter.task_contract_json == '[{"task":"retain"}]'
    assert session.get(Project, "book").premise == "Fixed root"
    assert session.get(ChapterPlan, "book-2").experience_plan_json == original_plan
    assert session.get(ChapterPlan, "other-3").experience_plan_json == original_plan
    proof = _applications(action)[0]
    assert proof["status"] == "applied"
    assert proof["before_plan_revision"] == before_revision
    assert proof["after_plan_revision"] == candidate_plan_revision(chapter)
    assert proof["before"]["experience_plan_json"] == original_plan
    assert proof["after"]["experience_plan_json"] == chapter.experience_plan_json
    assert proof["source"]["aggregate_id"] == action.aggregate_id
    assert proof["source"]["aggregate_evidence"] == json.loads(
        action.aggregate_evidence_json
    )
    assert proof["before_sha256"] != proof["after_sha256"]
    assert action.status == "selected"
    assert action.body_observation_json == action.effect_observation_json == "{}"
    session.rollback()
    assert session.get(ChapterPlan, "book-3").experience_plan_json == original_plan
    assert session.get(FeedbackActionRecord, action.id).plan_application_json == "{}"


def test_retry_keeps_original_application_and_never_appends_same_hint_twice(plan_case):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    original_revision = candidate_plan_revision(chapter)
    first = _apply(owner, session, chapter, action, revision=original_revision)
    session.commit()
    proof = action.plan_application_json
    second = _apply(owner, session, chapter, action, revision=original_revision)
    assert first.status == "applied"
    assert second.status == "already_applied"
    assert action.plan_application_json == proof
    assert (
        StateRepository(session)
        .get_chapter_experience_plan("book", 3)
        .rule_anchors.count("Clarify the existing rule's cost.")
        == 1
    )


def test_stale_plan_cas_preserves_new_plan_and_records_refusal(plan_case):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    previous = candidate_plan_revision(chapter)
    chapter.one_line = "Operator's newer plan"
    session.commit()
    result = _apply(owner, session, chapter, action, revision=previous)
    assert result.status == "refused"
    assert result.reason == "plan_revision_changed"
    assert chapter.one_line == "Operator's newer plan"
    assert _applications(action)[0]["reason"] == result.reason
    assert StateRepository(session).get_chapter_experience_plan(
        "book", 3
    ).rule_anchors == ["The existing rule"]


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"status": "proposed"}, "action_not_selected"),
        ({"source_qualified": False}, "action_source_unqualified"),
        ({"target_chapter_end": 2}, "outside_action_target"),
        ({"hint_expires_at_chapter": 2}, "action_hint_expired"),
        ({"project_id": "other"}, "action_project_mismatch"),
        ({"action_payload_json": "{}"}, "action_has_no_plan_hint"),
        (
            {
                "action_payload_json": '{"plan_hint":{"field":"active_subworld_ids","text":"new-root"}}'
            },
            "unsupported_plan_hint",
        ),
    ],
)
def test_unselected_unknown_out_of_scope_or_root_changes_never_touch_plan(
    plan_case, updates, reason
):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session, **updates)
    before = chapter.experience_plan_json
    result = _apply(owner, session, chapter, action)
    assert result.status == "refused"
    assert result.reason == reason
    assert chapter.experience_plan_json == before
    # Foreign-book requests cannot write an audit into another book's action.
    if reason != "action_project_mismatch":
        assert _applications(action)[0]["reason"] == reason


@pytest.mark.parametrize("existing", ["draft", "accepted", "publisher"])
def test_already_generated_accepted_or_external_chapter_is_ineligible(
    plan_case, existing
):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    if existing == "draft":
        session.add(
            ChapterDraft(
                chapter_plan_id=chapter.id, version=1, body_text="An existing body"
            )
        )
    elif existing == "accepted":
        chapter.status = "accepted"
    else:
        session.add(
            PublisherUploadJob(
                project_id="book",
                chapter_number=3,
                platform_id="qidian",
                body_text="Remote draft",
                status="uncertain",
            )
        )
    session.commit()
    before = candidate_plan_revision(chapter)
    result = _apply(owner, session, chapter, action)
    assert result.status == "refused"
    assert candidate_plan_revision(chapter) == before
    assert _applications(action)[0]["reason"] == result.reason


@pytest.mark.parametrize("occupied", ["reservation", "writer_without_draft"])
def test_generation_occupancy_defers_without_releasing_reservation_or_action(
    plan_case, occupied
):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    task = GenerationTask(
        id="task",
        project_id="book",
        status="running",
        current_stage="writing_chapter",
        current_chapter=3,
        lease_owner="worker",
        lease_epoch=1,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.add(task)
    if occupied == "reservation":
        session.add(
            ChapterCapacityReservation(
                project_id="book",
                chapter_number=3,
                chapter_plan_id=chapter.id,
                task_id="task",
                lease_epoch=1,
                config_version=1,
            )
        )
    session.commit()
    before = candidate_plan_revision(chapter)
    result = _apply(owner, session, chapter, action)
    assert result.status == "deferred"
    assert result.reason in {
        "chapter_generation_reserved",
        "chapter_generation_in_progress",
    }
    assert candidate_plan_revision(chapter) == before
    assert action.status == "selected"
    assert _applications(action)[0]["status"] == "deferred"
    if occupied == "reservation":
        assert session.get(ChapterCapacityReservation, ("book", 3)) is not None
    task.status = "completed"
    if occupied == "reservation":
        session.delete(session.get(ChapterCapacityReservation, ("book", 3)))
    session.commit()
    retry = _apply(owner, session, chapter, action)
    assert retry.status == "applied"
    assert [proof["status"] for proof in _applications(action)] == [
        "deferred",
        "applied",
    ]


def test_existing_hint_is_not_reported_as_a_new_plan_change(plan_case):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    plan = ChapterExperiencePlan.model_validate_json(chapter.experience_plan_json)
    plan.rule_anchors.append("Clarify the existing rule's cost.")
    chapter.experience_plan_json = plan.model_dump_json()
    session.commit()
    revision = candidate_plan_revision(chapter)
    result = _apply(owner, session, chapter, action)
    assert result.status == "unchanged"
    assert result.reason == "plan_hint_already_present"
    assert candidate_plan_revision(chapter) == revision
    assert (
        _applications(action)[0]["before_sha256"]
        == _applications(action)[0]["after_sha256"]
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"unrecognized_historical_contract":{"keep":true}}',
        '{"chapter_entry_targets":[{"entity_name":"X","historical_contract":{"keep":true}}]}',
        "{broken",
        "[]",
    ],
)
def test_unknown_or_malformed_experience_payload_is_preserved(plan_case, raw):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    chapter.experience_plan_json = raw
    session.commit()
    result = _apply(owner, session, chapter, action)
    assert result.status == "refused"
    assert result.reason == "unsupported_experience_plan"
    assert chapter.experience_plan_json == raw


def test_second_connection_sees_project_lock_and_atomic_plan_audit_rollback():
    owner = _service()
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from forwin.models.base import get_engine, init_db
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("feedback-plan-lock"))
    init_db(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            session.add(
                Project(
                    id="book", title="Book", premise="Root", target_total_chapters=10
                )
            )
            session.flush()
            session.add(
                ArcPlanVersion(id="arc-book", project_id="book", arc_synopsis="Arc")
            )
            session.flush()
            session.add(
                ChapterPlan(
                    id="book-3",
                    project_id="book",
                    arc_plan_id="arc-book",
                    chapter_number=3,
                )
            )
            session.commit()
            action = _selected(session)
            chapter = session.get(ChapterPlan, "book-3")
            before = candidate_plan_revision(chapter)
            assert _apply(owner, session, chapter, action).status == "applied"
            with factory() as observer:
                with pytest.raises(OperationalError):
                    observer.scalar(
                        select(Project)
                        .where(Project.id == "book")
                        .with_for_update(nowait=True)
                    )
                observer.rollback()
                assert (
                    candidate_plan_revision(observer.get(ChapterPlan, "book-3"))
                    == before
                )
                assert (
                    observer.get(FeedbackActionRecord, action.id).plan_application_json
                    == "{}"
                )
            session.rollback()
            with factory() as observer:
                observer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                assert observer.scalar(
                    select(Project)
                    .where(Project.id == "book")
                    .with_for_update(nowait=True)
                )
                assert (
                    candidate_plan_revision(observer.get(ChapterPlan, "book-3"))
                    == before
                )
                assert (
                    observer.get(FeedbackActionRecord, action.id).plan_application_json
                    == "{}"
                )
    finally:
        engine.dispose()


def test_plan_change_committed_while_waiting_for_project_lock_is_seen_before_cas():
    owner = _service()
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    from sqlalchemy.orm import sessionmaker

    from forwin.models.base import get_engine, init_db
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("feedback-plan-cas-race"))
    init_db(engine)
    factory = sessionmaker(bind=engine)
    ready = Event()
    start = Event()
    try:
        with factory() as seed:
            seed.add(
                Project(
                    id="book", title="Book", premise="Root", target_total_chapters=10
                )
            )
            seed.flush()
            seed.add(
                ArcPlanVersion(id="arc-book", project_id="book", arc_synopsis="Arc")
            )
            seed.flush()
            seed.add(
                ChapterPlan(
                    id="book-3",
                    project_id="book",
                    arc_plan_id="arc-book",
                    chapter_number=3,
                )
            )
            seed.commit()
            _selected(seed)

        def apply_after_loading_old_identity():
            with factory.begin() as worker:
                chapter = worker.get(ChapterPlan, "book-3")
                action = worker.get(FeedbackActionRecord, "action")
                expected = candidate_plan_revision(chapter)
                ready.set()
                assert start.wait(5)
                return _apply(owner, worker, chapter, action, revision=expected)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(apply_after_loading_old_identity)
            assert ready.wait(5)
            with factory.begin() as competing:
                competing.scalar(
                    select(Project).where(Project.id == "book").with_for_update()
                )
                chapter = competing.scalar(
                    select(ChapterPlan)
                    .where(ChapterPlan.id == "book-3")
                    .with_for_update()
                )
                chapter.one_line = "Newer plan from the competing owner"
                start.set()
                with pytest.raises(FutureTimeout):
                    future.result(timeout=0.1)
            result = future.result(timeout=5)
        assert result.status == "refused"
        assert result.reason == "plan_revision_changed"
        with factory() as observer:
            assert (
                observer.get(ChapterPlan, "book-3").one_line
                == "Newer plan from the competing owner"
            )
            assert (
                json.loads(observer.get(ChapterPlan, "book-3").experience_plan_json)
                == {}
            )
            assert (
                _applications(observer.get(FeedbackActionRecord, "action"))[0]["reason"]
                == "plan_revision_changed"
            )
    finally:
        start.set()
        engine.dispose()


def test_owner_requires_flushed_inputs_instead_of_discarding_callers_pending_plan(
    plan_case,
):
    owner = _service()
    session, chapter = plan_case
    action = _selected(session)
    revision = candidate_plan_revision(chapter)
    action_id = action.id
    chapter.one_line = "Pending work from the caller"
    assert chapter in session.dirty
    with pytest.raises(ValueError, match="requires flushed"):
        owner.apply(
            session=session,
            project_id="book",
            chapter_number=3,
            action_id=action_id,
            expected_plan_revision=revision,
        )
    assert chapter.one_line == "Pending work from the caller"
    assert action.plan_application_json == "{}"


@pytest.mark.parametrize(
    "signal,direction,field",
    [
        ("confusion", "unclear", "rule_anchors"),
        ("pacing", "too_slow", "progress_markers"),
        ("pacing", "too_fast", "immersion_anchors"),
    ],
)
def test_real_mapper_selected_plan_hint_reaches_existing_experience_prompt(
    plan_case, signal, direction, field
):
    owner = _service()
    from types import SimpleNamespace

    from forwin.audience.actions import ActionMapper
    from forwin.audience.feedback import FeedbackCooldown
    from forwin.context.providers.experience_provider import ExperienceContextProvider
    from forwin.context.request import ContextDraft, ContextRequest
    from forwin.writer.prompt_core.sections import _experience_overlay_section
    from tests.test_feedback_action_lifecycle import aggregate_view

    session, _ = plan_case
    chapter = session.get(ChapterPlan, "book-4")
    mapper = ActionMapper()
    cooldown = FeedbackCooldown()
    proposed = mapper.record_actions(
        session,
        project_id="book",
        chapter_number=3,
        cooldown=cooldown,
        actions=mapper.map_actions(
            [
                aggregate_view(
                    key=f"{signal}:{direction}", signal_type=signal, direction=direction
                )
            ]
        ),
    )
    mapper.select_actions(
        session,
        project_id="book",
        chapter_number=3,
        action_ids=[proposed[0].id],
        cooldown=cooldown,
    )
    action = proposed[0]
    hint = json.loads(action.action_payload_json)["plan_hint"]
    assert hint["field"] == field
    result = _apply(owner, session, chapter, action)
    assert result.status == "applied"
    draft = ContextDraft()
    ExperienceContextProvider().contribute(
        ContextRequest(
            project_id="book",
            chapter_plan=chapter,
            repo=StateRepository(session),
            session=session,
        ),
        draft,
    )
    text = _experience_overlay_section(
        SimpleNamespace(chapter_experience_plan=draft.data["chapter_experience_plan"])
    )
    assert hint["text"] in text
    assert json.loads(action.prompt_inclusions_json) == []
    assert json.loads(action.body_observation_json) == {}
    assert json.loads(action.effect_observation_json) == {}


@pytest.mark.parametrize("changed_input", ["chapter", "action"])
def test_expired_dirty_owner_input_is_rejected_without_implicit_flush(
    plan_case, changed_input
):
    from sqlalchemy import event, inspect

    session, chapter = plan_case
    action = _selected(session)
    revision = candidate_plan_revision(chapter)
    # End the read transaction so identity attributes are expired. Only the
    # changed field is set; accessing project_id/id can otherwise issue a query
    # and autoflush before the ownership check has decided whether to refuse.
    session.commit()
    row = chapter if changed_input == "chapter" else action
    field = "one_line" if changed_input == "chapter" else "notes"
    setattr(row, field, "Unsaved caller edit")
    assert "id" in inspect(row).expired_attributes
    assert row in session.dirty
    flushes = []
    event.listen(session, "before_flush", lambda *_: flushes.append("flush"))
    with pytest.raises(ValueError, match="requires flushed"):
        _service().apply(
            session=session,
            project_id="book",
            chapter_number=3,
            action_id="action",
            expected_plan_revision=revision,
        )
    assert flushes == []
    assert row in session.dirty
    assert getattr(row, field) == "Unsaved caller edit"
    with session.no_autoflush:
        assert action.plan_application_json == "{}"
