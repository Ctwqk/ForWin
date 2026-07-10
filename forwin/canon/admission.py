from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from forwin.book_state.compiler import BookStateCompiler
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_body_hash,
    candidate_plan_revision,
)
from forwin.config import InfrastructureConfig
from forwin.governance import DecisionEventType
from forwin.models.base import new_id
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.governance import DecisionEvent
from forwin.models.project import ChapterPlan, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.naming import EntityRegistrar
from forwin.generation.pipeline_core import quality_gates, world_projection
from forwin.outbox.store import enqueue_outbox_event
from forwin.protocol.book_state import BookStateCompileResult
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage.artifacts import ArtifactStore

from .entity_admission import EntityAdmissionCommitter
from .plan import CanonCommitPlan
from .types import CanonAdmissionOutcome


logger = logging.getLogger(__name__)


class CanonStaleVersion(RuntimeError):
    """The prepared plan no longer matches locked authoritative state."""


class CanonWriteFailure(RuntimeError):
    """An authoritative Canon write could not be completed."""


class CanonAdmissionRuntime(Protocol):
    policy: RuntimePolicy
    infrastructure: InfrastructureConfig
    artifact_store: ArtifactStore
    llm_client: Any

    def _record_decision_event(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        event_family: str,
        event_type: str,
        scope: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any: ...

    def _record_rule_decision_event(self, **kwargs: Any) -> Any: ...

    def _audit_operation_id(self) -> str: ...


class CanonAdmissionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.session_factory = session_factory

    def commit_plan(
        self,
        plan: CanonCommitPlan,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> CanonAdmissionOutcome:
        if self.session_factory is None:
            raise RuntimeError("CanonAdmissionService requires a session factory")
        inject = failure_injector or _ignore_failure_stage
        try:
            with self.session_factory.begin() as session:
                project = session.execute(
                    select(Project)
                    .where(Project.id == plan.project_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if project is None:
                    raise CanonStaleVersion("project no longer exists")

                prior = session.execute(
                    select(CanonCommitRecord)
                    .where(
                        CanonCommitRecord.idempotency_key
                        == plan.idempotency_key
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if prior is not None:
                    return _outcome_from_record(prior, idempotent=True)

                chapter = session.execute(
                    select(ChapterPlan)
                    .where(
                        ChapterPlan.project_id == plan.project_id,
                        ChapterPlan.chapter_number == plan.chapter_number,
                    )
                    .with_for_update()
                ).scalars().first()
                candidate = session.execute(
                    select(CandidateDraftRecord)
                    .where(CandidateDraftRecord.id == plan.candidate_id)
                    .with_for_update()
                ).scalar_one_or_none()
                self._revalidate_locked_plan(
                    session=session,
                    project=project,
                    chapter=chapter,
                    candidate=candidate,
                    plan=plan,
                )

                commit_id = new_id()
                candidate_repository = CandidateDraftRepository(session)
                candidate_repository.transition(plan.candidate_id, "committing")

                compile_result = BookStateCompiler(session).compile(
                    plan.approved_book_state_changes,
                    compiler_run_id=f"canon-commit-{commit_id}",
                )
                if not compile_result.committed:
                    raise CanonWriteFailure(
                        "; ".join(compile_result.blocked_reasons)
                        or "BookState compiler rejected the approved changes"
                    )
                if compile_result.metadata.get("idempotent"):
                    raise CanonStaleVersion(
                        "BookState deltas already exist without a Canon commit record"
                    )
                session.flush()
                inject("book_state")

                EntityAdmissionCommitter(session).apply(
                    project_id=plan.project_id,
                    plan=plan.entity_admission_plan,
                )
                session.flush()
                inject("entity")

                NarrativeObligationRepository(
                    session
                ).activate_planned_for_chapter(
                    plan.project_id,
                    origin_chapter_number=plan.chapter_number,
                )
                session.flush()
                inject("obligation")

                assert chapter is not None
                chapter.status = "accepted"
                chapter.acceptance_mode = plan.acceptance_mode
                chapter.repair_attempt_count = plan.repair_attempt_count
                chapter.residual_review_issues_json = json.dumps(
                    list(plan.residual_review_issues),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                chapter.canon_risk_level = plan.canon_risk_level
                session.add(chapter)
                candidate_repository.transition(
                    plan.candidate_id,
                    "accepted",
                    canon_commit_id=commit_id,
                )
                session.flush()
                inject("chapter")

                for event in plan.audit_events:
                    session.add(
                        DecisionEvent(
                            id=new_id(),
                            project_id=plan.project_id,
                            chapter_number=plan.chapter_number,
                            scope=event.scope,
                            event_family=event.event_family,
                            event_type=event.event_type,
                            actor_type="system",
                            summary=event.summary,
                            reason=event.reason,
                            payload_json=json.dumps(
                                event.payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            related_object_type=event.related_object_type,
                            related_object_id=event.related_object_id,
                        )
                    )
                for event in plan.outbox_events:
                    enqueue_outbox_event(
                        session,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=event.aggregate_id,
                        event_type=event.event_type,
                        payload=event.payload,
                        event_id=event.event_id,
                    )
                session.flush()
                inject("outbox")

                result_payload = {
                    "commit_id": commit_id,
                    "compile_result": compile_result.model_dump(mode="json"),
                }
                session.add(
                    CanonCommitRecord(
                        id=commit_id,
                        idempotency_key=plan.idempotency_key,
                        candidate_id=plan.candidate_id,
                        project_id=plan.project_id,
                        chapter_number=plan.chapter_number,
                        expected_previous_accepted_chapter=(
                            plan.expected_previous_accepted_chapter
                        ),
                        expected_book_state_chapter=(
                            plan.expected_book_state_chapter
                        ),
                        graph_delta_ids_json=json.dumps(
                            compile_result.graph_delta_ids,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        world_snapshot_id=compile_result.world_snapshot_id,
                        map_snapshot_id=compile_result.map_snapshot_id,
                        status="committed",
                        result_json=json.dumps(
                            result_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
                session.flush()
                return CanonAdmissionOutcome(
                    commit_id=commit_id,
                    compile_result=compile_result,
                )
        except CanonStaleVersion as exc:
            self._return_candidate_to_ready(plan.candidate_id)
            return CanonAdmissionOutcome(
                blocked_path=str(exc),
                block_kind="stale_canon_plan",
                stale=True,
                failure_reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Atomic Canon commit failed for chapter %d.",
                plan.chapter_number,
            )
            self._mark_candidate_failed(plan.candidate_id, str(exc))
            return CanonAdmissionOutcome(
                blocked_path=str(exc),
                block_kind="canon_write_failed",
                failure_reason=str(exc),
            )

    def _revalidate_locked_plan(
        self,
        *,
        session: Session,
        project: Project,
        chapter: ChapterPlan | None,
        candidate: CandidateDraftRecord | None,
        plan: CanonCommitPlan,
    ) -> None:
        if chapter is None:
            raise CanonStaleVersion("chapter plan no longer exists")
        if candidate is None:
            raise CanonStaleVersion("candidate no longer exists")
        if candidate.project_id != plan.project_id:
            raise CanonStaleVersion("candidate project changed")
        if int(candidate.chapter_number or 0) != plan.chapter_number:
            raise CanonStaleVersion("candidate chapter changed")
        if candidate.status != "ready_for_canon":
            raise CanonStaleVersion(
                f"candidate is {candidate.status}, expected ready_for_canon"
            )
        if candidate.idempotency_key != plan.idempotency_key:
            raise CanonStaleVersion("candidate idempotency key changed")
        try:
            persisted_plan = CanonCommitPlan.model_validate_json(
                candidate.canon_commit_plan_json
            )
        except Exception as exc:  # noqa: BLE001
            raise CanonStaleVersion("candidate Canon plan is invalid") from exc
        if persisted_plan != plan:
            raise CanonStaleVersion("candidate Canon plan changed")

        draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.id == candidate.candidate_draft_id)
            .with_for_update()
        ).scalar_one_or_none()
        if draft is None:
            raise CanonStaleVersion("candidate draft no longer exists")
        current_body_hash = candidate_body_hash(draft.body_text)
        if (
            current_body_hash != plan.candidate_body_hash
            or candidate.body_hash != plan.candidate_body_hash
        ):
            raise CanonStaleVersion("candidate body changed after preparation")
        current_plan_revision = candidate_plan_revision(chapter)
        if (
            current_plan_revision != plan.plan_revision
            or candidate.plan_revision != plan.plan_revision
        ):
            raise CanonStaleVersion("chapter plan changed after preparation")
        if (
            int(project.runtime_policy_version or 0) != plan.policy_version
            or int(candidate.policy_version or 0) != plan.policy_version
        ):
            raise CanonStaleVersion("runtime policy changed after preparation")

        previous_accepted_chapter = int(
            session.scalar(
                select(func.max(ChapterPlan.chapter_number)).where(
                    ChapterPlan.project_id == plan.project_id,
                    ChapterPlan.status == "accepted",
                    ChapterPlan.chapter_number < plan.chapter_number,
                )
            )
            or 0
        )
        if (
            previous_accepted_chapter
            != plan.expected_previous_accepted_chapter
        ):
            raise CanonStaleVersion("accepted chapter version changed")
        book_state_chapter = int(
            session.scalar(
                select(func.max(GraphDeltaRow.chapter_number)).where(
                    GraphDeltaRow.project_id == plan.project_id,
                    GraphDeltaRow.chapter_number < plan.chapter_number,
                )
            )
            or 0
        )
        if book_state_chapter != plan.expected_book_state_chapter:
            raise CanonStaleVersion("BookState version changed")
        current_chapter_delta_count = int(
            session.scalar(
                select(func.count(GraphDeltaRow.id)).where(
                    GraphDeltaRow.project_id == plan.project_id,
                    GraphDeltaRow.chapter_number == plan.chapter_number,
                )
            )
            or 0
        )
        if current_chapter_delta_count:
            raise CanonStaleVersion("BookState already contains this chapter")

    def _return_candidate_to_ready(self, candidate_id: str) -> None:
        if self.session_factory is None:
            return
        try:
            with self.session_factory.begin() as session:
                repository = CandidateDraftRepository(session)
                candidate = repository.get(candidate_id, for_update=True)
                if candidate is not None and candidate.status == "committing":
                    repository.transition(candidate.id, "ready_for_canon")
        except Exception:  # noqa: BLE001
            logger.exception("Could not restore stale candidate %s.", candidate_id)

    def _mark_candidate_failed(
        self,
        candidate_id: str,
        failure_reason: str,
    ) -> None:
        if self.session_factory is None:
            return
        try:
            with self.session_factory.begin() as session:
                repository = CandidateDraftRepository(session)
                candidate = repository.get(candidate_id, for_update=True)
                if candidate is None or candidate.status in {"accepted", "failed"}:
                    return
                if candidate.status == "committing":
                    candidate = repository.transition(
                        candidate.id,
                        "ready_for_canon",
                    )
                repository.transition(
                    candidate.id,
                    "failed",
                    failure_reason=failure_reason,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not mark candidate %s failed.", candidate_id)

    def commit(
        self,
        *,
        runtime: CanonAdmissionRuntime,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> CanonAdmissionOutcome:
        runtime._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_STARTED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon 写入开始。",
            payload={
                "state_changes_count": len(writer_output.state_changes),
                "events_count": len(writer_output.new_events),
                "thread_beats_count": len(writer_output.thread_beats),
            },
        )
        try:
            quality_outcome = quality_gates._apply_canon_quality_gate(
                runtime,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
            if quality_outcome.blocked:
                return CanonAdmissionOutcome(
                    blocked_path=quality_outcome.blocked_path,
                    block_kind="canon_quality",
                    canon_gate_result=quality_outcome.gate_result,
                )
            entity_admission_plan = EntityRegistrar(
                session=session
            ).verify_writer_output_admission(
                project_id=project_id,
                writer_output=writer_output,
            )
            book_state_blocked_path = world_projection._commit_book_state_canon(
                runtime,
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
            if book_state_blocked_path:
                return CanonAdmissionOutcome(
                    blocked_path=book_state_blocked_path,
                    block_kind="book_state",
                )
            EntityAdmissionCommitter(session).apply(
                project_id=project_id,
                plan=entity_admission_plan,
            )
            runtime._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="business_event",
                event_type=DecisionEventType.CANON_COMMIT,
                scope="chapter",
                summary=f"第{chapter_number}章 canon 写入成功。",
                payload={"issue_count": len(verdict.issues)},
            )
            NarrativeObligationRepository(session).activate_planned_for_chapter(
                project_id,
                origin_chapter_number=chapter_number,
            )
            CandidateDraftRepository(session).mark_canon_committed(
                project_id=project_id,
                chapter_number=chapter_number,
            )
            return CanonAdmissionOutcome()
        except Exception as exc:
            logger.exception(
                "Canon update failed for chapter %d; keeping saved draft and review.",
                chapter_number,
            )
            frozen_path = ""
            if runtime.policy.canon.hard_floor:
                frozen_path = runtime.artifact_store.save_frozen_candidate(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    payload={
                        "reason": "canon-update-failed",
                        "chapter_number": chapter_number,
                        "writer_output": writer_output.model_dump(mode="json"),
                        "review_verdict": verdict.model_dump(mode="json"),
                    },
                )
            runtime._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CANON_COMMIT_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 canon 写入失败。",
            )
            session.rollback()
            CandidateDraftRepository(session).mark_canon_failed(
                project_id=project_id,
                chapter_number=chapter_number,
                failure_reason=str(exc),
                canon_artifact_path=frozen_path,
            )
            return CanonAdmissionOutcome(
                blocked_path=frozen_path,
                block_kind="canon_admission_error",
            )


def _ignore_failure_stage(_stage: str) -> None:
    return None


def _outcome_from_record(
    record: CanonCommitRecord,
    *,
    idempotent: bool,
) -> CanonAdmissionOutcome:
    try:
        payload = json.loads(record.result_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    compile_payload = payload.get("compile_result")
    compile_result = (
        BookStateCompileResult.model_validate(compile_payload)
        if isinstance(compile_payload, dict)
        else None
    )
    return CanonAdmissionOutcome(
        commit_id=record.id,
        idempotent=idempotent,
        compile_result=compile_result,
    )


__all__ = [
    "CanonAdmissionService",
    "CanonStaleVersion",
    "CanonWriteFailure",
]
