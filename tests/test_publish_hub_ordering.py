"""Task 5: chapters on a hub-routed binding go out in order, after the book exists (D4, D9).

Ruling R36: release keeps the extension queue's cadence. A chapter is held (left `scheduled`, not
counted as released) only while its destination cannot take chapters at all: no destination id,
the destination not in the sync cache, the account paused for risk, or no book yet (then the
create_destination job is submitted once). Otherwise it is released like before: its request is
stored and counted, and it is POSTed only once every earlier released chapter of the binding is
done; `recover_unsubmitted` sends the rest in order.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from forwin.candidate_drafts import candidate_body_hash
from forwin.models.audit import DecisionEvent
from forwin.models.base import new_id
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.publisher import (
    UPLOAD_JOB_TERMINAL_STATUSES,
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
    PublishHubSyncState,
)
from forwin.production.executor import ProductionExecutor
from forwin.production.planner import ProductionPlanner
from forwin.production.policy import ProductionPolicy, ProductionQuota
from forwin.production.repository import ProductionRepository
from forwin.publish_hub.payloads import create_body
from forwin.publish_hub.state import cached_destination, write_destinations
from forwin.publish_hub.submit import (
    HOLD_TRANSITION,
    SubmitOutcome,
    hub_binding_for,
    recover_unsubmitted,
)
from tests.test_canon_publisher_jobs import _fixture
from tests.test_publish_hub_submit import (
    FANQIE,
    FANQIE_DESTINATION,
    NOW,
    QIDIAN_DESTINATION,
    FakeHub,
    _attach_hub,
    _job,
    _recover,
    _release,
    _set_routes,
)
from tests.test_publisher_runtime_covers import PNG_1X1

BOOK_ID = "fanqie-book-1"


def _seed_cache(fixture, *, account_status="active", remote_id=BOOK_ID, platform="fanqie"):
    """The destinations snapshot Task 6's sync writes, with the fanqie destination in it."""
    with fixture.runtime.session_factory.begin() as session:
        write_destinations(
            session,
            [
                {
                    "id": FANQIE_DESTINATION,
                    "platform": platform,
                    "remote_id": remote_id,
                    "account_status": account_status,
                    "kind": "novel_book",
                },
                {
                    "id": QIDIAN_DESTINATION,
                    "platform": "qidian",
                    "remote_id": "qidian-book-1",
                    "account_status": "active",
                    "kind": "novel_book",
                },
            ],
            now=NOW,
        )


