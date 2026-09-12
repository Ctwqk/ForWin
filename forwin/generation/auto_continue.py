"""Durable, replay-safe generation handoff on the existing outbox."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from sqlalchemy import select

from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.generation.review_auto_retry import chapter_numbers as _chapter_numbers
from forwin.generation.review_auto_retry import (
    eligible_for_auto_review_retry,
    prior_auto_review_retry_count,
    reset_chapter_for_auto_review_retry,
)
from forwin.generation.run_target import resolve_generation_run_target
from forwin.generation.task_payload import payload_from_json
from forwin.generation.task_repository import (
    GenerationTaskRepository,
    TERMINAL_GENERATION_STATUSES,
)
from forwin.models.audit import DecisionEvent
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask
from forwin.state.updater import StateUpdater


@dataclass(frozen=True)
class AutoContinueDecision:
    decision: str
    reason: str
    next_task_id: str = ""
    next_chapter: int = 0
    run_until_chapter: int = 0
    target_total_chapters: int = 0
    requested_chapters: int = 0
    workset_reason: str = ""


class GenerationAutoContinueController:
    """One decision owner, operating entirely in the caller's transaction."""

    def __init__(self, session):
        self.session = session

    def consume(self, event, application):
        """Caller owns commit; child, review reset and durable decision are indivisible."""
        session = self.session
        from forwin.generation.continuation_events import (
            GenerationContinuationEvent,
            continuation_event_id,
            continuation_decision_id,
        )

        project = session.scalar(
            select(Project)
            .where(Project.id == event.result.project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ValueError("continuation project missing")
        parent = session.scalar(
            select(GenerationTask)
            .where(GenerationTask.id == event.parent_task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if parent is None or parent.project_id != project.id:
            raise ValueError("continuation parent mismatch")
        existing = session.get(DecisionEvent, continuation_decision_id(parent.id))
        if existing:
            return AutoContinueDecision(**json.loads(existing.payload_json))
        # Validate against the committed event, not arbitrary caller-supplied result data.
        intent = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_id == continuation_event_id(parent.id)
            )
        )
        if (
            intent is None
            or GenerationContinuationEvent.model_validate_json(intent.payload_json)
            != event
        ):
            raise ValueError("continuation result does not match durable intent")
        if parent.status not in TERMINAL_GENERATION_STATUSES:
            raise ValueError("continuation parent is not terminal")
        payload = payload_from_json(parent.execution_payload_json)

        def record(decision):
            StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    id=continuation_decision_id(parent.id),
                    project_id=project.id,
                    task_id=parent.id,
                    scope="task",
                    event_family="audit_action",
                    event_type=DecisionEventType.AUTO_CONTINUE_DECISION,
                    actor_type="system",
                    summary=f"Auto-continue decision: {decision.decision} ({decision.reason})",
                    payload=asdict(decision),
                    related_object_type="generation_task",
                    related_object_id=parent.id,
                )
            )
            session.flush()
            return decision

        def stop(reason):
            return record(
                AutoContinueDecision(
                    decision="stop",
                    reason=reason,
                    run_until_chapter=payload.run_until_chapter
                    or project.target_total_chapters,
                    target_total_chapters=project.target_total_chapters,
                )
            )

        child = session.scalar(
            select(GenerationTask).where(
                GenerationTask.continuation_parent_task_id == parent.id
            )
        )
        if child:
            return record(
                AutoContinueDecision(
                    decision="continue", reason="existing_child", next_task_id=child.id
                )
            )
        if parent.deleted_at:
            return stop("parent_deleted")
        if parent.cancel_requested:
            return stop("cancel_requested")
        if parent.pause_requested:
            return stop("user_pause_requested")
        if not payload.auto_continue:
            return stop("auto_continue_disabled")
        if GenerationTaskRepository(session).has_active(project.id):
            return stop("superseded")
        # An explicit newer run permanently supersedes this intent, even if it already finished.
        if session.scalar(
            select(GenerationTask.id)
            .where(
                GenerationTask.project_id == project.id,
                GenerationTask.id != parent.id,
                GenerationTask.created_at > parent.created_at,
            )
            .limit(1)
        ):
            return stop("superseded")
        if project.runtime_policy_version != payload.policy_version:
            return stop("policy_changed")
        if project.creation_status == "completed":
            return stop("project_completed")

        plans = list(
            session.scalars(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == project.id)
                .order_by(ChapterPlan.chapter_number)
                .with_for_update()
            )
        )
        accepted_max = max(
            (p.chapter_number for p in plans if p.status == "accepted"), default=0
        )
        until = payload.run_until_chapter or project.target_total_chapters
        if accepted_max >= until:
            return stop(
                "target_total_reached"
                if accepted_max >= project.target_total_chapters
                else "run_until_reached"
            )

        # Maintenance is queried without constructing the LLM/pipeline runtime.
        from forwin.models.maintenance import PostCanonMaintenanceRun
        from forwin.maintenance.state import post_canon_barrier_ready
        from forwin.models.canon import CanonCommitRecord
        from forwin.canon.identity import active_commit_predicate

        commit_id = session.scalar(
            select(CanonCommitRecord.id)
            .where(
                CanonCommitRecord.project_id == project.id,
                CanonCommitRecord.chapter_number < accepted_max + 1,
                CanonCommitRecord.status == "committed",
                active_commit_predicate(),
            )
            .order_by(CanonCommitRecord.chapter_number.desc())
            .limit(1)
        )
        if commit_id:
            rows = list(
                session.scalars(
                    select(PostCanonMaintenanceRun).where(
                        PostCanonMaintenanceRun.canon_commit_id == commit_id
                    )
                )
            )
            if not post_canon_barrier_ready(
                rows,
                session=session,
                band_checkpoint_action=payload.policy_snapshot.pause.band_checkpoint_action,
            ):
                return stop("maintenance_blocker")

        terminal = self._terminal_block_reason(event.result)
        retry_plan = None
        if terminal:
            if (
                terminal in {"pending_review_blocker", "needs_review_blocker"}
                and event.result.paused_chapters
            ):
                number = event.result.paused_chapters[0]
                retry_plan = next(
                    (
                        p
                        for p in plans
                        if p.chapter_number == number and p.status == "needs_review"
                    ),
                    None,
                )
                if retry_plan is not None and (
                    not eligible_for_auto_review_retry(
                        retry_plan, set(event.result.system_block_chapters)
                    )
                    or prior_auto_review_retry_count(session, project.id, number)
                ):
                    retry_plan = None
            if retry_plan is None:
                return stop(terminal)
        if any(p.status == "needs_review" and p is not retry_plan for p in plans):
            return stop("pending_review_blocker")
        if any(p.status == "drafted" for p in plans):
            return stop("pending_acceptance_blocker")
        next_chapter = retry_plan.chapter_number if retry_plan else accepted_max + 1
        target = resolve_generation_run_target(
            project,
            next_chapter=next_chapter,
            run_until_chapter=until,
            max_chapters=payload.max_chapters or None,
        )
        if retry_plan:
            reset_chapter_for_auto_review_retry(
                session,
                project_id=project.id,
                task_id=parent.id,
                chapter_number=retry_plan.chapter_number,
                plan=retry_plan,
                source="auto_continue_review_retry",
                reason="auto_continue_review_retry",
                summary=f"第{next_chapter}章 needs_review 自动重置为 planned。",
                terminal_block_reason=terminal,
                system_block=next_chapter in event.result.system_block_chapters,
            )
        workset = build_continue_generation_workset(
            session,
            project.id,
            max_chapters=target.effective_max_chapters,
            source="auto_continue",
            preloaded_plans=plans,
        )
        if not workset.requested_chapters:
            # No reset is valid unless it actually hands off to a child.
            if retry_plan:
                raise ValueError("review retry has no continuation workset")
            return stop(workset.reason or "no_remaining_chapters")
        from forwin.application.generation import EnqueueGenerationCommand

        handle = application._enqueue_in_session(
            EnqueueGenerationCommand(
                project_id=project.id,
                requested_chapters=workset.requested_chapters,
                max_chapters=target.effective_max_chapters,
                run_until_chapter=until,
                auto_continue=True,
                title=project.title,
                subtitle=f"自动续跑 · {project.genre}",
                message="前一批结束，持久续跑交接。",
                root_event_type=DecisionEventType.CONTINUE_REQUESTED,
                long_run_mode=payload.long_run_mode,
                isolated=payload.isolated,
            ),
            session,
            continuation_parent=parent,
        )
        return record(
            AutoContinueDecision(
                decision="continue",
                reason="auto_retry_review_blocker"
                if retry_plan
                else (
                    "future_arc_materialized"
                    if workset.reason == "future_arc_materialization_required"
                    else "chapter_completed_no_blocker"
                ),
                next_task_id=handle.task_id,
                next_chapter=next_chapter,
                run_until_chapter=until,
                target_total_chapters=project.target_total_chapters,
                requested_chapters=workset.requested_chapters,
                workset_reason=workset.reason,
            )
        )

    @staticmethod
    def _terminal_block_reason(result) -> str:
        if bool(getattr(result, "cancelled", False)):
            return "cancelled"
        if list(getattr(result, "failed_chapters", []) or []):
            return "failed_chapters_blocker"
        paused_chapters = set(
            _chapter_numbers(getattr(result, "paused_chapters", []) or [])
        )
        completed_chapters = set(
            _chapter_numbers(getattr(result, "completed_chapters", []) or [])
        )
        unresolved_paused_chapters = paused_chapters - completed_chapters
        if unresolved_paused_chapters:
            return "pending_review_blocker"
        safe_completed_pause = bool(paused_chapters) and not unresolved_paused_chapters
        if bool(getattr(result, "paused", False)) and not safe_completed_pause:
            return "user_pause_reached"
        status = str(getattr(result, "status", "") or "").strip()
        if status and status != "completed":
            if safe_completed_pause and status in {"needs_review", "paused"}:
                return ""
            if status == "no_rule_matched":
                return "manual_review_required_blocker"
            if status == "paused":
                return "user_pause_reached"
            return f"{status}_blocker"
        return ""
