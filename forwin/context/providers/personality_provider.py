from __future__ import annotations

import logging

from forwin.context.request import ContextDraft, ContextRequest
from forwin.personality import CharacterPersonalityLibrary, build_active_personality_contexts

logger = logging.getLogger(__name__)


class PersonalityContextProvider:
    name = "personality"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        active_personality_contexts: list[dict] = []
        library = draft.data.setdefault("personality_library", CharacterPersonalityLibrary())
        allowed_entities = draft.data.get("allowed_entities", [])
        try:
            pressure_triggers = []
            world_pressure = draft.data.get("world_pressure")
            if world_pressure is not None:
                pressure_triggers.extend(
                    [
                        str(world_pressure.pressure_level or "").strip(),
                        str(world_pressure.pressure_summary or "").strip(),
                    ]
                )
                pressure_triggers.extend(
                    str(item).strip()
                    for item in world_pressure.notable_shifts
                    if str(item).strip()
                )
            active_personality_contexts = [
                item.model_dump(mode="json")
                for item in build_active_personality_contexts(
                    [
                        item
                        for item in (draft.data.get("book_state_overlay", {}).get("personality_characters") or [])
                        if str(item.get("character_name") or "") in allowed_entities
                        or str(item.get("character_id") or "") in allowed_entities
                    ],
                    library=library,
                    scene_flags=["chapter_generation"],
                    pressure_triggers=pressure_triggers,
                )
            ]
        except Exception:
            logger.warning("Failed to build active personality contexts.", exc_info=True)
        draft.data["active_personality_contexts"] = active_personality_contexts