def _add_chapter(fixture, number: int, *, bindings=(FANQIE,)) -> str:
    """Commit chapter `number` to Canon and materialize its scheduled job; the job id."""
    body = f"第{number}章的正文，旧城的钟声又响了一次。"
    title = f"第{number}章 钟声"
    plan_id, candidate_id, commit_id = new_id(), new_id(), new_id()
    canon_key = f"canon:{fixture.project_id}:{number}"
    with fixture.runtime.session_factory.begin() as session:
        arc_id = session.scalar(
            select(ArcPlanVersion.id).where(ArcPlanVersion.project_id == fixture.project_id)
        )
        session.add(
            ChapterPlan(
                id=plan_id,
                project_id=fixture.project_id,
                arc_plan_id=arc_id,
                chapter_number=number,
                title=title,
                one_line="推进",
                goals_json="[]",
                status="accepted",
            )
        )
        session.flush()
        draft = ChapterDraft(
            id=new_id(),
            chapter_plan_id=plan_id,
            version=1,
            body_text=body,
            summary="钟声。",
            char_count=len(body),
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(
            id=new_id(),
            draft_id=draft.id,
            verdict="pass",
            issues_json="[]",
            review_meta_json='{"verdict":"pass"}',
        )
        session.add(review)
        session.flush()
        session.add(
            CandidateDraftRecord(
                id=candidate_id,
                project_id=fixture.project_id,
                chapter_plan_id=plan_id,
                chapter_number=number,
                candidate_draft_id=draft.id,
                review_id=review.id,
                body_hash=candidate_body_hash(body),
                plan_revision="plan-r1",
                policy_version=1,
                status="accepted",
                canon_status="committed",
                canon_commit_id=commit_id,
                idempotency_key=canon_key,
            )
        )
        session.flush()
        session.add(
            CanonCommitRecord(
                id=commit_id,
                idempotency_key=canon_key,
                candidate_id=candidate_id,
                project_id=fixture.project_id,
                chapter_number=number,
                status="committed",
            )
        )
    created = fixture.runtime.canon_jobs.materialize(
        canon_commit_id=commit_id,
        canon_idempotency_key=canon_key,
        project_id=fixture.project_id,
        chapter_number=number,
        candidate_id=candidate_id,
        chapter_title=title,
        body_sha256=candidate_body_hash(body),
        bindings=list(bindings),
    )
    (job,) = created
    return job["job_id"]


def _ordered_fixture(
    name: str,
    *,
    chapters=(1, 2, 3),
    destination: str | None = FANQIE_DESTINATION,
    route: str = "hub",
    cache: dict | None = None,
):
    """A project with fanqie chapters `chapters` scheduled; returns (fixture, hub, client, ids)."""
    fixture = _fixture(name)
    hub = FakeHub()
    client = _attach_hub(fixture.runtime, hub)
    _set_routes(fixture, {"fanqie": (route, destination)})
    if cache is not None:
        _seed_cache(fixture, **cache)
    ids = {number: _add_chapter(fixture, number) for number in chapters}
    return fixture, hub, client, ids


def _posted_refs(hub: FakeHub) -> list[str]:
    return [json.loads(post).get("source_ref") or json.loads(post)["action"] for post in hub.posts]


def _chapter_ref(fixture, number: int) -> str:
    return f"project/{fixture.project_id}/chapter/{number}"


def _create_rows(fixture) -> list[PublisherUploadJob]:
    with fixture.runtime.session_factory() as session:
        rows = (
            session.execute(
                select(PublisherUploadJob).where(PublisherUploadJob.task_kind == "create_book")
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _hold_events(fixture, job_id) -> list[dict]:
    with fixture.runtime.session_factory() as session:
        events = (
            session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.related_object_id == job_id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            )
            .scalars()
            .all()
        )
    payloads = [json.loads(event.payload_json or "{}") for event in events]
    return [payload for payload in payloads if payload.get("transition") == HOLD_TRANSITION]


def _force_status(fixture, job_id, status: str) -> None:
    with fixture.runtime.session_factory.begin() as session:
        session.get(PublisherUploadJob, job_id).status = status


def _run_publish(fixture, *, quota: int, publish: bool = True):
    """One scheduler run's publishing step: the real backlog, planner and executor.

    The scheduler reserves the day exactly when the run's publish_job_count (or a generation
    task, or reviews) is non-zero; nothing else is run here, so the count decides the day slot.
    """
    policy = ProductionPolicy(
        enabled=True,
        auto_publish=publish,
        quota=ProductionQuota(write=0, publish=quota),
        publish_bindings=[{"platform": "fanqie", "book_name": FANQIE["book_name"]}],
    )
    with fixture.runtime.session_factory() as session:
        backlog = ProductionRepository(session).load_backlogs(
            [fixture.project_id],
            generation_terminal_statuses={"completed", "failed", "cancelled"},
            upload_terminal_statuses=set(UPLOAD_JOB_TERMINAL_STATUSES),
        )[fixture.project_id]
        plan = ProductionPlanner().plan(policy=policy, backlog=backlog, now=NOW)
        project = session.get(Project, fixture.project_id)
        result = ProductionExecutor(
            generation_application=None,
            publisher_manager_factory=lambda: SimpleNamespace(
                release_canon_jobs=fixture.runtime.canon_jobs.release
            ),
            session=session,
        ).execute(plan=plan, project=project, policy=policy, review_job_count=0)
        session.commit()
    return plan, result


# --- ordering: released like the extension queue, sent one chapter at a time ------------------


def test_one_run_releases_three_chapters_but_sends_only_the_first_then_recovery_keeps_order() -> None:
    fixture, hub, client, ids = _ordered_fixture("hub-order-q3", cache={})
    try:
        hub.lose_next_response = True  # chapter 1 reaches publish-hub, its answer is lost

        plan, result = _run_publish(fixture, quota=3)

        assert plan.publish_chapters == [1, 2, 3]
        assert result.publish_job_count == 3  # the same count the extension queue gives
        assert hub.post_count() == 1
        assert _posted_refs(hub) == [_chapter_ref(fixture, 1)]
        for number, job_id in ids.items():
            stored = _job(fixture, job_id)
            assert stored.status == "pending", number
            assert stored.hub_submit_body_json is not None, number  # hub-owned
            assert stored.hub_job_id is None, number
        assert "第 1 章" in _job(fixture, ids[2]).result_message
        ((first_bytes, hub_job),) = hub.jobs_by_key.values()

        outcomes = _recover(fixture, client)

        assert outcomes == {
            ids[1]: SubmitOutcome.SUBMITTED,
            ids[2]: SubmitOutcome.WAITING_PREVIOUS,
            ids[3]: SubmitOutcome.WAITING_PREVIOUS,
        }
        assert hub.post_count() == 2
        assert hub.posts[1] == hub.posts[0] == first_bytes
        assert _job(fixture, ids[1]).hub_job_id == hub_job["id"]
        assert len(hub.jobs_by_key) == 1

        _recover(fixture, client)
        assert hub.post_count() == 2  # chapter 1 is still running on publish-hub

        _force_status(fixture, ids[1], "succeeded")  # what Task 6's sync mirrors
        outcomes = _recover(fixture, client)

        assert hub.post_count() == 3
        assert _posted_refs(hub)[-1] == _chapter_ref(fixture, 2)
        assert outcomes[ids[2]] == SubmitOutcome.SUBMITTED
        assert outcomes[ids[3]] == SubmitOutcome.WAITING_PREVIOUS
        assert _job(fixture, ids[2]).hub_job_id is not None
        assert _job(fixture, ids[3]).hub_job_id is None
        assert "第 2 章" in _job(fixture, ids[3]).result_message
    finally:
        fixture.engine.dispose()


def test_next_chapter_waits_while_the_previous_one_is_pending_on_publish_hub() -> None:
    fixture, hub, client, ids = _ordered_fixture("hub-order-pending", chapters=(1, 2), cache={})
    try:
        _release(fixture, [ids[1]])
        assert _job(fixture, ids[1]).hub_job_id is not None

        released = _release(fixture, [ids[2]])

        stored = _job(fixture, ids[2])
        assert [item["job_id"] for item in released] == [ids[2]]
        assert (stored.status, stored.hub_job_id) == ("pending", None)
        assert stored.hub_submit_body_json is not None
        assert hub.post_count() == 1
        for status in ("pending", "paused", "reconciling", "failed", "cancelled"):
            _force_status(fixture, ids[1], status)
            assert _recover(fixture, client)[ids[2]] == SubmitOutcome.WAITING_PREVIOUS, status
        assert hub.post_count() == 1
    finally:
        fixture.engine.dispose()


def test_a_stale_failure_of_a_chapter_published_since_does_not_block_an_unfinished_one_does() -> None:
    fixture, hub, client, ids = _ordered_fixture("hub-order-stale-failure", cache={})
    try:
        # Chapter 1 failed long ago in the extension queue and was published by hand since.
        _force_status(fixture, ids[1], "failed")
        with fixture.runtime.session_factory.begin() as session:
            work = PublisherWorkBinding(
                project_id=fixture.project_id,
                platform_id="fanqie",
                book_name=FANQIE["book_name"],
                remote_book_id=BOOK_ID,
            )
            session.add(work)
            session.flush()
            session.add(
                PublisherChapterBinding(
                    work_binding_id=work.id,
                    project_id=fixture.project_id,
                    platform_id="fanqie",
                    chapter_number=1,
                    remote_chapter_id="remote-chapter-1",
                    publish_state="published",
                )
            )

        _release(fixture, [ids[2], ids[3]])

        assert _posted_refs(hub) == [_chapter_ref(fixture, 2)]
        assert _job(fixture, ids[3]).hub_job_id is None

        # Chapter 2 fails on publish-hub and nothing published it: chapter 3 keeps waiting.
        _force_status(fixture, ids[2], "failed")
        assert _recover(fixture, client) == {ids[3]: SubmitOutcome.WAITING_PREVIOUS}
        assert hub.post_count() == 1
    finally:
        fixture.engine.dispose()


def test_a_scheduled_or_deleted_earlier_chapter_does_not_block() -> None:
    fixture, hub, client, ids = _ordered_fixture("hub-order-unreleased-earlier", cache={})
    try:
        with fixture.runtime.session_factory.begin() as session:
            session.get(PublisherUploadJob, ids[2]).status = "failed"
            session.get(PublisherUploadJob, ids[2]).deleted_at = NOW.replace(tzinfo=None)

        _release(fixture, [ids[3]])  # chapter 1 is still scheduled, chapter 2's row is deleted

        assert _posted_refs(hub) == [_chapter_ref(fixture, 3)]
    finally:
        fixture.engine.dispose()


def test_publish_mode_is_frozen_when_the_request_is_stored() -> None:
    fixture, hub, client, ids = _ordered_fixture("hub-order-mode", chapters=(1, 2), cache={})
    try:
        _release(fixture, [ids[1], ids[2]], publish=True)
        assert _job(fixture, ids[2]).hub_job_id is None

        with pytest.raises(ValueError, match="frozen"):
            _release(fixture, [ids[2]], publish=False)

        _force_status(fixture, ids[1], "succeeded")
        _recover(fixture, client)
        assert json.loads(hub.posts[-1])["payload"]["mode"] == "publish"
        assert _job(fixture, ids[2]).publish is True
    finally:
        fixture.engine.dispose()


# --- holds: nothing stored, nothing counted -----------------------------------------------------


def test_no_destination_id_holds_without_any_hub_request() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-no-destination", chapters=(1,), destination=None, cache={}
    )
    try:
        plan, result = _run_publish(fixture, quota=1)

        stored = _job(fixture, ids[1])
        assert plan.publish_chapters == [1]
        assert result.publish_job_count == 0  # the day is not reserved
        assert result.action == "idle"
        assert stored.status == "scheduled"
        assert stored.hub_submit_body_json is None
        assert "发布目标" in stored.result_message
        assert hub.requests == []
        assert _create_rows(fixture) == []
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("cache", [None, {"platform": "qidian"}])
def test_destination_missing_from_the_cache_or_on_another_platform_holds(cache) -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        f"hub-order-cache-{'none' if cache is None else 'platform'}", chapters=(1,), cache=cache
    )
    try:
        assert _release(fixture, [ids[1]]) == []

        stored = _job(fixture, ids[1])
        assert stored.status == "scheduled"
        assert stored.hub_submit_body_json is None
        assert hub.requests == []
        assert _create_rows(fixture) == []
    finally:
        fixture.engine.dispose()


