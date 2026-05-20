from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.generation.run_target import resolve_generation_run_target
from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.project import ChapterPlan, Project
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
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        create_continue_generation_task: Callable[..., str],
    ) -> None:
        self.session_factory = session_factory
        self.create_continue_generation_task = create_continue_generation_task

    def after_task_completion(
        self,
        result: Any,
        *,
        parent_task_id: str,
        run_until_chapter: int | None,
        max_chapters: int | None,
        auto_continue: bool,
        runtime_config: Any = None,
    ) -> AutoContinueDecision:
        project_id = str(getattr(result, "project_id", "") or "").strip()
        if not project_id:
            return AutoContinueDecision(decision="stop", reason="missing_project_id")
        if not auto_continue:
            return self._record_decision(
                project_id=project_id,
                parent_task_id=parent_task_id,
                decision=AutoContinueDecision(decision="stop", reason="auto_continue_disabled"),
            )
        terminal_block_reason = self._terminal_block_reason(result)
        if terminal_block_reason:
            return self._record_decision(
                project_id=project_id,
                parent_task_id=parent_task_id,
                decision=AutoContinueDecision(decision="stop", reason=terminal_block_reason),
            )

        with self.session_factory() as session:
            project = session.get(Project, project_id)
            if project is None:
                return AutoContinueDecision(decision="stop", reason="project_not_found")

            target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
            normalized_until = (
                target_total_chapters if run_until_chapter is None else int(run_until_chapter)
            )
            project_title = str(getattr(project, "title", "") or "")
            project_genre = str(getattr(project, "genre", "") or "")
            plans = list(
                session.execute(
                    select(ChapterPlan)
                    .where(ChapterPlan.project_id == project_id)
                    .order_by(ChapterPlan.chapter_number.asc())
                ).scalars()
            )
            accepted_max = max(
                (int(plan.chapter_number or 0) for plan in plans if str(plan.status or "") == "accepted"),
                default=0,
            )
            if any(str(plan.status or "") == "needs_review" for plan in plans):
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason="pending_review_blocker",
                        run_until_chapter=normalized_until,
                        target_total_chapters=target_total_chapters,
                    ),
                )
            if any(str(plan.status or "") == "drafted" for plan in plans):
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason="pending_acceptance_blocker",
                        run_until_chapter=normalized_until,
                        target_total_chapters=target_total_chapters,
                    ),
                )

            if accepted_max >= normalized_until:
                reason = (
                    "target_total_reached"
                    if target_total_chapters > 0 and accepted_max >= target_total_chapters
                    else "run_until_reached"
                )
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason=reason,
                        next_chapter=accepted_max + 1,
                        run_until_chapter=normalized_until,
                        target_total_chapters=target_total_chapters,
                    ),
                )

            next_chapter = accepted_max + 1
            target = resolve_generation_run_target(
                project,
                next_chapter=next_chapter,
                run_until_chapter=normalized_until,
                max_chapters=max_chapters,
            )
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=target.effective_max_chapters,
                source="auto_continue",
                preloaded_plans=plans,
            )
            if workset.requested_chapters <= 0:
                return self._record_decision(
                    project_id=project_id,
                    parent_task_id=parent_task_id,
                    decision=AutoContinueDecision(
                        decision="stop",
                        reason=workset.reason or "no_remaining_chapters",
                        next_chapter=next_chapter,
                        run_until_chapter=target.run_until_chapter,
                        target_total_chapters=target.target_total_chapters,
                        workset_reason=workset.reason,
                    ),
                )

        next_task_id = _call_task_factory(
            self.create_continue_generation_task,
            {
                "project_id": project_id,
                "runtime_config": runtime_config,
                "requested_chapters": workset.requested_chapters,
                "max_chapters": target.effective_max_chapters,
                "auto_continue": True,
                "run_until_chapter": target.run_until_chapter,
                "title": project_title,
                "subtitle": f"自动续跑 · {project_genre}",
                "message": "前一批完成，无阻断，自动继续生成。",
            },
        )
        reason = (
            "future_arc_materialized"
            if workset.reason == "future_arc_materialization_required"
            else "chapter_completed_no_blocker"
        )
        return self._record_decision(
            project_id=project_id,
            parent_task_id=parent_task_id,
            decision=AutoContinueDecision(
                decision="continue",
                reason=reason,
                next_task_id=next_task_id,
                next_chapter=workset.chapter_numbers[0] if workset.chapter_numbers else next_chapter,
                run_until_chapter=target.run_until_chapter,
                target_total_chapters=target.target_total_chapters,
                requested_chapters=workset.requested_chapters,
                workset_reason=workset.reason,
            ),
        )

    def _terminal_block_reason(self, result: Any) -> str:
        if bool(getattr(result, "paused", False)):
            return "user_pause_reached"
        if bool(getattr(result, "cancelled", False)):
            return "cancelled"
        if list(getattr(result, "failed_chapters", []) or []):
            return "failed_chapters_blocker"
        if list(getattr(result, "paused_chapters", []) or []):
            return "pending_review_blocker"
        status = str(getattr(result, "status", "") or "").strip()
        if status and status != "completed":
            return f"{status}_blocker"
        return ""

    def _record_decision(
        self,
        *,
        project_id: str,
        parent_task_id: str,
        decision: AutoContinueDecision,
    ) -> AutoContinueDecision:
        with self.session_factory() as session:
            updater = StateUpdater(session)
            updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id,
                    task_id=parent_task_id,
                    scope="task",
                    event_family="audit_action",
                    event_type=DecisionEventType.AUTO_CONTINUE_DECISION,
                    actor_type="system",
                    summary=f"Auto-continue decision: {decision.decision} ({decision.reason})",
                    payload={
                        "decision": decision.decision,
                        "reason": decision.reason,
                        "next_task_id": decision.next_task_id,
                        "next_chapter": decision.next_chapter,
                        "run_until_chapter": decision.run_until_chapter,
                        "target_total_chapters": decision.target_total_chapters,
                        "requested_chapters": decision.requested_chapters,
                        "workset_reason": decision.workset_reason,
                    },
                    related_object_type="generation_task",
                    related_object_id=parent_task_id,
                )
            )
            session.commit()
        return decision


def _call_task_factory(
    create_continue_generation_task: Callable[..., str],
    kwargs: dict[str, Any],
) -> str:
    try:
        signature = inspect.signature(create_continue_generation_task)
    except (TypeError, ValueError):
        return create_continue_generation_task(**kwargs)

    accepted_names: set[str] = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return create_continue_generation_task(**kwargs)
        if parameter.kind in {
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            accepted_names.add(parameter.name)

    filtered_kwargs = {
        name: value for name, value in kwargs.items() if name in accepted_names
    }
    return create_continue_generation_task(**filtered_kwargs)
