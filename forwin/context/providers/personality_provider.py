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
        # Only the chapter's onstage characters, as the world view resolved them.
        view = draft.data.get("world_view")
        onstage_refs = {entity.ref for entity in view.onstage} if view is not None else set()
        try:
            active_personality_contexts = [
                item.model_dump(mode="json")
                for item in build_active_personality_contexts(
                    [
                        item
                        for item in (draft.data.get("book_state_overlay", {}).get("personality_characters") or [])
                        if str(item.get("character_id") or "") in onstage_refs
                    ],
                    library=library,
                    scene_flags=["chapter_generation"],
                )
            ]
        except Exception:
            logger.warning("Failed to build active personality contexts.", exc_info=True)
        draft.data["active_personality_contexts"] = active_personality_contexts