def test_account_paused_for_risk_holds_every_chapter_and_uses_no_quota() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-paused", cache={"account_status": "paused_risk"}
    )
    try:
        for _ in range(2):
            plan, result = _run_publish(fixture, quota=2)
            assert result.publish_job_count == 0
            assert result.action == "idle"
        assert hub.requests == []
        for job_id in ids.values():
            stored = _job(fixture, job_id)
            assert (stored.status, stored.hub_submit_body_json) == ("scheduled", None)
        assert "风控" in _job(fixture, ids[1]).result_message

        _seed_cache(fixture)  # the account is active again
        plan, result = _run_publish(fixture, quota=2)

        assert plan.publish_chapters == [1, 2]
        assert result.publish_job_count == 2
        assert _posted_refs(hub) == [_chapter_ref(fixture, 1)]
        assert _job(fixture, ids[3]).status == "scheduled"
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("ending", ["superseded", "failed"])
def test_a_chapter_superseded_or_refused_at_release_does_not_use_the_day(ending) -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        f"hub-order-ends-{ending}", chapters=(1,), cache={}
    )
    try:
        if ending == "superseded":
            hub.published_refs.add(_chapter_ref(fixture, 1))
        else:
            hub.refusals.append((422, "invalid_payload"))

        plan, result = _run_publish(fixture, quota=1)

        assert plan.publish_chapters == [1]
        assert _job(fixture, ids[1]).status == ending
        assert result.publish_job_count == 0
        assert result.action == "idle"
    finally:
        fixture.engine.dispose()


