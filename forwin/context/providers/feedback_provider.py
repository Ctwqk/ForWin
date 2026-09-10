from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest


class FeedbackContextProvider:
    name = "feedback"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        # Feedback collection and read APIs remain available, but their current
        # unqualified action records must not enter automated Writer context.
        draft.data["audience_hints_raw"] = None
        draft.data["audience_hints"] = None
        draft.data.setdefault("reader_feedback", None)
