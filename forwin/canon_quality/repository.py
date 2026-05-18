from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.canon_quality import (
    ArtifactCollectionLedgerRow,
    CanonAdmissionRunRow,
    CanonQualitySignalRow,
    ChapterBodyMetricRow,
    CharacterStateTransitionRow,
    CountdownLedgerRow,
    RevealRegistryEntryRow,
)
from forwin.models.draft import CandidateDraftRecord

from .signals import (
    ArtifactLedgerEntry,
    CanonAdmissionGateResult,
    CanonQualitySignal,
    ChapterBodyMetrics,
    CharacterStateTransition,
    CountdownLedgerEntry,
    RevealRegistryEntry,
)


class CanonQualityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_signals(self, signals: list[CanonQualitySignal]) -> list[CanonQualitySignalRow]:
        rows: list[CanonQualitySignalRow] = []
        for signal in signals:
            existing = self.session.execute(
                select(CanonQualitySignalRow).where(
                    CanonQualitySignalRow.project_id == signal.project_id,
                    CanonQualitySignalRow.signal_id == signal.signal_id,
                )
            ).scalar_one_or_none()
            row = existing or CanonQualitySignalRow(project_id=signal.project_id, signal_id=signal.signal_id)
            row.chapter_number = int(signal.chapter_number or 0)
            row.draft_id = str(signal.payload.get("draft_id", "") or "")
            row.signal_type = signal.signal_type
            row.severity = signal.severity
            row.target_scope = signal.target_scope
            row.subject_key = signal.subject_key
            row.description = signal.description
            row.evidence_refs_json = _json(signal.evidence_refs)
            row.span_start = signal.span_start
            row.span_end = signal.span_end
            row.payload_json = _json(signal.payload)
            row.status = signal.status
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def supersede_chapter_signals(self, project_id: str, chapter_number: int) -> int:
        rows = self.session.execute(
            select(CanonQualitySignalRow).where(
                CanonQualitySignalRow.project_id == project_id,
                CanonQualitySignalRow.chapter_number == int(chapter_number or 0),
                CanonQualitySignalRow.status == "open",
            )
        ).scalars().all()
        for row in rows:
            row.status = "superseded"
            self.session.add(row)
        self.session.flush()
        return len(rows)

    def list_open_signals(
        self,
        project_id: str,
        *,
        before_chapter: int | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[CanonQualitySignal]:
        query = select(CanonQualitySignalRow).where(
            CanonQualitySignalRow.project_id == project_id,
            CanonQualitySignalRow.status == "open",
        )
        if before_chapter is not None:
            query = query.where(CanonQualitySignalRow.chapter_number < int(before_chapter))
        if severity:
            query = query.where(CanonQualitySignalRow.severity == severity)
        rows = self.session.execute(
            query.order_by(CanonQualitySignalRow.chapter_number.desc(), CanonQualitySignalRow.created_at.desc())
            .limit(max(1, int(limit or 1)))
        ).scalars().all()
        return [_signal_from_row(row) for row in rows]

    def save_admission_run(
        self,
        result: CanonAdmissionGateResult,
        *,
        signals: list[CanonQualitySignal],
    ) -> CanonAdmissionRunRow:
        row = CanonAdmissionRunRow(
            project_id=result.project_id,
            chapter_number=result.chapter_number,
            draft_id=result.draft_id,
            review_id=result.review_id,
            commit_allowed="true" if result.commit_allowed else "false",
            verdict=result.verdict,
            admission_mode=result.admission_mode,
            obligation_ids_json=_json(result.obligation_ids),
            required_plan_patch_ids_json=_json(result.required_plan_patch_ids),
            blocking_reasons_json=_json(result.blocking_reasons),
            expired_obligation_ids_json=_json(result.expired_obligation_ids),
            over_budget="true" if result.over_budget else "false",
            blocking_issue_count=result.blocking_issue_count,
            warning_issue_count=result.warning_issue_count,
            gate_summary=result.gate_summary,
            signals_json=_json([signal.model_dump(mode="json") for signal in signals]),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def save_character_transitions(self, transitions: list[CharacterStateTransition]) -> list[CharacterStateTransitionRow]:
        rows: list[CharacterStateTransitionRow] = []
        for item in transitions:
            row = CharacterStateTransitionRow(
                project_id=item.project_id,
                character_id=item.character_id,
                character_name=item.character_name,
                chapter_number=item.chapter_number,
                transition_type=item.transition_type,
                from_state=item.from_state,
                to_state=item.to_state,
                terminality=item.terminality,
                can_participate="true" if item.can_participate else "false",
                requires_bridge_from_transition_id=item.requires_bridge_from_transition_id,
                bridge_event_id=item.bridge_event_id,
                evidence_refs_json=_json(item.evidence_refs),
                payload_json=_json(item.payload),
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def list_character_transitions(
        self,
        project_id: str,
        *,
        before_chapter: int | None = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        query = select(CharacterStateTransitionRow).where(CharacterStateTransitionRow.project_id == project_id)
        if before_chapter is not None:
            query = query.where(CharacterStateTransitionRow.chapter_number < int(before_chapter))
        rows = self.session.execute(query.order_by(CharacterStateTransitionRow.chapter_number.asc())).scalars().all()
        rows = self._filter_rows_to_committed_drafts(
            rows,
            project_id=project_id,
            before_chapter=before_chapter,
        )
        if not include_superseded:
            rows = [row for row in rows if not _is_superseded_payload(getattr(row, "payload_json", "{}"))]
        return [
            {
                "id": row.id,
                "project_id": row.project_id,
                "character_id": row.character_id,
                "character_name": row.character_name,
                "chapter_number": row.chapter_number,
                "transition_type": row.transition_type,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "terminality": row.terminality,
                "can_participate": row.can_participate == "true",
                "requires_bridge_from_transition_id": row.requires_bridge_from_transition_id,
                "bridge_event_id": row.bridge_event_id,
                "evidence_refs": _loads(row.evidence_refs_json, []),
                "payload": _loads(row.payload_json, {}),
            }
            for row in rows
        ]

    def save_countdown_entries(self, entries: list[CountdownLedgerEntry]) -> list[CountdownLedgerRow]:
        rows: list[CountdownLedgerRow] = []
        for item in entries:
            row = CountdownLedgerRow(
                project_id=item.project_id,
                countdown_key=item.countdown_key,
                label=item.label,
                chapter_number=item.chapter_number,
                normalized_remaining_minutes=item.normalized_remaining_minutes,
                raw_mention=item.raw_mention,
                is_reset_event="true" if item.is_reset_event else "false",
                is_branch_clock="true" if item.is_branch_clock else "false",
                is_resolution_event="true" if item.is_resolution_event else "false",
                previous_remaining_minutes=item.previous_remaining_minutes,
                status=item.status,
                evidence_refs_json=_json(item.evidence_refs),
                payload_json=_json(item.payload),
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def list_countdown_entries(
        self,
        project_id: str,
        *,
        before_chapter: int | None = None,
        include_details: bool = False,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        query = select(CountdownLedgerRow).where(CountdownLedgerRow.project_id == project_id)
        if before_chapter is not None:
            query = query.where(CountdownLedgerRow.chapter_number < int(before_chapter))
        rows = self.session.execute(query.order_by(CountdownLedgerRow.chapter_number.asc())).scalars().all()
        rows = self._filter_rows_to_committed_drafts(
            rows,
            project_id=project_id,
            before_chapter=before_chapter,
        )
        if not include_superseded:
            rows = [row for row in rows if not _is_superseded_payload(getattr(row, "payload_json", "{}"))]
        result: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {
                "countdown_key": row.countdown_key,
                "chapter_number": row.chapter_number,
                "normalized_remaining_minutes": row.normalized_remaining_minutes,
                "status": row.status,
            }
            if include_details:
                item.update(
                    {
                        "label": row.label,
                        "raw_mention": row.raw_mention,
                        "is_reset_event": row.is_reset_event == "true",
                        "is_branch_clock": row.is_branch_clock == "true",
                        "is_resolution_event": row.is_resolution_event == "true",
                        "previous_remaining_minutes": row.previous_remaining_minutes,
                        "payload": _loads(row.payload_json, {}),
                    }
                )
            result.append(item)
        return result

    def _filter_rows_to_committed_drafts(
        self,
        rows: list[Any],
        *,
        project_id: str,
        before_chapter: int | None = None,
    ) -> list[Any]:
        accepted_draft_ids = self._committed_draft_ids_by_chapter(
            project_id,
            before_chapter=before_chapter,
        )
        if not accepted_draft_ids:
            return [
                row
                for row in rows
                if not str(_loads(getattr(row, "payload_json", "{}") or "{}", {}).get("draft_id") or "")
            ]
        filtered: list[Any] = []
        for row in rows:
            chapter_number = int(getattr(row, "chapter_number", 0) or 0)
            accepted_draft_id = accepted_draft_ids.get(chapter_number)
            if not accepted_draft_id:
                continue
            payload = _loads(getattr(row, "payload_json", "{}") or "{}", {})
            if str(payload.get("draft_id") or "") == accepted_draft_id:
                filtered.append(row)
        return filtered

    def _committed_draft_ids_by_chapter(
        self,
        project_id: str,
        *,
        before_chapter: int | None = None,
    ) -> dict[int, str]:
        query = select(CandidateDraftRecord).where(
            CandidateDraftRecord.project_id == project_id,
            CandidateDraftRecord.status == "canon_committed",
            CandidateDraftRecord.canon_status == "canon",
            CandidateDraftRecord.candidate_draft_id != "",
        )
        if before_chapter is not None:
            query = query.where(CandidateDraftRecord.chapter_number < int(before_chapter))
        rows = self.session.execute(
            query.order_by(CandidateDraftRecord.chapter_number.asc(), CandidateDraftRecord.updated_at.asc())
        ).scalars().all()
        result: dict[int, str] = {}
        for row in rows:
            result[int(row.chapter_number or 0)] = str(row.candidate_draft_id or "")
        return result

    def save_artifact_entries(self, entries: list[ArtifactLedgerEntry]) -> list[ArtifactCollectionLedgerRow]:
        rows: list[ArtifactCollectionLedgerRow] = []
        for item in entries:
            row = ArtifactCollectionLedgerRow(
                project_id=item.project_id,
                collection_key=item.collection_key,
                collection_name=item.collection_name,
                target_total=item.target_total,
                chapter_number=item.chapter_number,
                mentioned_index=item.mentioned_index,
                mentioned_remaining=item.mentioned_remaining,
                collected_count_after=item.collected_count_after,
                new_items_json=_json(item.new_items),
                consumed_items_json=_json(item.consumed_items),
                evidence_refs_json=_json(item.evidence_refs),
                confidence=item.confidence,
                status=item.status,
                payload_json=_json(item.payload),
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def save_reveal_entries(self, entries: list[RevealRegistryEntry]) -> list[RevealRegistryEntryRow]:
        rows: list[RevealRegistryEntryRow] = []
        for item in entries:
            row = RevealRegistryEntryRow(
                project_id=item.project_id,
                reveal_key=item.reveal_key,
                claim_summary=item.claim_summary,
                first_revealed_chapter=item.first_revealed_chapter,
                latest_chapter=item.latest_chapter,
                repeat_count=item.repeat_count,
                status=item.status,
                subject_refs_json=_json(item.subject_refs),
                evidence_refs_json=_json(item.evidence_refs),
                payload_json=_json(item.payload),
            )
            self.session.add(row)
            rows.append(row)
        self.session.flush()
        return rows

    def save_body_metrics(self, metrics: ChapterBodyMetrics) -> ChapterBodyMetricRow:
        row = ChapterBodyMetricRow(
            project_id=metrics.project_id,
            chapter_number=metrics.chapter_number,
            draft_id=metrics.draft_id,
            paragraph_hashes_json=_json(metrics.paragraph_hashes),
            dialogue_fingerprints_json=_json(metrics.dialogue_fingerprints),
            scene_fingerprints_json=_json(metrics.scene_fingerprints),
            duplicate_spans_json=_json(metrics.duplicate_spans),
            style_motifs_json=_json(metrics.style_motifs),
            metrics_json=_json(metrics.metrics),
        )
        self.session.add(row)
        self.session.flush()
        return row


def _signal_from_row(row: CanonQualitySignalRow) -> CanonQualitySignal:
    return CanonQualitySignal(
        signal_id=row.signal_id,
        project_id=row.project_id,
        chapter_number=row.chapter_number,
        signal_type=row.signal_type,
        severity=row.severity,  # type: ignore[arg-type]
        target_scope=row.target_scope,  # type: ignore[arg-type]
        subject_key=row.subject_key,
        description=row.description,
        evidence_refs=_loads(row.evidence_refs_json, []),
        span_start=row.span_start,
        span_end=row.span_end,
        payload=_loads(row.payload_json, {}),
        status=row.status,  # type: ignore[arg-type]
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value


def _is_superseded_payload(raw: str) -> bool:
    payload = _loads(raw, {})
    return isinstance(payload, dict) and bool(payload.get("superseded_by"))
