"""Explicit private-prefix identity and installation of proven obligation state."""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select

from forwin.models.audit import DecisionEvent
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ChapterPlan

from . import history

CONTEXT = "obligation_candidate_prefix"
OVERLAYS = "obligation_candidate_acceptances"


def set_candidate_acceptance(
    session, *, project_id, chapter_number, acceptance_id, draft_id
):
    draft = session.get(ChapterDraft, draft_id)
    chapter = session.get(ChapterPlan, draft.chapter_plan_id) if draft else None
    if (
        not acceptance_id
        or chapter is None
        or (chapter.project_id, chapter.chapter_number) != (project_id, chapter_number)
    ):
        raise history.ObligationProvenanceUnknown(
            "candidate obligation acceptance/draft ownership mismatch"
        )
    overlay = {
        "acceptance_id": acceptance_id,
        "draft_id": draft_id,
        "body_sha256": hashlib.sha256(draft.body_text.encode()).hexdigest(),
    }
    entries = session.info.setdefault(OVERLAYS, {})
    existing = entries.get((project_id, chapter_number))
    if existing and existing != overlay:
        raise history.ObligationProvenanceUnknown(
            "candidate obligation acceptance identity is immutable"
        )
    entries[(project_id, chapter_number)] = overlay


def current_acceptance(session, project_id, chapter_number):
    overlay = session.info.get(OVERLAYS, {}).get((project_id, chapter_number))
    if overlay:
        return overlay["acceptance_id"]
    context = session.info.get(CONTEXT, {}).get(project_id)
    if context is not None:
        return (
            context["active"].get(chapter_number)
            if chapter_number < context["from_chapter"]
            else None
        )
    return session.scalar(
        select(ChapterPlan.active_commit_id).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == chapter_number,
        )
    )


def validate_activation(
    session, *, project_id, chapter_number, acceptance_id, draft_id
):
    if not acceptance_id:
        return  # Legacy ordinary caller receives no fabricated acceptance proof.
    if not draft_id:
        raise history.ObligationProvenanceUnknown(
            "obligation acceptance requires its exact draft"
        )
    overlay = session.info.get(OVERLAYS, {}).get((project_id, chapter_number))
    if overlay and (overlay["acceptance_id"], overlay["draft_id"]) == (
        acceptance_id,
        draft_id,
    ):
        return
    commit = session.get(CanonCommitRecord, acceptance_id)
    candidate = (
        session.get(CandidateDraftRecord, commit.candidate_id) if commit else None
    )
    if (
        commit is None
        or candidate is None
        or (commit.project_id, commit.chapter_number, candidate.candidate_draft_id)
        != (project_id, chapter_number, draft_id)
    ):
        raise history.ObligationProvenanceUnknown(
            "obligation activation acceptance/draft ownership mismatch"
        )


def apply_reviewed_resolutions(
    session, *, project_id, chapter_number, chapter_body, historical_form
):
    """Apply already-reviewed fulfilled answers; perform no second semantic review."""
    from .repository import NarrativeObligationRepository

    overlay = session.info.get(OVERLAYS, {}).get((project_id, chapter_number))
    body_hash = hashlib.sha256(chapter_body.encode()).hexdigest()
    data = (
        historical_form.model_dump(mode="json")
        if hasattr(historical_form, "model_dump")
        else historical_form
    )
    review = data.get("review", {})
    if (
        overlay is None
        or overlay["body_sha256"] != body_hash
        or (
            review.get("project_id"),
            review.get("chapter_number"),
            review.get("draft_id"),
        )
        != (project_id, chapter_number, overlay["draft_id"])
    ):
        raise history.ObligationProvenanceUnknown(
            "reviewed obligation candidate/body ownership mismatch"
        )
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
        review.get("blocking")
        or review.get("validation_report", {}).get("rejected")
        or len(checks) != 6
        or {check.get("dimension") for check in checks} != dimensions
    ):
        raise history.ObligationProvenanceUnknown(
            "reviewed obligation coverage is incomplete"
        )
    for check in checks:
        spans = [
            re.fullmatch(rf"body:{body_hash}#(\d+):(\d+)", ref)
            for ref in check.get("evidence_refs", [])
        ]
        if check.get("status") != "pass" or not any(
            match and 0 <= int(match[1]) < int(match[2]) <= len(chapter_body)
            for match in spans
        ):
            raise history.ObligationProvenanceUnknown(
                "reviewed obligation body evidence is unknown"
            )
    repo = NarrativeObligationRepository(session)
    active = {
        item.id: item
        for item in repo.list_active_for_context(
            project_id, chapter_number=chapter_number
        )
    }
    asks = review.get("form", {}).get("obligations", [])
    answers = review.get("answers", {}).get("obligations", [])
    if (
        len(asks) != len(active)
        or len(answers) != len(active)
        or {item.get("id") for item in asks} != set(active)
        or {item.get("id") for item in answers} != set(active)
    ):
        raise history.ObligationProvenanceUnknown(
            "reviewed obligation tracked identity coverage changed"
        )
    from .resolution_evidence import apply_resolution_plan, build_resolution_plan

    plan = build_resolution_plan(
        obligations=list(active.values()), form=review.get("form"), answers=review.get("answers"),
        project_id=project_id, chapter_number=chapter_number,
        candidate_id=overlay["acceptance_id"], draft_id=overlay["draft_id"],
        chapter_body=chapter_body, review_source="historical_chapter_review_form",
        validation_report=review.get("validation_report"),
    )
    claimed = {answer["id"] for answer in answers if answer["addressed"].get("value") == "fulfilled"}
    if claimed != set(plan.resolved_obligation_ids):
        raise history.ObligationProvenanceUnknown("reviewed fulfillment needs complete grounded subject/condition evidence quotes")
    return apply_resolution_plan(session, plan)


