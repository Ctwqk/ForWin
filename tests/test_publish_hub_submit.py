"""Task 4: a released job goes to publish-hub when its binding routes there, and only there.

The fake hub below keeps jobs by idempotency key and compares request bytes the way publish-hub
compares fingerprints: the same key with the same bytes is a replay (200, same job), the same key
with other bytes is 409 idempotency_conflict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from forwin.models.audit import DecisionEvent
from forwin.models.base import new_id
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadAttempt,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from forwin.publish_hub.client import PublishHubClient
from forwin.publish_hub.payloads import chapter_body, cover_body
from forwin.publish_hub.state import write_destinations
from forwin.publish_hub.submit import (
    UNRESOLVED_CONFLICT_ERROR,
    SubmitOutcome,
    recover_unsubmitted,
    submit_released_job,
)
from tests.test_canon_publisher_jobs import _fixture, _materialize
from tests.test_publisher_runtime_covers import PNG_1X1


NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
HUB_URL = "http://hub.test:18930"
API_KEY = "submit-test-key-not-a-secret"
QIDIAN_DESTINATION = "3b3c8f1e-0000-4000-8000-0000000000a1"
FANQIE_DESTINATION = "3b3c8f1e-0000-4000-8000-0000000000b2"


class FakeHub:
    """publish-hub's POST /v1/jobs and GET /v1/jobs?idempotency_key=, in memory."""

    def __init__(self) -> None:
        self.jobs_by_key: dict[str, tuple[bytes, dict]] = {}
        self.requests: list[httpx.Request] = []
        self.posts: list[bytes] = []
        self.published_refs: set[str] = set()
        # One entry per POST, in order: (status, code) refuses it, None lets it through.
        self.refusals: list[tuple[int, str] | None] = []
        self.new_job_status = "pending"
        self.down = False
        self.lose_next_response = False
        self.on_post = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("hub is down", request=request)
        if request.url.path != "/v1/jobs":
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        if request.method == "GET":
            entry = self.jobs_by_key.get(request.url.params["idempotency_key"])
            return httpx.Response(200, json={"items": [entry[1]] if entry else []})
        return self._post(request)

    def _post(self, request: httpx.Request) -> httpx.Response:
        content = request.content
        self.posts.append(content)
        if self.on_post is not None:
            self.on_post(request)
        refusal = self.refusals.pop(0) if self.refusals else None
        if refusal is not None:
            return _envelope(*refusal)
        body = json.loads(content)
        key = body["idempotency_key"]
        if key in self.jobs_by_key:
            stored, job = self.jobs_by_key[key]
            if stored != content:
                return _envelope(409, "idempotency_conflict")
            response = httpx.Response(200, json=job)
        elif body.get("source_ref") in self.published_refs:
            return _envelope(409, "publication_already_published")
        else:
            job = {
                "id": str(uuid4()),
                "status": self.new_job_status,
                "action": body["action"],
                "destination_id": body["destination_id"],
                "source_ref": body.get("source_ref"),
                "publication": None,
                "latest_receipt": None,
            }
            self.jobs_by_key[key] = (content, job)
            response = httpx.Response(201, json=job)
        if self.lose_next_response:
            self.lose_next_response = False
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return response

    def post_count(self) -> int:
        return len(self.posts)


def _envelope(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": f"refused: {code}"}})


def _client(hub: FakeHub) -> PublishHubClient:
    return PublishHubClient(HUB_URL, API_KEY, 5, transport=httpx.MockTransport(hub))


def _attach_hub(runtime, hub: FakeHub) -> PublishHubClient:
    """Configure the runtime the way the environment would, with the fake hub behind it."""
    client = _client(hub)
    runtime.publish_hub_url = HUB_URL
    runtime.publish_hub_api_key = API_KEY
    runtime._publish_hub_client = client
    return client


QIDIAN = {
    "platform": "qidian",
    "book_name": "事件时书名",
    "upload_url": "https://write.example/old",
    "create_if_missing": False,
    "publisher_compliance_required": False,
    "book_meta": {"audience": "male"},
}
FANQIE = {
    "platform": "fanqie",
    "book_name": "番茄版",
    "create_if_missing": False,
    "publisher_compliance_required": False,
}


def _set_routes(fixture, routes: dict[str, tuple[str, str | None]]) -> None:
    """Route the project's current publish bindings: platform -> (route, hub_destination_id)."""
    with fixture.runtime.session_factory.begin() as session:
        project = session.get(Project, fixture.project_id)
        bindings = []
        for template in (QIDIAN, FANQIE):
            platform = template["platform"]
            if platform not in routes:
                continue
            route, destination = routes[platform]
            bindings.append({**template, "route": route, "hub_destination_id": destination})
        project.automation_json = json.dumps({"publish_bindings": bindings})


def _seed_destinations(fixture) -> None:
    """Both destinations in the sync cache with their book: Task 5's release gate lets chapters by."""
    with fixture.runtime.session_factory.begin() as session:
        write_destinations(
            session,
            [
                {"id": QIDIAN_DESTINATION, "platform": "qidian", "remote_id": "qidian-book",
                 "account_status": "active"},
                {"id": FANQIE_DESTINATION, "platform": "fanqie", "remote_id": "fanqie-book",
                 "account_status": "active"},
            ],
            now=NOW,
        )


def _hub_fixture(name: str, *, platforms=("qidian",), routes=None):
    fixture = _fixture(name)
    hub = FakeHub()
    client = _attach_hub(fixture.runtime, hub)
    _seed_destinations(fixture)
    _set_routes(
        fixture,
        routes
        if routes is not None
        else {"qidian": ("hub", QIDIAN_DESTINATION), "fanqie": ("hub", FANQIE_DESTINATION)},
    )
    bindings = [QIDIAN if platform == "qidian" else FANQIE for platform in platforms]
    created = _materialize(fixture, bindings=bindings)
    return fixture, hub, client, created


def _release(fixture, job_ids, *, publish=True, session=None):
    return fixture.runtime.canon_jobs.release(
        project_id=fixture.project_id,
        job_ids=list(job_ids),
        publish=publish,
        actor_type="scheduler",
        session=session,
    )


def _job(fixture, job_id) -> PublisherUploadJob:
    with fixture.runtime.session_factory() as session:
        job = session.get(PublisherUploadJob, job_id)
        session.expunge(job)
        return job


def _canonical(body: dict) -> bytes:
    return json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _recover(fixture, client):
    with fixture.runtime.session_factory() as session:
        outcomes = recover_unsubmitted(session, client, NOW)
        session.commit()
    return outcomes


def _hub_events(fixture, job_id) -> list[dict]:
    """The audit events that record a publish-hub submit outcome for a job."""
    with fixture.runtime.session_factory() as session:
        events = (
            session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.related_object_id == job_id)
                .order_by(DecisionEvent.created_at.asc())
            )
            .scalars()
            .all()
        )
    payloads = [json.loads(event.payload_json or "{}") for event in events]
    return [payload for payload in payloads if "hub_outcome" in payload]


