"""Task 6: the publish-hub-sync process mirrors publish-hub's results into ForWin.

The hub below extends Task 4's FakeHub with the producer reads the sync uses: GET /v1/jobs/{id}
(publish-hub's job_view), GET /v1/changes (the change feed, numbered in commit order) and
GET /v1/destinations. Jobs are created through the real release point, so the rows mirrored
here are exactly the rows Tasks 4 and 5 leave behind.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
    PublishHubSyncState,
)
from forwin.publish_hub import sync as sync_module
from forwin.publish_hub.gate import earlier_chapter_not_done, hold_reason_for
from forwin.publish_hub.payloads import cover_body
from forwin.publish_hub.state import DESTINATIONS_KEY, cached_destination
from forwin.publish_hub.submit import SubmitOutcome
from forwin.publish_hub.sync import CHANGES_KEY, run_forever, sync_once
from forwin.publisher_runtime.bindings import PublisherBindingService
from tests.test_canon_publisher_jobs import _fixture, _materialize
from tests.test_publish_hub_submit import (
    API_KEY,
    FANQIE,
    FANQIE_DESTINATION,
    NOW,
    QIDIAN,
    QIDIAN_DESTINATION,
    FakeHub,
    _attach_hub,
    _canonical,
    _cover_setup,
    _envelope,
    _job,
    _release,
    _set_routes,
)
from tests.test_publisher_runtime_covers import PNG_1X1

FANQIE_BOOK = "fanqie-book"
QIDIAN_BOOK = "qidian-book"


class ProcessDied(BaseException):
    """The process is killed in the middle of a round: nothing in it may be caught."""


class SyncHub(FakeHub):
    """FakeHub plus the reads of the sync process, with the change feed of the real hub."""

    def __init__(self) -> None:
        super().__init__()
        self.feed: list[dict] = []
        self.destination_items = [
            {"id": QIDIAN_DESTINATION, "account_id": "acc-q", "platform": "qidian",
             "kind": "novel_book", "remote_id": QIDIAN_BOOK, "remote_url": None,
             "settings": {}, "account_status": "active"},
            {"id": FANQIE_DESTINATION, "account_id": "acc-f", "platform": "fanqie",
             "kind": "novel_book", "remote_id": FANQIE_BOOK, "remote_url": None,
             "settings": {}, "account_status": "active"},
        ]
        self.get_job_calls: list[str] = []
        self.on_get_job = None
        self.get_job_refusal: tuple[int, str] | None = None
        self.destinations_calls = 0

    # --- the hub's side of a job's life -------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/jobs/"):
            self.requests.append(request)
            job_id = path.rsplit("/", 1)[-1]
            if self.on_get_job is not None:
                self.on_get_job(job_id)
            if self.down:
                raise httpx.ConnectError("hub is down", request=request)
            self.get_job_calls.append(job_id)
            if self.get_job_refusal is not None:
                status, code = self.get_job_refusal
                return httpx.Response(status, json={"error": {"code": code, "message": code}})
            job = self.job(job_id)
            if job is None:
                return httpx.Response(
                    404, json={"error": {"code": "not_found", "message": "job not found"}}
                )
            return httpx.Response(200, json=job)
        if request.method == "GET" and path == "/v1/changes":
            self.requests.append(request)
            if self.down:
                raise httpx.ConnectError("hub is down", request=request)
            after = int(request.url.params["after"])
            limit = int(request.url.params["limit"])
            items = [item for item in self.feed if item["seq"] > after][:limit]
            return httpx.Response(
                200, json={"items": items, "next_after": items[-1]["seq"] if items else after}
            )
        if request.method == "GET" and path == "/v1/destinations":
            self.requests.append(request)
            if self.down:
                raise httpx.ConnectError("hub is down", request=request)
            self.destinations_calls += 1
            return httpx.Response(200, json=copy.deepcopy(self.destination_items))
        return super().__call__(request)

    def _post(self, request: httpx.Request) -> httpx.Response:
        refusal = self._cover_guard(json.loads(request.content))
        if refusal is not None:
            self.posts.append(request.content)
            return _envelope(409, refusal)
        known = set(self.jobs_by_key)
        response = super()._post(request)
        for key, (_content, job) in self.jobs_by_key.items():
            if key in known:
                continue
            if job.get("source_ref"):
                job["publication"] = {
                    "id": str(uuid4()), "remote_id": None, "remote_url": None,
                    "official_state": None,
                }
                self.change("publication", job["publication"]["id"], "created")
            job.setdefault("last_error", None)
            self.change("job", job["id"], job["status"])
        return response

    def _cover_guard(self, body: dict) -> str | None:
        """publish-hub's duplicate guard for upload_cover (its migration 0002): a new key on a
        publication with a succeeded or an active upload_cover is refused."""
        if body.get("action") != "upload_cover" or body["idempotency_key"] in self.jobs_by_key:
            return None
        same = [
            job for job in self.jobs("upload_cover")
            if (job["destination_id"], job["source_ref"])
            == (body["destination_id"], body.get("source_ref"))
        ]
        if any(job["status"] == "succeeded" for job in same):
            return "publication_already_published"
        if any(job["status"] in {"scheduled", "pending", "reconciling", "paused"} for job in same):
            return "publication_has_active_job"
        return None

    def change(self, entity: str, entity_id: str, status: str) -> None:
        self.feed.append(
            {"seq": len(self.feed) + 1, "entity": entity, "entity_id": entity_id,
             "status": status, "at": NOW.isoformat()}
        )

    def job(self, job_id: str) -> dict | None:
        for _content, job in self.jobs_by_key.values():
            if job["id"] == job_id:
                return job
        return None

    def jobs(self, action: str | None = None) -> list[dict]:
        return [
            job for _content, job in self.jobs_by_key.values()
            if action is None or job.get("action") == action
        ]

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        official_state: str | None = None,
        remote_id: str | None = None,
        remote_url: str | None = None,
        last_error: dict | None = None,
    ) -> dict:
        """Move a job to `status`, with a receipt when `official_state` is given."""
        job = self.job(job_id)
        if official_state is not None:
            job["latest_receipt"] = {
                "official_state": official_state, "remote_id": remote_id,
                "remote_url": remote_url, "executor_ref": "ext-1",
                "observed_at": NOW.isoformat(),
            }
            publication = job.get("publication")
            if publication is not None:
                publication.update(
                    remote_id=remote_id or publication["remote_id"],
                    remote_url=remote_url or publication["remote_url"],
                    official_state=official_state,
                )
                self.change("publication", publication["id"], official_state)
        job["status"] = status
        job["last_error"] = last_error
        self.change("job", job_id, status)
        return job

    def last_seq(self) -> int:
        return self.feed[-1]["seq"] if self.feed else 0


def _sync_fixture(name: str, *, platforms=("fanqie",), routes=None, release=True):
    """A Canon chapter 7 on hub-routed bindings, released (sent) unless release=False."""
    fixture = _fixture(name)
    hub = SyncHub()
    client = _attach_hub(fixture.runtime, hub)
    _set_routes(
        fixture,
        routes
        if routes is not None
        else {"qidian": ("hub", QIDIAN_DESTINATION), "fanqie": ("hub", FANQIE_DESTINATION)},
    )
    _round(fixture, client, refresh=True)  # the release gate needs the destinations cache
    bindings = [QIDIAN if platform == "qidian" else FANQIE for platform in platforms]
    created = {job["platform"]: job["job_id"] for job in _materialize(fixture, bindings=bindings)}
    if release:
        _release(fixture, [created[platform] for platform in platforms])
    return fixture, hub, client, created


def _round(fixture, client, *, refresh: bool = False):
    with fixture.runtime.session_factory() as session:
        return sync_once(session, client, NOW, refresh_destinations=refresh)


def _cursor(fixture) -> int:
    with fixture.runtime.session_factory() as session:
        row = session.get(PublishHubSyncState, CHANGES_KEY)
        return int(row.cursor) if row is not None else 0


def _chapter_binding(fixture, platform: str, number: int = 7):
    with fixture.runtime.session_factory() as session:
        row = session.execute(
            select(PublisherChapterBinding).where(
                PublisherChapterBinding.project_id == fixture.project_id,
                PublisherChapterBinding.platform_id == platform,
                PublisherChapterBinding.chapter_number == number,
            )
        ).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def _work_binding(fixture, platform: str):
    with fixture.runtime.session_factory() as session:
        row = session.execute(
            select(PublisherWorkBinding).where(
                PublisherWorkBinding.project_id == fixture.project_id,
                PublisherWorkBinding.platform_id == platform,
            )
        ).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def _events(fixture, job_id, event_type: str | None = None) -> list[DecisionEvent]:
    with fixture.runtime.session_factory() as session:
        stmt = select(DecisionEvent).where(DecisionEvent.related_object_id == job_id)
        if event_type is not None:
            stmt = stmt.where(DecisionEvent.event_type == event_type)
        rows = session.execute(stmt.order_by(DecisionEvent.created_at.asc())).scalars().all()
        for row in rows:
            session.expunge(row)
        return rows


def _age(fixture, job_id, *, minutes: int = 11) -> None:
    """Make a row look untouched by the sync for `minutes` (the database clock decides)."""
    with fixture.runtime.session_factory.begin() as session:
        session.execute(
            update(PublisherUploadJob)
            .where(PublisherUploadJob.id == job_id)
            .values(updated_at=func.now() - timedelta(minutes=minutes))
        )


def _row_state(job: PublisherUploadJob) -> dict:
    return {
        column: getattr(job, column)
        for column in (
            "status", "hub_status", "hub_job_id", "error_message", "result_message",
            "result_payload_json", "finished_at", "paused_at", "pause_reason", "current_url",
            "updated_at",
        )
    }


def hold_reason_for_session(fixture) -> str:
    with fixture.runtime.session_factory() as session:
        return hold_reason_for(session, fixture.project_id, "fanqie")


def _succeed_chapter(hub: SyncHub, job_id: str, *, chapter_id: str = "chapter-701",
                     official_state: str = "published") -> dict:
    return hub.finish(
        job_id,
        "succeeded",
        official_state=official_state,
        remote_id=chapter_id,
        remote_url=f"https://fanqienovel.com/main/writer/{FANQIE_BOOK}/publish/{chapter_id}",
    )


# --- the brief's five ------------------------------------------------------------------


def test_a_succeeded_chapter_is_mirrored_and_written_into_the_bindings() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-chapter-succeeded")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        assert hub_job_id is not None
        hub_job = _succeed_chapter(hub, hub_job_id)

        report = _round(fixture, client)

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_status) == ("succeeded", "succeeded")
        assert stored.finished_at is not None
        assert stored.error_message == ""
        assert stored.current_url == hub_job["latest_receipt"]["remote_url"]
        work = _work_binding(fixture, "fanqie")
        assert work is not None and work.remote_book_id == FANQIE_BOOK
        chapter = _chapter_binding(fixture, "fanqie")
        assert chapter is not None
        assert chapter.work_binding_id == work.id
        assert chapter.remote_chapter_id == "chapter-701"
        assert chapter.remote_url == hub_job["latest_receipt"]["remote_url"]
        assert chapter.publish_state == "published"
        assert chapter.hub_publication_id == hub_job["publication"]["id"]
        payload = json.loads(stored.result_payload_json)
        assert payload["chapter_binding"]["remote_chapter_id"] == "chapter-701"
        assert payload["work_binding"]["remote_book_id"] == FANQIE_BOOK
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_SUCCEEDED)) == 1
        assert _cursor(fixture) == hub.last_seq() == report.cursor
        with fixture.runtime.session_factory() as session:  # chapter 8 may be sent now (D9)
            assert earlier_chapter_not_done(
                session, project_id=fixture.project_id, platform_id="fanqie", chapter_number=8
            ) is None
    finally:
        fixture.engine.dispose()


def test_an_operator_confirmed_receipt_is_a_success() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-operator-confirmed")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        hub.finish(hub_job_id, "succeeded", official_state="operator_confirmed",
                   remote_id="chapter-found-by-operator", remote_url=None)

        _round(fixture, client)

        stored = _job(fixture, job_id)
        assert stored.status == "succeeded"
        chapter = _chapter_binding(fixture, "fanqie")
        assert chapter.remote_chapter_id == "chapter-found-by-operator"
        # The legacy projection's state for a confirmation without a platform state: the
        # chapter was sent for publishing (publish=True), so it counts as submitted.
        assert chapter.publish_state == "submitted"
        assert chapter.hub_publication_id is not None
    finally:
        fixture.engine.dispose()


def test_a_paused_hub_job_pauses_the_row_with_one_event_and_resumes_with_the_hub() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-paused")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        hub.finish(hub_job_id, "paused", last_error={"reason": "captcha", "detail": "slider"})

        _round(fixture, client)
        _age(fixture, job_id)
        _round(fixture, client)  # the catch-up pass sees it again: still one event

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_status) == ("paused", "paused")
        assert stored.pause_reason == "captcha"
        assert stored.paused_at is not None
        assert "captcha" in stored.error_message and "slider" in stored.error_message
        assert stored.finished_at is None
        paused = _events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)
        assert len(paused) == 1
        assert json.loads(paused[0].payload_json)["hub_job_id"] == hub_job_id

        hub.finish(hub_job_id, "pending")  # an operator resumed it in /ops
        _round(fixture, client)

        resumed = _job(fixture, job_id)
        assert (resumed.status, resumed.hub_status) == ("pending", "pending")
        assert resumed.paused_at is None and resumed.pause_reason == ""
        assert resumed.error_message == ""
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)) == 1
    finally:
        fixture.engine.dispose()


# --- pause reasons (final review, item 1) -------------------------------------------------


@pytest.mark.parametrize(
    "last_error,expected",
    [
        ({"reason": "risk", "detail": "account flagged"}, "account_risk"),
        ({"reason": "account_risk", "detail": "the legacy name"}, "account_risk"),
        ({"reason": "captcha", "detail": "slider"}, "captcha"),
        ({"reason": "mfa", "detail": "sms code"}, "mfa"),
        ({"reason": "needs_human", "detail": "remote_id_conflict"}, "needs_human"),
        ({"reason": "needs_human", "detail": "reconcile_unknown"}, "needs_human"),
        ({"reason": "some_future_reason", "detail": "x"}, "needs_human"),
        (None, "needs_human"),
    ],
    ids=["risk", "legacy-account-risk", "captcha", "mfa", "needs-human-conflict",
         "needs-human-reconcile", "unknown-reason", "no-last-error"],
)
def test_every_hub_pause_reason_mirrors_into_one_the_upload_job_api_accepts(
    request, last_error, expected
) -> None:
    """publish-hub pauses for risk, captcha, mfa or needs_human; ForWin's response model takes
    captcha, mfa, account_risk and needs_human. One row with another value used to make
    GET /api/publishers/upload-jobs and /upload-jobs/{id} fail for every row."""
    from forwin.application.publisher.operations import (
        get_publisher_upload_job,
        list_publisher_upload_jobs,
    )

    fixture, hub, client, created = _sync_fixture(f"hub-sync-pause-{request.node.callspec.id}")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        hub.finish(hub_job_id, "paused", last_error=last_error)

        _round(fixture, client)

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_status, stored.pause_reason) == (
            "paused", "paused", expected
        )
        if last_error is not None:  # publish-hub's own word stays readable
            assert last_error["reason"] in stored.error_message
            assert last_error["detail"] in stored.error_message
        listed = list_publisher_upload_jobs(publisher_manager=fixture.runtime.upload_jobs)
        assert [item.pause_reason for item in listed if item.job_id == job_id] == [expected]
        shown = get_publisher_upload_job(job_id, publisher_manager=fixture.runtime.upload_jobs)
        assert shown.pause_reason == expected and shown.hub_owned is True
        (paused,) = _events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)
        assert json.loads(paused.payload_json)["pause_reason"] == expected
    finally:
        fixture.engine.dispose()


def test_an_adopted_paused_job_gets_its_reason_and_one_paused_event_at_once() -> None:
    """A job publish-hub had already paused when ForWin adopted it is locally `paused` from the
    start (the state map), so mirroring it again as paused must still record why, once. The feed
    passed it while no local row named it, so the catch-up pass reads it without waiting."""
    fixture, hub, client, created = _sync_fixture("hub-sync-adopted-paused", release=False)
    job_id = created["fanqie"]
    try:
        session = fixture.runtime.session_factory()
        try:
            _release(fixture, [job_id], session=session)
            session.rollback()  # the process died before the release committed
        finally:
            session.close()
        (hub_job,) = hub.jobs()
        hub.finish(hub_job["id"], "paused", last_error={"reason": "risk", "detail": "flagged"})
        _round(fixture, client)  # the feed passes the job while no local row names it
        assert _cursor(fixture) == hub.last_seq()

        _release(fixture, [job_id])  # replay: publish-hub answers with the paused job
        adopted = _job(fixture, job_id)
        assert (adopted.status, adopted.hub_status, adopted.pause_reason) == (
            "paused", "paused", ""
        )
        assert _events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED) == []
        hub.get_job_calls.clear()

        report = _round(fixture, client)

        assert report.caught_up == [job_id]
        stored = _job(fixture, job_id)
        assert stored.pause_reason == "account_risk"
        assert stored.paused_at is not None
        assert stored.error_message == "risk: flagged"
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)) == 1

        _round(fixture, client)  # recorded: no longer read at once
        _age(fixture, job_id)
        _round(fixture, client)  # read again after ten minutes: nothing new
        assert hub.get_job_calls == [hub_job["id"], hub_job["id"]]
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)) == 1
    finally:
        fixture.engine.dispose()


def test_an_adopted_paused_job_named_by_the_feed_gets_its_reason() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-adopted-paused-feed", release=False)
    job_id = created["fanqie"]
    try:
        hub.new_job_status = "paused"
        _release(fixture, [job_id])
        (hub_job,) = hub.jobs()
        hub_job["last_error"] = {"reason": "captcha", "detail": "slider"}
        assert _job(fixture, job_id).status == "paused"

        _round(fixture, client)
        _round(fixture, client)

        stored = _job(fixture, job_id)
        assert stored.pause_reason == "captcha" and stored.paused_at is not None
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)) == 1
    finally:
        fixture.engine.dispose()


def test_a_paused_row_paused_again_for_another_reason_records_the_new_one() -> None:
    """Resumed in /ops and paused again between two rounds: the row never leaves `paused`, but
    the reason it shows and the audit follow publish-hub."""
    fixture, hub, client, created = _sync_fixture("hub-sync-repaused")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        hub.finish(hub_job_id, "paused", last_error={"reason": "captcha", "detail": "slider"})
        _round(fixture, client)

        hub.finish(hub_job_id, "scheduled")
        hub.finish(hub_job_id, "paused", last_error={"reason": "risk", "detail": "flagged"})
        _round(fixture, client)

        stored = _job(fixture, job_id)
        assert (stored.status, stored.pause_reason) == ("paused", "account_risk")
        assert stored.error_message == "risk: flagged"
        paused = _events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)
        assert [json.loads(event.payload_json)["pause_reason"] for event in paused] == [
            "captcha", "account_risk"
        ]

        # The same reason with a new detail: the text follows, no new event.
        hub.finish(hub_job_id, "paused", last_error={"reason": "risk", "detail": "still flagged"})
        _round(fixture, client)
        assert _job(fixture, job_id).error_message == "risk: still flagged"
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_PAUSED)) == 2
    finally:
        fixture.engine.dispose()


def test_the_cursor_moves_only_when_the_changes_it_covers_commit() -> None:
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-cursor-commit", platforms=("qidian", "fanqie")
    )
    try:
        _round(fixture, client)
        before = _cursor(fixture)
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _succeed_chapter(hub, qidian_hub, chapter_id="q-7")
        _succeed_chapter(hub, fanqie_hub, chapter_id="f-7")
        seen_mid_page = []

        def look_from_outside(job_id: str) -> None:
            if job_id == fanqie_hub:  # the qidian item is applied already, not committed
                seen_mid_page.append((_cursor(fixture), _job(fixture, created["qidian"]).status))

        hub.on_get_job = look_from_outside
        _round(fixture, client)

        assert seen_mid_page == [(before, "pending")]
        assert _cursor(fixture) == hub.last_seq() > before
        assert _job(fixture, created["qidian"]).status == "succeeded"
        assert _job(fixture, created["fanqie"]).status == "succeeded"
    finally:
        fixture.engine.dispose()


def test_a_crash_in_the_middle_of_a_page_replays_the_same_changes_idempotently() -> None:
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-crash-replay", platforms=("qidian", "fanqie")
    )
    try:
        _round(fixture, client)
        before = _cursor(fixture)
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _succeed_chapter(hub, qidian_hub, chapter_id="q-7")
        _succeed_chapter(hub, fanqie_hub, chapter_id="f-7")
        untouched = {platform: _row_state(_job(fixture, created[platform]))
                     for platform in ("qidian", "fanqie")}

        def die(job_id: str) -> None:
            if job_id == fanqie_hub:
                raise ProcessDied()

        hub.on_get_job = die
        with pytest.raises(ProcessDied):
            _round(fixture, client)

        assert _cursor(fixture) == before
        for platform in ("qidian", "fanqie"):
            assert _row_state(_job(fixture, created[platform])) == untouched[platform]
            assert _chapter_binding(fixture, platform) is None

        hub.on_get_job = None
        _round(fixture, client)
        _round(fixture, client)  # the same changes again change nothing

        assert _cursor(fixture) == hub.last_seq()
        for platform, chapter_id in (("qidian", "q-7"), ("fanqie", "f-7")):
            job_id = created[platform]
            assert _job(fixture, job_id).status == "succeeded"
            assert _chapter_binding(fixture, platform).remote_chapter_id == chapter_id
            assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_SUCCEEDED)) == 1
        with fixture.runtime.session_factory() as session:
            assert session.scalar(select(func.count(PublisherChapterBinding.id))) == 2
            assert session.scalar(select(func.count(PublisherWorkBinding.id))) == 2
    finally:
        fixture.engine.dispose()


# --- catch-up pass (R30) ----------------------------------------------------------------


def test_catch_up_mirrors_an_adopted_terminal_job_the_feed_already_passed() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-catch-up-adopted", release=False)
    job_id = created["fanqie"]
    try:
        session = fixture.runtime.session_factory()
        try:
            _release(fixture, [job_id], session=session)
            session.rollback()  # the process died before the release committed
        finally:
            session.close()
        (hub_job,) = hub.jobs()
        _succeed_chapter(hub, hub_job["id"], chapter_id="chapter-adopted")
        _round(fixture, client)  # the feed passes the job while no local row names it
        assert _cursor(fixture) == hub.last_seq()
        assert _job(fixture, job_id).status == "scheduled"

        _release(fixture, [job_id])  # replay: the hub answers the finished job (R30)
        adopted = _job(fixture, job_id)
        assert (adopted.status, adopted.hub_status) == ("pending", "succeeded")
        hub.get_job_calls.clear()

        report = _round(fixture, client)

        assert hub.get_job_calls == [hub_job["id"]]
        assert report.caught_up == [job_id]
        stored = _job(fixture, job_id)
        assert stored.status == "succeeded"
        chapter = _chapter_binding(fixture, "fanqie")
        assert chapter.remote_chapter_id == "chapter-adopted"
        assert chapter.hub_publication_id == hub_job["publication"]["id"]
    finally:
        fixture.engine.dispose()


def test_catch_up_rechecks_only_rows_the_sync_has_not_touched_for_ten_minutes() -> None:
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-catch-up-stale", platforms=("qidian", "fanqie")
    )
    try:
        _round(fixture, client)
        hub.get_job_calls.clear()
        _round(fixture, client)
        assert hub.get_job_calls == []  # nothing new in the feed, nothing stale

        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _age(fixture, created["fanqie"])
        _round(fixture, client)  # nothing changed on the hub: the check alone marks the row
        assert hub.get_job_calls == [fanqie_hub]
        hub.get_job_calls.clear()
        _round(fixture, client)
        assert hub.get_job_calls == []  # checked rows wait another ten minutes

        hub.job(fanqie_hub)["status"] = "reconciling"  # a change whose feed entry was lost
        _age(fixture, created["fanqie"])
        _round(fixture, client)

        assert hub.get_job_calls == [fanqie_hub]
        assert _job(fixture, created["fanqie"]).hub_status == "reconciling"
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("fault", ["projection_error", "not_found"])
def test_a_failed_row_retried_in_ops_is_recovered_by_the_catch_up_pass(monkeypatch, fault) -> None:
    """R43: the retry's feed item cannot be applied and the cursor passes it; the catch-up pass
    still finds the locally failed row once it is ten minutes old."""
    fixture, hub, client, created = _sync_fixture(f"hub-sync-ops-retry-{fault}")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        hub.finish(hub_job_id, "failed", last_error={"reason": "max_attempts", "detail": "x"})
        _round(fixture, client)
        assert _job(fixture, job_id).status == "failed"
        hub.get_job_calls.clear()
        _round(fixture, client)
        assert hub.get_job_calls == []  # a failed row just looked at is not asked about again

        hub.finish(hub_job_id, "scheduled")  # an operator retried it in /ops
        _succeed_chapter(hub, hub_job_id, chapter_id="chapter-after-retry")
        original = PublisherBindingService.upsert_chapter_binding_from_upload_job
        if fault == "projection_error":
            def broken(self, session, **kwargs):
                raise RuntimeError("cannot project the retry")

            monkeypatch.setattr(
                PublisherBindingService, "upsert_chapter_binding_from_upload_job", broken
            )
        else:
            hub.get_job_refusal = (404, "not_found")
        _round(fixture, client)
        assert _cursor(fixture) == hub.last_seq()  # the feed went past the retry
        assert _job(fixture, job_id).status == "failed"

        monkeypatch.setattr(
            PublisherBindingService, "upsert_chapter_binding_from_upload_job", original
        )
        hub.get_job_refusal = None
        _round(fixture, client)
        assert _job(fixture, job_id).status == "failed"  # not ten minutes yet
        _age(fixture, job_id)
        report = _round(fixture, client)

        assert report.caught_up == [job_id]
        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_status, stored.error_message) == (
            "succeeded", "succeeded", ""
        )
        assert _chapter_binding(fixture, "fanqie").remote_chapter_id == "chapter-after-retry"

        hub.finish(hub_job_id, "failed")  # cannot happen on publish-hub: a success stays
        _age(fixture, job_id)
        _round(fixture, client)
        assert _job(fixture, job_id).status == "succeeded"
        assert _chapter_binding(fixture, "fanqie").remote_chapter_id == "chapter-after-retry"
    finally:
        fixture.engine.dispose()


def test_adopted_terminal_rows_are_caught_up_before_old_failed_rows(monkeypatch) -> None:
    """The ≤50 batch is shared: rows waiting for their R30 result go first."""
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-catch-up-order", platforms=("qidian", "fanqie")
    )
    try:
        _round(fixture, client)
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        hub.finish(qidian_hub, "failed")
        _round(fixture, client)
        _age(fixture, created["qidian"], minutes=60)  # an old failed row, due for a recheck
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _succeed_chapter(hub, fanqie_hub)
        with fixture.runtime.session_factory.begin() as session:  # adopted terminal (R30)
            session.get(PublisherUploadJob, created["fanqie"]).hub_status = "succeeded"
            session.get(PublishHubSyncState, CHANGES_KEY).cursor = hub.last_seq()
        monkeypatch.setattr(sync_module, "CATCH_UP_LIMIT", 1)

        report = _round(fixture, client)

        assert report.caught_up == [created["fanqie"]]
        assert _job(fixture, created["fanqie"]).status == "succeeded"
        report = _round(fixture, client)
        assert report.caught_up == [created["qidian"]]
    finally:
        fixture.engine.dispose()


def test_a_stored_request_that_raises_on_resend_does_not_stop_the_round() -> None:
    """R44: one row of `recover_unsubmitted` raising is rolled back alone; the others are
    resent and the feed is still read."""
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-resend-raises", platforms=("qidian", "fanqie"), release=False
    )
    try:
        hub.down = True
        _release(fixture, [created["qidian"], created["fanqie"]])
        hub.down = False

        def broken_for_fanqie(request) -> None:
            if json.loads(request.content)["destination_id"] == FANQIE_DESTINATION:
                raise RuntimeError("bug on the way out")

        hub.on_post = broken_for_fanqie
        report = _round(fixture, client)

        assert report.resubmitted == {
            created["fanqie"]: SubmitOutcome.ERRORED,
            created["qidian"]: SubmitOutcome.SUBMITTED,
        }
        stuck = _job(fixture, created["fanqie"])
        assert (stuck.status, stuck.hub_job_id) == ("pending", None)
        assert stuck.hub_submit_body_json is not None
        assert _job(fixture, created["qidian"]).hub_job_id is not None
        assert _cursor(fixture) == hub.last_seq() > 0  # the feed was read in the same round

        hub.on_post = None
        report = _round(fixture, client)
        assert report.resubmitted == {created["fanqie"]: SubmitOutcome.SUBMITTED}
        assert _job(fixture, created["fanqie"]).hub_job_id is not None
    finally:
        fixture.engine.dispose()


# --- isolation ----------------------------------------------------------------------------


def test_one_item_that_cannot_be_applied_is_skipped_and_the_rest_commit(monkeypatch) -> None:
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-poison-item", platforms=("qidian", "fanqie")
    )
    try:
        _round(fixture, client)
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _succeed_chapter(hub, qidian_hub, chapter_id="q-7")
        _succeed_chapter(hub, fanqie_hub, chapter_id="f-7")
        original = PublisherBindingService.upsert_chapter_binding_from_upload_job

        def broken_for_qidian(self, session, *, job, **kwargs):
            if job.platform_id == "qidian":
                raise RuntimeError("cannot project this one")
            return original(self, session, job=job, **kwargs)

        monkeypatch.setattr(
            PublisherBindingService, "upsert_chapter_binding_from_upload_job", broken_for_qidian
        )
        report = _round(fixture, client)

        assert report.failed == [created["qidian"]]
        assert _cursor(fixture) == hub.last_seq()
        poisoned = _job(fixture, created["qidian"])
        assert (poisoned.status, poisoned.hub_status) == ("pending", "pending")
        assert _work_binding(fixture, "qidian") is None
        assert _job(fixture, created["fanqie"]).status == "succeeded"
        assert _chapter_binding(fixture, "fanqie").remote_chapter_id == "f-7"

        monkeypatch.setattr(
            PublisherBindingService, "upsert_chapter_binding_from_upload_job", original
        )
        _age(fixture, created["qidian"])
        report = _round(fixture, client)  # the catch-up pass retries it

        assert report.caught_up == [created["qidian"]]
        assert _job(fixture, created["qidian"]).status == "succeeded"
        assert _chapter_binding(fixture, "qidian").remote_chapter_id == "q-7"
    finally:
        fixture.engine.dispose()


def test_publish_hub_down_in_the_middle_of_a_round_leaves_the_cursor() -> None:
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-down-mid-round", platforms=("qidian", "fanqie")
    )
    try:
        _round(fixture, client)
        before = _cursor(fixture)
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _succeed_chapter(hub, qidian_hub, chapter_id="q-7")
        _succeed_chapter(hub, fanqie_hub, chapter_id="f-7")

        def go_down(job_id: str) -> None:
            if job_id == fanqie_hub:
                hub.down = True

        hub.on_get_job = go_down
        with pytest.raises(sync_module.HubUnavailable):
            _round(fixture, client)

        assert _cursor(fixture) == before
        assert _job(fixture, created["qidian"]).status == "pending"
        assert _chapter_binding(fixture, "qidian") is None

        hub.down = False
        hub.on_get_job = None
        _round(fixture, client)
        assert _cursor(fixture) == hub.last_seq()
        assert {_job(fixture, job_id).status for job_id in created.values()} == {"succeeded"}
    finally:
        fixture.engine.dispose()


def test_a_job_publish_hub_does_not_know_is_skipped_and_a_refused_key_ends_the_round() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-refusals")
    job_id = created["fanqie"]
    try:
        _round(fixture, client)
        hub_job_id = _job(fixture, job_id).hub_job_id
        _succeed_chapter(hub, hub_job_id)
        before = _row_state(_job(fixture, job_id))

        hub.get_job_refusal = (401, "unauthorized")
        with pytest.raises(sync_module.HubRequestError):
            _round(fixture, client)
        assert _cursor(fixture) < hub.last_seq()  # the round ended, nothing moved
        assert _row_state(_job(fixture, job_id)) == before

        hub.get_job_refusal = (404, "not_found")
        report = _round(fixture, client)
        assert _cursor(fixture) == hub.last_seq()  # skipped, the feed goes on
        assert report.failed == [] and report.skipped >= 1
        assert _job(fixture, job_id).status == "pending"

        _age(fixture, job_id)
        report = _round(fixture, client)  # the catch-up pass asks again, then waits
        assert report.caught_up == [job_id]
        hub.get_job_calls.clear()
        _round(fixture, client)
        assert hub.get_job_calls == []

        hub.get_job_refusal = None
        _age(fixture, job_id)
        _round(fixture, client)
        assert _job(fixture, job_id).status == "succeeded"
    finally:
        fixture.engine.dispose()


# --- state map details --------------------------------------------------------------------


@pytest.mark.parametrize(
    "last_error,expected",
    [
        ({"reason": "max_attempts", "detail": "lease_expired"}, "max_attempts: lease_expired"),
        ({"code": "chapter_rejected", "message": "title already used"},
         "chapter_rejected: title already used"),
    ],
)
def test_a_failed_hub_job_carries_its_last_error(last_error, expected) -> None:
    fixture, hub, client, created = _sync_fixture(f"hub-sync-failed-{len(expected)}")
    job_id = created["fanqie"]
    try:
        hub_job_id = _job(fixture, job_id).hub_job_id
        hub.finish(hub_job_id, "failed", last_error=last_error)

        _round(fixture, client)

        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_status) == ("failed", "failed")
        assert stored.error_message == expected
        assert stored.finished_at is not None
        assert _chapter_binding(fixture, "fanqie") is None
        assert len(_events(fixture, job_id, DecisionEventType.UPLOAD_JOB_FAILED)) == 1
    finally:
        fixture.engine.dispose()


def test_a_cancelled_hub_job_is_cancelled_locally() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-cancelled")
    job_id = created["fanqie"]
    try:
        hub.finish(_job(fixture, job_id).hub_job_id, "cancelled")
        _round(fixture, client)
        stored = _job(fixture, job_id)
        assert (stored.status, stored.hub_status) == ("cancelled", "cancelled")
        assert stored.finished_at is not None
    finally:
        fixture.engine.dispose()


def test_rows_on_the_extension_queue_are_never_touched() -> None:
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-extension-rows",
        platforms=("qidian", "fanqie"),
        routes={"qidian": ("legacy", None), "fanqie": ("hub", FANQIE_DESTINATION)},
    )
    try:
        extension_row = _job(fixture, created["qidian"])
        assert extension_row.status == "pending" and extension_row.hub_job_id is None
        _age(fixture, created["qidian"], minutes=60)
        with fixture.runtime.session_factory.begin() as session:
            work = PublisherWorkBinding(project_id=fixture.project_id, platform_id="qidian",
                                        book_name="事件时书名", remote_book_id="q-old")
            session.add(work)
            session.flush()
            session.add(PublisherChapterBinding(
                work_binding_id=work.id, project_id=fixture.project_id, platform_id="qidian",
                chapter_number=6, remote_chapter_id="q-6", publish_state="drafted",
            ))
        before = _row_state(_job(fixture, created["qidian"]))
        hub.change("job", str(uuid4()), "succeeded")  # a job no local row names
        hub.change("publication", str(uuid4()), "published")
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        _succeed_chapter(hub, fanqie_hub)

        _round(fixture, client)
        _round(fixture, client)

        assert _row_state(_job(fixture, created["qidian"])) == before
        assert _chapter_binding(fixture, "qidian", 6).publish_state == "drafted"
        assert _job(fixture, created["fanqie"]).status == "succeeded"
        # Only the hub-owned row is read back; an item naming no local row costs no request.
        assert hub.get_job_calls == [fanqie_hub]
    finally:
        fixture.engine.dispose()


# --- projections by task kind -------------------------------------------------------------


def test_a_succeeded_book_creation_records_the_book_and_opens_the_gate() -> None:
    fixture = _fixture("hub-sync-create-book")
    hub = SyncHub()
    hub.destination_items[1]["remote_id"] = None  # no book on fanqie yet
    client = _attach_hub(fixture.runtime, hub)
    try:
        _set_routes(fixture, {"fanqie": ("hub", FANQIE_DESTINATION)})
        _round(fixture, client, refresh=True)
        created = _materialize(fixture, bindings=[FANQIE])
        _release(fixture, [created[0]["job_id"]])
        (create_job,) = hub.jobs("create_destination")
        assert hold_reason_for_session(fixture) == "book_missing"
        hub.finish(create_job["id"], "succeeded", official_state="created",
                   remote_id="fanqie-book-new",
                   remote_url="https://fanqienovel.com/main/writer/book-info/fanqie-book-new")

        _round(fixture, client)

        with fixture.runtime.session_factory() as session:
            create_row = session.execute(
                select(PublisherUploadJob).where(PublisherUploadJob.task_kind == "create_book")
            ).scalar_one()
            assert (create_row.status, create_row.hub_status) == ("succeeded", "succeeded")
        work = _work_binding(fixture, "fanqie")
        assert work.remote_book_id == "fanqie-book-new"
        assert work.remote_url == "https://fanqienovel.com/main/writer/book-info/fanqie-book-new"
        assert work.hub_destination_id == FANQIE_DESTINATION
        assert hold_reason_for_session(fixture) == ""
    finally:
        fixture.engine.dispose()


@pytest.mark.parametrize("official_state", ["cover_uploaded", "operator_confirmed"])
def test_a_succeeded_cover_upload_marks_the_cover_uploaded(tmp_path, official_state) -> None:
    fixture = _fixture(f"hub-sync-cover-{official_state}")
    hub = SyncHub()
    client = _attach_hub(fixture.runtime, hub)
    try:
        _set_routes(fixture, {"qidian": ("hub", QIDIAN_DESTINATION)})
        work_id, cover_id, _file = _cover_setup(fixture, tmp_path)
        cover_job = fixture.runtime.cover_service.enqueue_cover_upload(cover_id)
        assert _work_binding(fixture, "qidian").cover_state == "queued"
        hub.finish(_job(fixture, cover_job.id).hub_job_id, "succeeded",
                   official_state=official_state, remote_id="book-cover")

        _round(fixture, client)

        assert _job(fixture, cover_job.id).status == "succeeded"
        work = _work_binding(fixture, "qidian")
        assert work.id == work_id and work.cover_state == "uploaded"
        with fixture.runtime.session_factory() as session:
            assert session.get(PublisherCoverAsset, cover_id).status == "uploaded"
    finally:
        fixture.engine.dispose()


# --- the cover after the first chapter (final review, item 2) -----------------------------


def _select_cover(fixture, tmp_path, *, work_binding_id: str = "") -> str:
    """A selected cover for the project (a project-wide one unless a work binding is named)."""
    cover_file = tmp_path / "cover.png"
    cover_file.write_bytes(PNG_1X1)
    with fixture.runtime.session_factory.begin() as session:
        cover = PublisherCoverAsset(
            project_id=fixture.project_id,
            work_binding_id=work_binding_id,
            status="selected",
            selection_state="selected",
            file_path=str(cover_file),
            mime_type="image/png",
        )
        session.add(cover)
        session.flush()
        return cover.id


def _cover_jobs(fixture) -> list[PublisherUploadJob]:
    with fixture.runtime.session_factory() as session:
        rows = session.execute(
            select(PublisherUploadJob).where(PublisherUploadJob.task_kind == "cover_upload")
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return rows


def _cover_posts(hub: SyncHub) -> list[bytes]:
    return [post for post in hub.posts if json.loads(post)["action"] == "upload_cover"]


def _add_work_binding(fixture, platform: str, book_id: str) -> str:
    with fixture.runtime.session_factory.begin() as session:
        work = PublisherWorkBinding(project_id=fixture.project_id, platform_id=platform,
                                    book_name="番茄版", remote_book_id=book_id)
        session.add(work)
        session.flush()
        return work.id


def test_the_first_hub_chapter_success_submits_the_selected_cover_to_publish_hub(
    tmp_path,
) -> None:
    """The legacy receipt path queues the book's cover once its first chapter is on the
    platform; a hub chapter's success does the same. On a hub-routed binding, queueing the
    cover is its submit to publish-hub (R26)."""
    fixture, hub, client, created = _sync_fixture("hub-sync-cover-first-chapter")
    job_id = created["fanqie"]
    try:
        # The book's work binding is there before its first chapter lands: made when publish-hub
        # was asked to create the book, or by uploads before the switch.
        work_id = _add_work_binding(fixture, "fanqie", FANQIE_BOOK)
        cover_id = _select_cover(fixture, tmp_path)
        _round(fixture, client)
        assert _cover_jobs(fixture) == [] and _cover_posts(hub) == []
        _succeed_chapter(hub, _job(fixture, job_id).hub_job_id)

        _round(fixture, client)

        assert _job(fixture, job_id).status == "succeeded"
        (cover_job,) = _cover_jobs(fixture)
        work = _work_binding(fixture, "fanqie")
        assert (cover_job.platform_id, cover_job.project_id) == ("fanqie", fixture.project_id)
        payload = json.loads(cover_job.result_payload_json)
        assert work.id == work_id
        assert (payload["cover_asset_id"], payload["work_binding_id"]) == (cover_id, work.id)
        assert cover_job.hub_submit_body_json == cover_body(cover_job, FANQIE_DESTINATION)
        assert _cover_posts(hub) == [_canonical(cover_job.hub_submit_body_json)]
        assert cover_job.hub_job_id is not None and cover_job.status == "pending"
        assert (work.cover_asset_id, work.cover_state) == (cover_id, "queued")
        chapter_payload = json.loads(_job(fixture, job_id).result_payload_json)
        assert chapter_payload["cover_upload_job_id"] == cover_job.id
        assert len(_events(fixture, cover_job.id, DecisionEventType.UPLOAD_JOB_CREATED)) == 1
    finally:
        fixture.engine.dispose()


def test_applying_a_chapter_success_again_queues_no_second_cover(tmp_path) -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-cover-applied-again")
    job_id = created["fanqie"]
    try:
        _add_work_binding(fixture, "fanqie", FANQIE_BOOK)
        _select_cover(fixture, tmp_path)
        _succeed_chapter(hub, _job(fixture, job_id).hub_job_id)
        _round(fixture, client)
        (cover_job,) = _cover_jobs(fixture)

        # The whole feed again: the chapter has succeeded, nothing is projected twice.
        with fixture.runtime.session_factory.begin() as session:
            session.get(PublishHubSyncState, CHANGES_KEY).cursor = 0
        _round(fixture, client)
        # Its success projected again (as if it had not been recorded): the cover job exists.
        with fixture.runtime.session_factory.begin() as session:
            session.get(PublisherUploadJob, job_id).status = "pending"
            session.get(PublishHubSyncState, CHANGES_KEY).cursor = 0
        _round(fixture, client)
        _age(fixture, job_id)
        _round(fixture, client)

        assert _job(fixture, job_id).status == "succeeded"
        assert [row.id for row in _cover_jobs(fixture)] == [cover_job.id]
        assert len(_cover_posts(hub)) == 1
        assert len(hub.jobs("upload_cover")) == 1
        assert len(_events(fixture, cover_job.id, DecisionEventType.UPLOAD_JOB_CREATED)) == 1
    finally:
        fixture.engine.dispose()


def test_a_crash_after_the_cover_was_sent_replays_to_the_same_hub_job(tmp_path) -> None:
    """The cover goes out inside the chapter item's savepoint. If the page dies before it
    commits, the replay stores the cover again and sends the same bytes: publish-hub answers
    with the job it already has."""
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-cover-crash", platforms=("qidian", "fanqie")
    )
    try:
        work_id = _add_work_binding(fixture, "fanqie", FANQIE_BOOK)  # part of the cover's key
        _select_cover(fixture, tmp_path, work_binding_id=work_id)
        _round(fixture, client)
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        _succeed_chapter(hub, fanqie_hub, chapter_id="f-7")
        _succeed_chapter(hub, qidian_hub, chapter_id="q-7")

        def die(job_id: str) -> None:
            if job_id == qidian_hub:  # the fanqie item, with its cover, is applied already
                raise ProcessDied()

        hub.on_get_job = die
        with pytest.raises(ProcessDied):
            _round(fixture, client)
        assert len(_cover_posts(hub)) == 1 and _cover_jobs(fixture) == []

        hub.on_get_job = None
        _round(fixture, client)
        _round(fixture, client)

        (cover_job,) = _cover_jobs(fixture)
        (hub_cover,) = hub.jobs("upload_cover")
        assert cover_job.hub_job_id == hub_cover["id"]
        posts = _cover_posts(hub)
        assert len(posts) == 2 and posts[0] == posts[1]
        assert _job(fixture, created["fanqie"]).status == "succeeded"
        assert _job(fixture, created["qidian"]).status == "succeeded"
    finally:
        fixture.engine.dispose()


def test_a_work_binding_made_by_the_chapter_itself_gets_its_cover_at_once(tmp_path) -> None:
    """The book's first chapter makes its work binding: the cover goes out in the same item."""
    fixture, hub, client, created = _sync_fixture("hub-sync-cover-new-work")
    job_id = created["fanqie"]
    try:
        cover_id = _select_cover(fixture, tmp_path)
        assert _work_binding(fixture, "fanqie") is None
        _succeed_chapter(hub, _job(fixture, job_id).hub_job_id)

        _round(fixture, client)

        assert _job(fixture, job_id).status == "succeeded"
        work = _work_binding(fixture, "fanqie")
        assert work is not None and work.remote_book_id == FANQIE_BOOK
        (cover_job,) = _cover_jobs(fixture)
        payload = json.loads(cover_job.result_payload_json)
        assert (payload["cover_asset_id"], payload["work_binding_id"]) == (cover_id, work.id)
        assert _cover_posts(hub) == [_canonical(cover_job.hub_submit_body_json)]
        assert cover_job.hub_job_id is not None and cover_job.status == "pending"
    finally:
        fixture.engine.dispose()


