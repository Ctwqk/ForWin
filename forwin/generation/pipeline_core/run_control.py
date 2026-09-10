from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.generation.pipeline_core.result import RunResult
from forwin.models.draft import ChapterDraft
from forwin.models.project import (
    ArcPlanVersion,
    ChapterPlan,
    Project,
)
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def _pending_chapter_numbers_for_active_arc(
    *,
    session: Session,
    project_id: str,
    max_chapters: int | None = None,
) -> list[int]:
    active_arc = session.execute(
        select(ArcPlanVersion)
        .where(
            ArcPlanVersion.project_id == project_id,
            ArcPlanVersion.status == "active",
        )
        .order_by(ArcPlanVersion.created_at.desc(), ArcPlanVersion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active_arc is None:
        return []
    chapter_numbers = list(
        session.execute(
            select(ChapterPlan.chapter_number)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.arc_plan_id == active_arc.id,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if max_chapters is not None:
        chapter_numbers = chapter_numbers[: max(1, int(max_chapters or 1))]
    return chapter_numbers


class RunControlStage:
    """Owns the run control stage behavior."""

    def run(
        self,
        premise: str,
        genre: str = "玄幻",
        num_chapters: int = 3,
    ) -> RunResult:
        """Generate *num_chapters* chapters from a premise.

        Returns a run summary including the project ID and chapter outcomes.
        """
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            project_id = ""

            # Step 1: Plan arc -----------------------------------------------
            self._emit_progress(
                "stage_changed",
                stage="planning_arc",
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            print(f"\n{'=' * 60}")
            print("正在规划故事大纲...")
            print(f"{'=' * 60}")
            arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

            # Step 2: Create project + seed state ----------------------------
            self._emit_progress(
                "stage_changed",
                stage="creating_project",
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            title = arc_plan.get("arc_synopsis", premise[:30])[:60]
            setting_summary = arc_plan.get("setting_summary", "")
            project = updater.create_project(
                title=title,
                premise=premise,
                genre=genre,
                setting_summary=setting_summary,
                target_total_chapters=num_chapters,
                runtime_policy=self.policy,
            )
            project_id = project.id
            self._bind_audit_context(project_id=project_id, updater=updater)

            self._seed_state(updater, project_id, arc_plan, num_chapters)
            session.commit()
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.RUN_STARTED,
                scope="task",
                summary="生成 run 已启动。",
                related_object_type="project",
                related_object_id=project_id,
                payload={"requested_chapters": num_chapters},
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.PROJECT_CREATED,
                scope="project",
                summary="项目已创建并写入初始规划。",
                related_object_type="project",
                related_object_id=project_id,
                payload={"requested_chapters": num_chapters},
            )
            session.commit()
            self._emit_progress(
                "project_created",
                project_id=project_id,
                title=project.title,
                requested_chapters=num_chapters,
            )

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=1,
            )
            session.commit()
            chapter_numbers = self._pending_chapter_numbers_for_active_arc(
                session=session,
                project_id=project_id,
            )
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            print(f"项目创建完成: {project.title}")
            print(f"项目ID: {project_id}")

            result = self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_numbers),
            )
            print(f"\n{'=' * 60}")
            if result.status == "needs_review":
                print(
                    "生成暂停："
                    f"第 {result.paused_chapters[0]} 章被质量门阻断，需要修复或重试。"
                )
            elif result.status == "completed":
                print(f"生成完毕！本轮完成 {len(result.completed_chapters)} 章")
            else:
                print(
                    "生成结束："
                    f"成功 {len(result.completed_chapters)} 章，"
                    f"失败 {len(result.failed_chapters)} 章"
                )
                print(
                    "失败章节: "
                    + ", ".join(str(chapter) for chapter in result.failed_chapters)
                )
            print(f"项目ID: {project_id}")
            print(f"数据库: {self.engine.url.render_as_string(hide_password=True)}")
            print(f"{'=' * 60}\n")

            self._emit_progress(
                "stage_changed",
                stage="paused_for_review"
                if result.status == "needs_review"
                else (
                    "failed"
                    if result.status in {"failed", "partial_failed"}
                    else ("cancelled" if result.status == "cancelled" else "completed")
                ),
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=result.completed_chapters[-1]
                if result.completed_chapters
                else 0,
                completed_chapters=result.completed_chapters,
                failed_chapters=result.failed_chapters,
                paused_chapters=result.paused_chapters,
                frozen_artifacts=result.frozen_artifacts,
            )
            return result

        except Exception:
            session.rollback()
            self._emit_progress("stage_changed", stage="failed")
            raise
        finally:
            self._clear_audit_context()
            session.close()

    def run_existing_project(
        self,
        project_id: str,
        *,
        num_chapters: int,
    ) -> RunResult:
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")

            existing_plans = (
                session.query(ChapterPlan)
                .filter(ChapterPlan.project_id == project_id)
                .count()
            )
            if existing_plans:
                session.close()
                return self.continue_project(project_id, max_chapters=num_chapters)

            premise = project.premise
            genre = project.genre or "玄幻"
            self._bind_audit_context(project_id=project_id, updater=updater)

            self._emit_progress(
                "stage_changed",
                stage="planning_arc",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)

            print(f"\n{'=' * 60}")
            print(f"正在为项目《{project.title}》规划故事大纲...")
            print(f"{'=' * 60}")
            arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

            self._emit_progress(
                "stage_changed",
                stage="creating_project",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)

            setting_summary = arc_plan.get("setting_summary", "")
            if setting_summary:
                project.setting_summary = setting_summary
            project.target_total_chapters = max(1, int(num_chapters or 1))
            if not str(project.title or "").strip():
                project.title = (
                    arc_plan.get("arc_synopsis", premise[:30]) or "未命名项目"
                )[:60]
            session.add(project)

            self._seed_state(updater, project_id, arc_plan, num_chapters)
            session.commit()

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=1,
            )
            session.commit()
            chapter_numbers = self._pending_chapter_numbers_for_active_arc(
                session=session,
                project_id=project_id,
            )
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            result = self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_numbers),
            )
            self._emit_progress(
                "stage_changed",
                stage="paused_for_review"
                if result.status == "needs_review"
                else (
                    "failed"
                    if result.status in {"failed", "partial_failed"}
                    else ("cancelled" if result.status == "cancelled" else "completed")
                ),
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=result.completed_chapters[-1]
                if result.completed_chapters
                else 0,
                completed_chapters=result.completed_chapters,
                failed_chapters=result.failed_chapters,
                paused_chapters=result.paused_chapters,
                frozen_artifacts=result.frozen_artifacts,
            )
            return result
        except Exception:
            session.rollback()
            self._emit_progress("stage_changed", stage="failed", project_id=project_id)
            raise
        finally:
            self._clear_audit_context()
            session.close()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        self.progress_recorder.emit(event, **payload)

    def _bind_audit_context(self, *, project_id: str, updater: StateUpdater) -> None:
        self.progress_recorder.bind(project_id=project_id, updater=updater)

    def _clear_audit_context(self) -> None:
        self.progress_recorder.clear()


    def _materialize_next_genesis_arc_if_needed(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
    ) -> bool:
        if str(getattr(project, "creation_status", "") or "").strip() != "writing":
            return False
        if not str(getattr(project, "active_genesis_revision_id", "") or "").strip():
            return False
        revision = self.book_genesis.active_revision(session, project)
        if revision is None:
            return False
        remaining_pending = session.execute(
            select(ChapterPlan.chapter_number)
            .where(
                ChapterPlan.project_id == project.id,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if remaining_pending is not None:
            return False
        promoted = self.book_genesis.promote_next_arc_if_needed(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
        )
        if promoted:
            session.commit()
        return promoted

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

            chapter_plans = (
                session.query(ChapterPlan)
                .filter(ChapterPlan.project_id == project_id)
                .order_by(ChapterPlan.chapter_number)
                .all()
            )
            if not chapter_plans:
                raise ValueError(f"项目没有章节规划: {project_id}")

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
                source="pipeline_continue",
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
                    "materialized_plan_count": workset.materialized_plan_count,
                    "workset_reason": workset.reason,
                },
            )
            session.commit()

            pending_chapter_numbers = list(workset.chapter_numbers)
            if workset.reason == "future_arc_materialization_required":
                if self._materialize_next_genesis_arc_if_needed(
                    session=session,
                    updater=updater,
                    project=project,
                ):
                    repo, updater, checker = self._make_state_helpers(session)
                    workset = build_continue_generation_workset(
                        session,
                        project_id,
                        max_chapters=max_chapters,
                        resume_from_chapter=resume_from_chapter,
                        source="pipeline_continue",
                    )
                    pending_chapter_numbers = list(workset.chapter_numbers)
            if not pending_chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                pending_chapter_count=len(pending_chapter_numbers),
                resolved_workset_count=workset.requested_chapters,
                current_chapter=min(pending_chapter_numbers) - 1,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, len(pending_chapter_numbers))
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=min(pending_chapter_numbers),
            )
            session.commit()
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=max_chapters,
                source="pipeline_continue",
            )
            chapter_numbers = list(workset.chapter_numbers)
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

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

    @staticmethod
    def _pending_chapter_numbers_for_active_arc(
        *, session: Session, project_id: str, max_chapters: int | None = None
    ) -> list[int]:
        return _pending_chapter_numbers_for_active_arc(
            session=session, project_id=project_id, max_chapters=max_chapters
        )


__all__ = ["RunControlStage"]
