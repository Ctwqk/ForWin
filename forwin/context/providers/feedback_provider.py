from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest
from forwin.protocol.context import AudienceHintView


class FeedbackContextProvider:
    name = "feedback"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        repo = request.repo
        audience_hints_getter = getattr(repo, "get_audience_hints", None)
        audience_hints_raw = (
            audience_hints_getter(request.project_id, before_chapter=request.chapter_plan.chapter_number)
            if callable(audience_hints_getter)
            else None
        )
        draft.data["audience_hints_raw"] = audience_hints_raw
        draft.data["audience_hints"] = (
            AudienceHintView(
                pacing_hints=audience_hints_raw.pacing_hints,
                clarity_hints=audience_hints_raw.clarity_hints,
                character_heat_changes=audience_hints_raw.character_heat_changes,
                risk_flags=audience_hints_raw.risk_flags,
            )
            if audience_hints_raw is not None
            else None
        )
        draft.data.setdefault("reader_feedback", None)
