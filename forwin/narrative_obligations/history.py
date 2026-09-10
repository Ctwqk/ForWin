"""Touched-row before images for obligation lifecycle mutations.

The current row points at a journal head; parent links preserve earlier branches
when a private candidate replica rewinds and validates a different suffix.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import DateTime, select
from sqlalchemy.orm.attributes import flag_modified

from forwin.models.audit import DecisionEvent
from forwin.models.base import new_id
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ChapterPlan, Project

EVENT = "canon_obligation_before_image"
PROVENANCE = "_canon_obligation_provenance"


class ObligationProvenanceUnknown(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def image(row):
    return {
        column.name: (
            value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
        ).isoformat()
        if isinstance(value, datetime)
        else value
        for column in row.__table__.columns
        for value in (getattr(row, column.name),)
    }


def assign_image(row, snapshot):
    """Install exact proven values without firing an ORM on-update default."""
    for column in row.__table__.columns:
        value = snapshot[column.name]
        if isinstance(column.type, DateTime) and value is not None:
            value = datetime.fromisoformat(value)
        setattr(row, column.name, value)
        # Assigning an unchanged value normally omits it from UPDATE. An
        # onupdate timestamp would then overwrite the historical image when
        # another column changes, even if the saved timestamp was identical.
        if column.onupdate is not None:
            flag_modified(row, column.name)


def _provenance(snapshot):
    if snapshot is None:
        return {}
    metadata = json.loads(snapshot.get("metadata_json") or "{}")
    return metadata.get(PROVENANCE, {})


def before_mutation(session, row):
    # Repository callers lock the row before this read, serializing both state
    # updates and the journal head without relying on timestamp ordering.
    before = image(row)
    head = _provenance(before).get("head_event_id")
    if head:
        event = session.get(DecisionEvent, head)
        if (
            event is None
            or event.event_type != EVENT
            or json.loads(event.payload_json).get("after") != before
        ):
            raise ObligationProvenanceUnknown(
                "obligation changed outside its proven after-image"
            )
    return before


def record_mutation(
    session,
    row,
    *,
    operation,
    before,
    chapter_number=0,
    acceptance_id="",
    actor="system",
    installation_previous_live_image=None,
):
    parent = _provenance(before)
    event_id = new_id()
    origin_acceptance = (
        acceptance_id
        if operation == "activate" and acceptance_id
        else parent.get("origin_acceptance_id", "")
    )
    metadata = json.loads(row.metadata_json or "{}")
    metadata[PROVENANCE] = {
        "head_event_id": event_id,
        "origin_acceptance_id": origin_acceptance,
    }
    row.metadata_json = _json(metadata)
    session.add(row)
    session.flush()
    effect_chapter, effect_acceptance, book_revision = _effect(
        session, row.project_id, chapter_number, acceptance_id
    )
    after = image(row)
    payload = {
        "version": 1,
        "project_id": row.project_id,
        "obligation_id": row.id,
        "operation": operation,
        "origin_acceptance_id": origin_acceptance,
        "effect_chapter": effect_chapter,
        "effect_acceptance_id": effect_acceptance,
        "book_revision": book_revision,
        "before": before,
        "after": after,
        "before_sha256": _digest(before),
        "after_sha256": _digest(after),
    }
    if installation_previous_live_image is not None:
        payload["installation_previous_live_image"] = installation_previous_live_image
    session.add(
        DecisionEvent(
            id=event_id,
            project_id=row.project_id,
            chapter_number=effect_chapter,
            event_family="business_event",
            event_type=EVENT,
            actor_type="human" if actor != "system" else "system",
            actor_id=actor,
            related_object_type="narrative_obligation",
            related_object_id=row.id,
            parent_event_id=parent.get("head_event_id", ""),
            scope="chapter",
            summary=f"Obligation {operation} before/after evidence",
            payload_json=_json(payload),
        )
    )
    session.flush()


def _effect(session, project_id, chapter_number, acceptance_id):
    project = session.get(Project, project_id)
    if not chapter_number:
        head = session.scalar(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.active_commit_id.is_not(None),
            )
            .order_by(ChapterPlan.chapter_number.desc())
            .limit(1)
        )
        chapter_number = head.chapter_number if head else 0
        acceptance_id = acceptance_id or (head.active_commit_id if head else "")
    elif not acceptance_id:
        from .candidate import current_acceptance

        acceptance_id = current_acceptance(session, project_id, chapter_number) or ""
    return (
        int(chapter_number),
        str(acceptance_id or ""),
        int(project.book_revision if project else 0),
    )


def _chain(session, row):
    current = image(row)
    chain = []
    seen = set()
    while head := _provenance(current).get("head_event_id"):
        if head in seen:
            raise ObligationProvenanceUnknown("cyclic obligation before-image history")
        seen.add(head)
        event = session.get(DecisionEvent, head)
        if (
            event is None
            or event.event_type != EVENT
            or event.project_id != row.project_id
            or event.related_object_id != row.id
        ):
            raise ObligationProvenanceUnknown(
                "obligation before-image history is missing"
            )
        payload = json.loads(event.payload_json)
        if (
            payload.get("version") != 1
            or payload.get("project_id") != row.project_id
            or payload.get("obligation_id") != row.id
            or payload.get("after") != current
        ):
            raise ObligationProvenanceUnknown(
                "obligation changed outside its proven after-image"
            )
        if payload.get("before_sha256") != _digest(
            payload.get("before")
        ) or payload.get("after_sha256") != _digest(payload.get("after")):
            raise ObligationProvenanceUnknown(
                "obligation before-image fingerprint mismatch"
            )
        before = payload["before"]
        if event.parent_event_id != _provenance(before).get("head_event_id", ""):
            raise ObligationProvenanceUnknown("obligation before-image parent mismatch")
        chain.append(payload)
        current = before
    return chain


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
        if len(records) != len(active_commit_ids) or len(records) != len(active):
            raise ObligationProvenanceUnknown(
                "obligation active acceptance manifest is ambiguous"
            )
    for number, acceptance_id in active.items():
        record = session.get(CanonCommitRecord, acceptance_id)
        if record is None or (record.project_id, record.chapter_number) != (
            project_id,
            number,
        ):
            raise ObligationProvenanceUnknown(
                "obligation acceptance manifest ownership mismatch"
            )
    return active


def restore_prefix(session, *, project_id, active_commit_ids, from_chapter):
    """Restore only lifecycle state; retain every original row and journal event."""
    active = _active_mapping(session, project_id, active_commit_ids)
    pending = []
    original_rows = list(
        session.scalars(
            select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id
            )
        )
    )
    original_images = {row.id: image(row) for row in original_rows}
    for row in original_rows:
        origin = _provenance(image(row)).get("origin_acceptance_id")
        if not origin:
            if row.status in {"proposed", "planned"}:
                continue  # Pre-Canon debt never became an accepted obligation.
            raise ObligationProvenanceUnknown(
                f"obligation origin provenance missing: {row.id}"
            )
        if active.get(row.origin_chapter_number) != origin:
            continue  # Historical row from an inactive acceptance stays preserved.
        commit = session.get(CanonCommitRecord, origin)
        candidate = (
            session.get(CandidateDraftRecord, commit.candidate_id) if commit else None
        )
        if candidate is None or candidate.candidate_draft_id != row.origin_draft_id:
            raise ObligationProvenanceUnknown(
                "obligation origin acceptance/draft mismatch"
            )
        chain = _chain(session, row)
        activation = next(
            (
                event
                for event in chain
                if event["operation"] == "activate"
                and event["origin_acceptance_id"] == origin
                and event["effect_acceptance_id"] == origin
            ),
            None,
        )
        if activation is None:
            raise ObligationProvenanceUnknown(
                "obligation acceptance activation history missing"
            )
        state = image(row)
        for event in chain:
            if event["operation"] not in {"create", "planned"} and event[
                "effect_acceptance_id"
            ] != active.get(event["effect_chapter"]):
                raise ObligationProvenanceUnknown(
                    "obligation mutation acceptance provenance changed"
                )
            if row.origin_chapter_number >= from_chapter:
                state = event["before"]
                if event is activation:
                    break
            elif event["effect_chapter"] >= from_chapter:
                state = event["before"]
            else:
                break
        if state is None:
            raise ObligationProvenanceUnknown(
                "obligation has no recoverable before-image"
            )
        pending.append((row, state))
    from .candidate import CONTEXT, OVERLAYS

    session.info.setdefault(CONTEXT, {})[project_id] = {
        "base_rows": original_images,
        "restored_rows": {row.id: state for row, state in pending},
        "active": active,
        "from_chapter": from_chapter,
    }
    overlays = session.info.setdefault(OVERLAYS, {})
    for key in [key for key in overlays if key[0] == project_id]:
        del overlays[key]
    # Validate the whole touched set before changing any state in the replica.
    for row, state in pending:
        assign_image(row, state)
        session.add(row)
    session.flush()


def active_origin(session, row):
    origin = _provenance(image(row)).get("origin_acceptance_id")
    if not origin:
        return True  # Ordinary legacy read; strict restore_prefix rejects it.
    from .candidate import current_acceptance

    current = current_acceptance(session, row.project_id, row.origin_chapter_number)
    return current == origin
