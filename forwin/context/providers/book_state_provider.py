from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest


class BookStateContextProvider:
    name = "book_state"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        from forwin.context.assembler import _book_state_context_overlay, _merge_book_state_map_overlay

        overlay = _book_state_context_overlay(
            request.session,
            request.project_id,
            request.chapter_plan.chapter_number,
        )
        map_context = _merge_book_state_map_overlay(draft.data.get("map_context", {}), overlay)
        book_state_world_lines = [
            str(item)
            for item in overlay.get("active_world_lines", [])
            if str(item).strip()
        ]
        book_state_knowledge_gaps = [
            str(item)
            for item in overlay.get("active_knowledge_gaps", [])
            if str(item).strip()
        ]
        draft.data.update(
            {
                "book_state_overlay": overlay,
                "map_context": map_context,
                "book_state_world_lines": book_state_world_lines,
                "book_state_knowledge_gaps": book_state_knowledge_gaps,
            }
        )
