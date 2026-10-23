"""Earliest chapter at which each scheduled secret may be revealed to the reader."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import ArcPlanVersion, Project


logger = logging.getLogger(__name__)


def earliest_reveal_chapters(session: Session, project_id: str) -> dict[str, int]:
    """Fact node id -> reader target start, otherwise the candidate's live arc start."""
    # book_state must not import planning at module load (import cycle via book_state.narrative).
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import READER_OBSERVER

    # The seed committer imports the review gate, which reads this schedule.
    from forwin.genesis.handoff.world_seed_committer import seed_ref_id

    pack = active_genesis_pack(session, project_id)
    seed = _mapping(pack.get("world_seed"))
    arc_starts = {
        arc.arc_number: arc.chapter_start for arc in session.scalars(select(ArcPlanVersion).where(
            ArcPlanVersion.project_id == project_id, ArcPlanVersion.status != "superseded",
        ).order_by(ArcPlanVersion.version))
        if type(arc.arc_number) is int and type(arc.chapter_start) is int
    }
    schedule: dict[str, int] = {}
    for candidate in _mappings(seed.get("reveal_candidates")):
        fact_id = str(candidate.get("fact_id") or "").strip()
        number = _integer(candidate.get("arc_number"))
        if number is None:
            logger.warning(
                "project %s: reveal candidate %r has a non-integer arc_number %r",
                project_id, fact_id, candidate.get("arc_number"),
            )
            continue
        start = arc_starts.get(number)
        if not fact_id or start is None:
            continue
        node_id = seed_ref_id(project_id, "fact", fact_id)
        schedule[node_id] = min(schedule.get(node_id, start), start)
    reader_targets: dict[str, int] = {}
    repo = ArcBeatSheetRepository(session)
    for row in repo.active_for_project(project_id):
        for target in repo.load(row).reveal_targets:
            if target.observer == READER_OBSERVER:
                ref, start = target.fact_ref.removeprefix(FACT_REF_PREFIX), target.range[0]
                reader_targets[ref] = min(reader_targets.get(ref, start), start)
    schedule.update(reader_targets)
    return schedule


def active_genesis_pack(session: Session, project_id: str) -> dict[str, Any]:
    """The project's active Genesis pack; empty when it has none or its JSON is not an object."""
    project = session.get(Project, project_id)
    revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
    revision = session.get(BookGenesisRevision, revision_id) if revision_id else None
    if revision is None:
        return {}
    try:
        pack = json.loads(revision.pack_json or "{}")
    except ValueError as exc:
        logger.warning("project %s: genesis revision %s pack_json is not JSON: %s", project_id, revision.id, exc)
        return {}
    if not isinstance(pack, dict):
        logger.warning("project %s: genesis revision %s pack_json is not an object", project_id, revision.id)
        return {}
    return pack


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mappings(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _integer(value: Any) -> int | None:
    return value if type(value) is int else None


__all__ = ["active_genesis_pack", "earliest_reveal_chapters"]
