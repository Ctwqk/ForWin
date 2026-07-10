from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.canon.preparation import CanonPreparationService
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.entity import Entity
from forwin.models.outbox import OutboxEvent
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def test_fail_verdict_is_rejected_before_canon_preparation_collaborators() -> None:
    engine = get_engine(postgres_test_url("candidate-ineligible-before-canon"))
    init_db(engine)
    Session = get_session_factory(engine)
    calls: list[str] = []
    try:
        with Session.begin() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Rejected candidate",
                premise="A failed review cannot enter Canon.",
                genre="thriller",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            arc = updater.create_arc_plan(project.id, "Arc one")
            chapter = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="Chapter one",
                one_line="Reject the candidate",
                goals=["Preserve authority"],
            )
            output = WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="Chapter one",
                body="This candidate has a hard continuity conflict.",
                char_count=46,
                end_of_chapter_summary="The review fails.",
            )
            draft = ChapterDraft(
                chapter_plan_id=chapter.id,
                version=1,
                body_text=output.body,
                summary=output.end_of_chapter_summary,
                char_count=output.char_count,
            )
            session.add(draft)
            session.flush()
            review = ChapterReview(
                draft_id=draft.id,
                verdict="fail",
                issues_json="[]",
                review_meta_json='{"verdict":"fail"}',
            )
            session.add(review)
            session.flush()
            candidate = CandidateDraftRepository(session).create_reviewed_version(
                project_id=project.id,
                chapter_plan=chapter,
                draft=draft,
                review=review,
                writer_output=output,
                plan_revision=candidate_plan_revision(chapter),
                policy_version=1,
            )

            def unexpected_quality(**_kwargs):
                calls.append("quality")
                raise AssertionError("quality must not run for an ineligible candidate")

            class UnexpectedBookState:
                def prepare(self, **_kwargs):
                    calls.append("book_state")
                    raise AssertionError(
                        "BookState extraction must not run for an ineligible candidate"
                    )

            outcome = CanonPreparationService(
                quality_evaluator=unexpected_quality,
                book_state_preparer=UnexpectedBookState(),
            ).prepare(
                runtime=object(),
                session=session,
                repo=StateRepository(session),
                updater=updater,
                candidate_id=candidate.id,
                project_id=project.id,
                chapter_number=1,
                writer_output=output,
                verdict=ReviewVerdict(verdict="fail"),
                acceptance_mode="normal",
                repair_attempt_count=0,
                residual_review_issues=[],
                canon_risk_level="high",
            )

            assert outcome.blocked is True
            assert outcome.block_kind == "candidate_ineligible"
            assert calls == []
            assert candidate.status == "needs_review"
            assert chapter.status == "planned"
            assert session.scalar(select(func.count(GraphDeltaRow.id))) == 0
            assert session.scalar(select(func.count(Entity.id))) == 0
            assert session.scalar(select(func.count(OutboxEvent.id))) == 0
            assert session.scalar(select(func.count(CanonCommitRecord.id))) == 0
    finally:
        engine.dispose()


def test_pipeline_only_uses_prepared_atomic_canon_entrypoint() -> None:
    project_source = Path(
        "forwin/generation/pipeline_core/project_chapters.py"
    ).read_text(encoding="utf-8")
    acceptance_source = Path(
        "forwin/generation/pipeline_core/acceptance.py"
    ).read_text(encoding="utf-8")
    projection_source = Path(
        "forwin/generation/pipeline_core/world_projection.py"
    ).read_text(encoding="utf-8")
    container_source = Path("forwin/runtime/container.py").read_text(
        encoding="utf-8"
    )

    assert ".canon_admission.commit(" not in project_source
    assert ".canon_admission.commit(" not in acceptance_source
    assert ".canon_admission.commit_plan(" in project_source
    assert ".canon_admission.commit_plan(" in acceptance_source
    assert "memory_index.upsert_chapter" not in project_source
    assert "memory_index.upsert_chapter" not in acceptance_source
    assert "_commit_book_state_canon" not in projection_source
    assert "KnowledgeProjectionRefresher" not in projection_source
    assert "CanonAdmissionService(" in container_source
    assert "session_factory=session_factory" in container_source
    assert "CanonPreparationService()" in container_source
