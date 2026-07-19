from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.plan import CanonCommitPlan
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forwin.application.errors import (
    ActiveGenerationTaskError,
    GenerationTaskLeaseLost,
    PermanentConfigurationError,
    ProjectNotFound,
)
from forwin.config import InfrastructureConfig
from forwin.generation.task_payload import (
    GenerationExecutionContext,
    build_execution_context,
    execution_payload,
    payload_from_json,
)
from forwin.generation.task_repository import (
    GenerationTaskRepository,
    new_task_id,
)
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
    ensure_decision_event_type,
)
from forwin.models.project import ChapterPlan, Project
from forwin.models.audit import DecisionEvent
from forwin.models.task import GenerationTask
from forwin.maintenance.deferred import (
    DeferredMaintenanceRecord,
    record_deferred_maintenance,
)
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater


@dataclass(frozen=True, slots=True)
class EnqueueGenerationCommand:
    project_id: str
    requested_chapters: int
    max_chapters: int
    run_until_chapter: int
    auto_continue: bool
    title: str
    subtitle: str
    root_event_type: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class GenerationTaskHandle:
    task_id: str
    project_id: str


GenerationRunner = Callable[
    [GenerationTask, GenerationExecutionContext, int, str, int],
    None,
]


class GenerationApplicationService:
    def __init__(
        self,
        *,
        session_factory,
        infrastructure: InfrastructureConfig,
        runner: GenerationRunner | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.infrastructure = infrastructure
        self.runner = runner or self._run_claimed

    def enqueue(self, command: EnqueueGenerationCommand) -> GenerationTaskHandle:
        project_id = str(command.project_id or "").strip()
        if not project_id:
            raise ProjectNotFound(project_id)
        requested_chapters = max(1, int(command.requested_chapters or 0))
        max_chapters = max(0, int(command.max_chapters or 0))
        run_until_chapter = max(0, int(command.run_until_chapter or 0))
        task_id = new_task_id()
        try:
            with self.session_factory.begin() as session:
                project = session.get(Project, project_id)
                if project is None:
                    raise ProjectNotFound(project_id)
                repository = GenerationTaskRepository(session)
                if repository.has_active(project_id):
                    raise ActiveGenerationTaskError(
                        f"active generation task exists for project: {project_id}"
                    )
                policy_record = ProjectPolicyStore(session).load(project)
                root_event = StateUpdater(session).save_decision_event(
                    DecisionEventInfo(
                        project_id=project_id,
                        task_id=task_id,
                        scope="task",
                        event_family="business_event",
                        event_type=ensure_decision_event_type(
                            str(command.root_event_type or "")
                        ),
                        actor_type="api",
                        summary="生成任务已创建。",
                        payload={
                            "requested_chapters": requested_chapters,
                            "max_chapters": max_chapters,
                            "run_until_chapter": run_until_chapter,
                        },
                        related_object_type="generation_task",
                        related_object_id=task_id,
                    )
                )
                payload = execution_payload(
                    mode="continue",
                    policy=policy_record.policy,
                    policy_version=policy_record.version,
                    root_event_id=root_event.id,
                    auto_continue=command.auto_continue,
                    run_until_chapter=run_until_chapter,
                    max_chapters=max_chapters,
                )
                task = repository.create(
                    task_id=task_id,
                    project_id=project_id,
                    title=str(command.title or ""),
                    subtitle=str(command.subtitle or ""),
                    message=str(command.message or ""),
                    requested_chapters=requested_chapters,
                    max_chapters=max_chapters,
                    run_until_chapter=run_until_chapter,
                    payload=payload,
                )
                return GenerationTaskHandle(
                    task_id=task.id,
                    project_id=project_id,
                )
        except IntegrityError as exc:
            if "ux_generation_tasks_one_active_per_project" in str(exc):
                raise ActiveGenerationTaskError(
                    f"active generation task exists for project: {project_id}"
                ) from exc
            raise

    def execute_claimed(
        self,
        task: GenerationTask,
        *,
        resume_from_chapter: int,
        worker_id: str,
        claim_kind: str = "queued",
        lease_epoch: int | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_lease_epoch = int(
            task.lease_epoch if lease_epoch is None else lease_epoch
        )
        if task.cancel_requested:
            acknowledged_at = datetime.now(timezone.utc)
            self._task_updater(
                worker_id=normalized_worker_id,
                lease_epoch=normalized_lease_epoch,
            )(
                task.id,
                status="cancelled",
                current_stage="cancelled",
                message="生成 worker 已确认终止请求，任务已取消。",
                error=None,
                finished_at=acknowledged_at,
            )
            return
        if task.pause_requested:
            acknowledged_at = datetime.now(timezone.utc)
            self._task_updater(
                worker_id=normalized_worker_id,
                lease_epoch=normalized_lease_epoch,
            )(
                task.id,
                status="paused",
                current_stage="paused",
                message="生成 worker 已确认暂停请求，任务已安全暂停。",
                error=None,
                finished_at=acknowledged_at,
                paused_at=acknowledged_at,
            )
            return
        if str(claim_kind or "") == "expired_running":
            resume_from_chapter = self._recover_committed_chapter(
                task,
                resume_from_chapter=max(0, int(resume_from_chapter or 0)),
                worker_id=normalized_worker_id,
                lease_epoch=normalized_lease_epoch,
            )
        completed_chapters = _task_chapter_numbers(task.completed_chapters_json)
        payload = payload_from_json(task.execution_payload_json)
        if int(task.requested_chapters or 0) > 0 and len(completed_chapters) >= int(
            task.requested_chapters or 0
        ):
            self._task_updater(
                worker_id=normalized_worker_id,
                lease_epoch=normalized_lease_epoch,
            )(
                task.id,
                status="completed",
                current_stage="completed",
                current_chapter=max(completed_chapters, default=0),
                completed_chapters=completed_chapters,
                message="已从 Canon 提交恢复任务进度。",
            )
            from forwin.generation.pipeline_core.result import RunResult

            result = RunResult(
                project_id=str(task.project_id or ""),
                requested_chapters=int(task.requested_chapters or 0),
                completed_chapters=completed_chapters,
                failed_chapters=_task_chapter_numbers(task.failed_chapters_json),
                paused_chapters=_task_chapter_numbers(task.paused_chapters_json),
            )
            try:
                self._completion_handler(task.id, payload)(result)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "Post-recovery completion handler failed for task %s",
                    task.id,
                )
            return
        context = build_execution_context(
            self.infrastructure,
            payload,
            task_id=task.id,
        )
        self.runner(
            task,
            context,
            max(0, int(resume_from_chapter or 0)),
            normalized_worker_id,
            normalized_lease_epoch,
        )

    def _run_claimed(
        self,
        task: GenerationTask,
        context: GenerationExecutionContext,
        resume_from_chapter: int,
        worker_id: str,
        lease_epoch: int,
    ) -> None:
        from forwin.application.generation_execution import execute_continuation

        project_id = str(task.project_id or "").strip()
        payload = payload_from_json(task.execution_payload_json)
        if not project_id or payload.mode != "continue":
            raise PermanentConfigurationError(
                "v5 generation executes project-backed continuation tasks only"
            )
        execute_continuation(
            context,
            project_id,
            self._task_updater(worker_id=worker_id, lease_epoch=lease_epoch),
            logging.getLogger(__name__),
            should_abort=self._task_flag(
                task.id,
                "cancel_requested",
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            ),
            should_pause=self._task_flag(
                task.id,
                "pause_requested",
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            ),
            max_chapters=self._remaining_max_chapters(task, payload),
            resume_from_chapter=resume_from_chapter,
            completion_handler=self._completion_handler(task.id, payload),
            canon_transaction_guard=self._canon_transaction_guard(
                task_id=task.id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            ),
            component="worker",
        )

    def _task_updater(self, *, worker_id: str, lease_epoch: int):
        def update(task_id: str, **changes: object) -> None:
            with self.session_factory.begin() as session:
                task = self._require_task_lease(
                    session,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                )
                normalized = dict(changes)
                if "completed_chapters" in normalized:
                    existing = _task_chapter_numbers(
                        task.completed_chapters_json
                    )
                    incoming = [
                        int(chapter)
                        for chapter in normalized.get("completed_chapters", []) or []
                    ]
                    normalized["completed_chapters"] = list(
                        dict.fromkeys([*existing, *incoming])
                    )
                GenerationTaskRepository(session).update(task_id, normalized)

        return update

    def _recover_committed_chapter(
        self,
        task: GenerationTask,
        *,
        resume_from_chapter: int,
        worker_id: str,
        lease_epoch: int,
    ) -> int:
        chapter_number = max(0, int(resume_from_chapter or 0))
        if chapter_number < 1:
            return chapter_number
        completed = _task_chapter_numbers(task.completed_chapters_json)
        if chapter_number in completed:
            return chapter_number + 1
        with self.session_factory() as session:
            chapter = (
                session.query(ChapterPlan)
                .filter(
                    ChapterPlan.project_id == task.project_id,
                    ChapterPlan.chapter_number == chapter_number,
                )
                .first()
            )
            if chapter is None:
                return chapter_number
            candidate = CandidateDraftRepository(session).latest_for_chapter(
                project_id=str(task.project_id or ""),
                chapter_number=chapter_number,
            )
            candidate_status = str(candidate.status or "") if candidate else ""
            chapter_status = str(chapter.status or "") if chapter else ""
            if candidate is None or candidate_status not in {
                "ready_for_canon",
                "accepted",
            }:
                if chapter_status == "accepted":
                    raise PermanentConfigurationError(
                        "accepted chapter has no accepted v5 candidate"
                    )
                return chapter_number
            if candidate_status == "accepted" and chapter_status != "accepted":
                raise PermanentConfigurationError(
                    "accepted candidate has no accepted chapter"
                )
            try:
                plan = CanonCommitPlan.model_validate_json(
                    candidate.canon_commit_plan_json
                )
            except Exception as exc:  # noqa: BLE001
                raise PermanentConfigurationError(
                    "accepted candidate has no valid Canon commit plan"
                ) from exc

        outcome = CanonAdmissionService(
            session_factory=self.session_factory,
            transaction_guard=self._canon_transaction_guard(
                task_id=task.id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            ),
        ).commit_plan(plan)
        if outcome.blocked:
            raise PermanentConfigurationError(
                "persisted chapter Canon plan could not be recovered: "
                f"{outcome.failure_reason or outcome.blocked_path}"
            )
        if candidate_status == "accepted" and not outcome.idempotent:
            raise PermanentConfigurationError(
                "accepted chapter Canon commit could not be replayed idempotently"
            )
        completed = list(dict.fromkeys([*completed, chapter_number]))
        failed = [
            number
            for number in _task_chapter_numbers(task.failed_chapters_json)
            if number != chapter_number
        ]
        paused = [
            number
            for number in _task_chapter_numbers(task.paused_chapters_json)
            if number != chapter_number
        ]
        self._persist_recovered_chapter(
            task=task,
            chapter_number=chapter_number,
            completed=completed,
            failed=failed,
            paused=paused,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            recovery_kind=(
                "canon_replay" if outcome.idempotent else "canon_commit"
            ),
        )
        task.completed_chapters_json = json.dumps(completed, ensure_ascii=False)
        task.failed_chapters_json = json.dumps(failed, ensure_ascii=False)
        task.paused_chapters_json = json.dumps(paused, ensure_ascii=False)
        task.current_chapter = chapter_number
        return chapter_number + 1

    @staticmethod
    def _remaining_max_chapters(task: GenerationTask, payload) -> int | None:
        configured = int(task.max_chapters or payload.max_chapters or 0)
        if configured <= 0:
            return None
        completed = len(_task_chapter_numbers(task.completed_chapters_json))
        return max(1, configured - completed)

    def _task_flag(
        self,
        task_id: str,
        attribute: str,
        *,
        worker_id: str,
        lease_epoch: int,
    ):
        def read() -> bool:
            with self.session_factory() as session:
                task = session.get(GenerationTask, task_id)
                if task is None:
                    return True
                if str(task.lease_owner or "") != str(worker_id or ""):
                    return True
                if int(task.lease_epoch or 0) != int(lease_epoch):
                    return True
                if _lease_expired(task.lease_expires_at):
                    return True
                return bool(getattr(task, attribute, False))

        return read

    def _persist_recovered_chapter(
        self,
        *,
        task: GenerationTask,
        chapter_number: int,
        completed: list[int],
        failed: list[int],
        paused: list[int],
        worker_id: str,
        lease_epoch: int,
        recovery_kind: str,
    ) -> None:
        with self.session_factory.begin() as session:
            self._require_task_lease(
                session,
                task_id=task.id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            )
            GenerationTaskRepository(session).update(
                task.id,
                {
                    "current_chapter": chapter_number,
                    "completed_chapters": completed,
                    "failed_chapters": failed,
                    "paused_chapters": paused,
                    "message": f"已从 Canon 提交恢复第{chapter_number}章进度。",
                },
            )
            existing = session.execute(
                select(DecisionEvent.id).where(
                    DecisionEvent.project_id == task.project_id,
                    DecisionEvent.chapter_number == chapter_number,
                    DecisionEvent.event_type
                    == DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
                    DecisionEvent.related_object_type == "generation_task",
                    DecisionEvent.related_object_id == task.id,
                )
            ).first()
            if existing is None:
                record_deferred_maintenance(
                    StateUpdater(session),
                    DeferredMaintenanceRecord(
                        project_id=str(task.project_id or ""),
                        task_id=task.id,
                        chapter_number=chapter_number,
                        task_type="post_acceptance_pipeline",
                        reason="generation task reclaimed across Canon boundary",
                        payload={"recovery_kind": recovery_kind},
                        related_object_type="generation_task",
                        related_object_id=task.id,
                    ),
                )

    @staticmethod
    def _require_task_lease(
        session,
        *,
        task_id: str,
        worker_id: str,
        lease_epoch: int,
    ) -> GenerationTask:
        task = session.execute(
            select(GenerationTask)
            .where(GenerationTask.id == task_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            task is None
            or str(task.lease_owner or "") != str(worker_id or "")
            or int(task.lease_epoch or 0) != int(lease_epoch)
            or _lease_expired(task.lease_expires_at)
        ):
            raise GenerationTaskLeaseLost(
                f"generation task lease lost: {task_id}"
            )
        return task

    @staticmethod
    def _canon_transaction_guard(
        *,
        task_id: str,
        worker_id: str,
        lease_epoch: int,
    ):
        def guard(session) -> bool:
            task = session.execute(
                select(GenerationTask)
                .where(GenerationTask.id == task_id)
                .with_for_update()
            ).scalar_one_or_none()
            return bool(
                task is not None
                and str(task.lease_owner or "") == str(worker_id or "")
                and int(task.lease_epoch or 0) == int(lease_epoch)
                and not _lease_expired(task.lease_expires_at)
            )

        return guard

    def _completion_handler(self, task_id: str, payload):
        def handle(result: object) -> None:
            if not payload.auto_continue:
                return
            from forwin.generation.auto_continue import GenerationAutoContinueController

            GenerationAutoContinueController(
                session_factory=self.session_factory,
                create_continue_generation_task=self._enqueue_continue,
            ).after_task_completion(
                result,
                parent_task_id=task_id,
                run_until_chapter=int(payload.run_until_chapter or 0) or None,
                max_chapters=int(payload.max_chapters or 0) or None,
                auto_continue=True,
            )

        return handle

    def _enqueue_continue(self, **values: object) -> str:
        handle = self.enqueue(
            EnqueueGenerationCommand(
                project_id=str(values.get("project_id") or ""),
                requested_chapters=int(values.get("requested_chapters") or 0),
                max_chapters=int(values.get("max_chapters") or 0),
                run_until_chapter=int(values.get("run_until_chapter") or 0),
                auto_continue=bool(values.get("auto_continue", True)),
                title=str(values.get("title") or ""),
                subtitle=str(values.get("subtitle") or ""),
                message=str(values.get("message") or ""),
                root_event_type=DecisionEventType.CONTINUE_REQUESTED,
            )
        )
        return handle.task_id


__all__ = [
    "EnqueueGenerationCommand",
    "GenerationApplicationService",
    "GenerationRunner",
    "GenerationTaskHandle",
]


def _task_chapter_numbers(raw: str) -> list[int]:
    try:
        payload = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    result: list[int] = []
    for item in payload:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def _lease_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized <= datetime.now(timezone.utc)
