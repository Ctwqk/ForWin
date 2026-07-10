from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.protocol.writer import WriterOutput


LEGAL_CANDIDATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "drafted": frozenset({"reviewing", "needs_review", "failed"}),
    "reviewing": frozenset({"reviewed", "repairing", "needs_review", "failed"}),
    "reviewed": frozenset({"repairing", "ready_for_canon", "needs_review", "failed"}),
    "repairing": frozenset({"reviewed", "ready_for_canon", "needs_review", "failed"}),
    "ready_for_canon": frozenset({"committing", "needs_review", "failed"}),
    "committing": frozenset({"accepted", "ready_for_canon"}),
    "needs_review": frozenset({"reviewing", "repairing", "ready_for_canon", "failed"}),
    "failed": frozenset(),
    "accepted": frozenset(),
}


class CandidateTransitionError(ValueError):
    pass


def candidate_body_hash(body: str) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


def candidate_plan_revision(chapter_plan: ChapterPlan) -> str:
    payload = {
        "chapter_plan_id": str(chapter_plan.id or ""),
        "arc_plan_id": str(chapter_plan.arc_plan_id or ""),
        "chapter_number": int(chapter_plan.chapter_number or 0),
        "title": str(chapter_plan.title or ""),
        "one_line": str(chapter_plan.one_line or ""),
        "goals_json": str(chapter_plan.goals_json or "[]"),
        "task_contract_json": str(chapter_plan.task_contract_json or "[]"),
        "experience_plan_json": str(chapter_plan.experience_plan_json or "{}"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dump_json(value: Any, *, fallback: Any) -> str:
    payload = fallback if value is None else value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _model_list(items: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items or []:
        if hasattr(item, "model_dump"):
            payload.append(item.model_dump(mode="json"))
        elif isinstance(item, Mapping):
            payload.append(dict(item))
        else:
            payload.append({"value": str(item)})
    return payload


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


class CandidateDraftRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, candidate_id: str, *, for_update: bool = False) -> CandidateDraftRecord | None:
        statement = select(CandidateDraftRecord).where(
            CandidateDraftRecord.id == str(candidate_id or "")
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.execute(statement).scalar_one_or_none()

    def latest_for_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
    ) -> CandidateDraftRecord | None:
        return self.session.execute(
            select(CandidateDraftRecord)
            .where(
                CandidateDraftRecord.project_id == project_id,
                CandidateDraftRecord.chapter_number == int(chapter_number),
            )
            .order_by(
                CandidateDraftRecord.version.desc(),
                CandidateDraftRecord.created_at.desc(),
                CandidateDraftRecord.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()

    def create_reviewed_version(
        self,
        *,
        project_id: str,
        chapter_plan: ChapterPlan,
        draft: ChapterDraft,
        review: ChapterReview,
        writer_output: WriterOutput,
        plan_revision: str,
        policy_version: int,
        parent_candidate_id: str = "",
        repair_attempt_count: int = 0,
        repair_history: list[dict[str, Any]] | None = None,
    ) -> CandidateDraftRecord:
        existing = self.session.execute(
            select(CandidateDraftRecord).where(
                CandidateDraftRecord.candidate_draft_id == draft.id
            )
        ).scalar_one_or_none()
        body_hash = candidate_body_hash(draft.body_text)
        if existing is not None:
            immutable = (
                existing.project_id,
                existing.chapter_plan_id,
                int(existing.chapter_number),
                existing.body_hash,
                existing.plan_revision,
                int(existing.policy_version),
            )
            requested = (
                project_id,
                chapter_plan.id,
                int(chapter_plan.chapter_number or writer_output.chapter_number or 0),
                body_hash,
                str(plan_revision or ""),
                max(1, int(policy_version or 1)),
            )
            if immutable != requested:
                raise ValueError("candidate draft already belongs to a different immutable version")
            return existing

        chapter_number = int(
            chapter_plan.chapter_number or writer_output.chapter_number or 0
        )
        latest_version = self.session.execute(
            select(func.max(CandidateDraftRecord.version)).where(
                CandidateDraftRecord.project_id == project_id,
                CandidateDraftRecord.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        version = max(int(latest_version or 0) + 1, int(draft.version or 1))
        generation_meta = _json_object(writer_output.generation_meta)
        entity_plan = _json_object(generation_meta.get("entity_admission_plan"))
        metadata = dict(generation_meta)
        metadata.setdefault("title", writer_output.title)
        metadata.setdefault("summary", writer_output.end_of_chapter_summary)
        metadata.setdefault(
            "char_count",
            int(writer_output.char_count or len(writer_output.body or "")),
        )
        row = CandidateDraftRecord(
            project_id=project_id,
            chapter_plan_id=chapter_plan.id,
            chapter_number=chapter_number,
            candidate_draft_id=draft.id,
            review_id=review.id,
            version=version,
            parent_candidate_id=str(parent_candidate_id or ""),
            body_hash=body_hash,
            plan_revision=str(plan_revision or ""),
            writer_artifact_ref=str(generation_meta.get("artifact_meta_path") or ""),
            review_result_json=str(review.review_meta_json or "{}"),
            repair_history_json=_dump_json(repair_history, fallback=[]),
            entity_admission_plan_json=_dump_json(entity_plan, fallback={}),
            policy_version=max(1, int(policy_version or 1)),
            status="reviewed",
            canon_status="candidate",
            scene_outputs_json=_dump_json(
                _model_list(writer_output.scene_outputs), fallback=[]
            ),
            state_change_candidates_json=_dump_json(
                _model_list(writer_output.state_changes), fallback=[]
            ),
            event_candidates_json=_dump_json(
                _model_list(writer_output.new_events), fallback=[]
            ),
            thread_beat_candidates_json=_dump_json(
                _model_list(writer_output.thread_beats), fallback=[]
            ),
            repair_attempt_count=max(0, int(repair_attempt_count or 0)),
            metadata_json=_dump_json(metadata, fallback={}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def transition(
        self,
        candidate_id: str,
        next_status: str,
        *,
        failure_reason: str = "",
        canon_commit_id: str = "",
    ) -> CandidateDraftRecord:
        row = self.get(candidate_id, for_update=True)
        if row is None:
            raise LookupError("candidate draft not found")
        current = str(row.status or "drafted")
        target = str(next_status or "").strip()
        if target not in LEGAL_CANDIDATE_TRANSITIONS.get(current, frozenset()):
            raise CandidateTransitionError(f"illegal candidate transition: {current} -> {target}")
        row.status = target
        row.canon_status = "canon" if target == "accepted" else "candidate"
        if target == "accepted":
            row.canon_commit_id = str(canon_commit_id or row.canon_commit_id or "")
            row.failure_reason = ""
        elif target in {"failed", "needs_review"}:
            row.failure_reason = str(failure_reason or "")
        self.session.add(row)
        self.session.flush()
        return row

    def attach_canon_plan(
        self,
        candidate_id: str,
        *,
        plan_payload: dict[str, Any],
        eligibility_payload: dict[str, Any],
        idempotency_key: str,
    ) -> CandidateDraftRecord:
        row = self.get(candidate_id, for_update=True)
        if row is None:
            raise LookupError("candidate draft not found")
        row.canon_commit_plan_json = _dump_json(plan_payload, fallback={})
        row.eligibility_decision_json = _dump_json(
            eligibility_payload,
            fallback={},
        )
        row.idempotency_key = str(idempotency_key or "")
        self.session.add(row)
        self.session.flush()
        return row

    # Transitional callers are removed when Canon adopts commit_plan.
    def mark_canon_committed(
        self,
        *,
        project_id: str,
        chapter_number: int,
        canon_artifact_path: str = "",
    ) -> CandidateDraftRecord | None:
        row = self.latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        if row is None:
            return None
        if row.status == "reviewed":
            self.transition(row.id, "ready_for_canon")
        if row.status == "ready_for_canon":
            self.transition(row.id, "committing")
        row = self.transition(row.id, "accepted")
        row.canon_artifact_path = str(canon_artifact_path or "")
        self.session.add(row)
        self.session.flush()
        return row

    def mark_canon_failed(
        self,
        *,
        project_id: str,
        chapter_number: int,
        failure_reason: str,
        canon_artifact_path: str = "",
    ) -> CandidateDraftRecord | None:
        row = self.latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        if row is None:
            return None
        if row.status not in {"failed", "accepted"}:
            row = self.transition(
                row.id,
                "failed",
                failure_reason=failure_reason,
            )
        row.canon_artifact_path = str(canon_artifact_path or "")
        self.session.add(row)
        self.session.flush()
        return row


__all__ = [
    "CandidateDraftRepository",
    "CandidateTransitionError",
    "LEGAL_CANDIDATE_TRANSITIONS",
    "candidate_body_hash",
    "candidate_plan_revision",
]
