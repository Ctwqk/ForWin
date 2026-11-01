"""Submitting released ForWin jobs to publish-hub, and resending the ones left unanswered.

A job whose project binding routes its platform to publish-hub is sent there when it is released
(chapters) or created (covers), instead of waiting in ForWin's extension queue.

* The request body is stored on the row (`hub_submit_body_json`) and flushed before it is sent
  (D3). From then on the row belongs to publish-hub: the extension queue never claims or sweeps
  it, and what gets sent again is exactly the stored body (Review Focus 1). A stored body also
  freezes the job's publish mode, like an extension's first claim does.
* Nothing is committed here; the caller's transaction is the unit. If the process dies after the
  request went out and before the commit, the row is still `scheduled` and the next release
  rebuilds the body: publish-hub answers the same job (200), or idempotency_conflict when the
  body differs (the mode was changed in between), and then the job found by key is adopted.
* One answer handler serves both the first send and `recover_unsubmitted`. No answer raises:
  every outcome is written on its own row, so one job never rolls back its siblings (R29).
* Once publish-hub cannot be used in a pass (unreachable, key refused, not configured), the rest
  of that pass is deferred without sending (R32): an outage costs one timeout, not one per row.
* Adoption records the hub job and its status but never a terminal local status (R30): the
  sync process mirrors terminal results, with their binding projection, in one place.

Nothing here calls a platform or writes Canon data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from forwin.application.read_models import normalize_project_automation
from forwin.models.project import Project
from forwin.models.publisher import PublisherUploadJob
from forwin.publisher_runtime.audit import (
    PublisherAuditService,
    terminal_upload_event_type,
)

from .client import HubRequestError, HubUnavailable, PublishHubClient
from .payloads import chapter_body, cover_body

if TYPE_CHECKING:
    from forwin.api_schema.project import ProjectAutomationPublishSettings

logger = logging.getLogger(__name__)

HUB_ROUTE = "hub"

# The state map: publish-hub job status -> local PublisherUploadJob.status. The sync process
# applies it; at submit time a terminal hub status is not applied (R30, see _adopt).
LOCAL_STATUS_FOR_HUB_STATUS = {
    "scheduled": "pending",
    "pending": "pending",
    "reconciling": "pending",
    "paused": "paused",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}
TERMINAL_HUB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
# Another job of the same publication (or the destination's create job) is still active.
ACTIVE_JOB_CODES = frozenset({"publication_has_active_job", "destination_has_active_job"})
# publish-hub holds the key for another request and shows no job for it: a person decides.
UNRESOLVED_CONFLICT_ERROR = "idempotency_conflict_unresolved"
# A wrong or rotated key is a configuration problem, not a verdict on the job (R25).
_KEY_REFUSED = frozenset({401, 403})


class SubmitOutcome(StrEnum):
    SUBMITTED = "submitted"  # publish-hub has the job; hub_job_id and hub_status are stored
    HELD = "held"  # Task 5's release gate held the job back: nothing stored, nothing sent
    SUPERSEDED = "superseded"  # publication_already_published: the chapter is out already
    # publish-hub cannot be used in this pass (unreachable, unconfigured, key refused); the
    # rest of the pass is not sent either.
    DEFERRED_UNAVAILABLE = "deferred_unavailable"
    DEFERRED_ACTIVE_JOB = "deferred_active_job"  # wait for the publication's active job
    # Refused for good, the key conflict could not be resolved, or the cover file changed.
    FAILED = "failed"


# Outcomes that leave the row pending with its body and no hub job: recovery sends it again.
DEFERRED_OUTCOMES = frozenset(
    {SubmitOutcome.DEFERRED_UNAVAILABLE, SubmitOutcome.DEFERRED_ACTIVE_JOB}
)

_DEFAULT_AUDIT = PublisherAuditService(session_factory=None)


def hub_binding_for(
    session, project_id: str, platform_id: str
) -> ProjectAutomationPublishSettings | None:
    """The project's publish binding for a platform when it routes to publish-hub, else None.

    Read from the project's settings as they are now, never from a job's frozen binding
    snapshot (R21): jobs scheduled before a cutover go where the binding points at release.
    """
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return None
    project = session.get(Project, normalized_project_id)
    if project is None:
        return None
    settings = normalize_project_automation(project.automation_json)
    for binding in settings.publish_bindings:
        if binding.platform == platform_id:
            return binding if binding.route == HUB_ROUTE else None
    return None


def client_from(
    provider: Callable[[], PublishHubClient | None] | None,
) -> PublishHubClient | None:
    """The configured client, or None when publish-hub is not (or wrongly) configured (R27)."""
    if provider is None:
        return None
    try:
        return provider()
    except ValueError as exc:
        logger.warning("publish-hub client is misconfigured: %s", exc)
        return None


def submit_released_job(
    session,
    job: PublisherUploadJob,
    client: PublishHubClient | None,
    *,
    now: datetime,
    audit: PublisherAuditService | None = None,
    actor_type: str = "system",
    transition: str = "submitted_to_hub",
) -> SubmitOutcome:
    """Send a released chapter job, or a new cover job, to publish-hub, and audit the outcome.

    The caller has found that the job's binding routes to publish-hub with a destination set,
    holds the row in its transaction and commits afterwards. `client` None means publish-hub
    cannot be used now (not configured, or an earlier row of the same pass found it down): the
    body is stored and the row waits for `recover_unsubmitted`; it never goes back to the
    extension queue. A caller sending several jobs passes None for the rest of the pass once
    one comes back DEFERRED_UNAVAILABLE (R32).
    """
    outcome = _submit(session, job, client, now=now)
    if outcome != SubmitOutcome.HELD:
        _record(audit, session, job, outcome, actor_type=actor_type, transition=transition)
    return outcome


def recover_unsubmitted(
    session,
    client: PublishHubClient | None,
    now: datetime,
    *,
    audit: PublisherAuditService | None = None,
) -> dict[str, SubmitOutcome]:
    """Resend, unchanged, every stored request that has no publish-hub job yet.

    The sync process calls this once per round inside its transaction and commits afterwards.
    Only rows still `pending` locally are resent; a failed or superseded row keeps its body for
    audit. Returns each row's outcome. The first DEFERRED_UNAVAILABLE ends the round: the rows
    after it are reported deferred and left untouched, without a request (R32). Deferrals are
    not audited (they repeat every round).
    """
    if client is None:
        logger.warning("publish-hub is not configured; stored job requests wait until it is")
        return {}
    jobs = (
        session.execute(
            select(PublisherUploadJob)
            .where(
                PublisherUploadJob.hub_submit_body_json.is_not(None),
                PublisherUploadJob.hub_job_id.is_(None),
                PublisherUploadJob.status == "pending",
                PublisherUploadJob.deleted_at.is_(None),
            )
            .order_by(
                PublisherUploadJob.created_at.asc(),
                PublisherUploadJob.chapter_number.asc(),
                PublisherUploadJob.id.asc(),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    outcomes: dict[str, SubmitOutcome] = {}
    for index, job in enumerate(jobs):
        outcome = _send(job, client, now=now)
        outcomes[job.id] = outcome
        if outcome not in DEFERRED_OUTCOMES:
            _record(audit, session, job, outcome, actor_type="system", transition="hub_resubmitted")
        if outcome == SubmitOutcome.DEFERRED_UNAVAILABLE:
            waiting = jobs[index + 1 :]
            if waiting:
                logger.warning(
                    "publish-hub unavailable; %d more stored requests wait for the next round",
                    len(waiting),
                )
            outcomes.update((later.id, SubmitOutcome.DEFERRED_UNAVAILABLE) for later in waiting)
            break
    session.flush()
    return outcomes


def hub_event_payload(
    job: PublisherUploadJob, outcome: SubmitOutcome, *, transition: str
) -> dict[str, Any]:
    """What an audit event records about a submit, without the request body."""
    return {
        "transition": transition,
        "hub_outcome": str(outcome),
        "hub_job_id": str(job.hub_job_id or ""),
        "hub_status": str(job.hub_status or ""),
        "hub_superseded_reason": str(job.hub_superseded_reason or ""),
        "error_message": str(job.error_message or ""),
    }


def _submit(
    session, job: PublisherUploadJob, client: PublishHubClient | None, *, now: datetime
) -> SubmitOutcome:
    if job.hub_job_id is not None:
        return SubmitOutcome.SUBMITTED
    if job.hub_submit_body_json is None:
        binding = hub_binding_for(session, job.project_id, job.platform_id)
        destination_id = binding.hub_destination_id if binding is not None else None
        if not destination_id:
            raise ValueError(
                f"publisher job is not routed to a publish-hub destination: {job.id}"
            )
        try:
            body = _body_for(job, destination_id)
        except ValueError as exc:
            if job.task_kind != "cover_upload":
                raise
            # The cover file is unreadable or differs from the one the job was made from.
            return _fail(
                job,
                error=str(exc),
                message="封面文件与创建上传任务时记录的不一致或不可读，未提交到 publish-hub。",
                now=now,
            )
        job.hub_submit_body_json = body
        job.hub_status = None
        job.status = "pending"
        job.abort_requested = False
        session.flush()
    elif job.status != "pending":
        raise ValueError(f"publisher job is no longer waiting for publish-hub: {job.id}")
    return _send(job, client, now=now)


def _record(
    audit: PublisherAuditService | None,
    session,
    job: PublisherUploadJob,
    outcome: SubmitOutcome,
    *,
    actor_type: str,
    transition: str,
) -> None:
    (audit or _DEFAULT_AUDIT).record_upload_job_event(
        session,
        job=job,
        event_type=terminal_upload_event_type(job.status),
        summary=f"publish-hub 提交结果：{outcome}。",
        actor_type=str(actor_type or "system"),
        extra_payload=hub_event_payload(job, outcome, transition=transition),
    )


def _body_for(job: PublisherUploadJob, destination_id: str) -> dict[str, Any]:
    if job.task_kind == "chapter_upload":
        return chapter_body(job, destination_id)
    if job.task_kind == "cover_upload":
        return cover_body(job, destination_id)
    raise ValueError(f"publish-hub is not sent {job.task_kind!r} jobs from here: {job.id}")


def _send(
    job: PublisherUploadJob, client: PublishHubClient | None, *, now: datetime
) -> SubmitOutcome:
    """POST the stored body and record publish-hub's answer on the row."""
    if client is None:
        logger.warning(
            "publish-hub cannot be used now; upload job %s waits with its stored request", job.id
        )
        return _defer(job, "publish-hub 当前不可用或未配置，请求已保存，由同步进程原样发送。")
    try:
        hub_job, _status = client.submit(job.hub_submit_body_json)
    except HubUnavailable as exc:
        logger.warning("publish-hub unavailable for upload job %s: %s", job.id, exc)
        return _defer(job, "publish-hub 暂不可用，请求已保存，由同步进程原样重发。")
    except HubRequestError as exc:
        return _answer_refusal(job, client, exc, now=now)
    return _adopt(job, hub_job)


