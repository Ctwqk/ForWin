"""Earliest chapter at which each scheduled secret may be revealed to the reader."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project


logger = logging.getLogger(__name__)


def earliest_reveal_chapters(session: Session, project_id: str) -> dict[str, int]:
    """Fact node id -> ``chapter_start`` of the arc its world_seed reveal candidate names."""
    # The seed committer imports the review gate, which reads this schedule.
    from forwin.genesis.handoff.world_seed_committer import seed_ref_id

    pack = _active_pack(session, project_id)
    seed = _mapping(pack.get("world_seed"))
    blueprint = _mapping(pack.get("book_arc_blueprint"))
    arc_starts: dict[int, int] = {}
    for arc in _mappings(blueprint.get("arcs")):
        number, start = _integer(arc.get("arc_number")), _integer(arc.get("chapter_start"))
        if number is None or start is None:
            logger.warning(
                "project %s: book_arc_blueprint arc has a non-integer arc_number or chapter_start: %r",
                project_id, {key: arc.get(key) for key in ("arc_number", "chapter_start")},
            )
            continue
        arc_starts[number] = start
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
    return schedule


def _active_pack(session: Session, project_id: str) -> dict[str, Any]:
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


__all__ = ["earliest_reveal_chapters"]