def test_hold_reason_is_recorded_and_audited_only_when_it_changes() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-hold-audit", chapters=(1,), destination=None, cache={"account_status": "paused_risk"}
    )
    job_id = ids[1]
    try:
        for _ in range(3):
            _release(fixture, [job_id])
        events = _hold_events(fixture, job_id)
        assert [(event["hub_gate"], event["hub_gate_cause"]) for event in events] == [
            ("wait_destination", "no_destination")
        ]

        _set_routes(fixture, {"fanqie": ("hub", FANQIE_DESTINATION)})
        for _ in range(3):
            _release(fixture, [job_id])

        events = _hold_events(fixture, job_id)
        assert [(event["hub_gate"], event["hub_gate_cause"]) for event in events] == [
            ("wait_destination", "no_destination"),
            ("wait_account", "account_paused_risk"),
        ]
        assert "风控" in _job(fixture, job_id).result_message
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


# --- the book comes first (D4, R1, R3, R33) ---------------------------------------------------


def test_book_creation_is_submitted_once_and_the_first_chapter_goes_out_once_the_book_exists() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-create", chapters=(1, 2), cache={"remote_id": None}
    )
    try:
        for _ in range(3):
            plan, result = _run_publish(fixture, quota=2)
            # A book being created is not an active upload: the planner keeps planning.
            assert plan.publish_chapters == [1, 2]
            assert result.publish_job_count == 0

        assert hub.post_count() == 1
        (create,) = _create_rows(fixture)
        with fixture.runtime.session_factory() as session:
            binding = hub_binding_for(session, fixture.project_id, "fanqie")
            work = session.execute(
                select(PublisherWorkBinding).where(
                    PublisherWorkBinding.project_id == fixture.project_id,
                    PublisherWorkBinding.platform_id == "fanqie",
                )
            ).scalar_one()
        expected = create_body(binding, FANQIE_DESTINATION, fixture.project_id)
        assert json.loads(hub.posts[0]) == expected
        assert create.hub_submit_body_json == expected
        assert create.idempotency_key == expected["idempotency_key"]
        assert create.idempotency_key.startswith(f"forwin:create:{fixture.project_id}:fanqie:")
        assert (create.task_kind, create.chapter_number, create.body_text) == ("create_book", 0, "")
        assert (create.project_id, create.platform_id) == (fixture.project_id, "fanqie")
        assert create.hub_job_id is not None
        assert create.status == "pending"
        assert work.hub_destination_id == FANQIE_DESTINATION
        for job_id in ids.values():
            stored = _job(fixture, job_id)
            assert (stored.status, stored.hub_submit_body_json) == ("scheduled", None)
            assert "建书" in stored.result_message

        _seed_cache(fixture)  # the sync saw the book's remote_id
        plan, result = _run_publish(fixture, quota=2)

        assert result.publish_job_count == 2
        assert _posted_refs(hub) == ["create_destination", _chapter_ref(fixture, 1)]
        assert len(_create_rows(fixture)) == 1
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("how", ["refused", "failed_on_hub"])
def test_failed_book_creation_is_not_resubmitted_and_shows_on_the_held_chapter(how) -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        f"hub-order-create-{how}", chapters=(1,), cache={"remote_id": None}
    )
    try:
        if how == "refused":
            hub.refusals.append((422, "invalid_payload"))
        _release(fixture, [ids[1]])
        (create,) = _create_rows(fixture)
        if how == "failed_on_hub":
            _force_status(fixture, create.id, "failed")  # mirrored by the sync
        assert _create_rows(fixture)[0].status == "failed"

        for _ in range(2):
            assert _release(fixture, [ids[1]]) == []

        assert hub.post_count() == 1
        assert len(_create_rows(fixture)) == 1
        stored = _job(fixture, ids[1])
        assert stored.status == "scheduled"
        assert "/ops" in stored.result_message
        causes = [event["hub_gate_cause"] for event in _hold_events(fixture, ids[1])]
        assert causes[-1] == "create_failed"
    finally:
        fixture.engine.dispose()


