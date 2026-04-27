from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.adapter import BookStateDeltaAdapter
from forwin.book_state.compiler import BookStateCompiler as BookStateCompilerV46
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.models.event import CanonEvent
from forwin.models.world_v4 import WorldCompileRunV4Row
from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    WorldCompileRequest,
    WorldCompileResult,
)
from forwin.reviewer_v4.types import V4ReviewGateVerdict
from forwin.world_model_v4.projection import WorldModelProjection
from forwin.world_model_v4.repository import WorldModelRepository


def _dump(value: object) -> str:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    return json.dumps(payload, ensure_ascii=False)


class WorldModelCompiler:
    """The sole v4 canon writer."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = WorldModelRepository(session)

    def compile(self, request: WorldCompileRequest) -> WorldCompileResult:
        changes = request.approved_changes
        world_delta_ids: list[str] = []
        belief_ids: list[str] = []
        knowledge_gap_ids: list[str] = []
        reveal_event_ids: list[str] = []
        knowledge_update_event_ids: list[str] = []
        reader_experience_delta_ids: list[str] = []
        derived_canon_event_ids: list[str] = []

        for world_delta in changes.world_deltas:
            world_delta = world_delta.model_copy(update={"project_id": request.project_id})
            self.repo.append_world_delta(world_delta)
            world_delta_ids.append(world_delta.delta_id)
            if world_delta.delta_kind.value in {"offscreen", "hint", "knowledge", "reveal"}:
                canon_event = CanonEvent(
                    project_id=request.project_id,
                    chapter_number=request.chapter_number,
                    summary=f"[v4:{world_delta.delta_kind.value}] {world_delta.summary}",
                    significance="background"
                    if world_delta.delta_kind.value in {"offscreen", "hint"}
                    else "minor",
                )
                self.session.add(canon_event)
                self.session.flush()
                derived_canon_event_ids.append(canon_event.id)

        for belief in changes.belief_updates:
            self.repo.append_belief(belief, project_id=request.project_id)
            belief_ids.append(belief.belief_id)

        for gap in changes.knowledge_gap_updates:
            gap = gap.model_copy(update={"project_id": request.project_id})
            self.repo.create_or_update_gap(gap)
            knowledge_gap_ids.append(gap.gap_id)

        for reveal_event in changes.reveal_events:
            reveal_event = reveal_event.model_copy(update={"project_id": request.project_id})
            self.repo.append_reveal_event(reveal_event)
            reveal_event_ids.append(reveal_event.reveal_event_id)

        for knowledge_update_event in changes.knowledge_update_events:
            knowledge_update_event = knowledge_update_event.model_copy(
                update={"project_id": request.project_id}
            )
            self.repo.append_knowledge_update(knowledge_update_event)
            knowledge_update_event_ids.append(knowledge_update_event.update_event_id)

        for reader_experience_delta in changes.reader_experience_deltas:
            reader_experience_delta = reader_experience_delta.model_copy(
                update={"project_id": request.project_id}
            )
            self.repo.append_reader_experience_delta(reader_experience_delta)
            reader_experience_delta_ids.append(
                reader_experience_delta.reader_experience_delta_id
            )

        snapshot = WorldModelProjection(self.session).rebuild_snapshot(
            request.project_id,
            as_of_chapter=request.chapter_number,
        )
        result = WorldCompileResult(
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            compiler_run_id=request.compiler_run_id
            or f"compile_{request.project_id}_{request.chapter_number}",
            committed=True,
            world_delta_ids=world_delta_ids,
            belief_ids=belief_ids,
            knowledge_gap_ids=knowledge_gap_ids,
            reveal_event_ids=reveal_event_ids,
            knowledge_update_event_ids=knowledge_update_event_ids,
            reader_experience_delta_ids=reader_experience_delta_ids,
            snapshot_id=snapshot.snapshot_id,
            derived_canon_event_ids=derived_canon_event_ids,
            derived_entity_state_ids=list(
                snapshot.metadata.get("derived_entity_state_ids", [])
            ),
        )
        book_state_sync = self._sync_book_state(request)
        if book_state_sync:
            result = result.model_copy(update={"metadata": {**result.metadata, "book_state_sync": book_state_sync}})
        self._record_compile_run(
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            compiler_run_id=result.compiler_run_id,
            review_verdict_id=request.review_verdict_id,
            committed=True,
            input_payload=request,
            result_payload=result,
            blocked_reasons=[],
            forced_accept_reason=request.forced_accept_reason
            or changes.forced_accept_reason,
        )
        self.session.flush()
        return result

    def _sync_book_state(self, request: WorldCompileRequest) -> dict[str, object]:
        changes = BookStateDeltaAdapter().from_world_change_set(
            request.approved_changes,
            approved_by=["v4_compiler"],
            review_verdict_id=request.review_verdict_id,
            forced_accept_reason=request.forced_accept_reason,
        )
        if not changes.graph_deltas:
            return {"committed": True, "graph_delta_ids": []}
        verdict = BookStateReviewGate(self.session).review(changes)
        if not verdict.accepted or verdict.approved_changes is None:
            return {
                "committed": False,
                "blocked_stage": "review_gate",
                "issues": [issue.model_dump(mode="json") for issue in verdict.issues],
            }
        result = BookStateCompilerV46(self.session).compile(
            verdict.approved_changes,
            compiler_run_id=f"book_state_compile_{request.project_id}_{request.chapter_number}",
        )
        return result.model_dump(mode="json")

    def compile_gate_verdict(
        self,
        *,
        project_id: str,
        chapter_number: int,
        verdict: V4ReviewGateVerdict,
        compiler_run_id: str = "",
        review_verdict_id: str = "",
        retrieval_pack_payload: dict[str, object] | None = None,
    ) -> WorldCompileResult:
        if verdict.passed and isinstance(verdict.approved_changes, ApprovedWorldChangeSet):
            result = self.compile(
                WorldCompileRequest(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    approved_changes=verdict.approved_changes,
                    compiler_run_id=compiler_run_id,
                    review_verdict_id=review_verdict_id,
                )
            )
            if retrieval_pack_payload:
                row = self.session.execute(
                    select(WorldCompileRunV4Row)
                    .where(
                        WorldCompileRunV4Row.project_id == project_id,
                        WorldCompileRunV4Row.compiler_run_id == result.compiler_run_id,
                    )
                    .order_by(WorldCompileRunV4Row.created_at.desc(), WorldCompileRunV4Row.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if row is not None:
                    row.retrieval_pack_json = json.dumps(
                        retrieval_pack_payload,
                        ensure_ascii=False,
                    )
                    self.session.flush()
            return result

        blocked_reasons = [
            f"{issue.failure_type}: {issue.message}"
            for issue in verdict.issues
            if issue.severity == "fail"
        ]
        result = WorldCompileResult(
            project_id=project_id,
            chapter_number=chapter_number,
            compiler_run_id=compiler_run_id or f"compile_blocked_{project_id}_{chapter_number}",
            committed=False,
            blocked_reasons=blocked_reasons,
        )
        self._record_compile_run(
            project_id=project_id,
            chapter_number=chapter_number,
            compiler_run_id=result.compiler_run_id,
            review_verdict_id=review_verdict_id,
            committed=False,
            input_payload=verdict,
            result_payload=result,
            blocked_reasons=blocked_reasons,
            forced_accept_reason="",
            retrieval_pack_payload=retrieval_pack_payload,
        )
        self.session.flush()
        return result

    def _record_compile_run(
        self,
        *,
        project_id: str,
        chapter_number: int,
        compiler_run_id: str,
        review_verdict_id: str,
        committed: bool,
        input_payload: object,
        result_payload: WorldCompileResult,
        blocked_reasons: list[str],
        forced_accept_reason: str,
        retrieval_pack_payload: dict[str, object] | None = None,
    ) -> WorldCompileRunV4Row:
        row = WorldCompileRunV4Row(
            project_id=project_id,
            compiler_run_id=compiler_run_id,
            chapter_number=chapter_number,
            review_verdict_id=review_verdict_id,
            committed=committed,
            forced_accept_reason=forced_accept_reason,
            input_json=_dump(input_payload),
            result_json=_dump(result_payload),
            retrieval_pack_json=json.dumps(
                retrieval_pack_payload or {},
                ensure_ascii=False,
            ),
            projection_refresh_json=json.dumps(
                {
                    "snapshot_id": result_payload.snapshot_id,
                    "derived_entity_state_ids": result_payload.derived_entity_state_ids,
                },
                ensure_ascii=False,
            ),
            blocked_reasons_json=json.dumps(blocked_reasons, ensure_ascii=False),
        )
        self.session.add(row)
        self.session.flush()
        return row
