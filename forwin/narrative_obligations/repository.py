from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.narrative_obligation import NarrativeObligationRow, NarrativePlanPatchRow

from .types import NarrativeObligation, NarrativePlanPatch


class NarrativeObligationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_obligation(self, obligation: NarrativeObligation) -> NarrativeObligation:
        item = obligation.model_copy(update={"id": obligation.id or new_id()})
        row = NarrativeObligationRow(
            id=item.id,
            project_id=item.project_id,
            origin_chapter_number=item.origin_chapter_number,
            origin_draft_id=item.origin_draft_id,
            origin_review_id=item.origin_review_id,
            origin_signal_ids_json=_json(item.origin_signal_ids),
            origin_plan_snapshot_id=item.origin_plan_snapshot_id,
            obligation_type=item.obligation_type,
            priority=item.priority,
            status=item.status,
            summary=item.summary,
            deferral_reason=item.deferral_reason,
            hardness=item.hardness,
            subject_refs_json=_json(item.subject_refs),
            evidence_refs_json=_json(item.evidence_refs),
            deadline_chapter=item.deadline_chapter,
            deadline_policy=item.deadline_policy,
            payoff_test=item.payoff_test,
            resolution_conditions_json=_json(item.resolution_conditions),
            linked_plan_patch_ids_json=_json(item.linked_plan_patch_ids),
            linked_future_chapters_json=_json(item.linked_future_chapters),
            blocking_policy=item.blocking_policy,
            created_by=item.created_by,
            resolution_chapter=item.resolution_chapter,
            resolution_evidence_refs_json=_json(item.resolution_evidence_refs),
            waive_reason=item.waive_reason,
            metadata_json=_json(item.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return self._obligation_from_row(row)

    def create_plan_patch(self, patch: NarrativePlanPatch) -> NarrativePlanPatch:
        item = patch.model_copy(update={"id": patch.id or new_id()})
        row = NarrativePlanPatchRow(
            id=item.id,
            project_id=item.project_id,
            patch_type=item.patch_type,
            target_scope=item.target_scope,
            target_plan_id=item.target_plan_id,
            target_arc_id=item.target_arc_id,
            target_band_id=item.target_band_id,
            affected_chapters_json=_json(item.affected_chapters),
            source_obligation_ids_json=_json(item.source_obligation_ids),
            source_signal_ids_json=_json(item.source_signal_ids),
            old_plan_digest=item.old_plan_digest,
            new_plan_digest=item.new_plan_digest,
            old_contract_json=_json(item.old_contract),
            new_contract_json=_json(item.new_contract),
            diff_summary=item.diff_summary,
            must_preserve_json=_json(item.must_preserve),
            must_not_change_json=_json(item.must_not_change),
            new_constraints_json=_json(item.new_constraints),
            writer_context_injections_json=_json(item.writer_context_injections),
            reviewer_context_injections_json=_json(item.reviewer_context_injections),
            expected_resolution_tests_json=_json(item.expected_resolution_tests),
            validation_status=item.validation_status,
            validation_errors_json=_json(item.validation_errors),
            applied=item.applied,
            applied_at=datetime.now(UTC) if item.applied else None,
            metadata_json=_json(item.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return self._patch_from_row(row)

    def mark_obligation_planned(
        self,
        obligation_id: str,
        *,
        linked_plan_patch_ids: list[str],
    ) -> NarrativeObligation | None:
        row = self.session.get(NarrativeObligationRow, obligation_id)
        if row is None:
            return None
        row.status = "planned"
        row.linked_plan_patch_ids_json = _json(linked_plan_patch_ids)
        future_chapters: list[int] = []
        for patch_id in linked_plan_patch_ids:
            patch = self.session.get(NarrativePlanPatchRow, patch_id)
            if patch is not None:
                future_chapters.extend(int(chapter) for chapter in _loads(patch.affected_chapters_json, []))
        row.linked_future_chapters_json = _json(sorted(set(future_chapters)))
        self.session.add(row)
        self.session.flush()
        return self._obligation_from_row(row)

    def activate_planned_for_chapter(self, project_id: str, *, origin_chapter_number: int) -> list[NarrativeObligation]:
        rows = self.session.execute(
            select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.origin_chapter_number == int(origin_chapter_number or 0),
                NarrativeObligationRow.status == "planned",
            )
        ).scalars().all()
        for row in rows:
            row.status = "active"
            self.session.add(row)
        self.session.flush()
        return [self._obligation_from_row(row) for row in rows]

    def mark_obligation_resolved(
        self,
        obligation_id: str,
        *,
        verifier_result: dict[str, Any],
        evidence_refs: list[str],
        resolution_chapter: int = 0,
    ) -> NarrativeObligation | None:
        row = self.session.get(NarrativeObligationRow, obligation_id)
        if row is None:
            return None
        row.status = "resolved"
        row.resolved_at = datetime.now(UTC)
        row.resolution_chapter = int(resolution_chapter or row.resolution_chapter or 0)
        row.resolution_evidence_refs_json = _json(evidence_refs)
        metadata = _loads(row.metadata_json, {})
        metadata["verifier_result"] = dict(verifier_result or {})
        row.metadata_json = _json(metadata)
        self.session.add(row)
        self.session.flush()
        return self._obligation_from_row(row)

    def expire_obligation(self, obligation_id: str, *, reason: str) -> NarrativeObligation | None:
        row = self.session.get(NarrativeObligationRow, obligation_id)
        if row is None:
            return None
        row.status = "expired"
        metadata = _loads(row.metadata_json, {})
        metadata["expire_reason"] = str(reason or "")
        row.metadata_json = _json(metadata)
        self.session.add(row)
        self.session.flush()
        return self._obligation_from_row(row)

    def block_expired_obligation(self, obligation_id: str) -> NarrativeObligation | None:
        row = self.session.get(NarrativeObligationRow, obligation_id)
        if row is None:
            return None
        row.status = "blocked"
        metadata = _loads(row.metadata_json, {})
        metadata["blocked_after_expiry"] = True
        row.metadata_json = _json(metadata)
        self.session.add(row)
        self.session.flush()
        return self._obligation_from_row(row)

    def waive_obligation(
        self,
        obligation_id: str,
        *,
        reason: str,
        actor: str,
    ) -> NarrativeObligation | None:
        normalized_actor = str(actor or "").strip()
        if not normalized_actor or normalized_actor == "system":
            raise ValueError("waive_obligation requires a human actor")
        row = self.session.get(NarrativeObligationRow, obligation_id)
        if row is None:
            return None
        row.status = "waived"
        row.waive_reason = str(reason or "").strip()
        metadata = _loads(row.metadata_json, {})
        metadata["waived_by"] = normalized_actor
        row.metadata_json = _json(metadata)
        self.session.add(row)
        self.session.flush()
        return self._obligation_from_row(row)

    def list_active_for_context(self, project_id: str, *, chapter_number: int) -> list[NarrativeObligation]:
        rows = self.session.execute(
            select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.status == "active",
                NarrativeObligationRow.origin_chapter_number < int(chapter_number or 0),
            ).order_by(NarrativeObligationRow.deadline_chapter.asc(), NarrativeObligationRow.priority.asc())
        ).scalars().all()
        result: list[NarrativeObligation] = []
        for row in rows:
            item = self._obligation_from_row(row)
            result.append(
                item.model_copy(
                    update={"must_resolve_now": item.deadline_chapter <= int(chapter_number or 0)}
                )
            )
        return result

    def list_patches_by_ids(self, patch_ids: list[str]) -> list[NarrativePlanPatch]:
        if not patch_ids:
            return []
        rows = self.session.execute(
            select(NarrativePlanPatchRow).where(NarrativePlanPatchRow.id.in_(patch_ids))
        ).scalars().all()
        return [self._patch_from_row(row) for row in rows]

    def list_active_structural_patches(
        self,
        project_id: str,
        *,
        chapter_number: int,
    ) -> list[NarrativePlanPatch]:
        rows = self.session.execute(
            select(NarrativePlanPatchRow)
            .where(
                NarrativePlanPatchRow.project_id == project_id,
                NarrativePlanPatchRow.target_scope.in_(("arc", "book")),
                NarrativePlanPatchRow.applied.is_(True),
            )
            .order_by(NarrativePlanPatchRow.created_at.asc(), NarrativePlanPatchRow.id.asc())
        ).scalars().all()
        current = int(chapter_number or 0)
        result: list[NarrativePlanPatch] = []
        for row in rows:
            patch = self._patch_from_row(row)
            affected = [int(item) for item in patch.affected_chapters if int(item or 0) > 0]
            if affected and current > max(affected):
                continue
            result.append(patch)
        return result

    def list_planned_for_chapter(self, project_id: str, *, origin_chapter_number: int) -> list[NarrativeObligation]:
        rows = self.session.execute(
            select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.origin_chapter_number == int(origin_chapter_number or 0),
                NarrativeObligationRow.status == "planned",
            ).order_by(NarrativeObligationRow.deadline_chapter.asc(), NarrativeObligationRow.priority.asc())
        ).scalars().all()
        return [self._obligation_from_row(row) for row in rows]

    @staticmethod
    def _obligation_from_row(row: NarrativeObligationRow) -> NarrativeObligation:
        return NarrativeObligation(
            id=row.id,
            project_id=row.project_id,
            origin_chapter_number=row.origin_chapter_number,
            origin_draft_id=row.origin_draft_id,
            origin_review_id=row.origin_review_id,
            origin_signal_ids=_loads(row.origin_signal_ids_json, []),
            origin_plan_snapshot_id=row.origin_plan_snapshot_id,
            obligation_type=row.obligation_type,
            priority=row.priority,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            summary=row.summary,
            deferral_reason=row.deferral_reason,
            hardness=row.hardness,  # type: ignore[arg-type]
            subject_refs=_loads(row.subject_refs_json, []),
            evidence_refs=_loads(row.evidence_refs_json, []),
            deadline_chapter=row.deadline_chapter,
            deadline_policy=row.deadline_policy,
            payoff_test=row.payoff_test,
            resolution_conditions=_loads(row.resolution_conditions_json, []),
            linked_plan_patch_ids=_loads(row.linked_plan_patch_ids_json, []),
            linked_future_chapters=[int(item) for item in _loads(row.linked_future_chapters_json, [])],
            blocking_policy=row.blocking_policy,
            created_by=row.created_by,
            resolution_chapter=row.resolution_chapter,
            resolution_evidence_refs=_loads(row.resolution_evidence_refs_json, []),
            waive_reason=row.waive_reason,
            metadata=_loads(row.metadata_json, {}),
        )

    @staticmethod
    def _patch_from_row(row: NarrativePlanPatchRow) -> NarrativePlanPatch:
        return NarrativePlanPatch(
            id=row.id,
            project_id=row.project_id,
            patch_type=row.patch_type,
            target_scope=row.target_scope,  # type: ignore[arg-type]
            target_plan_id=row.target_plan_id,
            target_arc_id=row.target_arc_id,
            target_band_id=row.target_band_id,
            affected_chapters=[int(item) for item in _loads(row.affected_chapters_json, [])],
            source_obligation_ids=_loads(row.source_obligation_ids_json, []),
            source_signal_ids=_loads(row.source_signal_ids_json, []),
            old_plan_digest=row.old_plan_digest,
            new_plan_digest=row.new_plan_digest,
            old_contract=_loads(row.old_contract_json, {}),
            new_contract=_loads(row.new_contract_json, {}),
            diff_summary=row.diff_summary,
            must_preserve=_loads(row.must_preserve_json, []),
            must_not_change=_loads(row.must_not_change_json, []),
            new_constraints=_loads(row.new_constraints_json, []),
            writer_context_injections=_loads(row.writer_context_injections_json, []),
            reviewer_context_injections=_loads(row.reviewer_context_injections_json, []),
            expected_resolution_tests=_loads(row.expected_resolution_tests_json, []),
            validation_status=row.validation_status,  # type: ignore[arg-type]
            validation_errors=_loads(row.validation_errors_json, []),
            applied=bool(row.applied),
            metadata=_loads(row.metadata_json, {}),
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value if value is not None else default
