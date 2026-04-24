from __future__ import annotations

from collections import defaultdict
from typing import Any

from forwin.protocol.world_model import EvidenceRef, WorldModelConflict


def detect_conflicts(snapshot: dict[str, Any]) -> list[WorldModelConflict]:
    """Run deterministic low-risk conflict checks over a compiled snapshot."""
    conflicts: list[WorldModelConflict] = []
    actor_model = snapshot.get("actor_model") if isinstance(snapshot.get("actor_model"), dict) else {}
    characters = actor_model.get("characters") if isinstance(actor_model.get("characters"), list) else []
    plot_model = snapshot.get("plot_model") if isinstance(snapshot.get("plot_model"), dict) else {}
    events = plot_model.get("canon_events") if isinstance(plot_model.get("canon_events"), list) else []

    dead_chapter_by_name: dict[str, int] = {}
    for character in characters:
        if not isinstance(character, dict):
            continue
        state = character.get("current_state") if isinstance(character.get("current_state"), dict) else {}
        alive = state.get("alive")
        status = str(state.get("status", "") or "").lower()
        if alive is False or status in {"dead", "deceased", "死亡", "已死"}:
            name = str(character.get("name", "") or "").strip()
            if name:
                dead_chapter_by_name[name] = int(character.get("state_chapter", 0) or 0)

    for event in events:
        if not isinstance(event, dict):
            continue
        chapter = int(event.get("chapter_number", 0) or 0)
        summary = str(event.get("summary", "") or "")
        involved = [str(item) for item in event.get("involved_entity_names", []) if str(item).strip()]
        for name, dead_chapter in dead_chapter_by_name.items():
            if chapter > dead_chapter and (name in involved or name in summary):
                conflicts.append(
                    WorldModelConflict(
                        conflict_type="dead_alive_conflict",
                        severity="error",
                        subject_key=f"character:{name}",
                        description=f"{name} 在第 {dead_chapter} 章后被标记为死亡，但第 {chapter} 章事件仍引用该角色。",
                        evidence_refs=[
                            EvidenceRef(source_type="entity_state", chapter_number=dead_chapter, summary="角色死亡状态"),
                            EvidenceRef(source_type="canon_event", source_id=str(event.get("id", "")), chapter_number=chapter, summary=summary),
                        ],
                    )
                )

    locations_by_chapter: dict[tuple[int, str], set[str]] = defaultdict(set)
    for character in characters:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name", "") or "").strip()
        state = character.get("current_state") if isinstance(character.get("current_state"), dict) else {}
        location = str(state.get("location", "") or "").strip()
        chapter = int(character.get("state_chapter", 0) or 0)
        if name and location and chapter:
            locations_by_chapter[(chapter, name)].add(location)
    for (chapter, name), locations in locations_by_chapter.items():
        if len(locations) > 1:
            conflicts.append(
                WorldModelConflict(
                    conflict_type="character_location_conflict",
                    severity="warning",
                    subject_key=f"character:{name}",
                    description=f"{name} 在第 {chapter} 章存在多个当前位置：{', '.join(sorted(locations))}。",
                    evidence_refs=[
                        EvidenceRef(source_type="entity_state", chapter_number=chapter, summary="角色位置状态")
                    ],
                )
            )
    return conflicts