def _seed_hub_job(hub: FakeHub, key: str, *, status: str = "pending") -> dict:
    """A job publish-hub already holds for a key, from a request with other bytes."""
    job = {"id": str(uuid4()), "status": status, "publication": None, "latest_receipt": None}
    hub.jobs_by_key[key] = (b"an earlier request with other bytes", job)
    return job


# --- release point: 201 path ---------------------------------------------------------------


def test_hub_route_release_stores_the_body_before_posting_and_records_the_hub_job() -> None:
    fixture, hub, _client_, created = _hub_fixture("hub-submit-201")
    job_id = created[0]["job_id"]
    seen_stored_body = []
    try:
        session = fixture.runtime.session_factory()

        def body_is_flushed_before_post(_request) -> None:
            # Same transaction, no autoflush: this reads what was flushed to the database.
            with session.no_autoflush:
                seen_stored_body.append(
                    session.execute(
                        select(PublisherUploadJob.hub_submit_body_json).where(
                            PublisherUploadJob.id == job_id
                        )
                    ).scalar_one()
                )

        hub.on_post = body_is_flushed_before_post
        try:
            released = _release(fixture, [job_id], session=session)
            session.commit()
        finally:
            session.close()

        assert [item["job_id"] for item in released] == [job_id]
        stored = _job(fixture, job_id)
        expected_body = chapter_body(stored, QIDIAN_DESTINATION)
        assert seen_stored_body == [expected_body]
        assert stored.hub_submit_body_json == expected_body
        assert hub.posts == [_canonical(expected_body)]
        (hub_job_id,) = [job["id"] for _, job in hub.jobs_by_key.values()]
        assert stored.hub_job_id == hub_job_id
        assert stored.hub_status == "pending"
        assert stored.status == "pending"
        assert stored.hub_superseded_reason is None
        assert expected_body["payload"]["mode"] == "publish"
        assert expected_body["destination_id"] == QIDIAN_DESTINATION
    finally:
        fixture.engine.dispose()


def test_runtime_wiring_hands_the_configured_client_to_the_release_point() -> None:
    fixture, hub, _client_, created = _hub_fixture("hub-submit-wiring")
    try:
        provider = fixture.runtime.upload_jobs.publish_hub_client_provider
        assert provider() is fixture.runtime.publish_hub_client
        assert fixture.runtime.cover_service.publish_hub_client_provider() is provider()
        _release(fixture, [created[0]["job_id"]])
        assert hub.post_count() == 1
    finally:
        fixture.engine.dispose()


# --- crash recovery (Review Focus 1) --------------------------------------------------------


def test_answer_lost_after_post_is_recovered_by_resending_identical_bytes() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-lost-answer")
    job_id = created[0]["job_id"]
    try:
        hub.lose_next_response = True
        _release(fixture, [job_id])

        lost = _job(fixture, job_id)
        assert lost.status == "pending"
        assert lost.hub_job_id is None
        assert lost.hub_submit_body_json is not None
        assert len(hub.jobs_by_key) == 1  # the hub did apply it

        outcomes = _recover(fixture, client)

        assert outcomes == {job_id: SubmitOutcome.SUBMITTED}
        recovered = _job(fixture, job_id)
        ((first_bytes, hub_job),) = hub.jobs_by_key.values()
        assert recovered.hub_job_id == hub_job["id"]
        assert len(hub.posts) == 2
        assert hub.posts[0] == hub.posts[1] == first_bytes
    finally:
        fixture.engine.dispose()


