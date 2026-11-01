"""What the release gate reads before a hub-routed chapter is released or sent (D4, D9, R34–R36).

There are two questions, both answered from ForWin's own tables:

* `check_destination`: can chapters go to this binding's destination at all? That needs a
  destination id, the destination in the sync cache on the binding's platform, an account that
  is not paused for risk, and a book on the platform (the cached remote_id or the work binding's
  remote_book_id). A chapter that fails this check is held: it stays `scheduled`.
* `earlier_chapter_not_done`: can a released chapter's stored request be sent now? Not while an
  earlier chapter of the same binding has been released and is not done (D9). publish-hub hands
  out jobs by `available_at`, so chapter N+1 could otherwise overtake chapter N while N is backing
  off.

The production planner reads `check_destination` too (through `hold_reason_for`), so a held
chapter does not take a slot of the day's publish quota (R38). It never submits anything: the
create_destination job is submitted by the release point (`submit.release_gate`).

Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import case, exists, func, select

from forwin.application.read_models import normalize_project_automation
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)

from .state import cached_destination

if TYPE_CHECKING:
    from forwin.api_schema.project import ProjectAutomationPublishSettings

GateResult = Literal["ok", "wait_destination", "wait_account"]

HUB_ROUTE = "hub"

PAUSED_RISK = "paused_risk"
# Why a chapter is held. Each value is recorded on the held row, and a change is audited.
NO_DESTINATION = "no_destination"
DESTINATION_NOT_SYNCED = "destination_not_synced"
DESTINATION_ON_OTHER_PLATFORM = "destination_platform_mismatch"
ACCOUNT_PAUSED = "account_paused_risk"
BOOK_MISSING = "book_missing"

# A released chapter is done when one of its rows got here, or when its chapter binding has a
# platform chapter id (R34): an old failure of a chapter that was published since does not block.
DONE_STATUSES = ("succeeded", "superseded")


@dataclass(frozen=True)
class DestinationCheck:
    result: GateResult
    cause: str  # "" when ok
    destination_id: str | None


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


def hold_reason_for(session, project_id: str, platform_id: str) -> str:
    """Why the release gate would hold the project's chapters on this platform now, or "".

    Read-only: a missing book reads as BOOK_MISSING whether or not its create job was
    submitted. "" for a binding that does not route to publish-hub.
    """
    binding = hub_binding_for(session, project_id, platform_id)
    if binding is None:
        return ""
    check = check_destination(session, binding, project_id=project_id)
    return check.cause if check.result != "ok" else ""


def check_destination(
    session, binding: ProjectAutomationPublishSettings, *, project_id: str
) -> DestinationCheck:
    """Whether the binding's destination can take chapters, in the order of R36 step 1."""
    destination_id = binding.hub_destination_id
    if not destination_id:
        return DestinationCheck("wait_destination", NO_DESTINATION, None)
    cached = cached_destination(session, destination_id)
    if cached is None:
        return DestinationCheck("wait_destination", DESTINATION_NOT_SYNCED, destination_id)
    if cached.platform != binding.platform:
        return DestinationCheck("wait_destination", DESTINATION_ON_OTHER_PLATFORM, destination_id)
    if cached.account_status == PAUSED_RISK:
        return DestinationCheck("wait_account", ACCOUNT_PAUSED, destination_id)
    if not cached.remote_id:
        work = work_binding_for(session, project_id, binding.platform)
        if work is None or not str(work.remote_book_id or "").strip():
            return DestinationCheck("wait_destination", BOOK_MISSING, destination_id)
    return DestinationCheck("ok", "", destination_id)


def earlier_chapter_not_done(
    session, *, project_id: str, platform_id: str, chapter_number: int
) -> int | None:
    """The lowest chapter before `chapter_number` on the binding that was released and is not done.

    Released: a non-deleted chapter_upload row that is no longer `scheduled`. Done: one of the
    chapter's rows is succeeded or superseded, or its chapter binding has a remote_chapter_id.
    Anything else (a stored request not sent yet, pending, paused, reconciling, failed or
    cancelled) blocks. None means the chapter may be sent.
    """
    published = exists().where(
        PublisherChapterBinding.project_id == project_id,
        PublisherChapterBinding.platform_id == platform_id,
        PublisherChapterBinding.chapter_number == PublisherUploadJob.chapter_number,
        PublisherChapterBinding.remote_chapter_id != "",
    )
    row_done = case((PublisherUploadJob.status.in_(DONE_STATUSES), 1), else_=0)
    return session.execute(
        select(PublisherUploadJob.chapter_number)
        .where(
            PublisherUploadJob.project_id == project_id,
            PublisherUploadJob.platform_id == platform_id,
            PublisherUploadJob.task_kind == "chapter_upload",
            PublisherUploadJob.deleted_at.is_(None),
            PublisherUploadJob.status != "scheduled",
            PublisherUploadJob.chapter_number > 0,
            PublisherUploadJob.chapter_number < int(chapter_number or 0),
            ~published,
        )
        .group_by(PublisherUploadJob.chapter_number)
        .having(func.max(row_done) == 0)
        .order_by(PublisherUploadJob.chapter_number.asc())
        .limit(1)
    ).scalar_one_or_none()


def work_binding_for(
    session, project_id: str, platform_id: str, *, for_update: bool = False
) -> PublisherWorkBinding | None:
    """The project's work binding on a platform (there is at most one)."""
    stmt = select(PublisherWorkBinding).where(
        PublisherWorkBinding.project_id == project_id,
        PublisherWorkBinding.platform_id == platform_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt.limit(1)).scalar_one_or_none()


def selected_cover(
    session, *, project_id: str, work_binding_id: str = ""
) -> PublisherCoverAsset | None:
    """The cover chosen for a work: selected or approved, newest first."""
    stmt = select(PublisherCoverAsset).where(
        PublisherCoverAsset.selection_state.in_(["selected", "approved"])
    )
    if work_binding_id:
        stmt = stmt.where(PublisherCoverAsset.work_binding_id.in_([work_binding_id, ""]))
    if project_id:
        stmt = stmt.where(PublisherCoverAsset.project_id == project_id)
    return session.execute(
        stmt.order_by(PublisherCoverAsset.updated_at.desc()).limit(1)
    ).scalar_one_or_none()


__all__ = [
    "ACCOUNT_PAUSED",
    "HUB_ROUTE",
    "BOOK_MISSING",
    "DESTINATION_NOT_SYNCED",
    "DESTINATION_ON_OTHER_PLATFORM",
    "DONE_STATUSES",
    "NO_DESTINATION",
    "PAUSED_RISK",
    "DestinationCheck",
    "GateResult",
    "check_destination",
    "earlier_chapter_not_done",
    "hold_reason_for",
    "hub_binding_for",
    "selected_cover",
    "work_binding_for",
]
