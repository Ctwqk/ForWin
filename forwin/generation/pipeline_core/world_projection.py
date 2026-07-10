from __future__ import annotations

from forwin.observability.payloads import (
    audit_payload,
    event_error_payload,
)
from forwin.book_state.review_gate_ext import BookStateDirectCommitService
from forwin.canon.preparation import BookStateCanonPreparer
from forwin.governance import DecisionEventType
from forwin.knowledge_system.refresher import KnowledgeProjectionRefresher
from forwin.protocol.review import ReviewVerdict
from forwin.audience.feedback import run_feedback_aggregation_pass
from forwin.simulation.world import (
    save_npc_intents,
    save_world_turn,
)
from forwin.planning.stage_analysis import save_stage_analysis
from sqlalchemy.orm import Session
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.protocol.writer import WriterOutput

@staticmethod
def _prompt_trace_success_summary(writer_output: WriterOutput) -> dict[str, object]:
    generation_meta = getattr(writer_output, "generation_meta", {}) or {}
    prompt_trace = generation_meta.get("prompt_trace") if isinstance(generation_meta, dict) else {}
    attempts = prompt_trace.get("attempts", []) if isinstance(prompt_trace, dict) else []
    if not isinstance(attempts, list):
        attempts = []
    successful = None
    for item in attempts:
        if not isinstance(item, dict):
            continue
        if int(item.get("output_chars") or 0) > 0 and not str(item.get("error_class") or ""):
            successful = item
    if successful is None and attempts:
        successful = next((item for item in reversed(attempts) if isinstance(item, dict)), None)
    if not isinstance(successful, dict):
        return {
            "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
            "effective_model": "",
            "effective_profile_id": "",
            "successful_attempt_no": 0,
            "attempt_group_id": "",
            "output_chars": int(getattr(writer_output, "char_count", 0) or 0),
            "fallback_chain": generation_meta.get("model_fallbacks", []),
        }
    return {
        "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
        "effective_model": str(successful.get("model") or ""),
        "effective_profile_id": str(successful.get("profile_id") or ""),
        "effective_profile_name": str(successful.get("profile_name") or ""),
        "successful_attempt_no": int(successful.get("attempt_no") or 0),
        "attempt_group_id": str(successful.get("attempt_group_id") or ""),
        "output_chars": int(successful.get("output_chars") or getattr(writer_output, "char_count", 0) or 0),
        "fallback_chain": generation_meta.get("model_fallbacks", []),
    }

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
) -> str | None:
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.BOOK_STATE_REVIEW_STARTED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState review gate 开始。",
        payload=audit_payload(
            stage="book_state_review",
            status="started",
            operation_id=self._audit_operation_id(),
            extraction_path="book_state_direct",
        ),
    )
    preparation = BookStateCanonPreparer().prepare(
        runtime=self,
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        writer_output=writer_output,
        verdict=verdict,
    )
    if preparation.blocked or preparation.approved_changes is None:
        extraction = preparation.extraction
        book_state_verdict = preparation.review_verdict
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_REVIEW_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState preparation 未通过。",
            payload=audit_payload(
                stage="book_state_review",
                status="failed",
                operation_id=self._audit_operation_id(),
                blocked_path=preparation.blocked_path,
                issues=(
                    [issue.model_dump(mode="json") for issue in book_state_verdict.issues]
                    if book_state_verdict is not None
                    else [
                        issue.model_dump(mode="json")
                        for issue in (extraction.issues if extraction is not None else [])
                    ]
                ),
                extraction_path="book_state_direct",
            ),
        )
        frozen_path = ""
        if self.policy.canon.hard_floor:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "book-state-review-gate-blocked",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "book_state_review": (
                        book_state_verdict.model_dump(mode="json")
                        if book_state_verdict is not None
                        else {}
                    ),
                    "book_state_extraction": (
                        extraction.model_dump(mode="json")
                        if extraction is not None
                        else {}
                    ),
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState preparation 阻止 canon 写入。",
            payload={"blocked_path": preparation.blocked_path},
        )
        return frozen_path or preparation.blocked_path

    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.BOOK_STATE_REVIEW_SUCCEEDED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState review gate 通过。",
        payload=audit_payload(
            stage="book_state_review",
            status="succeeded",
            operation_id=self._audit_operation_id(),
            issue_count=len(preparation.review_verdict.issues)
            if preparation.review_verdict is not None
            else 0,
            extraction_path="book_state_direct",
        ),
    )

    book_state_changes = preparation.approved_changes
    if not book_state_changes.graph_deltas:
        return None

    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.BOOK_STATE_COMPILE_STARTED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState compile 开始。",
        payload=audit_payload(
            stage="book_state_compile",
            status="started",
            operation_id=self._audit_operation_id(),
            graph_delta_count=len(book_state_changes.graph_deltas),
            extraction_path="book_state_direct",
        ),
    )
    commit_service = BookStateDirectCommitService(session)
    try:
        book_state_result = commit_service.compile_approved(
            book_state_changes,
            compiler_run_id=f"book_state_compile_{project_id}_{chapter_number}",
        )
    except Exception as exc:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_COMPILE_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState compile 异常失败。",
            reason=str(exc),
            payload=event_error_payload(
                exc,
                stage="book_state_compile",
                operation_id=self._audit_operation_id(),
            ),
        )
        raise
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=(
            DecisionEventType.BOOK_STATE_COMPILE_SUCCEEDED
            if book_state_result.committed
            else DecisionEventType.BOOK_STATE_COMPILE_FAILED
        ),
        scope="chapter",
        summary=(
            f"第{chapter_number}章 BookState compile 完成。"
            if book_state_result.committed
            else f"第{chapter_number}章 BookState compile 未提交。"
        ),
        payload=audit_payload(
            stage="book_state_compile",
            status="succeeded" if book_state_result.committed else "failed",
            operation_id=self._audit_operation_id(),
            result=book_state_result.model_dump(mode="json"),
            extraction_path="book_state_direct",
        ),
    )
    if not book_state_result.committed:
        frozen_path = ""
        if self.policy.canon.hard_floor:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "book-state-compile-blocked",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "book_state_review": (
                        preparation.review_verdict.model_dump(mode="json")
                        if preparation.review_verdict is not None
                        else {}
                    ),
                    "book_state_result": book_state_result.model_dump(mode="json"),
                    "book_state_extraction": (
                        preparation.extraction.model_dump(mode="json")
                        if preparation.extraction is not None
                        else {}
                    ),
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_FAILED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState compile 阻止 canon 写入。",
            payload={"book_state_blocked_reasons": list(book_state_result.blocked_reasons)},
        )
        return frozen_path or "book-state-compile-blocked"

    projection_refresh = KnowledgeProjectionRefresher(
        session,
        qdrant_url=self.infrastructure.qdrant_url,
        qdrant_collection=self.infrastructure.llm_kb_qdrant_collection,
    ).refresh(
        project_id,
        as_of_chapter=chapter_number,
        trigger="chapter_accepted",
    )
    self._record_decision_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=DecisionEventType.KNOWLEDGE_PROJECTION_REFRESHED,
        scope="chapter",
        summary=f"第{chapter_number}章 BookState projection refresh 完成。",
        payload=projection_refresh.as_dict(),
    )
    return None