def test_recovery_resends_the_stored_body_even_after_the_job_or_route_changed() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-resend-stored")
    job_id = created[0]["job_id"]
    try:
        hub.lose_next_response = True
        _release(fixture, [job_id])
        # Re-pointing the binding must not change what an already stored request says.
        _set_routes(fixture, {"qidian": ("hub", "another-destination")})

        _recover(fixture, client)

        assert hub.posts[0] == hub.posts[1]
        assert _job(fixture, job_id).hub_job_id is not None
    finally:
        fixture.engine.dispose()


def test_process_death_after_post_leaves_the_job_scheduled_and_the_next_release_gets_the_same_job() -> None:
    fixture, hub, _client_, created = _hub_fixture("hub-submit-death-same-body")
    job_id = created[0]["job_id"]
    try:
        session = fixture.runtime.session_factory()
        try:
            _release(fixture, [job_id], session=session)
            session.rollback()  # the process died before its transaction committed
        finally:
            session.close()
        died = _job(fixture, job_id)
        assert died.status == "scheduled"
        assert died.hub_submit_body_json is None
        ((_bytes, hub_job),) = hub.jobs_by_key.values()

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert stored.hub_job_id == hub_job["id"]
        assert hub.posts[0] == hub.posts[1]
        assert len(hub.jobs_by_key) == 1
    finally:
        fixture.engine.dispose()


def test_process_death_then_a_mode_change_adopts_the_job_found_by_key() -> None:
    fixture, hub, _client_, created = _hub_fixture("hub-submit-death-mode-change")
    job_id = created[0]["job_id"]
    try:
        session = fixture.runtime.session_factory()
        try:
            _release(fixture, [job_id], publish=True, session=session)
            session.rollback()
        finally:
            session.close()
        ((_bytes, hub_job),) = hub.jobs_by_key.values()

        _release(fixture, [job_id], publish=False)

        stored = _job(fixture, job_id)
        assert stored.hub_job_id == hub_job["id"]
        assert stored.status == "pending"
        assert [request.method for request in hub.requests] == ["POST", "POST", "GET"]
        assert len(hub.jobs_by_key) == 1
    finally:
        fixture.engine.dispose()


def test_idempotency_conflict_without_a_job_for_the_key_fails_the_row_for_a_person() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-conflict-unknown")
    job_id = created[0]["job_id"]
    try:
        hub.refusals.append((409, "idempotency_conflict"))

        released = _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert released == []  # nothing to upload: not counted as released (R36)
        assert stored.status == "failed"
        assert stored.error_message == UNRESOLVED_CONFLICT_ERROR
        assert stored.hub_submit_body_json is not None  # still hub-owned
        assert stored.hub_job_id is None
        assert [request.method for request in hub.requests] == ["POST", "GET"]
        assert [event["hub_outcome"] for event in _hub_events(fixture, job_id)] == ["failed"]
        assert _recover(fixture, client) == {}
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-conflict", connected_platforms=["qidian"], now=NOW
            )
            is None
        )
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("conflicted", [0, 1])
def test_unresolved_conflict_in_a_release_batch_leaves_the_siblings_submitted(conflicted) -> None:
    fixture, hub, _client_, created = _hub_fixture(
        f"hub-submit-conflict-batch-{conflicted}", platforms=("qidian", "fanqie")
    )
    job_ids = [item["job_id"] for item in created]
    try:
        hub.refusals = [None, None]
        hub.refusals[conflicted] = (409, "idempotency_conflict")

        released = _release(fixture, job_ids)

        failed_id, sibling_id = job_ids[conflicted], job_ids[1 - conflicted]
        failed, sibling = _job(fixture, failed_id), _job(fixture, sibling_id)
        assert [item["job_id"] for item in released] == [sibling_id]
        assert (failed.status, failed.error_message) == ("failed", UNRESOLVED_CONFLICT_ERROR)
        assert failed.hub_submit_body_json is not None
        assert sibling.status == "pending"
        assert sibling.hub_job_id is not None  # committed together with the failed row
        assert [event["hub_outcome"] for event in _hub_events(fixture, failed_id)] == ["failed"]
        assert [event["hub_outcome"] for event in _hub_events(fixture, sibling_id)] == [
            "submitted"
        ]
    finally:
        fixture.engine.dispose()


def test_unresolved_conflict_during_recovery_fails_that_row_and_recovers_the_rest() -> None:
    fixture, hub, client, created = _hub_fixture(
        "hub-submit-conflict-recovery", platforms=("qidian", "fanqie")
    )
    job_ids = [item["job_id"] for item in created]
    try:
        hub.down = True
        _release(fixture, job_ids)
        hub.down = False
        hub.refusals = [(409, "idempotency_conflict"), None]

        outcomes = _recover(fixture, client)

        assert sorted(outcomes.values()) == sorted(
            [SubmitOutcome.FAILED, SubmitOutcome.SUBMITTED]
        )
        (failed_id,) = [job_id for job_id, outcome in outcomes.items() if outcome == "failed"]
        (submitted_id,) = [job_id for job_id in job_ids if job_id != failed_id]
        failed, submitted = _job(fixture, failed_id), _job(fixture, submitted_id)
        assert (failed.status, failed.error_message) == ("failed", UNRESOLVED_CONFLICT_ERROR)
        assert failed.hub_submit_body_json is not None
        assert submitted.hub_job_id is not None
        resubmitted = [
            event for event in _hub_events(fixture, failed_id)
            if event["transition"] == "hub_resubmitted"
        ]
        assert [event["hub_outcome"] for event in resubmitted] == ["failed"]
        assert _recover(fixture, client) == {}
    finally:
        fixture.engine.dispose()


