"""The publish-hub sync process: mirrors publish-hub's results into ForWin.

It is the only writer of terminal state for hub-owned upload rows (D1). Each round
(`sync_once`):

1. resends the stored requests publish-hub has not answered yet, in chapter order
   (`recover_unsubmitted`, its own transaction);
2. reads the change feed after the stored cursor, a page at a time. A page is one transaction:
   each item is applied in a savepoint and the new cursor is written with them, so the cursor
   moves exactly when the changes it covers commit. A crash replays the page; applying an item
   again changes nothing the second time (a row already in the hub's state is left as it is, and
   events are written only when the local status changes). An item that cannot be applied is
   rolled back on its own and logged, and its row stays in flight for step 3. An item naming no
   local row is skipped without asking publish-hub;
3. catches up (R30, R43): hub-owned rows are read back with `get_job`, at most 50 a round. Rows
   still `pending` or `paused` locally qualify when their recorded hub status is already
   terminal, or paused with no pause reason recorded yet (adopted that way at submit; these go
   first), or when the sync has not looked at them for ten minutes. Locally `failed` rows qualify after the same ten minutes: an operator may
   retry the job in publish-hub's /ops, and that is the one terminal state publish-hub reopens.
   The feed cursor may already be past their changes;
4. refreshes the destinations snapshot the release gate reads, when asked (`run_forever` asks on
   its first round and every tenth).

publish-hub being unreachable (or refusing the key) anywhere in a round ends it: the open
transaction is rolled back and the error reaches `run_forever`, which waits and tries again.

How a job is mirrored (the state map): scheduled/pending/reconciling -> `pending`, paused ->
`paused` with one DecisionEvent, failed/cancelled -> the same with the hub's `last_error` in
`error_message`, succeeded -> `succeeded` with the result written into the bindings. `hub_status`
always records the raw hub status. A receipt an operator confirmed (`operator_confirmed`) is a
success like any other.

A pause carries publish-hub's reason in ForWin's words (`pause_reason`, which the upload-job API
only takes as captcha, mfa, account_risk or needs_human): risk -> account_risk, captcha and mfa
as they are, needs_human and anything else (or none) -> needs_human; the hub's own text stays in
`error_message`. A row that is paused on both sides already still gets this: one adopted while
publish-hub had it paused is `paused` from the start without a reason (the catch-up pass reads it
at once), and one paused again for another reason between two rounds records the new one. Each
reason is recorded, with its event, once.

The bindings are written by the same functions the extension-queue success path uses, fed the
same receipt-shaped payload:

* chapter_upload: work binding and chapter binding upserts, plus the chapter's
  `hub_publication_id`; the book id comes from the destinations snapshot. Then, last in the
  item, the book's selected cover is queued the way the receipt path queues it
  (`enqueue_cover_upload_if_ready`), which on a hub-routed binding submits it to publish-hub
  (R26). Once per cover: its job's key is the cover's, and a chapter already mirrored as
  succeeded is not projected again. A work binding not committed before the item (the chapter's
  own success made it) gets its cover with the next chapter (see `_Mirror._queue_cover`). A cover that cannot be queued is logged in
  a savepoint of its own and the chapter's success stands; the next chapter's success tries
  again;
* create_book: the work binding's `remote_book_id`, `remote_url` and `hub_destination_id`;
* cover_upload: the work binding's cover state.

A `publication` change updates the publish state of the chapter binding that names it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select

from forwin.audit.events import DecisionEventType
from forwin.models.publisher import (
    CREATE_BOOK_TASK_KIND,
    PublisherChapterBinding,
    PublisherUploadJob,
    PublisherWorkBinding,
    PublishHubSyncState,
    upload_job_is_hub_owned,
)
from forwin.publisher_runtime.audit import (
    PublisherAuditService,
    terminal_upload_event_type,
)
from forwin.publisher_runtime.bindings import (
    PublisherBindingService,
    _normalize_publish_state,
)
from forwin.publisher_runtime.browser_sessions import utc_now
from forwin.publisher_runtime.covers import PublisherCoverService
from forwin.publisher_runtime.upload_jobs import _receipt_projection_payload

from .client import HubRequestError, HubUnavailable, PublishHubClient
from .state import cached_destination, write_destinations
from .submit import (
    LOCAL_STATUS_FOR_HUB_STATUS,
    TERMINAL_HUB_STATUSES,
    SubmitOutcome,
    client_from,
    recover_unsubmitted,
)

logger = logging.getLogger(__name__)

CHANGES_KEY = "changes"
PAGE_SIZE = 200
# A backlog (the first start, or a long outage) is read over several rounds, not all at once.
MAX_PAGES_PER_ROUND = 10
CATCH_UP_LIMIT = 50
CATCH_UP_AFTER = timedelta(minutes=10)
ROUND_INTERVAL_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 300.0
DESTINATIONS_EVERY_ROUNDS = 10

MIRRORED_TASK_KINDS = frozenset({"chapter_upload", "cover_upload", CREATE_BOOK_TASK_KIND})
_IN_FLIGHT_STATUSES = ("pending", "paused")
# The only terminal state publish-hub reopens (an operator retry in /ops), so it is rechecked.
_REOPENABLE_STATUS = "failed"
_KEY_REFUSED = frozenset({401, 403})
_ERROR_LIMIT = 500
# publish-hub's pause reasons (its PAUSE_REASONS) in ForWin's words; the upload-job API takes no
# other value. "account_risk" is the older name of "risk" in the extension. Anything else, or no
# reason at all, is for a person to look at.
NEEDS_HUMAN = "needs_human"
_PAUSE_REASONS = {
    "risk": "account_risk",
    "account_risk": "account_risk",
    "captcha": "captcha",
    "mfa": "mfa",
    NEEDS_HUMAN: NEEDS_HUMAN,
}
# What publish-hub's own operator confirmation of a receipt is called (ruling R33 of its spec).
OPERATOR_CONFIRMED = "operator_confirmed"
SYNC_ACTOR_ID = "publish-hub-sync"


@dataclass
class SyncReport:
    """What a round did. Ids are local PublisherUploadJob ids."""

    resubmitted: dict[str, SubmitOutcome] = field(default_factory=dict)
    cursor: int = 0
    changes: int = 0
    mirrored: list[str] = field(default_factory=list)  # rows whose local status changed
    skipped: int = 0  # items naming no local row, or nothing to apply
    failed: list[str] = field(default_factory=list)  # rows whose item was rolled back
    failed_publications: list[str] = field(default_factory=list)
    caught_up: list[str] = field(default_factory=list)  # rows read back by the catch-up pass
    destinations_refreshed: bool = False


def sync_once(
    session,
    client: PublishHubClient,
    now: datetime,
    *,
    refresh_destinations: bool = False,
    audit: PublisherAuditService | None = None,
) -> SyncReport:
    """One round, committed step by step on `session` (see the module docstring).

    Raises HubUnavailable, or HubRequestError when publish-hub refuses the key or the feed
    request, after rolling back what the open step had done; earlier steps stay committed.
    """
    mirror = _Mirror(
        audit or PublisherAuditService(session_factory=None), covers=_cover_queue(client)
    )
    report = SyncReport()
    try:
        report.resubmitted = recover_unsubmitted(session, client, now, audit=mirror.audit)
        session.commit()
        _read_feed(session, client, now, mirror, report)
        _catch_up(session, client, now, mirror, report)
        session.commit()
        mirror.committed()
        if refresh_destinations:
            report.destinations_refreshed = _refresh_destinations(session, client, now)
    except BaseException:
        session.rollback()
        raise
    logger.info(
        "publish-hub sync round: cursor %s, %d changes, %d mirrored, %d failed, %d caught up",
        report.cursor,
        report.changes,
        len(report.mirrored),
        len(report.failed),
        len(report.caught_up),
    )
    return report


def run_forever(
    session_factory,
    client_provider: Callable[[], PublishHubClient | None],
    *,
    interval: float = ROUND_INTERVAL_SECONDS,
    max_backoff: float = MAX_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = utc_now,
    max_rounds: int | None = None,
    audit: PublisherAuditService | None = None,
) -> None:
    """Run a round every `interval` seconds; never exits on its own (`max_rounds` is for tests).

    While publish-hub is unreachable, refuses the key, or a round fails for any other reason,
    the wait doubles from `interval` up to `max_backoff`, with one log line per wait. Without a
    publish-hub URL or key it only waits, logging once per wait: nothing to sync yet. The
    destinations snapshot is refreshed on the first round that gets that far, and every tenth
    round after that.
    """
    passes = 0
    rounds = 0
    failures = 0
    refreshed = False
    while max_rounds is None or passes < max_rounds:
        passes += 1
        client = client_from(client_provider)
        if client is None:
            failures += 1
            delay = _backoff(failures, interval, max_backoff)
            logger.warning(
                "publish-hub is not configured (FORWIN_PUBLISH_HUB_URL or "
                "FORWIN_PUBLISH_HUB_API_KEY not set, or a setting is invalid); nothing is "
                "synced, checking again in %.0f s",
                delay,
            )
            sleep(delay)
            continue
        rounds += 1
        refresh = not refreshed or rounds % DESTINATIONS_EVERY_ROUNDS == 0
        try:
            with session_factory() as session:
                report = sync_once(
                    session, client, clock(), refresh_destinations=refresh, audit=audit
                )
        except (HubUnavailable, HubRequestError) as exc:
            failures += 1
            delay = _backoff(failures, interval, max_backoff)
            logger.warning("publish-hub unavailable (%s); next sync round in %.0f s", exc, delay)
            sleep(delay)
            continue
        except Exception:
            failures += 1
            delay = _backoff(failures, interval, max_backoff)
            logger.exception("publish-hub sync round failed; next round in %.0f s", delay)
            sleep(delay)
            continue
        failures = 0
        refreshed = refreshed or report.destinations_refreshed
        sleep(interval)


def client_provider(
    url: str, key: str, timeout: float
) -> Callable[[], PublishHubClient | None]:
    """A provider of one client for the process, or of None while the URL or key is unset.

    An invalid setting (a timeout that is not positive) is logged by `client_from` and also
    reads as not configured.
    """
    clients: list[PublishHubClient] = []

    def provide() -> PublishHubClient | None:
        if not str(url or "").strip() or not str(key or "").strip():
            return None
        if not clients:
            clients.append(PublishHubClient(url, key, timeout))
        return clients[0]

    return lambda: client_from(provide)


def _backoff(failures: int, interval: float, max_backoff: float) -> float:
    return min(float(interval) * 2 ** max(failures - 1, 0), float(max_backoff))


def _cover_queue(client: PublishHubClient) -> PublisherCoverService:
    """The cover service of the receipt path, used here only to queue a cover (it makes no
    images), sending to publish-hub with this round's client."""
    covers = PublisherCoverService(session_factory=None)
    covers.publish_hub_client_provider = lambda: client
    return covers


