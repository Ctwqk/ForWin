from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from forwin.naming import EntityAdmissionPlan
from forwin.protocol.book_state import ApprovedGraphDeltaSet


class _FrozenCanonModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CanonAuditEvent(_FrozenCanonModel):
    event_type: str
    event_family: str
    summary: str
    scope: str = "chapter"
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    related_object_type: str = ""
    related_object_id: str = ""


class CanonOutboxEvent(_FrozenCanonModel):
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    aggregate_type: str = "project"
    aggregate_id: str = ""
    event_id: str = ""


class CanonCommitPlan(_FrozenCanonModel):
    schema_version: Literal["v1"] = "v1"
    project_id: str
    chapter_number: int
    candidate_id: str
    candidate_body_hash: str
    plan_revision: str
    policy_version: int
    expected_previous_accepted_chapter: int
    expected_book_state_chapter: int
    approved_book_state_changes: ApprovedGraphDeltaSet
    entity_admission_plan: EntityAdmissionPlan
    acceptance_mode: str = "normal"
    repair_attempt_count: int = 0
    residual_review_issues: tuple[dict[str, Any], ...] = ()
    canon_risk_level: str = ""
    audit_events: tuple[CanonAuditEvent, ...] = ()
    outbox_events: tuple[CanonOutboxEvent, ...] = ()
    idempotency_key: str

    @classmethod
    def build(
        cls,
        *,
        project_id: str,
        chapter_number: int,
        candidate_id: str,
        candidate_body_hash: str,
        plan_revision: str,
        policy_version: int,
        expected_previous_accepted_chapter: int,
        expected_book_state_chapter: int,
        approved_book_state_changes: ApprovedGraphDeltaSet,
        entity_admission_plan: EntityAdmissionPlan,
        acceptance_mode: str = "normal",
        repair_attempt_count: int = 0,
        residual_review_issues: list[dict[str, Any]]
        | tuple[dict[str, Any], ...] = (),
        canon_risk_level: str = "",
        audit_events: tuple[CanonAuditEvent, ...] = (),
        outbox_events: tuple[CanonOutboxEvent, ...] = (),
        schema_version: Literal["v1"] = "v1",
    ) -> Self:
        normalized_project_id = str(project_id or "").strip()
        normalized_chapter = int(chapter_number or 0)
        if approved_book_state_changes.project_id != normalized_project_id:
            raise ValueError("BookState project mismatch")
        if int(approved_book_state_changes.chapter_number or 0) != normalized_chapter:
            raise ValueError("BookState chapter mismatch")
        if entity_admission_plan.project_id != normalized_project_id:
            raise ValueError("Entity admission project mismatch")
        if int(entity_admission_plan.chapter_number or 0) != normalized_chapter:
            raise ValueError("Entity admission chapter mismatch")

        idempotency_key = _idempotency_key(
            project_id=normalized_project_id,
            chapter_number=normalized_chapter,
            candidate_id=candidate_id,
            candidate_body_hash=candidate_body_hash,
            plan_revision=plan_revision,
            policy_version=policy_version,
            approved_book_state_changes=approved_book_state_changes,
            entity_admission_plan=entity_admission_plan,
        )
        event_type_counts: dict[str, int] = {}
        normalized_outbox: list[CanonOutboxEvent] = []
        for event in outbox_events:
            event_type = str(event.event_type or "").strip()
            count = event_type_counts.get(event_type, 0)
            event_type_counts[event_type] = count + 1
            suffix = event_type if count == 0 else f"{event_type}:{count}"
            normalized_outbox.append(
                event.model_copy(
                    update={
                        "aggregate_id": event.aggregate_id or normalized_project_id,
                        "event_id": f"{idempotency_key}:{suffix}",
                    }
                )
            )
        return cls(
            schema_version=schema_version,
            project_id=normalized_project_id,
            chapter_number=normalized_chapter,
            candidate_id=str(candidate_id or "").strip(),
            candidate_body_hash=str(candidate_body_hash or "").strip(),
            plan_revision=str(plan_revision or "").strip(),
            policy_version=max(1, int(policy_version or 1)),
            expected_previous_accepted_chapter=max(
                0,
                int(expected_previous_accepted_chapter or 0),
            ),
            expected_book_state_chapter=max(
                0,
                int(expected_book_state_chapter or 0),
            ),
            approved_book_state_changes=approved_book_state_changes,
            entity_admission_plan=entity_admission_plan,
            acceptance_mode=str(acceptance_mode or "normal"),
            repair_attempt_count=max(0, int(repair_attempt_count or 0)),
            residual_review_issues=tuple(
                dict(item) for item in residual_review_issues
            ),
            canon_risk_level=str(canon_risk_level or ""),
            audit_events=tuple(audit_events),
            outbox_events=tuple(normalized_outbox),
            idempotency_key=idempotency_key,
        )


def _idempotency_key(
    *,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
    candidate_body_hash: str,
    plan_revision: str,
    policy_version: int,
    approved_book_state_changes: ApprovedGraphDeltaSet,
    entity_admission_plan: EntityAdmissionPlan,
) -> str:
    payload = {
        "schema_version": "v1",
        "project_id": str(project_id or ""),
        "chapter_number": int(chapter_number or 0),
        "candidate_id": str(candidate_id or ""),
        "candidate_body_hash": str(candidate_body_hash or ""),
        "plan_revision": str(plan_revision or ""),
        "policy_version": max(1, int(policy_version or 1)),
        "graph_delta_ids": [
            str(delta.id or "")
            for delta in approved_book_state_changes.graph_deltas
        ],
        "entity_plan_fingerprint": str(
            entity_admission_plan.candidate_fingerprint or ""
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "canon-v1-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CanonAuditEvent",
    "CanonCommitPlan",
    "CanonOutboxEvent",
]