def _fanqie_cover_jobs_on_hub(hub: SyncHub) -> list[dict]:
    return [
        job for job in hub.jobs("upload_cover") if job["destination_id"] == FANQIE_DESTINATION
    ]


def test_a_replay_that_remakes_the_work_binding_still_uploads_the_cover_once(tmp_path) -> None:
    """The page dies after the cover of a work binding the chapter itself made went out. The
    replay makes the binding again under another id, so the cover goes out under another key.
    publish-hub refuses a second upload_cover of the cover's publication: the row waits while the
    first job is active and is superseded once that job has succeeded."""
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-cover-crash-new-work", platforms=("qidian", "fanqie")
    )
    try:
        _select_cover(fixture, tmp_path)
        _round(fixture, client)
        fanqie_hub = _job(fixture, created["fanqie"]).hub_job_id
        qidian_hub = _job(fixture, created["qidian"]).hub_job_id
        _succeed_chapter(hub, fanqie_hub, chapter_id="f-7")
        _succeed_chapter(hub, qidian_hub, chapter_id="q-7")

        def die(job_id: str) -> None:
            if job_id == qidian_hub:  # the fanqie item, with its binding and cover, is applied
                raise ProcessDied()

        hub.on_get_job = die
        with pytest.raises(ProcessDied):
            _round(fixture, client)
        (first,) = _fanqie_cover_jobs_on_hub(hub)
        assert _cover_jobs(fixture) == [] and _work_binding(fixture, "fanqie") is None

        hub.on_get_job = None
        _round(fixture, client)

        (waiting,) = [row for row in _cover_jobs(fixture) if row.platform_id == "fanqie"]
        assert (waiting.status, waiting.hub_job_id) == ("pending", None)
        assert "publication_has_active_job" in waiting.result_message
        assert _fanqie_cover_jobs_on_hub(hub) == [first]

        hub.finish(first["id"], "succeeded", official_state="cover_uploaded")
        _round(fixture, client)

        (superseded,) = [row for row in _cover_jobs(fixture) if row.platform_id == "fanqie"]
        assert superseded.id == waiting.id and superseded.status == "superseded"
        assert superseded.hub_superseded_reason == "publication_already_published"
        assert "封面" in superseded.result_message
        assert _fanqie_cover_jobs_on_hub(hub) == [first]
        assert _job(fixture, created["fanqie"]).status == "succeeded"
        assert _job(fixture, created["qidian"]).status == "succeeded"
    finally:
        fixture.engine.dispose()


