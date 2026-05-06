from __future__ import annotations

from forwin.protocol.context import ChapterContextPack, ReviewContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput


class ExperienceReviewer:
    name = "webnovel_experience"

    def __init__(
        self,
        *,
        enabled: bool = True,
        llm_client=None,
        llm_enabled: bool | None = None,
    ) -> None:
        from forwin.reviewer.webnovel import WebNovelExperienceReviewer

        self._delegate = WebNovelExperienceReviewer(
            enabled=enabled,
            llm_client=llm_client,
            llm_enabled=llm_enabled,
            include_map_movement=False,
        )

    def review(
        self,
        context: ReviewContextPack | ChapterContextPack,
        writer_output: WriterOutput,
        *,
        reviewer_skill_layers: list[object] | None = None,
    ) -> ReviewVerdict:
        return self._delegate.review(
            context,
            writer_output,
            reviewer_skill_layers=reviewer_skill_layers,
        )

    def choose_repair_escalation(self, **kwargs):
        return self._delegate.choose_repair_escalation(**kwargs)