def test_recovery_adopts_the_job_found_by_key_after_an_idempotency_conflict() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-conflict-adopt-recovery")
    job_id = created[0]["job_id"]
    try:
        hub.down = True
        _release(fixture, [job_id])
        hub.down = False
        key = _job(fixture, job_id).hub_submit_body_json["idempotency_key"]
        earlier = _seed_hub_job(hub, key, status="succeeded")

        assert _recover(fixture, client) == {job_id: SubmitOutcome.SUBMITTED}

        stored = _job(fixture, job_id)
        assert stored.hub_job_id == earlier["id"]
        # R30: a terminal hub status is recorded, the local row waits for the sync to mirror it.
        assert (stored.hub_status, stored.status) == ("succeeded", "pending")
        assert stored.finished_at is None
        assert [request.method for request in hub.requests] == ["POST", "POST", "GET"]
    finally:
        fixture.engine.dispose()


def test_stored_body_freezes_the_publish_mode_so_recovery_cannot_conflict() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-mode-frozen")
    job_id = created[0]["job_id"]
    try:
        hub.down = True
        _release(fixture, [job_id], publish=True)

        with pytest.raises(ValueError, match="frozen"):
            _release(fixture, [job_id], publish=False)

        frozen = _job(fixture, job_id)
        assert frozen.publish is True
        assert frozen.hub_submit_body_json["payload"]["mode"] == "publish"

        hub.down = False
        assert _recover(fixture, client) == {job_id: SubmitOutcome.SUBMITTED}
        assert json.loads(hub.posts[-1])["payload"]["mode"] == "publish"
    finally:
        fixture.engine.dispose()


# --- refusals and outages -------------------------------------------------------------------


def test_regenerated_chapter_already_published_is_superseded_not_retried() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-superseded")
    job_id = created[0]["job_id"]
    try:
        hub.published_refs.add(f"project/{fixture.project_id}/chapter/{fixture.chapter_number}")

        released = _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert stored.status == "superseded"
        assert stored.hub_superseded_reason == "publication_already_published"
        assert stored.hub_job_id is None
        assert stored.hub_submit_body_json is not None
        assert stored.finished_at is not None
        assert released == []  # nothing was uploaded: not counted as released (R36)
        assert _recover(fixture, client) == {}
        assert hub.post_count() == 1
    finally:
        fixture.engine.dispose()


def test_network_error_keeps_the_row_pending_without_a_hub_job_id() -> None:
    fixture, hub, client, created = _hub_fixture("hub-submit-network-error")
    job_id = created[0]["job_id"]
    try:
        hub.down = True

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert stored.status == "pending"
        assert stored.hub_job_id is None
        assert stored.hub_status is None
        assert stored.hub_submit_body_json == chapter_body(stored, QIDIAN_DESTINATION)
        assert len(hub.jobs_by_key) == 0
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_holds_the_row_like_an_unavailable_hub(status) -> None:
    fixture, hub, client, created = _hub_fixture(f"hub-submit-auth-{status}")
    job_id = created[0]["job_id"]
    try:
        hub.refusals.append((status, "unauthorized" if status == 401 else "forbidden"))

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_job_id) == ("pending", None)
        assert stored.hub_submit_body_json is not None
        assert _recover(fixture, client) == {job_id: SubmitOutcome.SUBMITTED}
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("code", ["publication_has_active_job", "destination_has_active_job"])
def test_active_job_conflicts_keep_the_row_pending_and_recovery_retries(code) -> None:
    fixture, hub, client, created = _hub_fixture(f"hub-submit-active-{code[:11]}")
    job_id = created[0]["job_id"]
    try:
        hub.refusals.append((409, code))

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_job_id) == ("pending", None)
        hub.refusals.append((409, code))
        assert _recover(fixture, client) == {job_id: SubmitOutcome.DEFERRED_ACTIVE_JOB}
        assert _recover(fixture, client) == {job_id: SubmitOutcome.SUBMITTED}
        assert hub.posts[0] == hub.posts[1] == hub.posts[2]
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize(
    ("status", "code"),
    [(422, "invalid_payload"), (409, "account_disabled"), (404, "not_found")],
)
def test_other_refusals_fail_the_row_with_the_code_and_keep_the_body(status, code) -> None:
    fixture, hub, client, created = _hub_fixture(f"hub-submit-refused-{status}")
    job_id = created[0]["job_id"]
    try:
        hub.refusals.append((status, code))

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert stored.status == "failed"
        assert stored.error_message == code
        assert stored.hub_job_id is None
        assert stored.hub_submit_body_json is not None
        assert stored.finished_at is not None
        assert _recover(fixture, client) == {}
        assert hub.post_count() == 1
    finally:
        fixture.engine.dispose()


