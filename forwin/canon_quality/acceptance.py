"""Immutable quality projections selected by acceptance identity, not draft recency."""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import select

from forwin.models.canon import CanonCommitRecord
from forwin.models.canon_quality import (
    CanonAdmissionRunRow,
    CanonQualityAcceptanceEvidenceRow,
)
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan

from .signals import CanonQualitySignal, CharacterStateTransition, CountdownLedgerEntry

PENDING = "canon_quality_pending_projection"
PENDING_TX = "canon_quality_pending_projection_transaction"
OVERLAYS = "canon_quality_candidate_projections"
PREFIX = "canon_quality_proven_prefix"


class QualityProvenanceUnknown(ValueError):
    pass


def encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def record_projection(session, payload, source_row_ids):
    analysis = payload.analysis
    draft = session.get(ChapterDraft, analysis.draft_id) if analysis.draft_id else None
    if draft is None:
        session.info.pop(PENDING, None)
        return
    plan = session.get(ChapterPlan, draft.chapter_plan_id)
    if plan is None or (plan.project_id, plan.chapter_number) != (
        analysis.project_id,
        analysis.chapter_number,
    ):
        raise QualityProvenanceUnknown("quality projection draft ownership mismatch")
    session.info[PENDING_TX] = (
        session.get_nested_transaction() or session.get_transaction()
    )
    session.info[PENDING] = {
        "version": 1,
        "complete": True,
        "project_id": analysis.project_id,
        "chapter_number": analysis.chapter_number,
        "draft_id": analysis.draft_id,
        "body_sha256": hashlib.sha256(draft.body_text.encode()).hexdigest(),
        "source_row_ids": source_row_ids,
        "character_transitions": [
            row.model_dump(mode="json") for row in payload.character_transitions
        ],
        "countdown_entries": [
            row.model_dump(mode="json") for row in payload.countdown_entries
        ],
        "signals": [row.model_dump(mode="json") for row in analysis.signals],
    }


def take_projection(session, result, signals):
    snapshot = session.info.pop(PENDING, None)
    transaction = session.info.pop(PENDING_TX, None)
    if not snapshot or transaction is not (
        session.get_nested_transaction() or session.get_transaction()
    ):
        return None
    if (snapshot["project_id"], snapshot["chapter_number"], snapshot["draft_id"]) != (
        result.project_id,
        result.chapter_number,
        result.draft_id,
    ):
        raise QualityProvenanceUnknown(
            "quality admission does not own its touched projection"
        )
    snapshot["signals"] = [signal.model_dump(mode="json") for signal in signals]
    return snapshot


def _identity(session, project_id, chapter_number, draft_id, acceptance_id):
    commit = session.get(CanonCommitRecord, acceptance_id)
    candidate = (
        session.get(CandidateDraftRecord, commit.candidate_id) if commit else None
    )
    draft = session.get(ChapterDraft, draft_id)
    if (
        commit is None
        or candidate is None
        or draft is None
        or (
            commit.project_id != project_id
            or commit.chapter_number != chapter_number
            or candidate.project_id != project_id
            or candidate.chapter_number != chapter_number
            or candidate.candidate_draft_id != draft_id
            or draft.chapter_plan_id != commit.chapter_plan_id
            or candidate.chapter_plan_id != commit.chapter_plan_id
        )
    ):
        raise QualityProvenanceUnknown(
            "quality evidence acceptance/draft ownership mismatch"
        )
    body_hash = hashlib.sha256(draft.body_text.encode()).hexdigest()
    if candidate.body_hash != body_hash:
        raise QualityProvenanceUnknown("quality evidence accepted body hash mismatch")
    return body_hash


def _validate_snapshot(snapshot, *, project_id, chapter_number, draft_id, body_hash):
    if (
        snapshot.get("version") != 1
        or snapshot.get("complete") is not True
        or (
            snapshot.get("project_id"),
            snapshot.get("chapter_number"),
            snapshot.get("draft_id"),
            snapshot.get("body_sha256"),
        )
        != (project_id, chapter_number, draft_id, body_hash)
    ):
        raise QualityProvenanceUnknown(
            "quality snapshot identity or completeness mismatch"
        )
    for key, model in (
        ("character_transitions", CharacterStateTransition),
        ("countdown_entries", CountdownLedgerEntry),
        ("signals", CanonQualitySignal),
    ):
        if not isinstance(snapshot.get(key), list):
            raise QualityProvenanceUnknown(f"quality snapshot missing {key}")
        for raw in snapshot[key]:
            item = model.model_validate(raw)
            if (
                item.project_id != project_id
                or item.chapter_number != chapter_number
                or str(item.payload.get("draft_id") or "") != draft_id
            ):
                raise QualityProvenanceUnknown(
                    f"quality snapshot {key} row ownership mismatch"
                )


