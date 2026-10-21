"""Earliest chapter at which each scheduled secret may be revealed to the reader."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project


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
        if number is not None and start is not None:
            arc_starts[number] = start
    schedule: dict[str, int] = {}
    for candidate in _mappings(seed.get("reveal_candidates")):
        fact_id = str(candidate.get("fact_id") or "").strip()
        start = arc_starts.get(_integer(candidate.get("arc_number")))
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
    except ValueError:
        return {}
    return _mapping(pack)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mappings(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _integer(value: Any) -> int | None:
    return value if type(value) is int else None


__all__ = ["earliest_reveal_chapters"]