def test_unconfigured_hub_holds_the_row_and_never_uses_the_extension_queue() -> None:
    fixture, hub, _client_, created = _hub_fixture("hub-submit-unconfigured")
    job_id = created[0]["job_id"]
    try:
        fixture.runtime.publish_hub_url = ""
        fixture.runtime.publish_hub_api_key = ""

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_job_id) == ("pending", None)
        assert stored.hub_submit_body_json is not None
        assert hub.requests == []
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-unconfigured", connected_platforms=["qidian"], now=NOW
            )
            is None
        )
        with fixture.runtime.session_factory() as session:
            assert recover_unsubmitted(session, None, NOW) == {}
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize(
    ("hub_status", "local_status"),
    [
        ("scheduled", "pending"),
        ("pending", "pending"),
        ("reconciling", "pending"),
        ("paused", "paused"),
        # R30: a terminal status is the sync process's to mirror, with its binding projection.
        ("succeeded", "pending"),
        ("failed", "pending"),
        ("cancelled", "pending"),
    ],
)
def test_hub_status_maps_to_the_local_status(hub_status, local_status) -> None:
    fixture, hub, _client_, created = _hub_fixture(f"hub-submit-map-{hub_status}")
    job_id = created[0]["job_id"]
    try:
        hub.new_job_status = hub_status

        _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert stored.hub_job_id is not None
        assert (stored.hub_status, stored.status) == (hub_status, local_status)
        assert stored.finished_at is None
    finally:
        fixture.engine.dispose()


# --- an unusable hub ends the pass (R32) ----------------------------------------------------


@pytest.mark.parametrize("failure", ["down", 401, 403])
def test_release_batch_stops_sending_once_the_hub_cannot_be_used(failure) -> None:
    fixture, hub, _client_, created = _hub_fixture(
        f"hub-submit-stop-release-{failure}", platforms=("qidian", "fanqie")
    )
    job_ids = [item["job_id"] for item in created]
    try:
        if failure == "down":
            hub.down = True
        else:
            hub.refusals.append((failure, "unauthorized" if failure == 401 else "forbidden"))

        released = _release(fixture, job_ids)

        assert len(hub.requests) == 1  # the second row was not sent
        assert {item["job_id"] for item in released} == set(job_ids)
        for job_id in job_ids:
            stored = _job(fixture, job_id)
            assert (stored.status, stored.hub_job_id) == ("pending", None)
            assert stored.hub_submit_body_json is not None
    finally:
        fixture.engine.dispose()


def test_recovery_round_stops_sending_once_the_hub_is_down() -> None:
    fixture, hub, client, created = _hub_fixture(
        "hub-submit-stop-recovery", platforms=("qidian", "fanqie")
    )
    job_ids = [item["job_id"] for item in created]
    try:
        hub.down = True
        _release(fixture, job_ids)
        hub.requests.clear()

        outcomes = _recover(fixture, client)

        assert outcomes == {job_id: SubmitOutcome.DEFERRED_UNAVAILABLE for job_id in job_ids}
        assert len(hub.requests) == 1
        hub.down = False
        assert _recover(fixture, client) == {
            job_id: SubmitOutcome.SUBMITTED for job_id in job_ids
        }
    finally:
        fixture.engine.dispose()


def test_recover_resends_only_pending_rows_that_have_a_body_and_no_hub_job() -> None:
    fixture, hub, client, created = _hub_fixture(
        "hub-submit-recover-filter", platforms=("qidian", "fanqie")
    )
    qidian_id, fanqie_id = (item["job_id"] for item in created)
    try:
        hub.down = True
        _release(fixture, [qidian_id, fanqie_id])
        with fixture.runtime.session_factory.begin() as session:
            session.get(PublisherUploadJob, fanqie_id).status = "failed"
        hub.down = False
        hub.requests.clear()

        assert _recover(fixture, client) == {qidian_id: SubmitOutcome.SUBMITTED}
        assert _recover(fixture, client) == {}
        assert [json.loads(post)["destination_id"] for post in hub.posts[-1:]] == [
            QIDIAN_DESTINATION
        ]
        assert len(hub.requests) == 1
    finally:
        fixture.engine.dispose()


# --- routing --------------------------------------------------------------------------------


def test_extension_queue_route_never_calls_the_client() -> None:
    fixture = _fixture("hub-submit-extension-route")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("legacy", QIDIAN_DESTINATION)})
        created = _materialize(fixture, bindings=[QIDIAN])
        provider_calls = []
        provider = fixture.runtime.upload_jobs.publish_hub_client_provider
        fixture.runtime.upload_jobs.publish_hub_client_provider = (
            lambda: provider_calls.append(1) or provider()
        )

        released = _release(fixture, [created[0]["job_id"]])

        stored = _job(fixture, created[0]["job_id"])
        assert released[0]["status"] == "pending"
        assert stored.hub_submit_body_json is None
        assert stored.hub_job_id is None
        assert hub.requests == []
        assert provider_calls == []
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-route", connected_platforms=["qidian"], now=NOW
        )
        assert claimed is not None and claimed["job_id"] == stored.id
    finally:
        fixture.engine.dispose()


def test_hub_route_without_a_destination_keeps_the_job_scheduled() -> None:
    fixture, hub, _client_, created = _hub_fixture(
        "hub-submit-no-destination", routes={"qidian": ("hub", None)}
    )
    job_id = created[0]["job_id"]
    try:
        released = _release(fixture, [job_id])

        stored = _job(fixture, job_id)
        assert released == []
        assert stored.status == "scheduled"
        assert stored.hub_submit_body_json is None
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