def _run_phase3_pass(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> None:
    stage = self.stage_analyzer.analyze(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    pacing = self.pacing_strategist.analyze(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    save_stage_analysis(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage=stage,
        pacing=pacing,
    )
    self.replan_governor.apply_if_needed(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage=stage,
        pacing=pacing,
    )
    self.arc_envelope_manager.ensure_active_arc_resolution(
        session=session,
        project_id=project_id,
        activation_chapter=chapter_number + 1,
    )
    self.arc_envelope_manager.record_provisional_promotion(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        reason="accepted-into-canon",
    )
    intents = self.npc_intent_generator.generate(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    self._flush_background_llm_trace(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage_key="npc_intents",
        trace_scope="phase4",
    )
    save_npc_intents(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        intents=intents,
    )
    world_turn = self.world_simulator.simulate(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    self._flush_background_llm_trace(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage_key="world_pressure",
        trace_scope="phase4",
    )
    save_world_turn(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        turn=world_turn,
    )
    # Phase B: windowed signal aggregation + cooldown filter
    run_feedback_aggregation_pass(
        session,
        project_id,
        chapter_number,
        cooldown_chapters=3,
        comment_to_reader_ratio=80,
    )



__all__ = ['_prompt_trace_success_summary', '_commit_book_state_canon', '_run_phase3_pass']
