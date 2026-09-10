"""Short, Project-first writes of personality fields on current character rows."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from forwin.book_state.repository import BookStateRepository
from forwin.canon.projection_lock import lock_projection_project
from forwin.models.book_state import WorldNodeRow
from forwin.protocol.book_state import WorldNode


class PersonalityUpdateConflict(ValueError):
    pass


@dataclass(frozen=True)
class PersonalityUpdate:
    before: WorldNode
    current: WorldNode
    applied: bool


def lock_current_character(session, project_id, character_id):
    lock_projection_project(session, project_id)
    row = session.scalar(
        select(WorldNodeRow)
        .where(WorldNodeRow.project_id == project_id, WorldNodeRow.id == character_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.node_type != "character" or not row.is_active:
        raise PersonalityUpdateConflict("character changed or is no longer active")
    return row, BookStateRepository(session).get_world_node(row.id)


def write_personality_fields(session, row, *, loadout, assignment=None):
    # The caller has refreshed this row under the owner lock. Never write the
    # old WorldNode's summary, status, visibility, identity or unrelated JSON.
    profile = json.loads(row.profile_json or "{}")
    profile["personality_loadout"] = loadout
    row.profile_json = json.dumps(profile, ensure_ascii=False)
    if assignment is not None:
        metadata = json.loads(row.metadata_json or "{}")
        metadata["personality_assignment"] = assignment
        row.metadata_json = json.dumps(metadata, ensure_ascii=False)
    session.flush()
    return BookStateRepository(session).get_world_node(row.id)


def _assignment_inputs(node):
    return (
        node.name,
        node.description,
        node.summary,
        *(
            node.profile.get(key)
            for key in (
                "public_identity",
                "role_archetype",
                "narrative_role",
                "personality_tags",
            )
        ),
    )


def apply_assignment(
    session, *, original, loadout, assignment, mode, respect_manual=True
):
    row, current = lock_current_character(session, original.project_id, original.id)
    latest_assignment = current.metadata.get("personality_assignment") or {}
    if not isinstance(latest_assignment, dict):
        latest_assignment = {}
    existing = current.profile.get("personality_loadout")
    if mode == "backfill" and existing:
        return PersonalityUpdate(current, current, False)
    if (
        mode == "reassign"
        and respect_manual
        and latest_assignment.get("manual_override")
    ):
        return PersonalityUpdate(current, current, False)
    if mode != "manual" and _assignment_inputs(current) != _assignment_inputs(original):
        raise PersonalityUpdateConflict(
            "character assignment inputs changed; retry against current state"
        )
    updated = write_personality_fields(
        session, row, loadout=loadout, assignment=assignment
    )
    return PersonalityUpdate(current, updated, True)
