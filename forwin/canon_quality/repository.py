from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.canon_quality import (
    CanonAdmissionRunRow,
    CanonQualitySignalRow,
    QualityAnalysisRunRow,
)

from .signals import CanonAdmissionGateResult, CanonQualitySignal


class CanonQualityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_quality_analysis_run(
        self,
        *,
        project_id: str,
        chapter_number: int,
        content_hash: str,
        plan_fingerprint: str,
        analysis_mode: str,
        analyzer_fingerprint: str,
    ) -> QualityAnalysisRunRow | None:
        return self.session.execute(
            select(QualityAnalysisRunRow).where(
                QualityAnalysisRunRow.project_id == project_id,
                QualityAnalysisRunRow.chapter_number == int(chapter_number or 0),
                QualityAnalysisRunRow.content_hash == content_hash,
                QualityAnalysisRunRow.plan_fingerprint == plan_fingerprint,
                QualityAnalysisRunRow.analysis_mode == analysis_mode,
                QualityAnalysisRunRow.analyzer_fingerprint == analyzer_fingerprint,
            )
        ).scalar_one_or_none()

    def save_quality_analysis_run(
        self,
        *,
        project_id: str,
        chapter_number: int,
        content_hash: str,
        plan_fingerprint: str,
        analysis_mode: str,
        analyzer_fingerprint: str,
        result: dict[str, Any],
    ) -> QualityAnalysisRunRow:
        row = self.find_quality_analysis_run(
            project_id=project_id,
            chapter_number=chapter_number,
            content_hash=content_hash,
            plan_fingerprint=plan_fingerprint,
            analysis_mode=analysis_mode,
            analyzer_fingerprint=analyzer_fingerprint,
        ) or QualityAnalysisRunRow(
            project_id=project_id,
            chapter_number=int(chapter_number or 0),
            content_hash=content_hash,
            plan_fingerprint=plan_fingerprint,
            analysis_mode=analysis_mode,
            analyzer_fingerprint=analyzer_fingerprint,
        )
        row.result_json = _json(result)
        self.session.add(row)
        self.session.flush()
        return row

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