def test_book_creation_carries_the_selected_cover_and_uses_the_existing_work_binding(
    tmp_path,
) -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-create-cover", chapters=(1,), cache={"remote_id": None}
    )
    try:
        cover_file = tmp_path / "cover.png"
        cover_file.write_bytes(PNG_1X1)
        with fixture.runtime.session_factory.begin() as session:
            work = PublisherWorkBinding(
                project_id=fixture.project_id, platform_id="fanqie", book_name="番茄版"
            )
            session.add(work)
            session.flush()
            session.add(
                PublisherCoverAsset(
                    project_id=fixture.project_id,
                    work_binding_id=work.id,
                    status="selected",
                    selection_state="selected",
                    file_path=str(cover_file),
                    mime_type="image/png",
                )
            )
            work_id = work.id

        _release(fixture, [ids[1]])

        (body,) = [json.loads(post) for post in hub.posts]
        assert body["action"] == "create_destination"
        assert [ref["uri"] for ref in body["content_refs"]] == [f"file://{cover_file}"]
        assert body["content_refs"][0]["role"] == "cover"
        with fixture.runtime.session_factory() as session:
            works = (
                session.execute(
                    select(PublisherWorkBinding).where(
                        PublisherWorkBinding.project_id == fixture.project_id
                    )
                )
                .scalars()
                .all()
            )
        assert [(item.id, item.hub_destination_id) for item in works] == [
            (work_id, FANQIE_DESTINATION)
        ]
    finally:
        fixture.engine.dispose()


