from __future__ import annotations


class ChapterReviewFormError(RuntimeError):
    """Base error for chapter review form failures."""


class ChapterReviewFormUnavailable(ChapterReviewFormError):
    """Raised when the form review LLM path cannot produce an answer."""


class ChapterReviewFormSchemaInvalid(ChapterReviewFormError):
    """Raised when an LLM answer cannot be parsed as the expected schema."""