def _assign(row, snapshot):
    history.assign_image(row, snapshot)


def install_candidate_projection(
    destination, *, source_session, project_id, acceptance_ids
):
    """Verify source history and destination before-images before installing state.

    The caller must first flush the actual new Canon records in its transaction.
    Old branches remain untouched; only missing immutable events are appended.
    """
    from forwin.canon.projection_lock import lock_projection_project

    lock_projection_project(destination, project_id)
    context = source_session.info.get(CONTEXT, {}).get(project_id)
    if context is None:
        raise history.ObligationProvenanceUnknown(
            "candidate obligation prefix was not proven"
        )
    supplied = history._active_mapping(destination, project_id, acceptance_ids)
    overlays = {
        chapter: value
        for (book, chapter), value in source_session.info.get(OVERLAYS, {}).items()
        if book == project_id
    }
    if supplied != {
        chapter: value["acceptance_id"] for chapter, value in overlays.items()
    }:
        raise history.ObligationProvenanceUnknown(
            "candidate obligation acceptance manifest mismatch"
        )
    active = {
        chapter: value
        for chapter, value in context["active"].items()
        if chapter < context["from_chapter"]
    }
    active.update(supplied)
    for chapter, overlay in overlays.items():
        validate_activation(
            destination,
            project_id=project_id,
            chapter_number=chapter,
            acceptance_id=overlay["acceptance_id"],
            draft_id=overlay["draft_id"],
        )
        draft = destination.get(ChapterDraft, overlay["draft_id"])
        commit = destination.get(CanonCommitRecord, overlay["acceptance_id"])
        candidate = destination.get(CandidateDraftRecord, commit.candidate_id)
        if (
            draft is None
            or hashlib.sha256(draft.body_text.encode()).hexdigest()
            != overlay["body_sha256"]
            or candidate.body_hash != overlay["body_sha256"]
        ):
            raise history.ObligationProvenanceUnknown(
                "candidate obligation accepted body identity changed"
            )
    before = context["base_rows"]
    rows = {
        row.id: row
        for row in destination.scalars(
            select(NarrativeObligationRow)
            .where(NarrativeObligationRow.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    fresh = {
        row.id: row
        for row in source_session.scalars(
            select(NarrativeObligationRow).where(
                NarrativeObligationRow.project_id == project_id
            )
        )
    }
    if (
        set(rows) != set(before)
        or not set(before).issubset(fresh)
        or any(history.image(row) != before[row_id] for row_id, row in rows.items())
    ):
        raise history.ObligationProvenanceUnknown(
            "live obligation state changed since candidate prefix capture"
        )
    pending_rows = []
    events = {}
    for row_id, row in fresh.items():
        snapshot = history.image(row)
        if snapshot == before.get(row_id):
            continue
        restored_only = snapshot == context.get("restored_rows", {}).get(row_id)
        chain = history._chain(source_session, row)
        if restored_only:
            original = NarrativeObligationRow()
            _assign(original, before[row_id])
            original_chain = history._chain(source_session, original)
            if not original_chain or not any(
                event["before"] == snapshot for event in original_chain
            ):
                raise history.ObligationProvenanceUnknown(
                    "restored obligation lacks its original before-image proof"
                )
            if context["from_chapter"] not in supplied:
                raise history.ObligationProvenanceUnknown(
                    "restored obligation requires the new revision acceptance"
                )
        if not chain and not restored_only:
            raise history.ObligationProvenanceUnknown(
                "candidate obligation state has no journal proof"
            )
        origin = history._provenance(snapshot).get("origin_acceptance_id")
        if origin:
            if active.get(row.origin_chapter_number) != origin:
                raise history.ObligationProvenanceUnknown(
                    "candidate obligation origin acceptance is inactive"
                )
            validate_activation(
                destination,
                project_id=project_id,
                chapter_number=row.origin_chapter_number,
                acceptance_id=origin,
                draft_id=row.origin_draft_id,
            )
        elif row.status not in {"planned", "proposed"}:
            raise history.ObligationProvenanceUnknown(
                "candidate obligation active origin is unknown"
            )
        for payload in chain:
            if (
                payload["operation"] not in {"create", "planned"}
                and active.get(payload["effect_chapter"])
                != payload["effect_acceptance_id"]
            ):
                raise history.ObligationProvenanceUnknown(
                    "candidate obligation effect acceptance mismatch"
                )
            event_id = history._provenance(payload["after"])["head_event_id"]
            source_event = source_session.get(DecisionEvent, event_id)
            existing = destination.get(DecisionEvent, event_id)
            if existing is not None and history.image(existing) != history.image(
                source_event
            ):
                raise history.ObligationProvenanceUnknown(
                    "immutable obligation journal event changed"
                )
            if existing is None:
                events[event_id] = history.image(source_event)
        pending_rows.append((row_id, snapshot, restored_only))
    for event_id, snapshot in events.items():
        event = DecisionEvent()
        _assign(event, snapshot)
        destination.add(event)
    for row_id, snapshot, restored_only in pending_rows:
        row = rows.get(row_id) or NarrativeObligationRow()
        _assign(row, snapshot)
        destination.add(row)
        if restored_only:
            # Branch from the proven prefix, never from the obsolete suffix.
            # The physical live before-image remains separate audit evidence.
            history.record_mutation(
                destination,
                row,
                operation="revision_restore",
                before=snapshot,
                chapter_number=context["from_chapter"],
                acceptance_id=supplied[context["from_chapter"]],
                installation_previous_live_image=before[row_id],
            )
    destination.flush()
    return [row_id for row_id, _, _ in pending_rows]