def test_a_cover_that_cannot_be_queued_leaves_the_chapter_success_as_it_is(
    tmp_path, caplog
) -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-cover-unreadable")
    job_id = created["fanqie"]
    try:
        _add_work_binding(fixture, "fanqie", FANQIE_BOOK)
        _select_cover(fixture, tmp_path)
        (tmp_path / "cover.png").unlink()
        _succeed_chapter(hub, _job(fixture, job_id).hub_job_id)

        with caplog.at_level(logging.WARNING, logger="forwin.publish_hub.sync"):
            report = _round(fixture, client)

        assert report.failed == []
        assert _job(fixture, job_id).status == "succeeded"
        assert _chapter_binding(fixture, "fanqie").remote_chapter_id == "chapter-701"
        assert _cover_jobs(fixture) == [] and _cover_posts(hub) == []
        assert any("cover" in record.getMessage() for record in caplog.records)
    finally:
        fixture.engine.dispose()


def test_a_legacy_chapter_never_gets_its_cover_from_the_sync(tmp_path) -> None:
    """The extension queue's own receipt path queues a legacy book's cover; the sync never
    touches a legacy row, its chapter or its cover."""
    fixture, hub, client, created = _sync_fixture(
        "hub-sync-cover-legacy",
        platforms=("qidian",),
        routes={"qidian": ("legacy", None), "fanqie": ("hub", FANQIE_DESTINATION)},
    )
    try:
        legacy_id = created["qidian"]
        with fixture.runtime.session_factory.begin() as session:
            work = PublisherWorkBinding(project_id=fixture.project_id, platform_id="qidian",
                                        book_name="事件时书名", remote_book_id="q-book")
            session.add(work)
            session.flush()
            session.add(PublisherChapterBinding(
                work_binding_id=work.id, project_id=fixture.project_id, platform_id="qidian",
                chapter_number=7, remote_chapter_id="q-7", publish_state="published",
            ))
            session.get(PublisherUploadJob, legacy_id).status = "succeeded"
            work_id = work.id
        _select_cover(fixture, tmp_path, work_binding_id=work_id)
        _age(fixture, legacy_id, minutes=60)

        _round(fixture, client)
        _round(fixture, client)

        assert _cover_jobs(fixture) == []
        assert [request for request in hub.requests if request.method == "POST"] == []
    finally:
        fixture.engine.dispose()


