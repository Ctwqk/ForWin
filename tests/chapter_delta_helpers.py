"""Chapter delta extractions for tests that feed admission, translation or canon directly."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

from forwin.book_state.extraction.chapter_delta_extractor import (
    ChapterDeltaExtractionResult,
    ChapterDeltaExtractor,
)
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction, ChapterTime, PlanFulfillment
from forwin.naming import EntityAdmissionPlan, writer_output_admission_fingerprint
from forwin.protocol.context import ChapterContextPack, ReviewContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.draft_service import DraftReviewService
from forwin.state.repo import StateRepository


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


def plan_review_context(goals: list[str]) -> ReviewContextPack:
    return ReviewContextPack(
        project_title="Test", chapter_number=1, chapter_plan_title="Test",
        chapter_plan_one_line="Test", chapter_goals=goals,
    )


def plan_review_output(*fulfillment: tuple[str, str], extracted: bool = True) -> WriterOutput:
    body = "The courier handed over the sealed packet."
    return WriterOutput(
        chapter_number=1, title="Delivery", body=body, end_of_chapter_summary=body,
        chapter_delta=ChapterDeltaExtraction(
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            time=ChapterTime(evidence=body),
            plan_fulfillment=[
                PlanFulfillment(target_id=target, status=status, evidence=body)
                for target, status in fulfillment
            ],
        ) if extracted else None,
    )


BODY = "The hero walked the road."


def world_review_output(world: Any, fields: dict[str, Any] | None = None, *, extracted: bool = True,
            meta: dict[str, Any] | None = None) -> WriterOutput:
    """A chapter-1 candidate with a current admission plan, as review sees it after admission."""
    output = WriterOutput(
        project_id=world.project_id, chapter_number=1, title="Chapter one", body=BODY,
        end_of_chapter_summary="The hero arrived.",
        chapter_delta=chapter_delta(BODY, **(fields or {})) if extracted else None,
    )
    plan = EntityAdmissionPlan(
        project_id=world.project_id, chapter_number=1,
        candidate_fingerprint=writer_output_admission_fingerprint(output),
    )
    return output.model_copy(update={"generation_meta": {
        **(meta or {}), "entity_admission_plan": plan.model_dump(mode="json"),
    }})


def draft_review(Session, world: Any, output: WriterOutput, *, goals: list[str] | None = None,
                  **service_kwargs: Any) -> ReviewVerdict:
    passing = ReviewVerdict(verdict="pass", issues=[])
    service = DraftReviewService(
        experience_review_enabled=False,
        lint_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
        **service_kwargs,
    )
    with Session() as session:
        return service.review(
            project_id=world.project_id,
            repo=StateRepository(session),
            context=ChapterContextPack(
                project_id=world.project_id, project_title="world", premise="p", genre="g",
                setting_summary="", chapter_number=1, chapter_plan_title="Chapter one",
                chapter_plan_one_line="The hero walks.", chapter_goals=list(goals or []),
            ),
            writer_output=output,
            continuity_checker=SimpleNamespace(check=lambda _project_id, _output: passing),
        )


