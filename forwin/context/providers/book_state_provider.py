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

        from forwin.book_state import BookStateRepository

        latest = (
            BookStateRepository(request.session).latest_available_chapter(request.project_id)
            if request.session is not None else 0
        )
        as_of = max(0, min(int(latest or 0), int(request.chapter_plan.chapter_number) - 1))
        runtime = request.runtime_as_of(as_of)
        if runtime is None:
            runtime = _load_book_state_context_runtime(
                request.session, request.project_id, request.chapter_plan.chapter_number,
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
