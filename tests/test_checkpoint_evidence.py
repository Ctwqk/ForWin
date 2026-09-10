"""Automatic checkpoints authorize only the accepted inputs actually evaluated."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from forwin.canon.admission import CanonAdmissionService
from forwin.canon.revision_service import (
    RevisionValidationService,
    revision_model_identity,
    save_revision_proposal,
)
from forwin.maintenance.post_canon import (
    PostCanonMaintenanceBusy,
    PostCanonMaintenanceService,
)
from forwin.maintenance.state import post_canon_checkpoint_blockers
from forwin.models import BandCheckpoint, BandExperiencePlan, ChapterPlan, DecisionEvent
from forwin.models.canon import CanonCommitRecord
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter
from tests import test_accepted_history_controls as controls
from tests import test_canon_atomic_transaction as atomic
from tests import test_revision_full_suffix as revision

history_session = controls.history_session
prepared_canon = atomic.prepared_canon


def _cached_band(session):
    project, arc = controls._book(session)
    accepted = [
        controls._accepted(session, project, arc, number) for number in (1, 2, 3)
    ]
    controls._band(session, project, arc)
    result = controls._checkpoint(session, project)
    assert result["status"] == "pass", result
    return project, arc, accepted, result


def _replace_body(session, chapter, body):
    draft, _review, candidate = controls._draft(session, chapter, version=2, body=body)
    previous = session.get(CanonCommitRecord, chapter.active_commit_id)
    current = CanonCommitRecord(
        project_id=chapter.project_id,
        chapter_plan_id=chapter.id,
        chapter_number=chapter.chapter_number,
        chapter_title=chapter.title,
        idempotency_key=f"{candidate.id}:replacement",
        candidate_id=candidate.id,
        acceptance_revision=previous.acceptance_revision + 1,
        base_book_revision=3,
    )
    session.add(current)
    session.flush()
    chapter.active_commit_id = current.id
    candidate.status = "accepted"
    session.flush()
    return draft


def _saved_controls(project, checkpoint):
    return [
        SimpleNamespace(
            project_id=project.id,
            chapter_number=3,
            step_name="feedback",
            result_json=json.dumps(
                {
                    "order_controls": {
                        "status": "succeeded",
                        "result": {"checkpoint": checkpoint},
                    }
                }
            ),
        )
    ]


def test_retained_checkpoint_id_cannot_release_maintenance_after_input_change(
    history_session,
):
    session = history_session
    project, _arc, accepted, before = _cached_band(session)
    _replace_body(session, accepted[0][0], "封存尚未完成。")
    blockers = post_canon_checkpoint_blockers(
        _saved_controls(project, before), session=session
    )
    assert blockers == ["band_checkpoint_pending"]


def test_direct_progression_rechecks_old_pass_before_opening_next_band(history_session):
    session = history_session
    project, arc, accepted, before = _cached_band(session)
    for chapter, _draft, _review in accepted:
        _replace_body(session, chapter, "封存尚未完成。")
    session.add(
        BandExperiencePlan(
            project_id=project.id,
            arc_id=arc.id,
            band_id="band-2",
            chapter_start=4,
            chapter_end=4,
        )
    )
    session.flush()
    code, _band_id, _message = controls._Controls()._strict_progression_block(
        session=session,
        repo=StateRepository(session),
        updater=StateUpdater(session),
        project=project,
        chapter_number=4,
    )
    assert code == "band_checkpoint_warn"
    assert session.get(BandCheckpoint, before["id"]).status == "pass"


def test_missing_original_evaluation_cannot_authorize_saved_pass(history_session):
    session = history_session
    project, _arc, _accepted, before = _cached_band(session)
    for event in session.scalars(
        select(DecisionEvent).where(DecisionEvent.related_object_id == before["id"])
    ):
        event.payload_json = "{}"
    session.flush()
    assert post_canon_checkpoint_blockers(
        _saved_controls(project, before), session=session
    ) == ["band_checkpoint_pending"]


def test_saving_an_unaccepted_revision_does_not_invalidate_checkpoint(history_session):
    session = history_session
    project, _arc, accepted, before = _cached_band(session)
    controls._draft(
        session, accepted[0][0], version=2, body="候选尚未封存。", verdict="fail"
    )
    after = controls._checkpoint(session, project)
    assert after == before


def test_locked_evaluation_refreshes_preloaded_band_contract(history_session):
    session = history_session
    project, _arc, _accepted, before = _cached_band(session)
    band = session.scalar(
        select(BandExperiencePlan).where(BandExperiencePlan.project_id == project.id)
    )
    project_id, band_id = project.id, band.id
    session.commit()
    # The real long-lived owner has already read this mutable plan.
    assert band.task_contract_json
    with Session(session.bind) as other, other.begin():
        changed = other.get(BandExperiencePlan, band_id)
        changed.task_contract_json = json.dumps(
            [
                {
                    "task_type": "plot_advance",
                    "required_keywords": ["不存在的新合同"],
                    "source": "explicit",
                }
            ]
        )
    from forwin.review.plan_checks import BandCheckpointEvaluator

    after = BandCheckpointEvaluator(session).refresh(project_id, 3)
    assert after.id != before["id"]
    assert after.status == "warn"


@pytest.mark.parametrize(
    "kind", ["next_band", "constraint", "active_obligation", "planned_obligation"]
)
def test_evaluation_reads_current_mutable_inputs_from_other_transaction(
    history_session, kind
):
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.planning_control import NarrativeConstraint
    from forwin.review.plan_checks import BandCheckpointEvaluator

    session = history_session
    project, arc, _accepted, _before = _cached_band(session)
    if kind == "next_band":
        row = BandExperiencePlan(
            project_id=project.id,
            arc_id=arc.id,
            band_id="next",
            chapter_start=4,
            chapter_end=6,
        )
        field, changed = (
            "task_contract_json",
            json.dumps([{"task_type": "plot_advance", "target_name": "下一段任务"}]),
        )
    elif kind == "constraint":
        row = NarrativeConstraint(
            project_id=project.id, subject_name="原约束", status="active"
        )
        field, changed = "subject_name", "更新的约束对象"
    else:
        row = NarrativeObligationRow(
            project_id=project.id,
            origin_chapter_number=3 if kind == "planned_obligation" else 1,
            status="planned" if kind == "planned_obligation" else "active",
            deadline_chapter=8,
        )
        field, changed = "deadline_chapter", 2
    session.add(row)
    session.flush()
    evaluator = BandCheckpointEvaluator(session)
    before = evaluator.refresh(project.id, 3)
    identity, row_id, project_id = before.id, row.id, project.id
    session.commit()
    getattr(row, field)
    with Session(session.bind) as other, other.begin():
        setattr(other.get(type(row), row_id), field, changed)
    assert not evaluator.inspect(session.get(BandCheckpoint, identity)).current
    assert evaluator.refresh(project_id, 3).id != identity


def test_unrelated_book_revision_and_continue_policy_preserve_existing_semantics(
    history_session,
):
    from forwin.review.plan_checks import BandCheckpointEvaluator

    session = history_session
    project, arc, accepted, before = _cached_band(session)
    project.book_revision += 1
    session.flush()
    assert (
        BandCheckpointEvaluator(session)
        .inspect(session.get(BandCheckpoint, before["id"]))
        .current
    )
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(
        update={
            "pause": policy.pause.model_copy(
                update={"band_checkpoint_action": "continue"}
            )
        }
    )
    project.runtime_policy_json = policy.model_dump_json()
    for chapter, _draft, _review in accepted:
        _replace_body(session, chapter, "未完成封存。")
    session.add(
        BandExperiencePlan(
            project_id=project.id,
            arc_id=arc.id,
            band_id="next",
            chapter_start=4,
            chapter_end=6,
        )
    )
    session.flush()
    pipeline = controls._Controls()
    pipeline.policy = policy
    result = pipeline._strict_progression_block(
        session=session,
        repo=StateRepository(session),
        updater=StateUpdater(session),
        project=project,
        chapter_number=4,
    )
    assert result == ("", "", "")
    current = StateRepository(session).get_latest_band_checkpoint(
        project.id, band_id="band-1"
    )
    assert current.status == "warn"


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ("skipped", ""),
        ("failed", "band_checkpoint_fail"),
        ("unaccepted", "chapter_not_canon"),
    ],
)
def test_continue_skip_does_not_wait_for_absent_checkpoint_but_keeps_real_blockers(
    history_session, evidence, expected
):
    session = history_session
    project, arc = controls._book(session)
    accepted = [
        controls._accepted(session, project, arc, number) for number in (1, 2, 3)
    ]
    controls._band(session, project, arc)
    session.add(
        BandExperiencePlan(
            project_id=project.id,
            arc_id=arc.id,
            band_id="next",
            chapter_start=4,
            chapter_end=6,
        )
    )
    pipeline = controls._Controls()
    pipeline.policy = pipeline.policy.model_copy(
        update={
            "pause": pipeline.policy.pause.model_copy(
                update={"band_checkpoint_action": "continue"}
            )
        }
    )
    if evidence == "failed":
        accepted[0][0].status = "needs_review"
        controls._checkpoint(session, project)
    if evidence == "unaccepted":
        accepted[-1][0].status = "needs_review"
    session.flush()
    if evidence == "skipped":
        assert (
            pipeline._run_post_canon_band_checkpoint(
                session=session,
                repo=StateRepository(session),
                updater=StateUpdater(session),
                project_id=project.id,
                chapter_number=3,
            )
            is None
        )
    result = pipeline._strict_progression_block(
        session=session,
        repo=StateRepository(session),
        updater=StateUpdater(session),
        project=project,
        chapter_number=4,
    )
    assert result[0] == expected


def test_completed_order_controls_refreshes_only_stale_checkpoint(history_session):
    session = history_session
    project, _arc, accepted, before = _cached_band(session)
    commit_id = accepted[-1][0].active_commit_id
    session.commit()
    service = PostCanonMaintenanceService(
        session_factory=sessionmaker(bind=session.bind),
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=object(),
    )
    service._ensure_runs(commit_id)
    previous_result = {
        "checkpoint": before,
        "future_plan_audit": {"blocking_reasons": [], "id": "retained-audit"},
    }
    for row in session.scalars(
        select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id
        )
    ):
        row.status = "succeeded"
        if row.step_name == "feedback":
            row.result_json = json.dumps(
                {"order_controls": {"status": "succeeded", "result": previous_result}}
            )
    band = session.scalar(
        select(BandExperiencePlan).where(BandExperiencePlan.project_id == project.id)
    )
    band.task_contract_json = json.dumps(
        [
            {
                "task_type": "plot_advance",
                "required_keywords": ["从未出现"],
                "source": "explicit",
            }
        ]
    )
    session.commit()

    def no_repeated_models(_session, _commit):
        pytest.fail(
            "completed maintenance models must not run to refresh a deterministic checkpoint"
        )

    after = service.run_order_controls(
        canon_commit_id=commit_id, runner=no_repeated_models
    )
    assert after["checkpoint"]["id"] != before["id"]
    assert after["checkpoint"]["status"] == "warn"
    assert after["future_plan_audit"] == previous_result["future_plan_audit"]
    assert (
        service.run_order_controls(canon_commit_id=commit_id, runner=no_repeated_models)
        == after
    )


def test_first_order_controls_does_not_hold_new_project_lock_during_model_runner(
    history_session,
):
    from forwin.canon.projection_lock import lock_projection_project

    session = history_session
    project, _arc, accepted, before = _cached_band(session)
    project_id, commit_id = project.id, accepted[-1][0].active_commit_id
    session.commit()
    sessions = sessionmaker(bind=session.bind)
    service = PostCanonMaintenanceService(
        session_factory=sessions,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=object(),
    )
    service._ensure_runs(commit_id)
    for row in session.scalars(
        select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id
        )
    ):
        row.status = "succeeded"
    session.commit()

    def runner(_session, _commit):
        with sessions.begin() as other:
            other.execute(text("SET LOCAL lock_timeout = '200ms'"))
            lock_projection_project(other, project_id)
        return {"checkpoint": before}

    assert (
        service.run_order_controls(canon_commit_id=commit_id, runner=runner)[
            "checkpoint"
        ]
        == before
    )


def test_first_runner_and_waiting_recovery_do_not_invert_project_row_locks(
    history_session,
):
    from forwin.review.plan_checks import BandCheckpointEvaluator

    session = history_session
    project, _arc, accepted, _before = _cached_band(session)
    project_id, commit_id = project.id, accepted[-1][0].active_commit_id
    session.commit()
    sessions = sessionmaker(bind=session.bind)
    service = PostCanonMaintenanceService(
        session_factory=sessions,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=object(),
    )
    service._ensure_runs(commit_id)
    for row in session.scalars(
        select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id
        )
    ):
        row.status = "succeeded"
    session.commit()
    entered, resume, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original_locked = service._locked_runs

    def locked(*args, **kwargs):
        if entered.is_set():
            waiting.set()
        return original_locked(*args, **kwargs)

    service._locked_runs = locked

    def runner(current, _commit):
        calls.append("model")
        entered.set()
        assert resume.wait(3)
        current.execute(text("SET LOCAL lock_timeout = '1s'"))
        checkpoint = BandCheckpointEvaluator(current).refresh(project_id, 3)
        return {
            "checkpoint": {
                "id": checkpoint.id,
                "status": checkpoint.status,
                "band_id": checkpoint.band_id,
            }
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.run_order_controls, canon_commit_id=commit_id, runner=runner
        )
        assert entered.wait(3)
        second = pool.submit(
            service.run_order_controls, canon_commit_id=commit_id, runner=runner
        )
        try:
            assert waiting.wait(3)
            assert not second.done()
        finally:
            resume.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert calls == ["model"]


def test_cached_refresh_does_not_wait_for_maintenance_rows_under_project_lock(
    history_session,
):
    session = history_session
    _project, _arc, accepted, before = _cached_band(session)
    commit_id = accepted[-1][0].active_commit_id
    session.commit()
    sessions = sessionmaker(bind=session.bind)
    service = PostCanonMaintenanceService(
        session_factory=sessions,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=object(),
    )
    service._ensure_runs(commit_id)
    for row in session.scalars(
        select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == commit_id
        )
    ):
        row.status = "succeeded"
        if row.step_name == "feedback":
            row.result_json = json.dumps(
                {
                    "order_controls": {
                        "status": "succeeded",
                        "result": {"checkpoint": before},
                    }
                }
            )
    session.commit()
    with sessions.begin() as owner:
        rows = service._locked_runs(owner, commit_id)
        original = rows[-1].result_json
        with pytest.raises(PostCanonMaintenanceBusy):
            service.run_order_controls(
                canon_commit_id=commit_id,
                runner=lambda *_args: pytest.fail("cached model rerun"),
            )
        assert rows[-1].result_json == original


def test_two_refreshers_reuse_one_current_evaluation(history_session):
    from forwin.review.plan_checks import BandCheckpointEvaluator

    session = history_session
    project, _arc, _accepted, _before = _cached_band(session)
    project_id = project.id
    band = session.scalar(
        select(BandExperiencePlan).where(BandExperiencePlan.project_id == project_id)
    )
    band.stall_guard_max_gap = 23
    session.commit()
    ready = threading.Barrier(2)

    def refresh():
        with Session(session.bind) as other, other.begin():
            ready.wait(timeout=3)
            return BandCheckpointEvaluator(other).refresh(project_id, 3).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(refresh), pool.submit(refresh)
        assert left.result(timeout=5) == right.result(timeout=5)
    assert (
        len(
            list(
                session.scalars(
                    select(BandCheckpoint).where(
                        BandCheckpoint.project_id == project_id
                    )
                )
            )
        )
        == 2
    )


def test_project_detail_does_not_present_stale_pass_as_current_permission(
    history_session,
):
    from forwin.application.read_models.project_detail import build_project_detail

    session = history_session
    project, arc, accepted, _before = _cached_band(session)
    session.add(
        ChapterPlan(
            project_id=project.id,
            arc_plan_id=arc.id,
            chapter_number=4,
            status="planned",
        )
    )
    _replace_body(session, accepted[0][0], "替换后的正式正文。")
    detail = build_project_detail(
        session=session, project=project, display_datetime=lambda _value: ""
    )
    assert detail.generation_control.blocking_reason.code == "band_checkpoint_pending"


def test_band_plan_change_creates_new_evaluation_without_overwriting_old(
    history_session,
):
    session = history_session
    project, _arc, _accepted, before = _cached_band(session)
    old_event = session.scalar(
        select(DecisionEvent).where(DecisionEvent.related_object_id == before["id"])
    )
    original_payload = old_event.payload_json
    band = session.scalar(
        select(BandExperiencePlan).where(BandExperiencePlan.project_id == project.id)
    )
    band.task_contract_json = json.dumps(
        [
            {
                "task_type": "plot_advance",
                "required_keywords": ["从未出现的完成证据"],
                "source": "explicit",
                "description": "新增合同要求",
            }
        ]
    )
    session.flush()
    after = controls._checkpoint(session, project)
    assert after["id"] != before["id"]
    assert after["status"] == "warn"
    assert session.get(BandCheckpoint, before["id"]).status == "pass"
    assert old_event.payload_json == original_payload


def test_refresh_same_transaction_orders_evaluations_without_random_id_tiebreak(
    history_session, monkeypatch
):
    import forwin.state.updater as updater_module

    session = history_session
    project, _arc, _accepted, before = _cached_band(session)
    band = session.scalar(
        select(BandExperiencePlan).where(BandExperiencePlan.project_id == project.id)
    )
    band.stall_guard_max_gap = 73
    session.flush()
    ids = iter(f"0000000000000000000000000000{i:06d}" for i in range(100))
    monkeypatch.setattr(updater_module, "new_id", lambda: next(ids))
    after = controls._checkpoint(session, project)
    assert after["id"] != before["id"]
    assert controls._checkpoint(session, project) == after
    assert (
        StateRepository(session)
        .get_latest_band_checkpoint(project.id, band_id=band.band_id)
        .id
        == after["id"]
    )


def _real_revision(fixture):
    ids, body, old_ids, _successor_candidate = revision._book(fixture)
    with fixture.Session.begin() as session:
        first = session.get(ChapterPlan, ids[1])
        session.add(
            BandExperiencePlan(
                project_id=ids[0],
                arc_id=first.arc_plan_id,
                band_id="archive-band",
                chapter_start=1,
                chapter_end=2,
                task_contract_json=json.dumps(
                    [
                        {
                            "task_type": "plot_advance",
                            "required_keywords": ["往事"],
                            "source": "explicit",
                            "description": "保留往事记录",
                        }
                    ]
                ),
                schedule_json=json.dumps(
                    {"band_id": "archive-band", "chapter_start": 1, "chapter_end": 2}
                ),
            )
        )
        session.flush()
        before = controls._Controls()._run_post_canon_band_checkpoint(
            session=session,
            repo=StateRepository(session),
            updater=StateUpdater(session),
            project_id=ids[0],
            chapter_number=2,
        )
        assert before["status"] == "pass"
    with fixture.Session.begin() as session:
        candidate = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body.replace("往事", "昔年"),
            expected_book_revision=2,
        )
        candidate_id = candidate.id
    writer = ChapterWriter(revision.BodyModel())
    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=writer,
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert not prepared.blocked, prepared
    accepted = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        prepared.plan,
        revision_model_identity=revision_model_identity(writer),
    )
    assert not accepted.blocked, accepted
    return ids, old_ids, before


def test_real_revision_rechecks_checkpoint_against_new_accepted_body(prepared_canon):
    fixture = prepared_canon
    ids, _old_ids, before = _real_revision(fixture)
    with fixture.Session.begin() as session:
        after = controls._Controls()._run_post_canon_band_checkpoint(
            session=session,
            repo=StateRepository(session),
            updater=StateUpdater(session),
            project_id=ids[0],
            chapter_number=2,
        )
        assert after["id"] != before["id"]
        assert after["status"] == "warn"
        assert session.get(BandCheckpoint, before["id"]).status == "pass"


def test_real_revision_reused_successor_materializes_maintenance_without_backref_rewrite(
    prepared_canon,
):
    fixture = prepared_canon
    ids, old_ids, _before = _real_revision(fixture)
    service = PostCanonMaintenanceService(
        session_factory=fixture.Session,
        stage_analyzer=object(),
        pacing_strategist=object(),
        replan_governor=object(),
        arc_envelope_manager=object(),
        world_simulator=object(),
        artifact_store=object(),
        llm_client=object(),
    )
    with fixture.Session() as session:
        commit_id = session.get(ChapterPlan, ids[2]).active_commit_id
    service._ensure_runs(commit_id)
    with fixture.Session() as session:
        rows = list(
            session.scalars(
                select(PostCanonMaintenanceRun).where(
                    PostCanonMaintenanceRun.canon_commit_id == commit_id
                )
            )
        )
        assert len(rows) == 4
        from forwin.models import CandidateDraftRecord

        candidate = session.get(CandidateDraftRecord, rows[0].candidate_id)
        assert candidate.canon_commit_id == old_ids[1]
