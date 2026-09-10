"""Real PG action boundaries bind checkpoint approval to current evidence."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schema import (
    BandCheckpointApproveRequest,
    ProjectContinueGenerationRequest,
)
from forwin.application.project_control import operations, support
from forwin.application.projects.generation import continue_project_generation
from forwin.generation.gate_delegation import GateResolution
from forwin.generation.pipeline_core.gate_delegation import GateDelegationStage
from forwin.models import (
    BandCheckpoint,
    BandExperiencePlan,
    ChapterPlan,
    DecisionEvent,
    Project,
)
from forwin.models.base import get_engine, get_session_factory
from forwin.review.plan_checks import BandCheckpointEvaluator
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url
from tests.test_accepted_history_controls import _accepted, _band, _book


@pytest.fixture
def checkpoint_book():
    engine = get_engine(postgres_test_url("checkpoint-evidence-actions"))
    Session = get_session_factory(engine)
    with Session.begin() as session:
        project, arc = _book(session)
        project.creation_status = "writing"
        project.target_total_chapters = 4
        for number in (1, 2, 3):
            _accepted(session, project, arc, number)
        _band(session, project, arc)
        session.add(
            ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=4,
                title="第四章",
                status="planned",
            )
        )
        row = BandCheckpointEvaluator(session).refresh(project.id, 3)
        assert row.status == "pass"
        ids = SimpleNamespace(
            project_id=project.id, arc_id=arc.id, checkpoint_id=row.id
        )
    try:
        yield SimpleNamespace(Session=Session, **vars(ids))
    finally:
        engine.dispose()


def _change_contract(book, *, delivered=False):
    with book.Session.begin() as session:
        band = session.scalar(
            select(BandExperiencePlan).where(
                BandExperiencePlan.project_id == book.project_id
            )
        )
        band.task_contract_json = json.dumps(
            [
                {
                    "task_type": "plot_advance",
                    "source": "explicit",
                    "required_keywords": [
                        "档案已封存" if delivered else "额外任务已完成"
                    ],
                    "description": "修改后的有限任务",
                }
            ],
            ensure_ascii=False,
        )


def _approve(book, *, status="overridden"):
    return operations.approve_band_checkpoint(
        book.project_id,
        "band-1",
        BandCheckpointApproveRequest(status=status, reason="已查看证据"),
        get_session=book.Session,
        latest_band_checkpoint_row=support.latest_band_checkpoint_row,
        latest_related_decision_event=support.latest_related_decision_event,
        require_reason=lambda reason, **_: reason,
        log_decision_event=support.log_decision_event,
        serialize_band_checkpoint=support.serialize_band_checkpoint,
    )


@pytest.mark.parametrize("status", ["pass", "overridden"])
def test_manual_approval_cannot_wash_stale_automatic_evidence(checkpoint_book, status):
    book = checkpoint_book
    _change_contract(book)
    with pytest.raises(HTTPException) as exc:
        _approve(book, status=status)
    assert exc.value.status_code == 409
    with book.Session() as session:
        assert session.get(BandCheckpoint, book.checkpoint_id).status == "pass"
        assert not list(
            session.scalars(
                select(DecisionEvent).where(
                    DecisionEvent.project_id == book.project_id,
                    DecisionEvent.event_type.in_(
                        ("band_checkpoint_approved", "band_checkpoint_overridden")
                    ),
                )
            )
        )


def test_manual_approval_preserves_current_automatic_and_manual_boundary(
    checkpoint_book,
):
    book = checkpoint_book
    assert _approve(book).status == "overridden"
    with book.Session.begin() as session:
        row = session.get(BandCheckpoint, book.checkpoint_id)
        row.trigger_source = "manual_boundary"
        row.status = "pending"
    _change_contract(book)
    assert _approve(book).status == "overridden"


def test_approval_targets_current_refreshed_evidence_and_preserves_old_record(
    checkpoint_book,
):
    book = checkpoint_book
    with book.Session() as session:
        old_created_event = session.scalar(
            select(DecisionEvent).where(
                DecisionEvent.related_object_id == book.checkpoint_id,
                DecisionEvent.event_type == "band_checkpoint_created",
            )
        )
        old_payload = old_created_event.payload_json
    _change_contract(book)
    with book.Session.begin() as session:
        row = BandCheckpointEvaluator(session).refresh(book.project_id, 3)
        current_id = row.id
        assert current_id != book.checkpoint_id
        assert row.status == "warn"
        current_sha = BandCheckpointEvaluator(session).inspect(row).input_sha256
    result = _approve(book)
    assert result.id == current_id
    assert result.status == "overridden"
    with book.Session() as session:
        assert session.get(BandCheckpoint, book.checkpoint_id).status == "pass"
        old_event = session.get(DecisionEvent, old_created_event.id)
        assert old_event.payload_json == old_payload
        approved = session.scalar(
            select(DecisionEvent).where(
                DecisionEvent.related_object_id == current_id,
                DecisionEvent.event_type == "band_checkpoint_overridden",
            )
        )
        assert (
            json.loads(approved.payload_json)["checkpoint_input_sha256"] == current_sha
        )


def _continue(book, calls):
    return continue_project_generation(
        book.project_id,
        ProjectContinueGenerationRequest(
            max_chapters=1, long_run_mode="soak_test", isolated=True
        ),
        get_session=book.Session,
        config=SimpleNamespace(),
        display_datetime=lambda value: value.isoformat() if value else "",
        active_generation_task_error_cls=RuntimeError,
        project_has_active_generation_task=lambda *_, **__: False,
        generation_task_conflict_message=lambda _: "active",
        log_decision_event=support.log_decision_event,
        create_continue_generation_task=lambda **request: (
            calls.append(request) or "task-new"
        ),
        serialize_task=lambda task_id, _: task_id,
        get_generation_task_or_404=lambda _: {},
    )


@pytest.mark.parametrize("delivered", [False, True])
def test_continue_refreshes_stale_auto_checkpoint_before_enqueue(
    checkpoint_book, delivered
):
    book = checkpoint_book
    _change_contract(book, delivered=delivered)
    calls = []
    if delivered:
        assert _continue(book, calls) == "task-new"
        assert len(calls) == 1
    else:
        with pytest.raises(HTTPException) as exc:
            _continue(book, calls)
        assert exc.value.status_code == 409
        assert calls == []
    with book.Session() as session:
        rows = list(
            session.scalars(
                select(BandCheckpoint).where(
                    BandCheckpoint.project_id == book.project_id
                )
            )
        )
        assert len(rows) == 2
        new = next(row for row in rows if row.id != book.checkpoint_id)
        assert new.status == ("pass" if delivered else "warn")
        assert session.get(BandCheckpoint, book.checkpoint_id).status == "pass"


def test_continue_policy_keeps_automatic_checkpoint_skip(checkpoint_book):
    book = checkpoint_book
    with book.Session.begin() as session:
        store = ProjectPolicyStore(session)
        project = session.get(Project, book.project_id)
        record = store.load(project)
        store.save(
            project,
            record.policy.with_user_settings(band_checkpoint_action="continue"),
            expected_version=record.version,
        )
    calls = []
    assert _continue(book, calls) == "task-new"
    with book.Session() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(BandCheckpoint).where(
                            BandCheckpoint.project_id == book.project_id
                        )
                    )
                )
            )
            == 1
        )


def test_repeated_blocked_continue_reuses_current_evaluation(checkpoint_book):
    book = checkpoint_book
    _change_contract(book)
    calls = []
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            _continue(book, calls)
        assert exc.value.status_code == 409
    assert calls == []
    with book.Session() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(BandCheckpoint).where(
                            BandCheckpoint.project_id == book.project_id,
                        )
                    )
                )
            )
            == 2
        )


@pytest.mark.parametrize("status", ["error", "unsupported"])
def test_continue_maps_checkpoint_error_to_a_supported_blocking_reason(
    checkpoint_book, status
):
    book = checkpoint_book
    with book.Session.begin() as session:
        session.get(BandCheckpoint, book.checkpoint_id).status = status
    calls = []
    with pytest.raises(HTTPException) as exc:
        _continue(book, calls)
    assert exc.value.status_code == 409
    assert calls == []
    with book.Session() as session:
        event = session.scalar(
            select(DecisionEvent).where(
                DecisionEvent.project_id == book.project_id,
                DecisionEvent.event_type == "hard_gate_hit",
            )
        )
        assert (
            json.loads(event.payload_json)["blocking_reason"] == "band_checkpoint_fail"
        )


def _spark(book, session, callback):
    stage = SimpleNamespace(
        _resolve_gate_delegation=callback,
        _audit_task_id="test-task",
        policy=RuntimePolicy.for_profile("standard").with_user_settings(
            gate_delegate="spark"
        ),
    )
    return GateDelegationStage._resolve_checkpoint_gate(
        stage,
        updater=StateUpdater(session),
        checkpoint=session.get(BandCheckpoint, book.checkpoint_id),
        gate_kind="band_checkpoint_pause",
        chapter_number=3,
    )


def test_spark_never_receives_stale_checkpoint_evidence(checkpoint_book):
    book = checkpoint_book
    _change_contract(book)
    calls = []
    with book.Session.begin() as session:
        assert (
            _spark(
                book,
                session,
                lambda **request: (
                    calls.append(request)
                    or GateResolution(approved=True, delegate="spark")
                ),
            )
            is False
        )
    assert calls == []


@pytest.mark.parametrize("change_during_model", [False, True])
def test_spark_checks_evidence_after_model_without_holding_project_lock(
    checkpoint_book, change_during_model
):
    book = checkpoint_book
    calls = []

    def model_boundary(**request):
        calls.append(request)
        with book.Session.begin() as other:
            assert (
                other.scalar(
                    select(Project.id)
                    .where(Project.id == book.project_id)
                    .with_for_update(nowait=True)
                )
                == book.project_id
            )
        if change_during_model:
            _change_contract(book)
        return GateResolution(
            approved=True,
            resolved=True,
            decision="approve",
            delegate="spark",
            reason="可继续",
        )

    with book.Session.begin() as session:
        assert _spark(book, session, model_boundary) is (not change_during_model)
    assert len(calls) == 1
    assert calls[0]["input_snapshot"]["checkpoint_input_sha256"]
    with book.Session() as session:
        assert session.get(BandCheckpoint, book.checkpoint_id).status == (
            "pass" if change_during_model else "overridden"
        )