def _from_historical(
    project_id,
    chapter_number,
    draft_id,
    body_hash,
    historical_form,
    *,
    require_checks,
    body_length=0,
):
    data = (
        historical_form.model_dump(mode="json")
        if hasattr(historical_form, "model_dump")
        else dict(historical_form)
    )
    review = data.get("review", data)
    if require_checks:
        checks = data.get("checks", [])
        dimensions = {
            "possession",
            "knowledge",
            "life_state",
            "time",
            "place",
            "obligations",
        }
        if (
            len(checks) != 6
            or {check.get("dimension") for check in checks} != dimensions
            or any(
                check.get("status") != "pass"
                or not any(
                    (match := re.fullmatch(rf"body:{body_hash}#(\d+):(\d+)", ref))
                    and 0 <= int(match[1]) < int(match[2]) <= body_length
                    for ref in check.get("evidence_refs", [])
                )
                for check in checks
            )
        ):
            raise QualityProvenanceUnknown(
                "historical quality evidence requires complete grounded checks"
            )
    if review.get("blocking") or (
        review.get("project_id"),
        review.get("chapter_number"),
        review.get("draft_id"),
    ) != (project_id, chapter_number, draft_id):
        raise QualityProvenanceUnknown(
            "historical quality form ownership or decision mismatch"
        )
    snapshot = {
        "version": 1,
        "complete": True,
        "project_id": project_id,
        "chapter_number": chapter_number,
        "draft_id": draft_id,
        "body_sha256": body_hash,
        "source_row_ids": {},
        **{
            key: review.get(key)
            for key in ("character_transitions", "countdown_entries", "signals")
        },
    }
    _validate_snapshot(
        snapshot,
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body_hash=body_hash,
    )
    return snapshot


def bind_acceptance(
    session,
    *,
    project_id,
    chapter_number,
    draft_id,
    acceptance_id,
    quality_admission_run_id="",
    historical_form=None,
):
    if not quality_admission_run_id and historical_form is None:
        # Old/manual accepted plans remain readable, but gain no fabricated proof.
        return None
    body_hash = _identity(session, project_id, chapter_number, draft_id, acceptance_id)
    if historical_form is not None:
        snapshot = _from_historical(
            project_id,
            chapter_number,
            draft_id,
            body_hash,
            historical_form,
            require_checks=True,
            body_length=len(session.get(ChapterDraft, draft_id).body_text),
        )
        source_id = None
    else:
        run = session.get(CanonAdmissionRunRow, quality_admission_run_id)
        if (
            run is None
            or not run.projection_json
            or run.commit_allowed != "true"
            or (run.project_id, run.chapter_number, run.draft_id)
            != (project_id, chapter_number, draft_id)
        ):
            raise QualityProvenanceUnknown(
                "quality admission snapshot provenance is missing"
            )
        snapshot = json.loads(run.projection_json)
        if digest(snapshot) != run.projection_fingerprint:
            raise QualityProvenanceUnknown("quality snapshot fingerprint mismatch")
        source_id = run.id
    _validate_snapshot(
        snapshot,
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body_hash=body_hash,
    )
    evidence = encoded({"snapshot": snapshot, "sha256": digest(snapshot)})
    existing = session.get(CanonQualityAcceptanceEvidenceRow, acceptance_id)
    if existing is not None:
        if (
            existing.evidence_json != evidence
            or existing.source_admission_run_id != source_id
        ):
            raise QualityProvenanceUnknown(
                "quality acceptance evidence is immutable; different projection supplied"
            )
        return existing
    row = CanonQualityAcceptanceEvidenceRow(
        acceptance_id=acceptance_id,
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=draft_id,
        body_sha256=body_hash,
        source_admission_run_id=source_id,
        evidence_json=evidence,
    )
    session.add(row)
    session.flush()
    return row


def _load_evidence(session, acceptance_id, project_id, chapter_number):
    row = session.get(CanonQualityAcceptanceEvidenceRow, acceptance_id)
    if row is None:
        return None
    body_hash = _identity(
        session, project_id, chapter_number, row.draft_id, acceptance_id
    )
    envelope = json.loads(row.evidence_json)
    snapshot = envelope.get("snapshot", {})
    if (
        row.project_id != project_id
        or row.chapter_number != chapter_number
        or row.body_sha256 != body_hash
        or digest(snapshot) != envelope.get("sha256")
    ):
        raise QualityProvenanceUnknown(
            "quality acceptance evidence fingerprint or ownership mismatch"
        )
    _validate_snapshot(
        snapshot,
        project_id=project_id,
        chapter_number=chapter_number,
        draft_id=row.draft_id,
        body_hash=body_hash,
    )
    return snapshot


