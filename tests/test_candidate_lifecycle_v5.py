from __future__ import annotations

import json

import pytest

from forwin.candidate_drafts import (
    CandidateDraftRepository,
    CandidateTransitionError,
    candidate_body_hash,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _setup_project(session):
    updater = StateUpdater(session)
    project = updater.create_project(
        title="v5 candidate lifecycle",
        premise="不可变候选版本",
        genre="悬疑",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    )
    arc = updater.create_arc_plan(project.id, "第一弧", version=3)
    chapter = updater.create_chapter_plan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=1,
        title="第一章",
        one_line="候选进入评审",
        goals=["验证版本链"],
    )
    return project, chapter


def _reviewed_draft(
    session,
    *,
    chapter_plan_id: str,
    draft_version: int,
    body: str,
    verdict: str = "warn",
):
    output = WriterOutput(
        chapter_number=1,
        title="第一章",
        body=body,
        char_count=len(body),
        end_of_chapter_summary="候选摘要",
        generation_meta={"artifact_meta_path": f"artifacts/draft-{draft_version}.json"},
    )
    draft = ChapterDraft(
        chapter_plan_id=chapter_plan_id,
        version=draft_version,
        body_text=body,
        summary=output.end_of_chapter_summary,
        char_count=len(body),
    )
    session.add(draft)
    session.flush()
    review_payload = ReviewVerdict(verdict=verdict, review_summary="reviewed")
    review = ChapterReview(
        draft_id=draft.id,
        verdict=verdict,
        issues_json="[]",
        review_meta_json=json.dumps(review_payload.model_dump(mode="json"), ensure_ascii=False),
    )
    session.add(review)
    session.flush()
    return draft, review, output


def test_candidate_versions_are_append_only_and_linked() -> None:
    engine = get_engine(postgres_test_url("candidate-lifecycle-append-only"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, chapter = _setup_project(session)
        first_draft, first_review, first_output = _reviewed_draft(
            session,
            chapter_plan_id=chapter.id,
            draft_version=1,
            body="第一版正文",
        )
        repository = CandidateDraftRepository(session)
        first = repository.create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=first_draft,
            review=first_review,
            writer_output=first_output,
            plan_revision="arc-v3:chapter-1",
            policy_version=1,
        )

        second_draft, second_review, second_output = _reviewed_draft(
            session,
            chapter_plan_id=chapter.id,
            draft_version=2,
            body="第二版修复正文",
        )
        second = repository.create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=second_draft,
            review=second_review,
            writer_output=second_output,
            plan_revision="arc-v3:chapter-1",
            policy_version=1,
            parent_candidate_id=first.id,
            repair_attempt_count=1,
        )

        assert first.id != second.id
        assert first.version == 1
        assert second.version == 2
        assert first.parent_candidate_id == ""
        assert second.parent_candidate_id == first.id
        assert first.body_hash == candidate_body_hash("第一版正文")
        assert second.body_hash == candidate_body_hash("第二版修复正文")
        assert first.writer_artifact_ref == "artifacts/draft-1.json"
        assert second.writer_artifact_ref == "artifacts/draft-2.json"
        assert first.status == "reviewed"
        assert second.status == "reviewed"


def test_candidate_creation_is_idempotent_for_same_draft() -> None:
    engine = get_engine(postgres_test_url("candidate-lifecycle-idempotent"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, chapter = _setup_project(session)
        draft, review, output = _reviewed_draft(
            session,
            chapter_plan_id=chapter.id,
            draft_version=1,
            body="正文不变",
        )
        repository = CandidateDraftRepository(session)
        first = repository.create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision="arc-v3:chapter-1",
            policy_version=1,
        )
        second = repository.create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision="arc-v3:chapter-1",
            policy_version=1,
        )

        assert second.id == first.id
        assert second.body_hash == first.body_hash


def test_candidate_transitions_follow_the_v5_state_graph() -> None:
    engine = get_engine(postgres_test_url("candidate-lifecycle-transitions"))
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project, chapter = _setup_project(session)
        draft, review, output = _reviewed_draft(
            session,
            chapter_plan_id=chapter.id,
            draft_version=1,
            body="准备进入 Canon",
            verdict="pass",
        )
        repository = CandidateDraftRepository(session)
        candidate = repository.create_reviewed_version(
            project_id=project.id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision="arc-v3:chapter-1",
            policy_version=1,
        )

        with pytest.raises(CandidateTransitionError, match="reviewed -> accepted"):
            repository.transition(candidate.id, "accepted")

        repository.transition(candidate.id, "ready_for_canon")
        repository.transition(candidate.id, "committing")
        accepted = repository.transition(candidate.id, "accepted")

        assert accepted.status == "accepted"
        assert accepted.canon_status == "canon"