def test_blocked_preflight_on_a_hub_route_submits_nothing() -> None:
    fixture, hub, _client_, created = _hub_fixture("hub-submit-blocked-preflight")

    class BlockingPreflight:
        @staticmethod
        def check_upload_readiness(**_request):
            return {"ok": False, "blocking": [{"code": "missing"}], "warnings": []}

    fixture.runtime.upload_jobs.preflight = BlockingPreflight()
    try:
        assert _release(fixture, [created[0]["job_id"]]) == []
        assert _job(fixture, created[0]["job_id"]).status == "scheduled"
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


def test_route_flip_after_scheduling_uses_current_settings_and_keeps_the_snapshot_valid() -> None:
    fixture = _fixture("hub-submit-route-flip")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _seed_destinations(fixture)
        _set_routes(fixture, {"qidian": ("legacy", None)})
        created = _materialize(fixture, bindings=[{**QIDIAN, "route": "legacy"}])
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})

        # The same Canon event replayed after the cutover still matches the scheduled job.
        replay = _materialize(
            fixture,
            bindings=[{**QIDIAN, "route": "hub", "hub_destination_id": QIDIAN_DESTINATION}],
        )
        assert replay[0]["job_id"] == created[0]["job_id"]

        _release(fixture, [created[0]["job_id"]])

        stored = _job(fixture, created[0]["job_id"])
        assert stored.hub_job_id is not None
        assert json.loads(hub.posts[0])["destination_id"] == QIDIAN_DESTINATION
    finally:
        fixture.engine.dispose()


# --- covers ---------------------------------------------------------------------------------


def _cover_setup(fixture, tmp_path: Path, *, content: bytes = PNG_1X1):
    cover_file = tmp_path / "cover.png"
    cover_file.write_bytes(content)
    with fixture.runtime.session_factory.begin() as session:
        work = PublisherWorkBinding(
            project_id=fixture.project_id,
            platform_id="qidian",
            book_name="事件时书名",
            remote_book_id="book-cover",
        )
        session.add(work)
        session.flush()
        cover = PublisherCoverAsset(
            project_id=fixture.project_id,
            work_binding_id=work.id,
            status="selected",
            selection_state="selected",
            file_path=str(cover_file),
            mime_type="image/png",
        )
        session.add(cover)
        session.flush()
        return work.id, cover.id, cover_file


def test_hub_routed_cover_upload_is_submitted_when_it_is_enqueued(tmp_path) -> None:
    fixture = _fixture("hub-submit-cover-manual")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})
        _work_id, cover_id, cover_file = _cover_setup(fixture, tmp_path)

        job = fixture.runtime.cover_service.enqueue_cover_upload(cover_id)

        stored = _job(fixture, job.id)
        expected = cover_body(stored, QIDIAN_DESTINATION)
        assert stored.hub_submit_body_json == expected
        assert hub.posts == [_canonical(expected)]
        assert expected["content_refs"][0]["uri"] == f"file://{cover_file}"
        assert stored.hub_job_id is not None
        assert stored.status == "pending"
        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-cover", connected_platforms=["qidian"], now=NOW
            )
            is None
        )
    finally:
        fixture.engine.dispose()


def test_hub_routed_cover_after_the_first_chapter_is_submitted(tmp_path) -> None:
    fixture = _fixture("hub-submit-cover-auto")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})
        work_id, _cover_id, _file = _cover_setup(fixture, tmp_path)
        created = _materialize(fixture, bindings=[QIDIAN])
        with fixture.runtime.session_factory() as session:
            work = session.get(PublisherWorkBinding, work_id)
            session.add(
                PublisherChapterBinding(
                    project_id=fixture.project_id,
                    work_binding_id=work.id,
                    platform_id="qidian",
                    chapter_number=1,
                    publish_state="published",
                )
            )
            session.flush()
            chapter_job = session.get(PublisherUploadJob, created[0]["job_id"])
            cover_job = fixture.runtime.cover_service.enqueue_cover_upload_if_ready(
                session, job=chapter_job, payload={}, work_binding=work
            )
            session.commit()
            cover_job_id = cover_job.id

        stored = _job(fixture, cover_job_id)
        assert stored.task_kind == "cover_upload"
        assert stored.hub_job_id is not None
        assert hub.post_count() == 1
    finally:
        fixture.engine.dispose()


def test_extension_queue_cover_never_calls_the_client(tmp_path) -> None:
    fixture = _fixture("hub-submit-cover-extension")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("legacy", None)})
        _work_id, cover_id, _file = _cover_setup(fixture, tmp_path)

        job = fixture.runtime.cover_service.enqueue_cover_upload(cover_id)

        stored = _job(fixture, job.id)
        assert stored.hub_submit_body_json is None
        assert stored.status == "pending"
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


def test_hub_routed_cover_without_a_destination_is_refused_and_not_queued(tmp_path) -> None:
    fixture = _fixture("hub-submit-cover-no-destination")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("hub", None)})
        _work_id, cover_id, _file = _cover_setup(fixture, tmp_path)

        with pytest.raises(ValueError, match="publish-hub"):
            fixture.runtime.cover_service.enqueue_cover_upload(cover_id)

        with fixture.runtime.session_factory() as session:
            assert (
                session.scalar(
                    select(func.count(PublisherUploadJob.id)).where(
                        PublisherUploadJob.task_kind == "cover_upload"
                    )
                )
                == 0
            )
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


