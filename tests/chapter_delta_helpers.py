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


def chapter_delta(body: str, *, plan_fingerprint: str = "", **fields: Any) -> ChapterDeltaExtraction:
    """A validated extraction of ``body``: no elapsed time and only the given sections.

    ``plan_fingerprint`` names the plan it was extracted against, as ``plan_fingerprint(plan)``
    does; canon only reuses an extraction whose body and plan are both current, so an
    extraction attached to a chapter that has a plan must carry that plan's fingerprint.
    """
    return ChapterDeltaExtraction.model_validate({
        "body_sha256": body_sha256(body),
        "plan_fingerprint": plan_fingerprint,
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

    def extract(
        self, *, chapter_body: str, plan_fingerprint: str = "", **_kwargs: Any,  # noqa: ANN001
    ) -> ChapterDeltaExtractionResult:
        bodies.append(chapter_body)
        # Like the real extractor, stamp the plan it was asked about: canon reuses an
        # extraction only while both its body and plan are current.
        return ChapterDeltaExtractionResult(chapter_delta(chapter_body, plan_fingerprint=plan_fingerprint), (), 1, {})

    monkeypatch.setattr(ChapterDeltaExtractor, "extract", extract)
    return bodies