def test_a_publication_change_updates_the_chapter_binding_state() -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-publication-change")
    job_id = created["fanqie"]
    try:
        hub_job = _succeed_chapter(hub, _job(fixture, job_id).hub_job_id,
                                   official_state="review_pending")
        _round(fixture, client)
        assert _chapter_binding(fixture, "fanqie").publish_state == "submitted"

        hub.change("publication", hub_job["publication"]["id"], "published")  # a status sync
        _round(fixture, client)

        assert _chapter_binding(fixture, "fanqie").publish_state == "published"
        assert _job(fixture, job_id).status == "succeeded"
    finally:
        fixture.engine.dispose()


# --- the round ----------------------------------------------------------------------------


def test_a_round_first_resends_stored_requests_and_reads_the_feed_page_by_page(
    monkeypatch,
) -> None:
    fixture, hub, client, created = _sync_fixture("hub-sync-resend-pages", release=False)
    job_id = created["fanqie"]
    try:
        hub.down = True
        _release(fixture, [job_id])
        assert _job(fixture, job_id).hub_job_id is None
        hub.down = False
        monkeypatch.setattr(sync_module, "PAGE_SIZE", 1)

        report = _round(fixture, client)

        assert report.resubmitted == {job_id: SubmitOutcome.SUBMITTED}
        hub_job_id = _job(fixture, job_id).hub_job_id
        assert hub_job_id is not None
        _succeed_chapter(hub, hub_job_id)
        _round(fixture, client)
        assert _cursor(fixture) == hub.last_seq() >= 4
        assert _job(fixture, job_id).status == "succeeded"
    finally:
        fixture.engine.dispose()


