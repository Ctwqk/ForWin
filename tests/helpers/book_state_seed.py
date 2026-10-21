from __future__ import annotations

from typing import Any

from forwin.book_state import BookStateCompiler
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch


def character_node(
    project_id: str,
    node_id: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    state: dict[str, Any] | None = None,
) -> NodePatch:
    return NodePatch(
        node_id=node_id,
        node_type="character",
        op="create",
        new_value={"project_id": project_id, "name": name, "aliases": list(aliases), "state": dict(state or {})},
    )


def clock_node(
    project_id: str,
    node_id: str,
    label: str,
    *,
    due_at: int,
    status: str = "running",
    scope: str = "public",
) -> NodePatch:
    return NodePatch(
        node_id=node_id,
        node_type="clock",
        op="create",
        new_value={
            "project_id": project_id,
            "name": label,
            "profile": {"label": label, "owner_ref": "", "scope": scope, "on_expire": ""},
            "state": {"due_at": due_at, "status": status},
        },
    )


def state_change(node_id: str, field: str, old_value: Any, new_value: Any) -> NodePatch:
    return NodePatch(
        node_id=node_id,
        node_type="character",
        op="set",
        field_path=f"state.{field}",
        old_value=old_value,
        new_value=new_value,
    )


def commit_chapter(
    session,
    project_id: str,
    chapter_number: int,
    node_patches: list[NodePatch],
    *,
    story_minute: int,
) -> None:
    """Commit one world delta for ``chapter_number`` that ends at ``story_minute``."""
    delta = GraphDelta(
        id=f"seed-{project_id}-{chapter_number}",
        project_id=project_id,
        chapter_number=chapter_number,
        story_time_start=story_minute,
        story_time_end=story_minute,
        node_patches=node_patches,
    )
    result = BookStateCompiler(session).compile(
        ApprovedGraphDeltaSet(project_id=project_id, chapter_number=chapter_number, graph_deltas=[delta])
    )
    assert result.committed, result.blocked_reasons
