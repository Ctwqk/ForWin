"""Chapter delta extractions for tests that feed admission, translation or canon directly."""

from __future__ import annotations

import hashlib
from typing import Any

from forwin.book_state.extraction.chapter_delta_extractor import (
    ChapterDeltaExtractionResult,
    ChapterDeltaExtractor,
)
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def chapter_delta(body: str, **fields: Any) -> ChapterDeltaExtraction:
    """A validated extraction of ``body``: no elapsed time and only the given sections."""
    return ChapterDeltaExtraction.model_validate({
        "body_sha256": body_sha256(body),
        "time": {"elapsed": None, "evidence": ""},
        **fields,
    })


def mention(name: str, kind: str = "character") -> dict[str, Any]:
    return {"name": name, "kind": kind, "evidence": name}


def stub_chapter_delta_extraction(monkeypatch) -> list[str]:  # noqa: ANN001
    """Answer every chapter delta extraction with an empty extraction of its body.

    For pipeline tests whose LLM client is not stubbed; returns the extracted bodies.
    """
    bodies: list[str] = []

    def extract(self, *, chapter_body: str, **_kwargs: Any) -> ChapterDeltaExtractionResult:  # noqa: ANN001
        bodies.append(chapter_body)
        return ChapterDeltaExtractionResult(chapter_delta(chapter_body), (), 1, {})

    monkeypatch.setattr(ChapterDeltaExtractor, "extract", extract)
    return bodies