# --- run_forever --------------------------------------------------------------------------


class _Sleeps:
    def __init__(self, on_sleep=None) -> None:
        self.delays: list[float] = []
        self.on_sleep = on_sleep

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.delays))


def test_destinations_are_cached_on_the_first_round_and_every_tenth() -> None:
    fixture = _fixture("hub-sync-destinations-rounds")
    hub = SyncHub()
    client = _attach_hub(fixture.runtime, hub)
    seen = []

    def after_round(number: int) -> None:
        with fixture.runtime.session_factory() as session:
            cached = cached_destination(session, FANQIE_DESTINATION)
            seen.append((hub.destinations_calls, cached.account_status if cached else None))
        if number == 1:
            hub.destination_items[1]["account_status"] = "paused_risk"

    try:
        run_forever(
            fixture.runtime.session_factory,
            lambda: client,
            sleep=_Sleeps(after_round),
            clock=lambda: NOW,
            max_rounds=10,
        )
        assert seen[0] == (1, "active")
        assert seen[8] == (1, "active")  # rounds 2 to 9 keep the first snapshot
        assert seen[9] == (2, "paused_risk")
        with fixture.runtime.session_factory() as session:
            row = session.get(PublishHubSyncState, DESTINATIONS_KEY)
            assert set(row.value_json) == {QIDIAN_DESTINATION, FANQIE_DESTINATION}
    finally:
        fixture.engine.dispose()