def _answer_refusal(
    job: PublisherUploadJob,
    client: PublishHubClient,
    exc: HubRequestError,
    *,
    now: datetime,
) -> SubmitOutcome:
    if exc.status in _KEY_REFUSED:
        return _key_refused(job, exc)
    if exc.code == "publication_already_published":
        return _supersede(job, exc.code, now=now)
    if exc.code == "idempotency_conflict":
        return _adopt_job_found_by_key(job, client, now=now)
    if exc.code in ACTIVE_JOB_CODES:
        logger.info("publish-hub deferred upload job %s: %s", job.id, exc.code)
        return _defer(
            job,
            f"publish-hub 上同一内容还有进行中的任务（{exc.code}），请求已保存，稍后重发。",
            outcome=SubmitOutcome.DEFERRED_ACTIVE_JOB,
        )
    logger.warning("publish-hub refused upload job %s: %s %s", job.id, exc.status, exc.code)
    detail = f"：{exc.message}" if exc.message else ""
    return _fail(job, error=exc.code, message=f"publish-hub 拒绝了提交（{exc.code}）{detail}", now=now)


def _adopt_job_found_by_key(
    job: PublisherUploadJob, client: PublishHubClient, *, now: datetime
) -> SubmitOutcome:
    """idempotency_conflict: the key was used before, so that job is this job's (ruling R7).

    When publish-hub shows no job for the key (or the lookup itself is refused), the row fails
    with UNRESOLVED_CONFLICT_ERROR and keeps its body (R29): resending cannot fix it, and a
    raise would roll back the jobs submitted alongside it.
    """
    key = str(job.hub_submit_body_json.get("idempotency_key") or "")
    try:
        found = client.find_by_key(key)
    except HubUnavailable as exc:
        logger.warning("publish-hub unavailable looking up upload job %s: %s", job.id, exc)
        return _defer(job, "publish-hub 暂不可用，请求已保存，由同步进程原样重发。")
    except HubRequestError as exc:
        if exc.status in _KEY_REFUSED:
            return _key_refused(job, exc)
        logger.error(
            "publish-hub answered idempotency_conflict for upload job %s and the key lookup "
            "was refused: %s %s",
            job.id,
            exc.status,
            exc.code,
        )
        return _unresolved_conflict(job, now=now)
    if found is None:
        logger.error(
            "publish-hub holds the idempotency key of upload job %s for another request and "
            "shows no job for it",
            job.id,
        )
        return _unresolved_conflict(job, now=now)
    return _adopt(job, found, found_by_key=True)


