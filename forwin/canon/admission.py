from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy.orm import Session

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.config import InfrastructureConfig
from forwin.governance import DecisionEventType
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.naming import EntityRegistrar
from forwin.orchestrator_loop_core import governance, quality_gates, world_projection
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage.artifacts import ArtifactStore

from .types import CanonAdmissionOutcome
from .entity_admission import EntityAdmissionCommitter


logger = logging.getLogger(__name__)


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
