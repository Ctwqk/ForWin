from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy.orm import Session

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.governance import DecisionEventType
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage.artifacts import ArtifactStore

from .types import CanonAdmissionOutcome, CanonQualityGateOutcome


logger = logging.getLogger(__name__)


class CanonAdmissionRuntime(Protocol):
    policy: RuntimePolicy
    artifact_store: ArtifactStore

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

    def _apply_canon_quality_gate(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> CanonQualityGateOutcome: ...

    def _commit_book_state_canon(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> str | None: ...

    def _validate_subworld_admission(self, **kwargs: Any) -> None: ...

    def _ensure_genesis_canon_seed_entities(self, **kwargs: Any) -> None: ...

    def _filter_supported_state_changes(self, changes: list[Any]) -> list[Any]: ...

    def _filter_resolvable_state_changes(self, *args: Any) -> list[Any]: ...

    def _ensure_event_mentioned_non_character_entities(self, *args: Any) -> None: ...

    def _filter_resolvable_events(self, *args: Any) -> list[Any]: ...


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
            quality_outcome = runtime._apply_canon_quality_gate(
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
            book_state_blocked_path = runtime._commit_book_state_canon(
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
            runtime._validate_subworld_admission(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
            runtime._ensure_genesis_canon_seed_entities(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
            )
            filtered_state_changes = runtime._filter_supported_state_changes(
                writer_output.state_changes
            )
            filtered_state_changes = runtime._filter_resolvable_state_changes(
                repo,
                project_id,
                chapter_number,
                filtered_state_changes,
            )
            updater.apply_state_changes(
                project_id,
                chapter_number,
                filtered_state_changes,
            )
            runtime._ensure_event_mentioned_non_character_entities(
                repo,
                updater,
                project_id,
                chapter_number,
                writer_output,
            )
            filtered_events = runtime._filter_resolvable_events(
                repo,
                project_id,
                chapter_number,
                writer_output.new_events,
            )
            updater.apply_events(project_id, chapter_number, filtered_events)
            updater.apply_thread_beats(
                project_id,
                chapter_number,
                writer_output.thread_beats,
            )
            if writer_output.time_advance:
                updater.apply_time_advance(
                    project_id,
                    chapter_number,
                    writer_output.time_advance,
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
