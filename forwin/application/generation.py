from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forwin.application.errors import (
    ActiveGenerationTaskError,
    GenerationTaskLeaseLost,
    PermanentConfigurationError,
    ProjectNotFound,
)
from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
    ensure_decision_event_type,
)
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon.admission import CanonAdmissionService
from forwin.canon.plan import CanonCommitPlan
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
from forwin.maintenance.deferred import (
    DeferredMaintenanceRecord,
    record_deferred_maintenance,
)
from forwin.models.audit import DecisionEvent
from forwin.models.project import ChapterPlan, Project
from forwin.models.task import GenerationTask
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
    long_run_mode: str = "daily_serial"
    isolated: bool = False


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

    def enqueue(
        self,
        command: EnqueueGenerationCommand,
        *,
        session: Session | None = None,
    ) -> GenerationTaskHandle:
        """Enqueue atomically with caller work when a session is supplied."""
        try:
            if session is None:
                with self.session_factory.begin() as managed_session:
                    return self._enqueue_in_session(command, managed_session)
            # A racing active-task constraint must not poison the caller's transaction.
            with session.begin_nested():
                return self._enqueue_in_session(command, session)
        except IntegrityError as exc:
            if "ux_generation_tasks_one_active_per_project" in str(exc):
                raise ActiveGenerationTaskError(
                    f"active generation task exists for project: {command.project_id}"
                ) from exc
            raise

    def _enqueue_in_session(
        self,
        command: EnqueueGenerationCommand,
        session: Session,
        *,
        continuation_parent: GenerationTask | None = None,
    ) -> GenerationTaskHandle:
        project_id = str(command.project_id or "").strip()
        if not project_id:
            raise ProjectNotFound(project_id)
        requested_chapters = max(1, int(command.requested_chapters or 0))
        max_chapters = max(0, int(command.max_chapters or 0))
        run_until_chapter = max(0, int(command.run_until_chapter or 0))
        task_id = new_task_id()
        project = session.scalar(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProjectNotFound(project_id)
        repository = GenerationTaskRepository(session)
        if repository.has_active(project_id):
            raise ActiveGenerationTaskError(
                f"active generation task exists for project: {project_id}"
            )
        from forwin.production.capacity import SerialCapacityService

        if command.long_run_mode not in {"daily_serial", "factory_batch", "soak_test"}:
            raise ValueError("unsupported long_run_mode")
        if command.long_run_mode != "daily_serial" and not command.isolated:
            raise ValueError("offline production mode requires explicit isolated task")
        capacity = SerialCapacityService(session).snapshot(project_id)
        offline = command.long_run_mode != "daily_serial" and command.isolated
        if not offline:
            batch_limit = max(1, capacity.available)
            requested_chapters = min(requested_chapters, batch_limit)
            max_chapters = min(max_chapters or requested_chapters, batch_limit)
        policy_record = ProjectPolicyStore(session).load(project)
        parent_payload = (
            payload_from_json(continuation_parent.execution_payload_json)
            if continuation_parent is not None
            else None
        )
        if parent_payload and parent_payload.policy_version != policy_record.version:
            raise ValueError("continuation policy changed")
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
            long_run_mode=command.long_run_mode,
            isolated=command.isolated,
            capacity_config_version=capacity.config_version,
            policy=parent_payload.policy_snapshot
            if parent_payload
            else policy_record.policy,
            policy_version=parent_payload.policy_version
            if parent_payload
            else policy_record.version,
            root_event_id=root_event.id,
            auto_continue=command.auto_continue,
            run_until_chapter=run_until_chapter,
            max_chapters=max_chapters,
        )
        if parent_payload:
            # Copy the entire frozen run scope; only batch and causal identity change.
            payload = parent_payload.model_copy(
                update={
                    "root_event_id": root_event.id,
                    "max_chapters": max_chapters,
                    "capacity_config_version": capacity.config_version,
                }
            )
            run_until_chapter = parent_payload.run_until_chapter
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
            continuation_parent_task_id=continuation_parent.id
            if continuation_parent
            else None,
        )
        # Persist this task's first intended chapter before it can enter a wait.
        task.resume_from_chapter = capacity.accepted + 1
        if not offline and not capacity.available:
            task.status = "capacity_wait"
            task.current_stage = "capacity_wait"
            task.message = capacity.wait_reason
            session.flush()
        return GenerationTaskHandle(
            task_id=task.id,
            project_id=project_id,
        )

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
        if task.cancel_requested or task.pause_requested:
            from forwin.generation.pipeline_core.result import RunResult

            self.finish_claimed_task(
                task.id,
                RunResult(
                    project_id=task.project_id,
                    requested_chapters=task.requested_chapters,
                    cancelled=bool(task.cancel_requested),
                    paused=bool(task.pause_requested and not task.cancel_requested),
                ),
                worker_id=normalized_worker_id,
                lease_epoch=normalized_lease_epoch,
            )
            return
        from forwin.production.capacity import CapacityWait

        if str(claim_kind or "") in {"expired_running", "capacity_wait"}:
            try:
                resume_from_chapter = self._recover_committed_chapter(
                    task,
                    resume_from_chapter=max(0, int(resume_from_chapter or 0)),
                    worker_id=normalized_worker_id,
                    lease_epoch=normalized_lease_epoch,
                    require_task_provenance=claim_kind == "capacity_wait",
                )
            except CapacityWait as exc:
                self._task_updater(
                    worker_id=normalized_worker_id, lease_epoch=normalized_lease_epoch
                )(
                    task.id,
                    status="capacity_wait",
                    current_stage="capacity_wait",
                    current_chapter=exc.chapter_number or resume_from_chapter,
                    message=exc.reason,
                    error=None,
                )
                return
        completed_chapters = _task_chapter_numbers(task.completed_chapters_json)
        payload = payload_from_json(task.execution_payload_json)
        if int(task.requested_chapters or 0) > 0 and len(completed_chapters) >= int(
            task.requested_chapters or 0
        ):
            from forwin.generation.pipeline_core.result import RunResult

            result = RunResult(
                project_id=str(task.project_id or ""),
                requested_chapters=int(task.requested_chapters or 0),
                completed_chapters=completed_chapters,
                failed_chapters=_task_chapter_numbers(task.failed_chapters_json),
                paused_chapters=_task_chapter_numbers(task.paused_chapters_json),
            )
            self.finish_claimed_task(
                task.id,
                result,
                worker_id=normalized_worker_id,
                lease_epoch=normalized_lease_epoch,
            )
            return
        try:
            self._capacity_reserver(
                task.id, normalized_worker_id, normalized_lease_epoch
            )(task.project_id, max(1, int(resume_from_chapter or 1)))
        except CapacityWait as exc:
            self._task_updater(
                worker_id=normalized_worker_id, lease_epoch=normalized_lease_epoch
            )(
                task.id,
                status="capacity_wait",
                current_stage="capacity_wait",
                current_chapter=exc.chapter_number or resume_from_chapter,
                message=exc.reason,
                error=None,
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
            finish_task=lambda result: self.finish_claimed_task(
                task.id, result, worker_id=worker_id, lease_epoch=lease_epoch
            ),
            canon_transaction_guard=self._canon_transaction_guard(
                task_id=task.id,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            ),
            reserve_chapter=self._capacity_reserver(task.id, worker_id, lease_epoch),
            component="worker",
        )

    def _capacity_reserver(self, task_id: str, worker_id: str, lease_epoch: int):
        def reserve(project_id: str, chapter_number: int) -> None:
            from forwin.production.capacity import SerialCapacityService

            with self.session_factory.begin() as session:
                SerialCapacityService(session).reserve(
                    project_id,
                    chapter_number,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                )

        return reserve

    def _task_updater(self, *, worker_id: str, lease_epoch: int):
        def update(task_id: str, **changes: object) -> None:
            from forwin.generation.task_repository import TERMINAL_GENERATION_STATUSES

            if changes.get("status") in TERMINAL_GENERATION_STATUSES:
                raise ValueError("terminal task status requires atomic finalization")
            with self.session_factory.begin() as session:
                task = self._require_task_lease(
                    session,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                )
                normalized = dict(changes)
                if task.status in TERMINAL_GENERATION_STATUSES:
                    # Post-completion display callbacks cannot alter durable results.
                    normalized = {
                        key: value
                        for key, value in normalized.items()
                        if key == "message"
                    }

                if "completed_chapters" in normalized:
                    existing = _task_chapter_numbers(task.completed_chapters_json)
                    incoming = [
                        int(chapter)
                        for chapter in normalized.get("completed_chapters", []) or []
                    ]
                    normalized["completed_chapters"] = list(
                        dict.fromkeys([*existing, *incoming])
                    )
                if normalized.get("status") == "capacity_wait":
                    from datetime import timedelta

                    task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
                    task.resume_from_chapter = int(
                        normalized.get("current_chapter") or task.current_chapter or 1
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
        require_task_provenance: bool = False,
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
            if require_task_provenance:
                try:
                    provenance = (
                        json.loads(candidate.metadata_json or "{}") if candidate else {}
                    )
                except (TypeError, ValueError):
                    provenance = {}
                # Capacity rechecks recover only work started by this task; a
                # newly enqueued wait must not adopt prior accepted chapters.
                if (
                    not isinstance(provenance, dict)
                    or provenance.get("generation_task_id") != task.id
                ):
                    return chapter_number
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

        if candidate_status != "accepted":
            self._capacity_reserver(task.id, worker_id, lease_epoch)(
                task.project_id, chapter_number
            )
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
            recovery_kind=("canon_replay" if outcome.idempotent else "canon_commit"),
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
            select(GenerationTask).where(GenerationTask.id == task_id).with_for_update()
        ).scalar_one_or_none()
        if (
            task is None
            or str(task.lease_owner or "") != str(worker_id or "")
            or int(task.lease_epoch or 0) != int(lease_epoch)
            or _lease_expired(task.lease_expires_at)
        ):
            raise GenerationTaskLeaseLost(f"generation task lease lost: {task_id}")
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

    def finish_claimed_task(
        self, task_id: str, result, *, worker_id: str, lease_epoch: int
    ) -> None:
        """Fenced final result and continuation intent share one commit."""
        from forwin.generation.continuation_events import (
            GenerationCompletionResult,
            continuation_event_id,
            enqueue_continuation,
        )
        from forwin.models.outbox import OutboxEvent
        from forwin.generation.task_repository import TERMINAL_GENERATION_STATUSES

        frozen = GenerationCompletionResult.from_result(result)
        with self.session_factory.begin() as session:
            project_id = session.scalar(
                select(GenerationTask.project_id).where(GenerationTask.id == task_id)
            )
            session.scalar(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            task = self._require_task_lease(
                session, task_id=task_id, worker_id=worker_id, lease_epoch=lease_epoch
            )
            if frozen.project_id != task.project_id:
                raise ValueError("completion project mismatch")
            if session.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.event_id == continuation_event_id(task_id)
                )
            ):
                return
            if task.status in TERMINAL_GENERATION_STATUSES:
                raise GenerationTaskLeaseLost(
                    f"generation task already terminal: {task_id}"
                )
            completed = list(
                dict.fromkeys(
                    [
                        *_task_chapter_numbers(task.completed_chapters_json),
                        *frozen.completed_chapters,
                    ]
                )
            )
            frozen = frozen.model_copy(update={"completed_chapters": completed})
            changes = dict(
                status=frozen.status,
                current_stage=frozen.status,
                completed_chapters=completed,
                failed_chapters=frozen.failed_chapters,
                paused_chapters=frozen.paused_chapters,
                frozen_artifacts=frozen.frozen_artifacts,
                error=frozen.failure_reason,
            )
            if frozen.status == "capacity_wait":
                from datetime import timedelta

                task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
                task.resume_from_chapter = (
                    frozen.capacity_wait_chapter or task.current_chapter or 1
                )
                changes.update(
                    current_chapter=task.resume_from_chapter,
                    message=frozen.capacity_wait_reason,
                )
            else:
                changes.update(finished_at=datetime.now(UTC))
                if frozen.paused:
                    changes.update(paused_at=datetime.now(UTC))
            GenerationTaskRepository(session).update(task_id, changes)
            if frozen.status in TERMINAL_GENERATION_STATUSES:
                enqueue_continuation(session, task, frozen)


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
        normalized = normalized.replace(tzinfo=UTC)
    return normalized <= datetime.now(UTC)