def _adopt(
    job: PublisherUploadJob, hub_job: Any, *, found_by_key: bool = False
) -> SubmitOutcome:
    """Record publish-hub's job on the row.

    The local status follows the state map for scheduled/pending/reconciling (pending) and
    paused (paused). A terminal hub status is recorded in hub_status only and the row stays
    pending (R30): the sync process's catch-up over pending rows with a hub_job_id mirrors it
    and applies the binding projection in one place.
    """
    fields = hub_job if isinstance(hub_job, Mapping) else {}
    hub_job_id = str(fields.get("id") or "").strip()
    hub_status = str(fields.get("status") or "").strip()
    local_status = LOCAL_STATUS_FOR_HUB_STATUS.get(hub_status)
    if not hub_job_id or local_status is None:
        logger.warning(
            "publish-hub answered upload job %s with an unusable job (id %r, status %r)",
            job.id,
            hub_job_id,
            hub_status,
        )
        return _defer(job, "publish-hub 的应答无法识别，请求已保存，由同步进程原样重发。")
    job.hub_job_id = hub_job_id
    job.hub_status = hub_status
    job.status = "pending" if hub_status in TERMINAL_HUB_STATUSES else local_status
    job.error_message = ""
    how = "按幂等键找回了已有任务" if found_by_key else "已提交"
    job.result_message = f"publish-hub {how}（任务 {hub_job_id}，状态 {hub_status}）。"
    return SubmitOutcome.SUBMITTED


