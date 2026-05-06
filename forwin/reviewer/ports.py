from __future__ import annotations

from typing import Protocol

from forwin.protocol.context import ChapterContextPack, ReviewContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput


class ReviewerPort(Protocol):
    name: str

    def review(
        self,
        context: ReviewContextPack | ChapterContextPack,
        writer_output: WriterOutput,
        **kwargs,
    ) -> ReviewVerdict:
        ...
