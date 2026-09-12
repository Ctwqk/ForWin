from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forwin.naming import EntityAdmissionPlan
from forwin.narrative_obligations.resolution_evidence import ObligationResolutionPlan
from forwin.protocol.book_state import ApprovedGraphDeltaSet

from .outbox_events import (
    CanonOutboxEvent,
    CanonPublisherBindingSnapshot,
    CanonPublisherEventPayload,
    build_canon_recovery_events,
    canon_commit_id,
    parse_canon_event_payload,
)


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
    expected_book_revision: int = 0
    revision_validation_id: str = ""
    quality_admission_run_id: str = ""
    obligation_resolution_plan: ObligationResolutionPlan | None = None
    approved_book_state_changes: ApprovedGraphDeltaSet
    entity_admission_plan: EntityAdmissionPlan
    acceptance_mode: str = "normal"
    repair_attempt_count: int = 0
    residual_review_issues: tuple[dict[str, Any], ...] = ()
    canon_risk_level: str = ""
    chapter_title: str
    publisher_bindings: tuple[CanonPublisherBindingSnapshot, ...] = ()
    audit_events: tuple[CanonAuditEvent, ...] = ()
    outbox_events: tuple[CanonOutboxEvent, ...]
    canon_commit_id: str
    idempotency_key: str

    @model_validator(mode="after")
    def _validate_recovery_events(self) -> Self:
        expected_commit_id = canon_commit_id(self.idempotency_key)
        if self.canon_commit_id != expected_commit_id:
            raise ValueError("Canon commit ID does not match the idempotency key")
        expected_events = build_canon_recovery_events(
            canon_idempotency_key=self.idempotency_key,
            canon_commit_id_value=self.canon_commit_id,
            project_id=self.project_id,
            chapter_number=self.chapter_number,
            candidate_id=self.candidate_id,
            chapter_title=self.chapter_title,
            body_sha256=self.candidate_body_hash,
            publisher_bindings=self.publisher_bindings,
        )
        if self.outbox_events != expected_events:
            raise ValueError("Canon recovery event plan is not canonical")
        return self

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
        expected_book_revision: int = 0,
        revision_validation_id: str = "",
        quality_admission_run_id: str = "",
        obligation_resolution_plan: ObligationResolutionPlan | None = None,
        approved_book_state_changes: ApprovedGraphDeltaSet,
        entity_admission_plan: EntityAdmissionPlan,
        acceptance_mode: str = "normal",
        repair_attempt_count: int = 0,
        residual_review_issues: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        canon_risk_level: str = "",
        chapter_title: str,
        publisher_bindings: Sequence[
            Mapping[str, Any] | CanonPublisherBindingSnapshot
        ] = (),
        audit_events: tuple[CanonAuditEvent, ...] = (),
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
            expected_book_revision=expected_book_revision,
            revision_validation_id=revision_validation_id,
            quality_admission_run_id=quality_admission_run_id,
            obligation_resolution_plan=obligation_resolution_plan,
        )
        normalized_commit_id = canon_commit_id(idempotency_key)
        normalized_outbox = build_canon_recovery_events(
            canon_idempotency_key=idempotency_key,
            canon_commit_id_value=normalized_commit_id,
            project_id=normalized_project_id,
            chapter_number=normalized_chapter,
            candidate_id=str(candidate_id or "").strip(),
            chapter_title=str(chapter_title or "").strip(),
            body_sha256=str(candidate_body_hash or "").strip(),
            publisher_bindings=publisher_bindings,
        )
        publisher_payload = parse_canon_event_payload(
            normalized_outbox[-1].event_type,
            normalized_outbox[-1].payload,
        )
        if not isinstance(publisher_payload, CanonPublisherEventPayload):
            raise TypeError("Canon publisher event payload normalization failed")
        return cls(
            schema_version=schema_version,
            expected_book_revision=max(0, int(expected_book_revision)),
            revision_validation_id=revision_validation_id,
            quality_admission_run_id=quality_admission_run_id,
            obligation_resolution_plan=obligation_resolution_plan,
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
            residual_review_issues=tuple(dict(item) for item in residual_review_issues),
            canon_risk_level=str(canon_risk_level or ""),
            chapter_title=publisher_payload.chapter_title,
            publisher_bindings=publisher_payload.publisher_bindings,
            audit_events=tuple(audit_events),
            outbox_events=tuple(normalized_outbox),
            canon_commit_id=normalized_commit_id,
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
    expected_book_revision: int = 0,
    revision_validation_id: str = "",
    quality_admission_run_id: str = "",
    obligation_resolution_plan: ObligationResolutionPlan | None = None,
) -> str:
    payload = {
        "schema_version": "v1",
        "expected_book_revision": max(0, int(expected_book_revision)),
        "project_id": str(project_id or ""),
        "chapter_number": int(chapter_number or 0),
        "candidate_id": str(candidate_id or ""),
        "candidate_body_hash": str(candidate_body_hash or ""),
        "plan_revision": str(plan_revision or ""),
        "policy_version": max(1, int(policy_version or 1)),
        "graph_delta_ids": [
            str(delta.id or "") for delta in approved_book_state_changes.graph_deltas
        ],
        "entity_plan_fingerprint": str(
            entity_admission_plan.candidate_fingerprint or ""
        ),
    }
    if revision_validation_id:
        payload["revision_validation_id"] = revision_validation_id
    if quality_admission_run_id:
        payload["quality_admission_run_id"] = quality_admission_run_id
    if obligation_resolution_plan is not None:
        payload["obligation_resolution_plan"] = obligation_resolution_plan.model_dump(mode="json")
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
]