def test_a_book_id_known_locally_counts_as_the_book_existing() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-local-book", chapters=(1,), cache={"remote_id": None}
    )
    try:
        with fixture.runtime.session_factory.begin() as session:
            session.add(
                PublisherWorkBinding(
                    project_id=fixture.project_id,
                    platform_id="fanqie",
                    book_name="番茄版",
                    remote_book_id=BOOK_ID,
                )
            )

        _release(fixture, [ids[1]])

        assert _posted_refs(hub) == [_chapter_ref(fixture, 1)]
        assert _create_rows(fixture) == []
    finally:
        fixture.engine.dispose()


def test_a_book_creation_stored_while_the_hub_is_down_is_sent_by_recovery_not_again() -> None:
    fixture, hub, client, ids = _ordered_fixture(
        "hub-order-create-recover", chapters=(1,), cache={"remote_id": None}
    )
    try:
        hub.down = True
        _release(fixture, [ids[1]])
        hub.down = False
        (create,) = _create_rows(fixture)
        assert (create.status, create.hub_job_id) == ("pending", None)
        assert create.hub_submit_body_json is not None
        assert len(hub.requests) == 1  # tried once, the hub was down

        _release(fixture, [ids[1]])  # the gate finds the stored request and leaves it to recovery
        assert len(hub.requests) == 1

        outcomes = _recover(fixture, client)

        assert outcomes == {create.id: SubmitOutcome.SUBMITTED}
        assert json.loads(hub.posts[0]) == create.hub_submit_body_json
        assert _create_rows(fixture)[0].hub_job_id is not None
        assert _job(fixture, ids[1]).status == "scheduled"
    finally:
        fixture.engine.dispose()