def is_candidate_context(session, project_id):
    return project_id in session.info.get(PREFIX, {})


def active_projections(session, project_id, before_chapter=None):
    active = session.info.get(PREFIX, {}).get(project_id)
    if active is None:
        active = {
            chapter.chapter_number: chapter.active_commit_id
            for chapter in session.scalars(
                select(ChapterPlan).where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.active_commit_id.is_not(None),
                )
            )
        }
    result = {}
    for number, acceptance_id in active.items():
        if before_chapter is None or number < before_chapter:
            snapshot = _load_evidence(session, acceptance_id, project_id, number)
            if snapshot is not None:
                result[number] = snapshot
    result.update(
        {
            number: data
            for (book, number), data in session.info.get(OVERLAYS, {}).items()
            if book == project_id
            and (before_chapter is None or number < before_chapter)
        }
    )
    return result


def restore_prefix(session, *, project_id, active_commit_ids, from_chapter):
    active = _active_mapping(session, project_id, active_commit_ids)
    prefix = {
        number: acceptance_id
        for number, acceptance_id in active.items()
        if number < from_chapter
    }
    for number, acceptance_id in prefix.items():
        if _load_evidence(session, acceptance_id, project_id, number) is None:
            raise QualityProvenanceUnknown(
                f"quality prefix provenance missing for acceptance {acceptance_id}"
            )
    session.info.setdefault(PREFIX, {})[project_id] = prefix
    overlays = session.info.setdefault(OVERLAYS, {})
    for key in [key for key in overlays if key[0] == project_id]:
        del overlays[key]


def _active_mapping(session, project_id, active_commit_ids):
    if isinstance(active_commit_ids, dict):
        active = dict(active_commit_ids)
    else:
        records = list(
            session.scalars(
                select(CanonCommitRecord).where(
                    CanonCommitRecord.id.in_(active_commit_ids)
                )
            )
        )
        active = {record.chapter_number: record.id for record in records}
        if len(records) != len(active_commit_ids) or len(active) != len(records):
            raise QualityProvenanceUnknown(
                "active quality acceptance manifest is ambiguous"
            )
    for number, acceptance_id in active.items():
        commit = session.get(CanonCommitRecord, acceptance_id)
        if commit is None or (commit.project_id, commit.chapter_number) != (
            project_id,
            number,
        ):
            raise QualityProvenanceUnknown(
                "active quality acceptance manifest ownership mismatch"
            )
    return active


def activate_candidate_projection(
    session,
    *,
    project_id,
    chapter_number,
    draft_id,
    body_sha256,
    historical_form,
    projection_id,
):
    if not projection_id:
        raise QualityProvenanceUnknown(
            "candidate quality projection requires validation identity"
        )
    draft = session.get(ChapterDraft, draft_id)
    if (
        draft is None
        or hashlib.sha256(draft.body_text.encode()).hexdigest() != body_sha256
    ):
        raise QualityProvenanceUnknown("candidate quality body identity mismatch")
    chapter = session.get(ChapterPlan, draft.chapter_plan_id)
    if chapter is None or (chapter.project_id, chapter.chapter_number) != (
        project_id,
        chapter_number,
    ):
        raise QualityProvenanceUnknown("candidate quality draft ownership mismatch")
    snapshot = _from_historical(
        project_id,
        chapter_number,
        draft_id,
        body_sha256,
        historical_form,
        require_checks=False,
    )
    session.info.setdefault(OVERLAYS, {})[(project_id, chapter_number)] = snapshot


def projected_rows(rows, snapshots, section):
    result = [row for row in rows if row.chapter_number not in snapshots]
    for number, snapshot in sorted(snapshots.items()):
        for index, raw in enumerate(snapshot[section]):
            values = dict(raw)
            values["id"] = (
                values.get("id") or f"quality:{snapshot['draft_id']}:{section}:{index}"
            )
            values["payload_json"] = encoded(values.get("payload", {}))
            values["evidence_refs_json"] = encoded(values.get("evidence_refs", []))
            for key in (
                "can_participate",
                "is_reset_event",
                "is_branch_clock",
                "is_resolution_event",
            ):
                if key in values:
                    values[key] = "true" if values[key] else "false"
            result.append(SimpleNamespace(**values))
    return sorted(result, key=lambda row: row.chapter_number)
