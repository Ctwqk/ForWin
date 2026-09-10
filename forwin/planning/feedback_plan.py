"""Apply one selected feedback hint to one unwritten future chapter plan."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from forwin.audience.actions import action_is_qualified
from forwin.candidate_drafts import candidate_plan_revision
from forwin.canon.projection_lock import lock_projection_project
from forwin.experience.persistence import ExperiencePersistence
from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.capacity import ChapterCapacityReservation
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.models.publisher import (
    FeedbackActionRecord,
    PublisherChapterBinding,
    PublisherUploadJob,
)
from forwin.models.task import GenerationTask
from forwin.production.capacity import SerialCapacityService
from forwin.protocol.experience import ChapterExperiencePlan

_PLAN_FIELDS = frozenset({"rule_anchors", "progress_markers", "immersion_anchors"})
_TERMINAL_TASK_STATES = frozenset(
    {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}
)


@dataclass(frozen=True, slots=True)
class FeedbackPlanResult:
    status: Literal["applied", "already_applied", "refused", "deferred", "unchanged"]
    reason: str
    action_id: str
    chapter_number: int
    plan_revision: str = ""


def _json_object(raw: str) -> dict | None:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _snapshot(chapter: ChapterPlan | None) -> dict:
    if chapter is None:
        return {}
    return {
        "id": chapter.id,
        "project_id": chapter.project_id,
        "arc_plan_id": chapter.arc_plan_id,
        "chapter_number": chapter.chapter_number,
        "title": chapter.title,
        "one_line": chapter.one_line,
        "goals_json": chapter.goals_json,
        "task_contract_json": chapter.task_contract_json,
        "experience_plan_json": chapter.experience_plan_json,
        "status": chapter.status,
        "active_commit_id": chapter.active_commit_id,
        "acceptance_mode": chapter.acceptance_mode,
        "repair_attempt_count": chapter.repair_attempt_count,
        "residual_review_issues_json": chapter.residual_review_issues_json,
        "canon_risk_level": chapter.canon_risk_level,
    }


def _source(action: FeedbackActionRecord) -> dict:
    selected_at = action.selected_at
    if selected_at is not None:
        selected_at = (
            selected_at.replace(tzinfo=UTC)
            if selected_at.tzinfo is None
            else selected_at.astimezone(UTC)
        )
    return {
        "action_id": action.id,
        "project_id": action.project_id,
        "signal_key": action.signal_key,
        "signal_type": action.signal_type,
        "action_type": action.action_type,
        "direction": action.direction,
        "aggregate_id": action.aggregate_id,
        "aggregate_evidence": _json_object(action.aggregate_evidence_json),
        "aggregate_evidence_json": action.aggregate_evidence_json,
        "source_qualified": action.source_qualified,
        "action_payload": _json_object(action.action_payload_json),
        "action_payload_json": action.action_payload_json,
        "selected_at_chapter": action.selected_at_chapter,
        "selected_at": selected_at.isoformat() if selected_at is not None else "",
        "target_chapter_start": action.target_chapter_start,
        "target_chapter_end": action.target_chapter_end,
        "hint_valid_from_chapter": action.hint_valid_from_chapter,
        "hint_expires_at_chapter": action.hint_expires_at_chapter,
    }


class FeedbackPlanService:
    def __init__(self, *, persistence: ExperiencePersistence | None = None):
        self.persistence = persistence or ExperiencePersistence()

    def apply(
        self,
        *,
        session: Session,
        project_id: str,
        action_id: str,
        chapter_number: int,
        expected_plan_revision: str,
    ) -> FeedbackPlanResult:
        """Caller commits plan and audit together; no qualification or Canon writes."""
        # Expired identity attributes can issue a query during this precheck.
        with session.no_autoflush:
            for row in set(session.new) | set(session.dirty) | set(session.deleted):
                owned_input = (
                    isinstance(row, Project)
                    and row.id == project_id
                    or isinstance(row, ChapterPlan)
                    and row.project_id == project_id
                    and row.chapter_number == chapter_number
                    or isinstance(row, FeedbackActionRecord)
                    and row.id == action_id
                )
                if owned_input and (
                    row in session.new
                    or row in session.deleted
                    or session.is_modified(row)
                ):
                    raise ValueError("feedback plan requires flushed owner inputs")
        # Avoid implicit flush before the project's serialization lock. The caller
        # must persist its proposed/selected action with the existing ActionMapper.
        with session.no_autoflush:
            lock_projection_project(session, project_id)
            project = session.scalar(
                select(Project)
                .where(Project.id == project_id)
                .execution_options(populate_existing=True)
            )
            if project is None:
                return FeedbackPlanResult(
                    "refused", "project_missing", action_id, chapter_number
                )
            chapter = session.scalar(
                select(ChapterPlan)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number == chapter_number,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            action_project = session.scalar(
                select(FeedbackActionRecord.project_id).where(
                    FeedbackActionRecord.id == action_id
                )
            )
            if action_project is not None and action_project != project_id:
                return FeedbackPlanResult(
                    "refused", "action_project_mismatch", action_id, chapter_number
                )
            action = session.scalar(
                select(FeedbackActionRecord)
                .where(
                    FeedbackActionRecord.id == action_id,
                    FeedbackActionRecord.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if action is None:
            return FeedbackPlanResult(
                "refused", "action_missing", action_id, chapter_number
            )
        if action.project_id != project_id:
            return FeedbackPlanResult(
                "refused", "action_project_mismatch", action_id, chapter_number
            )
        audit = _json_object(action.plan_application_json)
        if audit is None or (
            audit
            and (
                audit.get("version") != 1
                or not isinstance(audit.get("applications"), list)
                or not all(isinstance(entry, dict) for entry in audit["applications"])
            )
        ):
            # An unreadable prior audit is evidence, never replace it with {}.
            return FeedbackPlanResult(
                "refused", "invalid_plan_application_history", action_id, chapter_number
            )
        entries = audit.get("applications", [])
        before = _snapshot(chapter)
        before_revision = (
            candidate_plan_revision(chapter) if chapter is not None else ""
        )
        source = _source(action)
        source_sha = _hash(source)
        for entry in entries:
            if (
                entry.get("chapter_plan_id") == (chapter.id if chapter else "")
                and entry.get("status") == "applied"
            ):
                if entry.get("source_sha256") != source_sha:
                    return FeedbackPlanResult(
                        "refused",
                        "action_source_changed_after_application",
                        action_id,
                        chapter_number,
                        before_revision,
                    )
                return FeedbackPlanResult(
                    "already_applied",
                    "",
                    action_id,
                    chapter_number,
                    entry["after_plan_revision"],
                )

        def finish(status, reason):
            after = _snapshot(chapter)
            after_revision = (
                candidate_plan_revision(chapter) if chapter is not None else ""
            )
            entry = {
                "action_id": action_id,
                "chapter_plan_id": chapter.id if chapter else "",
                "chapter_number": chapter_number,
                "status": status,
                "reason": reason,
                "expected_plan_revision": expected_plan_revision,
                "before_plan_revision": before_revision,
                "after_plan_revision": after_revision,
                "before": before,
                "after": after,
                "before_sha256": _hash(before),
                "after_sha256": _hash(after),
                "source": source,
                "source_sha256": source_sha,
            }
            # Repeated identical refusal/defer is the same observation, not a new
            # attempt counter or a permanent change to the selected action.
            if entry not in entries:
                action.plan_application_json = json.dumps(
                    {"version": 1, "applications": [*entries, entry]},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                session.flush()
            return FeedbackPlanResult(
                status, reason, action_id, chapter_number, after_revision
            )

        if action.status != "selected":
            return finish("refused", "action_not_selected")
        try:
            qualified = action_is_qualified(action)
        except (ValueError, TypeError, AttributeError):
            qualified = False
        if not qualified:
            return finish("refused", "action_source_unqualified")
        if action.selected_at is None or action.selected_at_chapter is None:
            return finish("refused", "action_selection_unknown")
        if not (
            0
            < action.target_chapter_start
            <= chapter_number
            <= action.target_chapter_end
        ):
            return finish("refused", "outside_action_target")
        if chapter_number < action.hint_valid_from_chapter:
            return finish("refused", "action_hint_not_active")
        if chapter_number > action.hint_expires_at_chapter:
            return finish("refused", "action_hint_expired")
        if chapter is None:
            return finish("refused", "chapter_plan_missing")
        if chapter.status != "planned" or chapter.active_commit_id:
            return finish("refused", "chapter_not_unwritten")
        capacity = SerialCapacityService(session).snapshot(
            project_id, synchronize=False
        )
        latest_active = (
            session.scalar(
                select(ChapterPlan.chapter_number)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.active_commit_id.is_not(None),
                )
                .order_by(ChapterPlan.chapter_number.desc())
                .limit(1)
            )
            or 0
        )
        if chapter_number <= max(
            capacity.accepted,
            latest_active,
            action.selected_at_chapter,
            action.triggered_at_chapter,
        ):
            return finish("refused", "chapter_not_future")
        if (
            project.target_total_chapters
            and chapter_number > project.target_total_chapters
        ):
            return finish("refused", "outside_project_plan")
        if session.scalar(
            select(ChapterDraft.id)
            .where(ChapterDraft.chapter_plan_id == chapter.id)
            .limit(1)
        ):
            return finish("refused", "chapter_has_draft")
        if session.scalar(
            select(CandidateDraftRecord.id)
            .where(
                CandidateDraftRecord.project_id == project_id,
                CandidateDraftRecord.chapter_number == chapter_number,
            )
            .limit(1)
        ):
            return finish("refused", "chapter_has_candidate")
        if session.scalar(
            select(CanonCommitRecord.id)
            .where(
                or_(
                    CanonCommitRecord.chapter_plan_id == chapter.id,
                    (CanonCommitRecord.project_id == project_id)
                    & (CanonCommitRecord.chapter_number == chapter_number),
                )
            )
            .limit(1)
        ):
            return finish("refused", "chapter_has_canon_history")
        for model in (
            PublisherUploadJob,
            PublisherChapterBinding,
            CanonPublicationProtection,
        ):
            if session.scalar(
                select(model.id)
                .where(
                    model.project_id == project_id,
                    model.chapter_number == chapter_number,
                )
                .limit(1)
            ):
                return finish("refused", "chapter_has_publication_evidence")
        if (
            session.get(ChapterCapacityReservation, (project_id, chapter_number))
            is not None
        ):
            return finish("deferred", "chapter_generation_reserved")
        if session.scalar(
            select(GenerationTask.id)
            .where(
                GenerationTask.project_id == project_id,
                GenerationTask.task_kind == "generation",
                GenerationTask.deleted_at.is_(None),
                GenerationTask.status.not_in(_TERMINAL_TASK_STATES),
                GenerationTask.current_chapter == chapter_number,
            )
            .limit(1)
        ):
            return finish("deferred", "chapter_generation_in_progress")
        if not expected_plan_revision or expected_plan_revision != before_revision:
            return finish("refused", "plan_revision_changed")
        payload = _json_object(action.action_payload_json)
        hint = payload.get("plan_hint") if payload is not None else None
        if hint is None:
            return finish("refused", "action_has_no_plan_hint")
        if not isinstance(hint, dict) or set(hint) != {"field", "text"}:
            return finish("refused", "unsupported_plan_hint")
        field, text = hint["field"], hint["text"]
        if (
            not isinstance(field, str)
            or field not in _PLAN_FIELDS
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 500
        ):
            return finish("refused", "unsupported_plan_hint")
        raw_plan = _json_object(chapter.experience_plan_json)
        if raw_plan is None or set(raw_plan) - set(ChapterExperiencePlan.model_fields):
            return finish("refused", "unsupported_experience_plan")
        try:
            plan = ChapterExperiencePlan.model_validate(
                raw_plan, strict=True, extra="forbid"
            )
        except ValidationError:
            return finish("refused", "unsupported_experience_plan")
        if text in getattr(plan, field):
            return finish("unchanged", "plan_hint_already_present")
        updated = plan.model_copy(update={field: [*getattr(plan, field), text]})
        self.persistence.save_chapter_experience_plan(
            session=session,
            chapter_plan=chapter,
            experience_plan=updated,
            expected_plan_revision=before_revision,
        )
        return finish("applied", "")
