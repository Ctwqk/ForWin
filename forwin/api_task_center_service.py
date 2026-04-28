from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from forwin.api_project_payloads import build_generation_control, _recent_rows_by_project
from forwin.api_schemas import TaskCenterItemResponse
from forwin.models.governance import BandCheckpoint, DecisionEvent
from forwin.models.phase import ProvisionalBandExecution
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask


@dataclass(slots=True)
class TaskCenterService:
    get_session: Callable[[], Any]
    has_db_session: Callable[[], bool]
    prune_tasks: Callable[[], None]
    utcnow: Callable[[], datetime]
    display_datetime: Callable[[Any], str]
    coerce_task_datetime: Callable[[Any], datetime]
    new_stage_history_entry: Callable[..., dict[str, Any]]
    cached_generation_task: Callable[[str], dict[str, Any] | None]
    iter_cached_generation_tasks: Callable[[], list[tuple[str, dict[str, Any]]]]
    prefer_cached_generation_task: Callable[[dict[str, Any] | None, dict[str, Any] | None], dict[str, Any] | None]
    generation_task_from_row: Callable[[Any], dict[str, Any]]
    config_provider: Callable[[], Any]
    terminal_statuses: set[str]
    terminal_stage_by_status: dict[str, str]

    @staticmethod
    def project_task_id(project_id: str) -> str:
        return f"project-{project_id}"

    @staticmethod
    def parse_project_task_id(task_id: str) -> str | None:
        normalized = str(task_id or "").strip()
        if not normalized.startswith("project-"):
            return None
        project_id = normalized[len("project-"):].strip()
        return project_id or None

    def task_has_stage(self, task: dict[str, Any], stage: str) -> bool:
        history = task.get("stage_history", [])
        if not isinstance(history, list):
            return False
        return any(
            str(entry.get("stage", "")).strip() == stage
            for entry in history
            if isinstance(entry, dict)
        )

    def normalize_loaded_generation_task(self, task: dict[str, Any]) -> dict[str, Any]:
        status = str(task.get("status", "")).strip()
        expected_stage = self.terminal_stage_by_status.get(status)
        if not expected_stage:
            return task
        current_stage = str(task.get("current_stage", "")).strip()
        if current_stage == expected_stage:
            return task
        normalized = dict(task)
        normalized["current_stage"] = expected_stage
        history = list(normalized.get("stage_history", []))
        if not history or str(history[-1].get("stage", "")).strip() != expected_stage:
            history.append(
                self.new_stage_history_entry(
                    expected_stage,
                    now=normalized.get("updated_at") if isinstance(normalized.get("updated_at"), datetime) else None,
                    current_chapter=int(normalized.get("current_chapter", 0) or 0),
                    message=str(normalized.get("message", "")).strip(),
                )
            )
            normalized["stage_history"] = history
        return normalized

    def apply_task_visibility_rules(
        self,
        task: dict[str, Any] | None,
        *,
        include_deleted: bool,
    ) -> dict[str, Any] | None:
        if task is None:
            return None
        normalized = self.normalize_loaded_generation_task(task)
        if normalized.get("deleted") and not include_deleted:
            return None
        return normalized

    def load_generation_task(
        self,
        task_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        if not self.has_db_session():
            return self.apply_task_visibility_rules(
                self.cached_generation_task(task_id),
                include_deleted=include_deleted,
            )

        with self.get_session() as session:
            cached = self.cached_generation_task(task_id)
            row = session.get(GenerationTask, task_id)
            persisted = self.generation_task_from_row(row) if row is not None else None
            task = self.prefer_cached_generation_task(persisted, cached)
            if task is not None:
                task = self._augment_task_with_provisional_history(
                    session,
                    task_id,
                    task,
                )
            return self.apply_task_visibility_rules(task, include_deleted=include_deleted)

    def list_generation_tasks(self, limit: int) -> list[tuple[str, dict[str, Any]]]:
        self.prune_tasks()
        normalized_limit = max(1, min(int(limit or 30), 100))
        if not self.has_db_session():
            return [
                (task_id, dict(task))
                for task_id, task in sorted(
                    self.iter_cached_generation_tasks(),
                    key=lambda item: item[1].get("updated_at", self.utcnow()),
                    reverse=True,
                )
                if not task.get("deleted")
            ][:normalized_limit]

        with self.get_session() as session:
            rows = session.execute(
                select(GenerationTask)
                .where(GenerationTask.deleted_at.is_(None))
                .order_by(GenerationTask.updated_at.desc())
                .limit(normalized_limit)
            ).scalars().all()
            merged: dict[str, dict[str, Any]] = {}
            persisted_tasks: list[tuple[str, dict[str, Any]]] = []
            for row in rows:
                persisted = self.generation_task_from_row(row)
                cached = self.cached_generation_task(row.id)
                task = self.prefer_cached_generation_task(persisted, cached)
                if task is not None:
                    persisted_tasks.append((row.id, task))
            provisional_map = self._provisional_execution_map(session, persisted_tasks)
            for task_id, task in persisted_tasks:
                visible = self.apply_task_visibility_rules(
                    self._apply_provisional_execution(task, provisional_map.get(task_id)),
                    include_deleted=False,
                )
                if visible is not None:
                    merged[task_id] = visible
            for task_id, cached in self.iter_cached_generation_tasks():
                visible = self.apply_task_visibility_rules(cached, include_deleted=False)
                if visible is None:
                    continue
                current = merged.get(task_id)
                merged[task_id] = self.prefer_cached_generation_task(current, visible) or visible
            return sorted(
                merged.items(),
                key=lambda item: self.coerce_task_datetime(item[1].get("updated_at")),
                reverse=True,
            )[:normalized_limit]

    def list_project_backed_task_items(self, limit: int) -> list[TaskCenterItemResponse]:
        live_project_ids: set[str] = set()
        if self.has_db_session():
            with self.get_session() as session:
                live_project_ids = {
                    str(project_id).strip()
                    for project_id in session.execute(
                        select(GenerationTask.project_id).where(
                            GenerationTask.deleted_at.is_(None),
                            GenerationTask.project_id != "",
                            GenerationTask.status.notin_(tuple(self.terminal_statuses)),
                        )
                    ).scalars().all()
                    if str(project_id).strip()
                }
        session = self.get_session()
        try:
            projects = session.execute(
                select(Project)
                .order_by(Project.updated_at.desc())
                .limit(max(1, min(int(limit or 50), 200)))
            ).scalars().all()
            plans_by_project = self._load_project_task_center_plans(
                session,
                [project.id for project in projects],
            )
            latest_checkpoint_map = self._latest_band_checkpoint_by_project(
                session,
                [project.id for project in projects],
            )
            decision_timeline_map = self._decision_timeline_by_project(
                session,
                [project.id for project in projects],
            )
            items: list[TaskCenterItemResponse] = []
            for project in projects:
                if project.id in live_project_ids:
                    continue
                items.append(
                    self._build_project_task_center_item(
                        project,
                        plans_by_project.get(project.id, []),
                        latest_band_checkpoint=latest_checkpoint_map.get(project.id),
                        decision_events=decision_timeline_map.get(project.id, []),
                    )
                )
            return items
        finally:
            session.close()

    def get_project_backed_task_item_or_404(self, task_id: str) -> TaskCenterItemResponse:
        project_id = self.parse_project_task_id(task_id)
        if not project_id:
            raise HTTPException(404, "任务不存在")
        session = self.get_session()
        try:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "项目不存在")
            plans_by_project = self._load_project_task_center_plans(session, [project.id])
            latest_checkpoint_map = self._latest_band_checkpoint_by_project(session, [project.id])
            decision_timeline_map = self._decision_timeline_by_project(session, [project.id])
            return self._build_project_task_center_item(
                project,
                plans_by_project.get(project.id, []),
                latest_band_checkpoint=latest_checkpoint_map.get(project.id),
                decision_events=decision_timeline_map.get(project.id, []),
            )
        finally:
            session.close()

    def _augment_task_with_provisional_history(
        self,
        session,
        task_id: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        provisional_map = self._provisional_execution_map(session, [(task_id, task)])
        return self._apply_provisional_execution(task, provisional_map.get(task_id))

    def _provisional_execution_map(
        self,
        session,
        task_entries: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, ProvisionalBandExecution]:
        windows: list[tuple[str, str, datetime, datetime]] = []
        project_filters: list[Any] = []
        project_ranges: dict[str, tuple[datetime, datetime]] = {}
        minimum = datetime.min.replace(tzinfo=timezone.utc)

        for task_id, task in task_entries:
            project_id = str(task.get("project_id", "") or "").strip()
            if not project_id or self.task_has_stage(task, "running_provisional_preview"):
                continue
            created_at = self.coerce_task_datetime(task.get("created_at"))
            if created_at == minimum:
                continue
            finished_at = self.coerce_task_datetime(task.get("finished_at"))
            updated_at = self.coerce_task_datetime(task.get("updated_at"))
            window_end = max(finished_at, updated_at)
            if window_end == minimum:
                window_end = self.utcnow()
            start = created_at - timedelta(seconds=5)
            end = window_end + timedelta(seconds=5)
            windows.append((task_id, project_id, start, end))
            current_range = project_ranges.get(project_id)
            if current_range is None:
                project_ranges[project_id] = (start, end)
            else:
                project_ranges[project_id] = (min(current_range[0], start), max(current_range[1], end))

        if not windows:
            return {}

        for project_id, (start, end) in project_ranges.items():
            project_filters.append(
                and_(
                    ProvisionalBandExecution.project_id == project_id,
                    ProvisionalBandExecution.created_at >= start,
                    ProvisionalBandExecution.created_at <= end,
                )
            )

        executions = session.execute(
            select(ProvisionalBandExecution)
            .where(or_(*project_filters))
            .order_by(ProvisionalBandExecution.created_at.asc())
        ).scalars().all()

        executions_by_project: dict[str, list[ProvisionalBandExecution]] = {}
        for execution in executions:
            executions_by_project.setdefault(str(execution.project_id or ""), []).append(execution)

        matched: dict[str, ProvisionalBandExecution] = {}
        for task_id, project_id, start, end in windows:
            for execution in executions_by_project.get(project_id, []):
                created_at = self.coerce_task_datetime(getattr(execution, "created_at", None))
                if created_at == minimum:
                    continue
                if start <= created_at <= end:
                    matched[task_id] = execution
                    break
        return matched

    def _apply_provisional_execution(
        self,
        task: dict[str, Any],
        execution: ProvisionalBandExecution | None,
    ) -> dict[str, Any]:
        if execution is None:
            return task
        try:
            chapter_numbers = json.loads(execution.chapter_numbers_json or "[]")
        except json.JSONDecodeError:
            chapter_numbers = []
        chapter = int(chapter_numbers[0]) if chapter_numbers else 0
        augmented = dict(task)
        history = list(augmented.get("stage_history", []))
        history.append(
            self.new_stage_history_entry(
                "running_provisional_preview",
                now=self.coerce_task_datetime(execution.created_at),
                current_chapter=chapter,
                message=str(augmented.get("message", "")).strip(),
            )
        )
        if (
            str(execution.aggregate_verdict or "").strip().lower() == "fail"
            or int(execution.failure_count or 0) > 0
        ) and not self.task_has_stage(augmented, "provisional_failed"):
            history.append(
                self.new_stage_history_entry(
                    "provisional_failed",
                    now=execution.created_at,
                    current_chapter=chapter,
                    message="Provisional 预演失败，已阻断正式写作。",
                )
            )
        augmented["stage_history"] = history
        return augmented

    def _load_project_task_center_plans(
        self,
        session,
        project_ids: list[str],
    ) -> dict[str, list[ChapterPlan]]:
        ids = [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]
        if not ids:
            return {}
        rows = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.project_id.in_(ids))
            .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
        ).scalars().all()
        grouped: dict[str, list[ChapterPlan]] = {project_id: [] for project_id in ids}
        for row in rows:
            grouped.setdefault(str(row.project_id), []).append(row)
        return grouped

    def _build_project_task_center_item(
        self,
        project: Project,
        plans: list[ChapterPlan],
        *,
        latest_band_checkpoint: BandCheckpoint | None,
        decision_events: list[Any],
    ) -> TaskCenterItemResponse:
        requested = len(plans)
        config = self.config_provider()
        review_interval = max(0, int(getattr(config, "review_interval_chapters", 0) if config else 0))
        generation_control = build_generation_control(
            plans=plans,
            latest_replan=None,
            review_interval_chapters=review_interval,
            latest_band_checkpoint=latest_band_checkpoint,
            decision_events=decision_events,
        )
        accepted = list(generation_control.accepted_chapters)
        failed = list(generation_control.failed_chapters)
        paused = list(generation_control.pending_review_chapters)
        generated = list(generation_control.generated_chapters)
        planned = list(generation_control.planned_chapters)
        if requested == 0:
            status = "created"
            current_stage = "queued"
        elif paused:
            status = "needs_review"
            current_stage = "paused_for_review"
        elif failed and not generated:
            status = "failed"
            current_stage = "failed"
        elif failed:
            status = "partial_failed"
            current_stage = "failed"
        else:
            status = "completed"
            current_stage = "completed"
        current_chapter = max(generated + failed, default=0)
        message = "书本已创建，当前没有活跃生成任务。" if requested == 0 else "项目入口（当前没有活跃生成任务）"
        can_resume = bool((planned or failed) and not paused)
        stage_history = [
            self.new_stage_history_entry(
                current_stage,
                now=project.updated_at,
                current_chapter=current_chapter,
                message=message,
            )
        ]
        generation_control = generation_control.model_copy(
            update={
                "plan_state": "none" if requested == 0 else generation_control.plan_state,
                "current_stage": current_stage,
                "current_chapter": current_chapter,
                "next_chapter": min(planned + failed, default=0),
                "can_resume": can_resume,
            }
        )
        return TaskCenterItemResponse(
            task_kind="generation",
            task_id=self.project_task_id(project.id),
            status=status,
            title=project.title,
            subtitle=f"书本入口 · {project.genre}",
            project_id=project.id,
            message=message,
            current_stage=current_stage,
            stage_history=stage_history,
            requested_chapters=requested,
            current_chapter=current_chapter,
            completed_chapters=accepted,
            failed_chapters=failed,
            paused_chapters=paused,
            generation_control=generation_control,
            resumable=can_resume,
            created_at=self.display_datetime(project.created_at),
            updated_at=self.display_datetime(project.updated_at),
            terminable=False,
            deletable=False,
        )

    def _latest_band_checkpoint_by_project(
        self,
        session,
        project_ids: list[str],
    ) -> dict[str, BandCheckpoint]:
        ids = [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]
        if not ids:
            return {}
        rows = session.execute(
            select(BandCheckpoint)
            .where(BandCheckpoint.project_id.in_(ids))
            .order_by(
                BandCheckpoint.project_id.asc(),
                BandCheckpoint.created_at.desc(),
                BandCheckpoint.id.desc(),
            )
        ).scalars().all()
        latest: dict[str, BandCheckpoint] = {}
        for row in rows:
            project_id = str(row.project_id or "")
            if project_id and project_id not in latest:
                latest[project_id] = row
        return latest

    def _decision_timeline_by_project(
        self,
        session,
        project_ids: list[str],
        *,
        limit: int = 12,
    ) -> dict[str, list[DecisionEvent]]:
        ids = [str(project_id or "").strip() for project_id in project_ids if str(project_id or "").strip()]
        if not ids:
            return {}
        rows_by_project = _recent_rows_by_project(
            session,
            DecisionEvent,
            DecisionEvent.project_id,
            ids,
            order_by=(
                DecisionEvent.created_at.desc(),
                DecisionEvent.id.desc(),
            ),
            limit=limit,
        )
        return {project_id: list(rows) for project_id, rows in rows_by_project.items()}