def _key_refused(job: PublisherUploadJob, exc: HubRequestError) -> SubmitOutcome:
    logger.error(
        "publish-hub refused ForWin's API key (%s %s) for upload job %s",
        exc.status,
        exc.code,
        job.id,
    )
    return _defer(job, f"publish-hub 拒绝了 API key（{exc.status}），请求已保存，修正配置后重发。")


def _defer(
    job: PublisherUploadJob,
    message: str,
    *,
    outcome: SubmitOutcome = SubmitOutcome.DEFERRED_UNAVAILABLE,
) -> SubmitOutcome:
    job.status = "pending"
    job.result_message = message
    return outcome


def _supersede(job: PublisherUploadJob, code: str, *, now: datetime) -> SubmitOutcome:
    """Review Focus 2: a regenerated chapter whose publication is out already. Not retried."""
    job.status = "superseded"
    job.hub_superseded_reason = code
    job.finished_at = now
    job.error_message = ""
    job.result_message = "publish-hub 报告这一章已经发布过（publication_already_published），本任务不再执行。"
    return SubmitOutcome.SUPERSEDED


def _unresolved_conflict(job: PublisherUploadJob, *, now: datetime) -> SubmitOutcome:
    return _fail(
        job,
        error=UNRESOLVED_CONFLICT_ERROR,
        message="publish-hub 的幂等键已被另一个请求使用且查不到对应任务，需要人工处理；请求已保存。",
        now=now,
    )


def _fail(
    job: PublisherUploadJob, *, error: str, message: str, now: datetime
) -> SubmitOutcome:
    job.status = "failed"
    job.error_message = error
    job.result_message = message
    job.finished_at = now
    return SubmitOutcome.FAILED


__all__ = [
    "ACTIVE_JOB_CODES",
    "DEFERRED_OUTCOMES",
    "HUB_ROUTE",
    "LOCAL_STATUS_FOR_HUB_STATUS",
    "TERMINAL_HUB_STATUSES",
    "UNRESOLVED_CONFLICT_ERROR",
    "SubmitOutcome",
    "client_from",
    "hub_binding_for",
    "hub_event_payload",
    "recover_unsubmitted",
    "submit_released_job",
]
