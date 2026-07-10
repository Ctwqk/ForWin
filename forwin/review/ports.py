from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

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


@dataclass(frozen=True)
class ReviewChapterRequest:
    project_id: str
    chapter_number: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewChapterResult:
    verdict: Any
    repair_instruction: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ReviewPort(Protocol):
    def review_chapter(self, request: ReviewChapterRequest) -> ReviewChapterResult:
        ...


class CallableReviewPort:
    def __init__(
        self,
        review_chapter: Callable[[ReviewChapterRequest], ReviewChapterResult],
    ) -> None:
        self._review_chapter = review_chapter

    def review_chapter(self, request: ReviewChapterRequest) -> ReviewChapterResult:
        return self._review_chapter(request)


__all__ = [
    "CallableReviewPort",
    "ReviewChapterRequest",
    "ReviewChapterResult",
    "ReviewPort",
    "ReviewerPort",
]