def test_run_forever_backs_off_while_publish_hub_is_down_and_never_exits(caplog) -> None:
    fixture = _fixture("hub-sync-backoff")
    hub = SyncHub()
    client = _attach_hub(fixture.runtime, hub)
    hub.down = True

    def recover_after_seven(number: int) -> None:
        if number == 7:
            hub.down = False

    sleeps = _Sleeps(recover_after_seven)
    try:
        with caplog.at_level(logging.DEBUG, logger="forwin.publish_hub"):
            run_forever(
                fixture.runtime.session_factory,
                lambda: client,
                sleep=sleeps,
                clock=lambda: NOW,
                max_rounds=9,
            )
        assert sleeps.delays == [15, 30, 60, 120, 240, 300, 300, 15, 15]
        assert API_KEY not in caplog.text
        assert "unavailable" in caplog.text
        with fixture.runtime.session_factory() as session:
            assert session.get(PublishHubSyncState, DESTINATIONS_KEY) is not None
    finally:
        fixture.engine.dispose()


def test_run_forever_logs_and_backs_off_after_an_unexpected_error(monkeypatch, caplog) -> None:
    calls = []

    def flaky(session, client, now, **kwargs):
        calls.append(kwargs["refresh_destinations"])
        if len(calls) == 1:
            raise RuntimeError("database went away")
        return sync_module.SyncReport(destinations_refreshed=True)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sync_module, "sync_once", flaky)
    sleeps = _Sleeps()
    with caplog.at_level(logging.WARNING, logger="forwin.publish_hub"):
        run_forever(_Session, lambda: object(), sleep=sleeps, clock=lambda: NOW, max_rounds=3)
    assert sleeps.delays == [15, 15, 15]
    assert calls == [True, True, False]  # refreshed again after the failed first round
    assert "database went away" in caplog.text