def test_a_book_being_created_is_not_an_active_upload_but_a_chapter_is() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-active-flag", chapters=(1,), cache={"remote_id": None}
    )
    try:
        _release(fixture, [ids[1]])
        assert len(_create_rows(fixture)) == 1

        def active() -> bool:
            with fixture.runtime.session_factory() as session:
                return ProductionRepository(session).load_backlogs(
                    [fixture.project_id],
                    generation_terminal_statuses={"completed", "failed", "cancelled"},
                    upload_terminal_statuses=set(UPLOAD_JOB_TERMINAL_STATUSES),
                )[fixture.project_id].has_active_upload_task

        assert active() is False
        _force_status(fixture, ids[1], "pending")
        assert active() is True
    finally:
        fixture.engine.dispose()


# --- the cache helpers -----------------------------------------------------------------------


def test_destinations_cache_round_trips_the_r35_shape() -> None:
    fixture = _fixture("hub-order-cache-helpers")
    try:
        _seed_cache(fixture, remote_id=None, account_status="paused_risk")
        with fixture.runtime.session_factory() as session:
            fanqie = cached_destination(session, FANQIE_DESTINATION)
            qidian = cached_destination(session, QIDIAN_DESTINATION)
            missing = cached_destination(session, "not-a-destination")
            row = session.get(PublishHubSyncState, "destinations")
        assert (fanqie.platform, fanqie.account_status, fanqie.remote_id) == (
            "fanqie",
            "paused_risk",
            None,
        )
        assert qidian.remote_id == "qidian-book-1"
        assert missing is None
        assert row.value_json == {
            FANQIE_DESTINATION: {
                "account_status": "paused_risk",
                "remote_id": None,
                "platform": "fanqie",
            },
            QIDIAN_DESTINATION: {
                "account_status": "active",
                "remote_id": "qidian-book-1",
                "platform": "qidian",
            },
        }
        assert row.updated_at is not None

        # A refresh replaces the snapshot: a destination that is gone is gone.
        with fixture.runtime.session_factory.begin() as session:
            write_destinations(
                session,
                [{"id": QIDIAN_DESTINATION, "platform": "qidian", "remote_id": None,
                  "account_status": "active"}],
                now=NOW + timedelta(minutes=5),
            )
        with fixture.runtime.session_factory() as session:
            assert cached_destination(session, FANQIE_DESTINATION) is None
            assert session.scalar(select(func.count()).select_from(PublishHubSyncState)) == 1
    finally:
        fixture.engine.dispose()


# --- the extension queue keeps its behaviour ---------------------------------------------------


def test_extension_queue_route_is_not_gated() -> None:
    fixture, hub, _client_, ids = _ordered_fixture(
        "hub-order-extension-route", chapters=(1, 2), route="legacy", destination=None
    )
    try:
        plan, result = _run_publish(fixture, quota=2)

        assert result.publish_job_count == 2
        for job_id in ids.values():
            stored = _job(fixture, job_id)
            assert (stored.status, stored.hub_submit_body_json) == ("pending", None)
        assert hub.requests == []
        assert _create_rows(fixture) == []
        assert _hold_events(fixture, ids[1]) == []
    finally:
        fixture.engine.dispose()


def test_recovery_with_no_stored_requests_sends_nothing() -> None:
    fixture, hub, client, _ids = _ordered_fixture("hub-order-recover-empty", chapters=(1,), cache={})
    try:
        with fixture.runtime.session_factory() as session:
            assert recover_unsubmitted(session, client, NOW) == {}
        assert hub.requests == []
    finally:
        fixture.engine.dispose()