# --- feed ---------------------------------------------------------------------------------


def _read_feed(session, client, now: datetime, mirror: _Mirror, report: SyncReport) -> None:
    for _page in range(MAX_PAGES_PER_ROUND):
        state = _cursor_row(session)
        after = int(state.cursor or 0)
        items, next_after = client.changes(after=after, limit=PAGE_SIZE)
        report.changes += len(items)
        _apply_page(session, client, items, now, mirror, report)
        if next_after > after:
            state.cursor = next_after
            state.updated_at = now
        session.commit()
        mirror.committed()
        report.cursor = int(state.cursor)
        if len(items) < PAGE_SIZE or next_after <= after:
            return


def _cursor_row(session) -> PublishHubSyncState:
    """The feed cursor, locked for this page's transaction (created at 0 on the first round)."""
    row = session.get(PublishHubSyncState, CHANGES_KEY, with_for_update=True)
    if row is None:
        row = PublishHubSyncState(key=CHANGES_KEY, cursor=0)
        session.add(row)
        session.flush()
    return row


def _apply_page(session, client, items, now: datetime, mirror: _Mirror, report) -> None:
    """Jobs first, each once (its current view is read anyway), then each publication's last
    state in the page, so a publication whose binding the job item creates is found."""
    job_ids: dict[str, None] = {}
    publications: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            report.skipped += 1
            continue
        entity = str(item.get("entity") or "")
        entity_id = str(item.get("entity_id") or "").strip()
        if not entity_id:
            report.skipped += 1
        elif entity == "job":
            job_ids.setdefault(entity_id, None)
        elif entity == "publication":
            publications.pop(entity_id, None)
            publications[entity_id] = str(item.get("status") or "").strip()
        else:
            report.skipped += 1
    for hub_job_id in job_ids:
        row = session.execute(
            select(PublisherUploadJob)
            .where(PublisherUploadJob.hub_job_id == hub_job_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or not _mirrored(row):
            report.skipped += 1
            continue
        _mirror_from_hub(session, client, row, now, mirror, report)
    for publication_id, official_state in publications.items():
        _apply_publication(session, publication_id, official_state, now, report)


def _mirrored(row: PublisherUploadJob) -> bool:
    """Only rows publish-hub runs, of the kinds it reports results for, are ever written."""
    return (
        upload_job_is_hub_owned(row)
        and bool(row.hub_job_id)
        and row.task_kind in MIRRORED_TASK_KINDS
    )


def _mirror_from_hub(
    session, client, row: PublisherUploadJob, now: datetime, mirror: _Mirror, report
) -> bool:
    """Read the row's job and mirror it, in a savepoint; False when the item was not applied.

    publish-hub unreachable, or refusing the key, ends the round (re-raised). A job publish-hub
    does not know (404) or another refusal is logged and skipped. Anything else that goes
    wrong rolls back this row only: it stays in flight and the catch-up pass retries it.
    """
    row_id, hub_job_id = row.id, str(row.hub_job_id)
    try:
        with session.begin_nested():
            view = client.get_job(hub_job_id)
            changed = mirror.apply(session, row, view, now=now)
    except HubUnavailable:
        raise
    except HubRequestError as exc:
        if exc.status in _KEY_REFUSED:
            raise
        logger.warning(
            "publish-hub refused reading job %s of upload job %s: %s %s",
            hub_job_id,
            row_id,
            exc.status,
            exc.code,
        )
        report.skipped += 1
        return False
    except Exception:
        logger.exception(
            "could not mirror publish-hub job %s into upload job %s; it is retried later",
            hub_job_id,
            row_id,
        )
        report.failed.append(row_id)
        return False
    if changed:
        report.mirrored.append(row_id)
    return True


def _apply_publication(session, publication_id: str, official_state: str, now, report) -> None:
    """A publication's platform state changed: the chapter binding that names it follows.

    In a savepoint of its own, like a job item: a failure is logged and skipped.
    """
    if not official_state or official_state == "created":
        report.skipped += 1  # "created": publish-hub opened the record, nothing on the platform
        return
    try:
        with session.begin_nested():
            applied = _publication_state(session, publication_id, official_state, now)
    except Exception:
        logger.exception("could not apply the state of publication %s", publication_id)
        report.failed_publications.append(publication_id)
        return
    if not applied:
        report.skipped += 1


def _publication_state(session, publication_id: str, official_state: str, now) -> bool:
    chapter = session.execute(
        select(PublisherChapterBinding)
        .where(PublisherChapterBinding.hub_publication_id == publication_id)
        .with_for_update()
    ).scalar_one_or_none()
    if chapter is None:
        return False
    job = session.execute(
        select(PublisherUploadJob)
        .where(
            PublisherUploadJob.project_id == chapter.project_id,
            PublisherUploadJob.platform_id == chapter.platform_id,
            PublisherUploadJob.task_kind == "chapter_upload",
            PublisherUploadJob.chapter_number == chapter.chapter_number,
            PublisherUploadJob.hub_job_id.is_not(None),
        )
        .order_by(PublisherUploadJob.created_at.desc(), PublisherUploadJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        logger.warning(
            "publication %s names chapter binding %s but no publish-hub upload job; skipped",
            publication_id,
            chapter.id,
        )
        return False
    # Same reading of the platform state as the receipt projection (it needs the job's mode).
    chapter.publish_state = _normalize_publish_state(job, {"official_state": official_state})
    chapter.last_synced_at = now
    session.flush()
    return True


# --- catch-up (R30) -------------------------------------------------------------------------


def _catch_up(session, client, now: datetime, mirror: _Mirror, report: SyncReport) -> None:
    in_flight = PublisherUploadJob.status.in_(_IN_FLIGHT_STATUSES)
    adopted = or_(
        and_(in_flight, PublisherUploadJob.hub_status.in_(sorted(TERMINAL_HUB_STATUSES))),
        # Adopted while publish-hub had it paused: paused from the start, its reason not read yet.
        and_(
            PublisherUploadJob.status == "paused",
            PublisherUploadJob.hub_status == "paused",
            PublisherUploadJob.pause_reason == "",
        ),
    )
    # The database clock, the one that wrote updated_at.
    stale = PublisherUploadJob.updated_at < func.now() - CATCH_UP_AFTER
    rows = (
        session.execute(
            select(PublisherUploadJob)
            .where(
                PublisherUploadJob.hub_job_id.is_not(None),
                PublisherUploadJob.task_kind.in_(sorted(MIRRORED_TASK_KINDS)),
                PublisherUploadJob.deleted_at.is_(None),
                or_(
                    adopted,
                    and_(in_flight, stale),
                    # Its hub status is terminal too, so only the ten minutes count (R43).
                    and_(PublisherUploadJob.status == _REOPENABLE_STATUS, stale),
                ),
            )
            .order_by(
                case((adopted, 0), else_=1),
                PublisherUploadJob.updated_at.asc(),
                PublisherUploadJob.id.asc(),
            )
            .limit(CATCH_UP_LIMIT)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not _mirrored(row):
            continue
        report.caught_up.append(row.id)
        if not _mirror_from_hub(session, client, row, now, mirror, report):
            _touch(row)  # checked: wait the full period before asking again
    session.flush()


def _touch(row: PublisherUploadJob) -> None:
    """Mark the row as looked at by the sync now (the catch-up pass reads updated_at)."""
    row.updated_at = func.now()


# --- destinations ---------------------------------------------------------------------------


def _refresh_destinations(session, client, now: datetime) -> bool:
    items = client.destinations()
    try:
        write_destinations(session, items, now=now)
    except (ValueError, AttributeError) as exc:
        session.rollback()
        logger.error("publish-hub answered /v1/destinations with an unusable item: %s", exc)
        return False
    session.commit()
    return True


# --- mirroring one job ----------------------------------------------------------------------


class _Mirror:
    """Writes one publish-hub job view onto its local row, by the state map."""

    def __init__(
        self, audit: PublisherAuditService, *, covers: PublisherCoverService | None = None
    ) -> None:
        self.audit = audit
        self.bindings = PublisherBindingService(session_factory=None)
        self.covers = covers
        # Work bindings made by items whose transaction has not committed yet (_queue_cover).
        self.uncommitted_work_ids: set[str] = set()

    def committed(self) -> None:
        """The open transaction committed: the work bindings it made are durable now."""
        self.uncommitted_work_ids.clear()

    def apply(
        self, session, row: PublisherUploadJob, view: Any, *, now: datetime
    ) -> bool:
        """True when the local status changed. Applying the same view again changes nothing."""
        fields = view if isinstance(view, Mapping) else {}
        if str(fields.get("id") or "").strip() != str(row.hub_job_id):
            raise ValueError(f"publish-hub answered another job than {row.hub_job_id}")
        hub_status = str(fields.get("status") or "").strip()
        _touch(row)
        target = LOCAL_STATUS_FOR_HUB_STATUS.get(hub_status)
        if hub_status:
            row.hub_status = hub_status
        if target is None:
            logger.warning(
                "publish-hub job %s of upload job %s has an unknown status %r; left as it is",
                row.hub_job_id,
                row.id,
                hub_status,
            )
            return False
        if row.status == target:
            if target == "paused":
                self._still_paused(session, row, fields, now=now)
            return False
        if row.status == "succeeded":
            # publish-hub never takes a success back; the bindings stay as they were written.
            logger.warning(
                "publish-hub job %s reads %s but upload job %s has succeeded; left as it is",
                row.hub_job_id,
                hub_status,
                row.id,
            )
            return False
        previous = row.status
        if target == "succeeded":
            self._succeed(session, row, fields, now=now)
        elif target == "paused":
            self._pause(session, row, fields, now=now)
        elif target in ("failed", "cancelled"):
            self._stop(session, row, fields, target, now=now)
        else:
            self._resume(session, row, fields, previous)
        session.flush()
        return True

    # Each state ------------------------------------------------------------------------

    def _succeed(self, session, row: PublisherUploadJob, view: Mapping, *, now: datetime) -> None:
        result = _hub_result(view)
        payload = _load_object(row.result_payload_json)
        work = None
        if row.task_kind == "chapter_upload":
            known = _work_binding_ids(session, row)
            work = self._project_chapter(session, row, view, result, payload)
        elif row.task_kind == CREATE_BOOK_TASK_KIND:
            self._project_book(session, row, view, result, payload)
        else:
            self._project_cover(session, row, result, payload)
        payload["hub_result"] = result
        row.result_payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        row.status = "succeeded"
        row.finished_at = now
        row.paused_at = None
        row.pause_reason = ""
        row.error_message = ""
        if result["remote_url"]:
            row.current_url = result["remote_url"]
        row.result_message = (
            f"publish-hub 任务已完成（任务 {row.hub_job_id}，平台状态 "
            f"{result['official_state'] or '-'}）。"
        )
        self._record(
            session,
            row,
            DecisionEventType.UPLOAD_JOB_SUCCEEDED,
            f"publish-hub 发布任务已完成（{result['official_state'] or '-'}）。",
            extra={
                "remote_id": result["remote_id"],
                "official_state": result["official_state"],
                "hub_publication_id": result["publication_id"],
            },
        )
        if work is not None:
            new_work = work.id not in known or work.id in self.uncommitted_work_ids
            if work.id not in known:
                self.uncommitted_work_ids.add(work.id)
            # Last in the item, so that once the cover's request is out only the flush and the
            # page's commit are left to fail.
            self._queue_cover(session, row, work, payload, new_work=new_work)

    def _pause(
        self,
        session,
        row: PublisherUploadJob,
        view: Mapping,
        *,
        now: datetime,
        previous_reason: str = "",
    ) -> None:
        last_error = view.get("last_error")
        reason = _pause_reason(last_error)
        row.status = "paused"
        row.paused_at = now
        row.pause_reason = reason
        row.finished_at = None
        row.error_message = _error_text(last_error)
        row.result_message = (
            f"publish-hub 暂停了这个任务（{reason}），请在 publish-hub /ops 处理"
            f"（任务 {row.hub_job_id}）。"
        )
        extra = {
            "pause_reason": reason,
            "hub_pause_reason": _error_reason(last_error),
            "error_message": row.error_message,
        }
        if previous_reason:
            extra["previous_pause_reason"] = previous_reason
        self._record(
            session,
            row,
            DecisionEventType.UPLOAD_JOB_PAUSED,
            f"publish-hub 暂停了发布任务（{reason}）。",
            extra=extra,
        )

    def _still_paused(
        self, session, row: PublisherUploadJob, view: Mapping, *, now: datetime
    ) -> None:
        """Paused here and on publish-hub. A reason not recorded yet (the row was adopted
        paused) or a new one (paused again between two rounds) is recorded with its one event;
        the same reason again only refreshes the error text."""
        reason = _pause_reason(view.get("last_error"))
        if row.pause_reason != reason:
            self._pause(session, row, view, now=now, previous_reason=row.pause_reason)
            return
        error = _error_text(view.get("last_error"))
        if row.error_message != error:
            row.error_message = error

    def _stop(
        self, session, row: PublisherUploadJob, view: Mapping, target: str, *, now: datetime
    ) -> None:
        row.status = target
        row.finished_at = now
        row.paused_at = None
        row.pause_reason = ""
        row.error_message = _error_text(view.get("last_error"))
        detail = f"：{row.error_message}" if row.error_message else ""
        row.result_message = f"publish-hub 任务结束（{target}）{detail}（任务 {row.hub_job_id}）。"
        self._record(
            session,
            row,
            terminal_upload_event_type(target),
            f"publish-hub 发布任务结束（{target}）。",
            extra={"error_message": row.error_message},
        )

    def _resume(self, session, row: PublisherUploadJob, view: Mapping, previous: str) -> None:
        """Back in flight on publish-hub: resumed or retried by an operator in /ops."""
        row.status = "pending"
        row.finished_at = None
        row.paused_at = None
        row.pause_reason = ""
        row.error_message = ""
        row.result_message = (
            f"publish-hub 任务继续执行（任务 {row.hub_job_id}，状态 {row.hub_status}）。"
        )
        self._record(
            session,
            row,
            DecisionEventType.UPLOAD_JOB_PROGRESS,
            f"publish-hub 发布任务从 {previous} 恢复执行。",
            extra={"previous_status": previous},
        )

    # Projections, through the functions the extension-queue success path uses -------------

    def _project_chapter(
        self, session, row: PublisherUploadJob, view: Mapping, result: dict, payload: dict
    ) -> PublisherWorkBinding:
        destination = cached_destination(session, _destination_id(row, view))
        projected = _receipt_projection_payload(
            payload,
            {
                "remote_book_id": destination.remote_id if destination is not None else "",
                "remote_chapter_id": result["remote_id"],
                "remote_url": result["remote_url"],
                "official_state": result["official_state"],
                "content_sha256": row.body_sha256,
            },
        )
        work = self.bindings.upsert_work_binding_from_upload_job(
            session, job=row, result_payload=projected, current_url=result["remote_url"]
        )
        chapter = self.bindings.upsert_chapter_binding_from_upload_job(
            session,
            job=row,
            work_binding=work,
            result_payload=projected,
            current_url=result["remote_url"],
        )
        if result["publication_id"]:
            chapter.hub_publication_id = result["publication_id"]
        session.flush()
        payload["work_binding"] = self.bindings.serialize_work_binding(work)
        payload["chapter_binding"] = self.bindings.serialize_chapter_binding(chapter)
        return work

    def _queue_cover(
        self,
        session,
        row: PublisherUploadJob,
        work: PublisherWorkBinding,
        payload: dict,
        *,
        new_work: bool,
    ) -> None:
        """After a chapter lands, queue the book's cover as the receipt path does.

        `enqueue_cover_upload_if_ready` decides: a selected cover, a chapter of the book on the
        platform, and no job for that cover yet (the job's key is made from the work binding id,
        the cover and its bytes, so a later chapter finds the first job and queues nothing). On
        a hub-routed binding it submits the cover to publish-hub (R26). If the page dies after
        that request and before its commit, the replay stores the cover again and sends the
        same bytes, and publish-hub answers with the job it already has.

        That holds only for a work binding that was committed before: one made by this chapter's
        success (or by another item of the same uncommitted page) would get another id on a
        replay, hence another key, and publish-hub would take the cover a second time. Its cover
        waits for the next chapter's success, when the binding is committed (books created
        through publish-hub, and books uploaded before the switch, have theirs already).

        In a savepoint of its own: a cover that cannot be queued (its file unreadable, say) is
        logged and the chapter's success stands; the next chapter's success tries again.
        """
        if self.covers is None or not str(work.remote_book_id or "").strip():
            return  # a cover job needs the platform book id
        if new_work:
            logger.info(
                "work binding %s was made by upload job %s; its cover is queued with the next "
                "chapter",
                work.id,
                row.id,
            )
            return
        try:
            with session.begin_nested():
                cover_job = self.covers.enqueue_cover_upload_if_ready(
                    session, job=row, payload=payload, work_binding=work
                )
                if cover_job is not None:
                    self.audit.record_upload_job_event(
                        session,
                        job=cover_job,
                        event_type=DecisionEventType.UPLOAD_JOB_CREATED,
                        summary="封面上传任务已创建（publish-hub 章节完成后）。",
                        actor_type="worker",
                        actor_id=SYNC_ACTOR_ID,
                        extra_payload={
                            "transition": "cover_after_hub_chapter",
                            "chapter_upload_job_id": row.id,
                        },
                    )
        except ValueError as exc:
            logger.warning(
                "cover of work binding %s not queued after upload job %s: %s",
                work.id,
                row.id,
                exc,
            )
            return
        except Exception:
            logger.exception(
                "queueing the cover of work binding %s after upload job %s failed; the next "
                "chapter tries again",
                work.id,
                row.id,
            )
            return
        if cover_job is not None:
            payload["cover_upload_job_id"] = cover_job.id
            row.result_payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _project_book(
        self, session, row: PublisherUploadJob, view: Mapping, result: dict, payload: dict
    ) -> None:
        destination_id = _destination_id(row, view)
        book_id = result["remote_id"]
        if not book_id:
            cached = cached_destination(session, destination_id)
            book_id = (cached.remote_id if cached is not None else None) or ""
        if not book_id:
            logger.warning(
                "book creation %s succeeded on publish-hub without a book id; chapters wait for "
                "the next destinations refresh",
                row.hub_job_id,
            )
        projected = _receipt_projection_payload(
            payload,
            {
                "remote_book_id": book_id,
                "remote_chapter_id": "",
                "remote_url": result["remote_url"],
                "official_state": result["official_state"],
                "content_sha256": "",
            },
        )
        projected["remote_book_url"] = result["remote_url"]
        work = self.bindings.upsert_work_binding_from_upload_job(
            session, job=row, result_payload=projected, current_url=result["remote_url"]
        )
        if destination_id:
            work.hub_destination_id = destination_id
        session.flush()
        payload["work_binding"] = self.bindings.serialize_work_binding(work)

    def _project_cover(
        self, session, row: PublisherUploadJob, result: dict, payload: dict
    ) -> None:
        official_state = result["official_state"]
        if official_state == OPERATOR_CONFIRMED:
            # The extension-queue cover receipt confirms "cover_uploaded"; an operator's
            # confirmation of the cover means the same.
            official_state = "cover_uploaded"
        projected = _receipt_projection_payload(
            payload,
            {
                "remote_book_id": str(payload.get("remote_book_id") or ""),
                "remote_chapter_id": "",
                "remote_url": result["remote_url"],
                "official_state": official_state,
                "content_sha256": row.body_sha256,
            },
        )
        work = self.bindings.update_from_cover_upload_result(
            session, job=row, result_payload=projected, current_url=result["remote_url"]
        )
        if work is None:
            raise ValueError(f"cover upload job {row.id} names no work binding")
        payload["work_binding"] = self.bindings.serialize_work_binding(work)

    def _record(
        self, session, row: PublisherUploadJob, event_type: str, summary: str, *, extra: dict
    ) -> None:
        self.audit.record_upload_job_event(
            session,
            job=row,
            event_type=event_type,
            summary=summary,
            actor_type="worker",
            actor_id=SYNC_ACTOR_ID,
            extra_payload={
                "transition": "hub_mirrored",
                "hub_job_id": str(row.hub_job_id or ""),
                "hub_status": str(row.hub_status or ""),
                **extra,
            },
        )


def _work_binding_ids(session, row: PublisherUploadJob) -> set[str]:
    """The work bindings of the row's project and platform, as they are before it is projected."""
    return set(
        session.execute(
            select(PublisherWorkBinding.id).where(
                PublisherWorkBinding.project_id == row.project_id,
                PublisherWorkBinding.platform_id == row.platform_id,
            )
        ).scalars()
    )


def _hub_result(view: Mapping) -> dict[str, str]:
    """remote id, url and platform state of a finished job: its latest receipt, then its
    publication for any field the receipt leaves empty."""
    receipt = view.get("latest_receipt") if isinstance(view.get("latest_receipt"), Mapping) else {}
    publication = view.get("publication") if isinstance(view.get("publication"), Mapping) else {}
    return {
        "status": _text(view.get("status")),
        "publication_id": _text(publication.get("id")),
        "remote_id": _text(receipt.get("remote_id")) or _text(publication.get("remote_id")),
        "remote_url": _text(receipt.get("remote_url")) or _text(publication.get("remote_url")),
        "official_state": _text(receipt.get("official_state"))
        or _text(publication.get("official_state")),
        "executor_ref": _text(receipt.get("executor_ref")),
    }


def _destination_id(row: PublisherUploadJob, view: Mapping) -> str:
    body = row.hub_submit_body_json if isinstance(row.hub_submit_body_json, Mapping) else {}
    return _text(view.get("destination_id")) or _text(body.get("destination_id"))


def _error_reason(last_error: Any) -> str:
    if isinstance(last_error, Mapping):
        return (_text(last_error.get("reason")) or _text(last_error.get("code")))[:64]
    return ""


def _pause_reason(last_error: Any) -> str:
    """The `pause_reason` ForWin records for a job publish-hub paused (see _PAUSE_REASONS)."""
    return _PAUSE_REASONS.get(_error_reason(last_error), NEEDS_HUMAN)


def _error_text(last_error: Any) -> str:
    """publish-hub's last_error as one line: "reason: detail" ({reason, detail} or
    {code, message}), else its JSON."""
    if last_error is None or last_error == {}:
        return ""
    if isinstance(last_error, Mapping):
        reason = _error_reason(last_error)
        detail = _text(last_error.get("detail")) or _text(last_error.get("message"))
        if reason or detail:
            text = f"{reason}: {detail}" if reason and detail else reason or detail
        else:
            text = json.dumps(last_error, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(last_error)
    return text[:_ERROR_LIMIT]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _load_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "CHANGES_KEY",
    "HubUnavailable",
    "SyncReport",
    "client_provider",
    "run_forever",
    "sync_once",
]
