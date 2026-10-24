from __future__ import annotations

import logging

from forwin.generation.pipeline_core.result import RunResult
from typing import Any
from forwin.models.project import ChapterPlan
from forwin.models.project import (
    Project,
)
from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.models.draft import ChapterDraft
from forwin.planning.band_planner.service import BandPlanUnavailable
from forwin.audit.events import DecisionEventType
from forwin.observability.context import OperationContext
from sqlalchemy.orm import Session
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
import time

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------




class RunControlStage:
    """Owns the run control stage behavior."""


    def _emit_progress(self, event: str, **payload: Any) -> None:
        if event == "stage_changed":
            try:
                self._record_stage_transition(payload)
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring stage transition tracking error.", exc_info=True)
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, payload)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring progress callback error.", exc_info=True)

    def _bind_audit_context(
        self,
        *,
        project_id: str,
        updater: StateUpdater,
    ) -> None:
        self._audit_project_id = str(project_id or "").strip()
        self._audit_updater = updater
        self._audit_stage_name = ""
        self._audit_stage_started_at = 0.0
        self._audit_stage_chapter_number = 0
        self._audit_stage_span = None

    def _clear_audit_context(self) -> None:
        self._finish_audit_stage_span(next_stage="", chapter_number=0)
        self._audit_project_id = ""
        self._audit_updater = None
        self._audit_stage_name = ""
        self._audit_stage_started_at = 0.0
        self._audit_stage_chapter_number = 0
        self._audit_stage_span = None

    def _start_audit_stage_span(
        self, *, project_id: str, stage: str, chapter_number: int
    ) -> None:
        if self._audit_stage_span is not None:
            return
        context = OperationContext(
            project_id=project_id,
            task_id=self._audit_task_id,
            chapter_number=int(chapter_number or 0),
            stage=stage,
            operation_id=self._audit_operation_id(),
        )
        span = self.observability.span(
            context,
            f"stage.{stage}",
            span_kind="stage",
            component="pipeline",
            tags={"stage": stage},
        )
        span.__enter__()
        self._audit_stage_span = span

    def _finish_audit_stage_span(self, *, next_stage: str, chapter_number: int) -> None:
        span = self._audit_stage_span
        if span is None:
            return
        try:
            span.tag("next_stage", str(next_stage or ""))
            stage_chapter_number = int(
                getattr(self, "_audit_stage_chapter_number", 0) or chapter_number or 0
            )
            if stage_chapter_number:
                span.metric("chapter_number", stage_chapter_number)
            span.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.debug(
                "Ignoring audit control stage span close failure.", exc_info=True
            )
        finally:
            self._audit_stage_span = None

    def _record_stage_transition(self, payload: dict[str, Any]) -> None:
        updater = self._audit_updater
        project_id = str(
            payload.get("project_id") or self._audit_project_id or ""
        ).strip()
        stage = str(payload.get("stage") or "").strip()
        if updater is None or not project_id or not stage:
            return
        now = time.perf_counter()
        chapter_number = int(payload.get("current_chapter") or 0)
        if self._audit_stage_name and self._audit_stage_name != stage:
            stage_chapter_number = int(
                getattr(self, "_audit_stage_chapter_number", 0) or chapter_number or 0
            )
            duration_ms = max(0, int((now - self._audit_stage_started_at) * 1000))
            stage_payload = {
                "stage": self._audit_stage_name,
                "next_stage": stage,
                "duration_ms": duration_ms,
            }
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=stage_chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_EXITED,
                scope="task",
                summary=f"阶段 {self._audit_stage_name} 已结束。",
                payload=stage_payload,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=stage_chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                scope="task",
                summary=f"阶段 {self._audit_stage_name} 用时 {duration_ms}ms。",
                payload=stage_payload,
            )
            self._finish_audit_stage_span(
                next_stage=stage, chapter_number=stage_chapter_number
            )
        if self._audit_stage_name != stage:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_ENTERED,
                scope="task",
                summary=f"阶段 {stage} 已开始。",
                payload={"stage": stage},
            )
            self._audit_stage_name = stage
            self._audit_stage_started_at = now
            self._audit_stage_chapter_number = chapter_number
            self._start_audit_stage_span(
                project_id=project_id,
                stage=stage,
                chapter_number=chapter_number,
            )

    def _ensure_chapter_plan(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        policy: RuntimePolicy,
    ) -> ChapterPlan | None:
        """The chapter's plan row, after activating its arc and planning its band when needed.

        None when the band cannot be planned; the planner has then opened, or found, the band_planning
        conflict that blocks the chapter. Both steps may call a model, so nothing is left locked first.
        """
        session.commit()
        repo = StateRepository(session)
        active = repo.get_active_arc_plan(project_id)
        if active is not None and chapter_number < active.chapter_start:
            existing = repo.get_chapter_plan(project_id, chapter_number)
            if existing is not None:
                session.refresh(existing)
                return existing
        if active is None or not active.chapter_start <= chapter_number <= active.chapter_end:
            self.arc_progression.activate_next_arc_if_needed(
                session, project_id=project_id, after_chapter=chapter_number - 1, policy=policy
            )
        try:
            plan = self.band_planner.ensure_plan_for_chapter(
                session, project_id=project_id, chapter_number=chapter_number, policy=policy
            )
        except BandPlanUnavailable:
            # The active arc has no beat sheet: it was activated before beat sheets, or by an
            # activation that stopped before writing one. Without an active arc there is nothing to plan from.
            session.rollback()
            if self.arc_progression.ensure_beat_sheet(
                session, project_id=project_id, chapter_number=chapter_number, policy=policy
            ) is None:
                raise
            plan = self.band_planner.ensure_plan_for_chapter(
                session, project_id=project_id, chapter_number=chapter_number, policy=policy
            )
        session.commit()
        return plan

    def continue_project(
        self,
        project_id: str,
        max_chapters: int | None = None,
        resume_from_chapter: int | None = None,
    ) -> RunResult:
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            self._bind_audit_context(project_id=project_id, updater=updater)
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")

            # Chapters without a plan row are planned band by band as the run reaches them.
            chapter_plans = (
                session.query(ChapterPlan)
                .filter(ChapterPlan.project_id == project_id)
                .order_by(ChapterPlan.chapter_number)
                .all()
            )

            waiting_review: list[int] = []
            for plan in chapter_plans:
                if plan.status != "needs_review":
                    continue
                latest_draft = (
                    session.query(ChapterDraft)
                    .filter(ChapterDraft.chapter_plan_id == plan.id)
                    .order_by(ChapterDraft.version.desc())
                    .first()
                )
                if latest_draft is None:
                    logger.warning(
                        "Resetting orphan needs_review chapter back to planned for project=%s chapter=%d",
                        project_id,
                        plan.chapter_number,
                    )
                    plan.status = "planned"
                    session.add(plan)
                    continue
                waiting_review.append(plan.chapter_number)
            if waiting_review:
                waiting = ", ".join(str(number) for number in waiting_review)
                raise ValueError(f"仍有章节等待 review：{waiting}")
            session.commit()
            repo, updater, checker = self._make_state_helpers(session)
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=max_chapters,
                resume_from_chapter=resume_from_chapter,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.RUN_STARTED,
                scope="task",
                summary="已有项目生成 run 已启动。",
                related_object_type="project",
                related_object_id=project_id,
                payload={
                    "resolved_workset_count": workset.requested_chapters,
                    "workset_reason": workset.reason,
                },
            )
            session.commit()

            pending_chapter_numbers = list(workset.chapter_numbers)
            if not pending_chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            self._emit_progress(
                "stage_changed",
                stage="planning_band",
                project_id=project_id,
                pending_chapter_count=len(pending_chapter_numbers),
                resolved_workset_count=workset.requested_chapters,
                current_chapter=min(pending_chapter_numbers) - 1,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, len(pending_chapter_numbers))
            chapter_numbers = pending_chapter_numbers
            return self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_numbers),
            )
        finally:
            self._clear_audit_context()
            session.close()



__all__ = ["RunControlStage"]