def test_run_forever_waits_without_a_url_or_key(caplog) -> None:
    def no_database():
        raise AssertionError("an unconfigured sync must not open the database")

    sleeps = _Sleeps()
    with caplog.at_level(logging.WARNING, logger="forwin.publish_hub"):
        run_forever(no_database, lambda: None, sleep=sleeps, clock=lambda: NOW, max_rounds=4)
    assert sleeps.delays == [15, 30, 60, 120]
    not_set = [record for record in caplog.records if "not set" in record.getMessage()]
    assert len(not_set) == 4  # once per wait


def test_cli_runs_the_sync_with_the_configured_publish_hub(monkeypatch) -> None:
    from forwin import cli

    args = cli.build_parser().parse_args(["publish-hub-sync"])
    assert args.command == "publish-hub-sync"
    called = []
    monkeypatch.setattr(cli, "cmd_publish_hub_sync", lambda parsed: called.append(parsed))
    monkeypatch.setattr("sys.argv", ["forwin", "publish-hub-sync"])
    cli.main()
    assert [parsed.command for parsed in called] == ["publish-hub-sync"]


def test_the_sync_process_does_not_load_the_api_schema() -> None:
    """The publish-hub-sync container is small (256 MiB): the settings models are loaded only
    when a cover is queued, never at start."""
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "import sys, forwin.cli, forwin.publish_hub.sync; "
        "print(sorted(m for m in ('forwin.application.read_models', 'forwin.api_schema') "
        "if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_client_provider_builds_one_client_and_none_without_url_or_key() -> None:
    provider = sync_module.client_provider("http://hub.test:18930", API_KEY, 20)
    assert provider() is provider()
    assert API_KEY not in repr(provider())
    assert sync_module.client_provider("", API_KEY, 20)() is None
    assert sync_module.client_provider("http://hub.test:18930", "", 20)() is None
    assert sync_module.client_provider("http://hub.test:18930", API_KEY, 0)() is None