def test_cover_whose_file_changed_since_the_job_was_made_fails_the_row(tmp_path) -> None:
    fixture = _fixture("hub-submit-cover-hash")
    hub = FakeHub()
    try:
        client = _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})
        work_id, cover_id, cover_file = _cover_setup(fixture, tmp_path)
        with fixture.runtime.session_factory() as session:
            job = PublisherUploadJob(
                project_id=fixture.project_id,
                idempotency_key="cover-hash-test",
                platform_id="qidian",
                task_kind="cover_upload",
                status="pending",
                book_name="事件时书名",
                body_text="",
                body_sha256=hashlib.sha256(b"the bytes the job was made from").hexdigest(),
                result_payload_json=json.dumps(
                    {
                        "work_binding_id": work_id,
                        "cover_asset_id": cover_id,
                        "file_path": str(cover_file),
                    }
                ),
            )
            session.add(job)
            session.flush()

            outcome = submit_released_job(session, job, client, now=NOW)
            session.commit()
            job_id = job.id

        assert outcome == SubmitOutcome.FAILED
        stored = _job(fixture, job_id)
        assert stored.status == "failed"
        assert "sha256" in stored.error_message
        assert stored.hub_submit_body_json is None
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


def test_unresolved_conflict_on_a_cover_fails_the_row_and_commits_it(tmp_path) -> None:
    fixture = _fixture("hub-submit-cover-conflict")
    hub = FakeHub()
    try:
        client = _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})
        _work_id, cover_id, _file = _cover_setup(fixture, tmp_path)
        hub.refusals.append((409, "idempotency_conflict"))

        job = fixture.runtime.cover_service.enqueue_cover_upload(cover_id)

        stored = _job(fixture, job.id)
        assert (stored.status, stored.error_message) == ("failed", UNRESOLVED_CONFLICT_ERROR)
        assert stored.hub_submit_body_json is not None
        assert [request.method for request in hub.requests] == ["POST", "GET"]
        assert [event["hub_outcome"] for event in _hub_events(fixture, job.id)] == ["failed"]
        assert _recover(fixture, client) == {}
    finally:
        fixture.engine.dispose()


# --- direct upload jobs -----------------------------------------------------------------------


def _direct_upload(fixture, **overrides):
    request = {
        "project_id": fixture.project_id,
        "platform": "qidian",
        "book_name": "事件时书名",
        "chapter_title": "第八章 直接上传",
        "body": "直接上传的正文。",
        "upload_url": None,
        "publish": True,
        "book_meta": {"audience": "male"},
    }
    request.update(overrides)
    return fixture.runtime.upload_jobs.create_upload_job(**request)


def test_direct_upload_is_refused_on_a_hub_routed_binding() -> None:
    fixture = _fixture("hub-submit-direct-refused")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})
        fixture.runtime.upload_jobs.preflight = None

        with pytest.raises(ValueError, match="publish-hub"):
            _direct_upload(fixture, create_if_missing=True)

        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherUploadJob.id))) == 0
        assert hub.requests == []
    finally:
        fixture.engine.dispose()


def test_direct_upload_on_an_extension_queue_binding_is_unchanged() -> None:
    fixture = _fixture("hub-submit-direct-extension")
    hub = FakeHub()
    try:
        _attach_hub(fixture.runtime, hub)
        _set_routes(fixture, {"qidian": ("legacy", None), "fanqie": ("hub", FANQIE_DESTINATION)})

        created = _direct_upload(fixture)

        stored = _job(fixture, created["job_id"])
        assert (stored.status, stored.task_kind) == ("pending", "chapter_upload")
        assert stored.hub_submit_body_json is None
        assert hub.requests == []
        claimed = fixture.runtime.attempts.claim(
            client_id="extension-direct", connected_platforms=["qidian"], now=NOW
        )
        assert claimed is not None and claimed["job_id"] == stored.id
    finally:
        fixture.engine.dispose()


# --- extension-queue runtime leaves hub rows alone (R5) -----------------------------------


def _row_state(fixture, job_id) -> dict:
    job = _job(fixture, job_id)
    with fixture.runtime.session_factory() as session:
        attempts = session.scalar(
            select(func.count(PublisherUploadAttempt.id)).where(
                PublisherUploadAttempt.upload_job_id == job_id
            )
        )
    return {
        "status": job.status,
        "current_attempt_id": job.current_attempt_id,
        "extension_client_id": job.extension_client_id,
        "available_at": job.available_at,
        "reconcile_after": job.reconcile_after,
        "claimed_at": job.claimed_at,
        "finished_at": job.finished_at,
        "abort_requested": job.abort_requested,
        "result_message": job.result_message,
        "error_message": job.error_message,
        "result_payload_json": job.result_payload_json,
        "hub_job_id": job.hub_job_id,
        "hub_status": job.hub_status,
        "hub_submit_body_json": job.hub_submit_body_json,
        "attempts": attempts,
    }


def _force_status(fixture, job_id, status: str) -> None:
    with fixture.runtime.session_factory.begin() as session:
        session.get(PublisherUploadJob, job_id).status = status


