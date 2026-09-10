from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest
from forwin.protocol.context import AudienceHintView


class FeedbackContextProvider:
    name = "feedback"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        # The repository owns qualification, selection and chapter validity.
        # Legacy string packs and raw reader comments are not canonical hints.
        getter = getattr(request.repo, "get_audience_hints", None)
        hints = (
            getter(
                request.project_id, before_chapter=request.chapter_plan.chapter_number
            )
            if callable(getter)
            else None
        )
        hints = hints.clipped() if isinstance(hints, AudienceHintView) else None
        if hints is not None and not hints.items:
            hints = None
        draft.data["audience_hints_raw"] = hints
        draft.data["audience_hints"] = hints
        draft.data.setdefault("reader_feedback", None)
