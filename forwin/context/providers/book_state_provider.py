from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest


class BookStateContextProvider:
    name = "book_state"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        from forwin.context.assembler_core import (
            _book_state_context_overlay,
            _load_book_state_context_runtime,
            _merge_book_state_map_overlay,
        )

        runtime = _load_book_state_context_runtime(
            request.session,
            request.project_id,
            request.chapter_plan.chapter_number,
        )
        overlay = _book_state_context_overlay(
            request.session,
            request.project_id,
            request.chapter_plan.chapter_number,
            runtime=runtime,
        )
        map_context = _merge_book_state_map_overlay(draft.data.get("map_context", {}), overlay)
        draft.data.update(
            {
                "book_state_overlay": overlay,
                # Later providers read the same BookState instead of loading it again.
                "book_state_runtime": runtime,
                "map_context": map_context,
            }
        )