def test_hub_owned_pending_fanqie_row_is_not_handed_to_an_extension_claim() -> None:
    fixture, hub, _client_, created = _hub_fixture(
        "hub-submit-no-extension-claim",
        platforms=("qidian", "fanqie"),
        routes={"qidian": ("legacy", None), "fanqie": ("hub", FANQIE_DESTINATION)},
    )
    qidian_id, fanqie_id = (item["job_id"] for item in created)
    try:
        _release(fixture, [qidian_id, fanqie_id])
        hub_row = _job(fixture, fanqie_id)
        assert (hub_row.status, hub_row.hub_status) == ("pending", "pending")
        before = _row_state(fixture, fanqie_id)

        first = fixture.runtime.attempts.claim(
            client_id="extension-a", connected_platforms=["qidian", "fanqie"], now=NOW
        )
        second = fixture.runtime.attempts.claim(
            client_id="extension-b", connected_platforms=["qidian", "fanqie"], now=NOW
        )
        only_fanqie = fixture.runtime.attempts.claim(
            client_id="extension-c", connected_platforms=["fanqie"], now=NOW
        )

        assert first is not None and first["job_id"] == qidian_id
        assert second is None
        assert only_fanqie is None
        assert _row_state(fixture, fanqie_id) == before
    finally:
        fixture.engine.dispose()


def test_unsubmitted_hub_row_is_not_claimed_either() -> None:
    fixture, hub, _client_, created = _hub_fixture(
        "hub-submit-no-claim-unsubmitted",
        platforms=("fanqie",),
        routes={"fanqie": ("hub", FANQIE_DESTINATION)},
    )
    job_id = created[0]["job_id"]
    try:
        hub.down = True
        _release(fixture, [job_id])
        before = _row_state(fixture, job_id)
        assert before["hub_job_id"] is None

        assert (
            fixture.runtime.attempts.claim(
                client_id="extension-a", connected_platforms=["qidian", "fanqie"], now=NOW
            )
            is None
        )
        assert _row_state(fixture, job_id) == before
    finally:
        fixture.engine.dispose()


def test_extension_queue_sweeps_and_operator_actions_leave_hub_rows_unchanged() -> None:
    fixture, hub, _client_, created = _hub_fixture(
        "hub-submit-sweeps", platforms=("fanqie",), routes={"fanqie": ("hub", FANQIE_DESTINATION)}
    )
    job_id = created[0]["job_id"]
    try:
        _release(fixture, [job_id])
        later = NOW + timedelta(days=1)
        for status in ("pending", "reconciling", "running", "terminating", "paused"):
            _force_status(fixture, job_id, status)
            before = _row_state(fixture, job_id)

            fixture.runtime.attempts.recover_interrupted(now=later)
            claimed = fixture.runtime.attempts.claim(
                client_id=f"extension-{status}",
                connected_platforms=["qidian", "fanqie"],
                now=later,
            )
            with pytest.raises(ValueError, match="publish-hub"):
                fixture.runtime.upload_jobs.terminate_upload_job(job_id)
            with pytest.raises(ValueError, match="publish-hub"):
                fixture.runtime.attempts.resume(
                    job_id=job_id,
                    expected_pause_reason="captcha",
                    expected_pause_token="token",
                    operator_reason="looked at it",
                    operator_actor_id="operator",
                    operator_auth_method="basic",
                    now=later,
                )

            assert claimed is None
            assert _row_state(fixture, job_id) == before, status
        serialized = fixture.runtime.upload_jobs.get_upload_job(job_id)
        assert serialized["terminable"] is False
        assert serialized["resumable"] is False
    finally:
        fixture.engine.dispose()


def test_create_book_rows_are_never_claimed_or_swept() -> None:
    fixture = _fixture("hub-submit-create-book")
    try:
        with fixture.runtime.session_factory.begin() as session:
            row = PublisherUploadJob(
                project_id=fixture.project_id,
                idempotency_key=f"forwin:create:{fixture.project_id}:qidian:abc",
                platform_id="qidian",
                task_kind="create_book",
                status="pending",
                body_text="",
                body_sha256=hashlib.sha256(b"").hexdigest(),
            )
            session.add(row)
            session.flush()
            row_id = row.id
        for status in ("pending", "reconciling", "running"):
            _force_status(fixture, row_id, status)
            before = _row_state(fixture, row_id)
            fixture.runtime.attempts.recover_interrupted(now=NOW)
            assert (
                fixture.runtime.attempts.claim(
                    client_id=f"extension-{status}", connected_platforms=["qidian"], now=NOW
                )
                is None
            )
            assert _row_state(fixture, row_id) == before
    finally:
        fixture.engine.dispose()


def test_backend_worker_skips_rows_owned_by_publish_hub(tmp_path) -> None:
    fixture = _fixture("hub-submit-backend-worker")
    try:
        fixture.runtime.cover_service.cover_dir = tmp_path
        with fixture.runtime.session_factory.begin() as session:
            row = PublisherUploadJob(
                id=new_id(),
                project_id=fixture.project_id,
                platform_id="qidian",
                task_kind="cover_generate",
                status="pending",
                body_text="",
                hub_submit_body_json={"idempotency_key": "forwin:held"},
            )
            session.add(row)
            session.flush()
            row_id = row.id
        assert fixture.runtime.backend_jobs.claim_next_cover_generate_job() is None
        _force_status(fixture, row_id, "running")
        before = _row_state(fixture, row_id)
        assert fixture.runtime.backend_jobs.recover_interrupted_cover_jobs(now=NOW) == []
        assert _row_state(fixture, row_id) == before
    finally:
        fixture.engine.dispose()
