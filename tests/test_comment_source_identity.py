from __future__ import annotations

pytest_plugins = ["tests.test_comment_consumption_contract"]

from sqlalchemy import select

from forwin.candidate_drafts import candidate_body_hash
from forwin.models.base import new_id
from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherRawComment,
    PublisherWorkBinding,
)
from forwin.simulation.world import CommentAnalyzer


def _published_chapter(runtime, project_id):
    with runtime.session_factory() as session:
        arc = ArcPlanVersion(id=new_id(), project_id=project_id, arc_synopsis="arc")
        session.add(arc)
        session.flush()
        plan = ChapterPlan(
            id=new_id(), project_id=project_id, arc_plan_id=arc.id, chapter_number=2
        )
        session.add(plan)
        session.flush()
        commits = []
        for version in (1, 2):
            draft = ChapterDraft(
                id=new_id(),
                chapter_plan_id=plan.id,
                version=version,
                body_text=f"正文{version}",
            )
            session.add(draft)
            session.flush()
            review = ChapterReview(id=new_id(), draft_id=draft.id, verdict="pass")
            session.add(review)
            session.flush()
            candidate = CandidateDraftRecord(
                id=new_id(),
                project_id=project_id,
                chapter_plan_id=plan.id,
                chapter_number=2,
                candidate_draft_id=draft.id,
                body_hash=candidate_body_hash(draft.body_text),
                review_id=review.id,
                version=version,
            )
            session.add(candidate)
            session.flush()
            commit = CanonCommitRecord(
                id=new_id(),
                idempotency_key=new_id(),
                candidate_id=candidate.id,
                project_id=project_id,
                chapter_plan_id=plan.id,
                chapter_number=2,
                acceptance_revision=version,
            )
            session.add(commit)
            session.flush()
            commits.append(commit)
        plan.active_commit_id = commits[1].id
        binding = PublisherWorkBinding(
            id=new_id(),
            project_id=project_id,
            platform_id="fanqie",
            remote_book_id="remote-work",
        )
        session.add(binding)
        session.flush()
        chapter_binding = PublisherChapterBinding(
            id=new_id(),
            project_id=project_id,
            platform_id="fanqie",
            work_binding_id=binding.id,
            chapter_number=2,
            remote_chapter_id="remote-chapter",
        )
        session.add(chapter_binding)
        publication = CanonPublicationProtection(
            id=new_id(),
            project_id=project_id,
            chapter_plan_id=plan.id,
            chapter_number=2,
            canon_commit_id=commits[0].id,
            upload_job_id=new_id(),
            platform_id="fanqie",
            content_sha256=candidate_body_hash("正文1"),
            state="published",
            remote_book_id="remote-work",
            remote_chapter_id="remote-chapter",
        )
        session.add(publication)
        session.commit()
        return plan.id, commits[0].id, commits[1].id, publication.id


def test_late_comment_binds_remote_publication_not_current_active_commit(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    plan_id, published, active, publication_id = _published_chapter(runtime, project_id)
    for comment_id in ("later", "earlier", "later"):
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            comments=[
                {
                    "project_id": project_id,
                    "work_id": "remote-work",
                    "chapter_id": "remote-chapter",
                    "remote_comment_id": comment_id,
                    "body": "太拖了",
                    "created_at": "2026-08-01T10:00:00Z",
                    "observed_at": "2026-09-01T10:00:00Z",
                }
            ],
        )
    with runtime.session_factory() as session:
        comments = session.scalars(select(PublisherRawComment)).all()
        assert len(comments) == 2
        assert {c.source_chapter_number for c in comments} == {2}
        assert {c.source_chapter_plan_id for c in comments} == {plan_id}
        assert {c.source_canon_commit_id for c in comments} == {published}
        assert all(
            c.source_canon_commit_id != active
            and c.source_publication_id == publication_id
            for c in comments
        )
        signals = CommentAnalyzer().analyze_and_store(
            session=session, project_id=project_id, comments=comments, chapter_number=99
        )
        assert {s.chapter_number for s in signals} == {2}
        assert all(
            c.ingested_at and c.observed_at and c.remote_created_at for c in comments
        )


def test_missing_remote_chapter_is_unknown_despite_same_title_and_active_chapter(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    _published_chapter(runtime, project_id)
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "project_id": project_id,
                "work_id": "remote-work",
                "chapter_title": "同一章",
                "remote_comment_id": "unknown",
                "body": "太拖了",
            }
        ],
    )
    with runtime.session_factory() as session:
        comment = session.scalars(select(PublisherRawComment)).one()
        assert comment.source_status == "unknown"
        assert comment.source_chapter_number is None
        assert comment.source_canon_commit_id == ""


def test_binding_without_publication_does_not_guess_canon_version(comments_runtime):
    runtime, project_id = comments_runtime
    _published_chapter(runtime, project_id)
    with runtime.session_factory() as session:
        session.delete(session.scalars(select(CanonPublicationProtection)).one())
        session.commit()
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "project_id": project_id,
                "work_id": "remote-work",
                "chapter_id": "remote-chapter",
                "remote_comment_id": "comment",
                "body": "太拖了",
            }
        ],
    )
    with runtime.session_factory() as session:
        comment = session.scalars(select(PublisherRawComment)).one()
        assert comment.source_status == "chapter_known"
        assert comment.source_chapter_number == 2
        assert comment.source_canon_commit_id == ""


def test_contradictory_remote_publication_is_not_silently_filtered_to_current_binding(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    _published_chapter(runtime, project_id)
    with runtime.session_factory() as session:
        binding = session.scalars(select(PublisherChapterBinding)).one()
        plan = session.scalars(select(ChapterPlan)).one()
        other = ChapterPlan(
            id=new_id(),
            project_id=project_id,
            arc_plan_id=plan.arc_plan_id,
            chapter_number=3,
        )
        session.add(other)
        binding.chapter_number = 3
        session.commit()
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "project_id": project_id,
                "work_id": "remote-work",
                "chapter_id": "remote-chapter",
                "remote_comment_id": "contradiction",
                "body": "太拖了",
            }
        ],
    )
    with runtime.session_factory() as session:
        comment = session.scalars(select(PublisherRawComment)).one()
        assert comment.source_status == "unknown"
        assert comment.source_chapter_number is None


def test_ingest_enriches_same_account_comment_and_pending_reanalyzes_source_version(
    comments_runtime,
):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime
    item = {
        "project_id": project_id,
        "account_id": "author-account",
        "work_id": "remote-work",
        "chapter_id": "remote-chapter",
        "remote_comment_id": "enriched",
        "body": "太拖了",
    }
    runtime.comment_sync.ingest_comments_batch(
        client_id="client", platform="fanqie", comments=[item]
    )
    analyzer = CommentAnalyzer()
    with runtime.session_factory() as session:
        old = session.scalars(select(PublisherRawComment)).one()
        original_id = old.id
        analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=[old], chapter_number=80
        )
        session.commit()
    _published_chapter(runtime, project_id)
    runtime.comment_sync.ingest_comments_batch(
        client_id="client", platform="fanqie", comments=[item]
    )
    with runtime.session_factory() as session:
        comment = session.scalars(select(PublisherRawComment)).one()
        assert comment.id == original_id
        assert comment.source_status == "confirmed"
        pending = analyzer.pending_comments(
            session=session, project_id=project_id, limit=8
        )
        assert pending == [comment]
        signals = analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=pending, chapter_number=90
        )
        assert {s.chapter_number for s in signals} == {2}
        records = session.scalars(select(CommentAnalysisRecord)).all()
        assert len(records) == 2
        assert len({r.source_sha256 for r in records}) == 2
        assert len({r.content_sha256 for r in records}) == 1


def test_reverted_body_requeues_and_reactivates_exact_completed_analysis(comments_runtime):
    from forwin.models.publisher import CommentAnalysisRecord
    from forwin.simulation.world import load_recent_signals
    runtime, project_id = comments_runtime
    _published_chapter(runtime, project_id)
    item = {"project_id": project_id, "account_id": "a", "work_id": "remote-work", "chapter_id": "remote-chapter", "remote_comment_id": "revert"}
    def ingest(body, observed):
        runtime.comment_sync.ingest_comments_batch(client_id="client", platform="fanqie", comments=[dict(item, body=body, observed_at=observed)])
    ingest("太拖了", "2026-09-01T00:00:00Z")
    analyzer = CommentAnalyzer()
    with runtime.session_factory.begin() as session:
        row = session.scalars(select(PublisherRawComment)).one()
        signals = analyzer.analyze_and_store(session=session, project_id=project_id, comments=[row])
        signal_id = signals[0].id
        analysis_id = row.active_analysis_id
        analyzed_at = session.get(CommentAnalysisRecord, analysis_id).analyzed_at
    ingest("没有意见", "2026-09-02T00:00:00Z")
    ingest("太拖了", "2026-09-03T00:00:00Z")
    with runtime.session_factory.begin() as session:
        row = session.scalars(select(PublisherRawComment)).one()
        pending = analyzer.pending_comments(session=session, project_id=project_id, limit=8)
        assert pending == [row]
        assert analyzer.store.status(session, project_id=project_id)["pending_count"] == 1
        analyzer.analyze_and_store(session=session, project_id=project_id, comments=pending)
        assert row.active_analysis_id == analysis_id
        assert [signal.id for signal in load_recent_signals(session, project_id, current_chapter=2)] == [signal_id]
        record = session.get(CommentAnalysisRecord, analysis_id)
        assert record.attempt_count == 1
        assert record.analyzed_at == analyzed_at
        assert len(session.scalars(select(CommentAnalysisRecord)).all()) == 1
        assert analyzer.pending_comments(session=session, project_id=project_id, limit=8) == []


def test_known_work_enriches_project_even_when_chapter_remains_unknown(comments_runtime):
    runtime, project_id = comments_runtime
    item = {"account_id": "a", "work_id": "new-work", "remote_comment_id": "book-review", "body": "喜欢这个故事"}
    runtime.comment_sync.ingest_comments_batch(client_id="client", platform="fanqie", comments=[item])
    with runtime.session_factory.begin() as session:
        raw_id = session.scalar(select(PublisherRawComment.id))
        binding = PublisherWorkBinding(project_id=project_id, platform_id="fanqie", remote_book_id="new-work")
        session.add(binding)
        session.flush()
        binding_id = binding.id
    runtime.comment_sync.ingest_comments_batch(client_id="client", platform="fanqie", comments=[item])
    with runtime.session_factory.begin() as session:
        raw = session.scalars(select(PublisherRawComment)).one()
        assert raw.id == raw_id
        assert raw.project_id == project_id
        assert raw.work_binding_id == binding_id
        assert raw.source_status == "unknown"
        assert raw.source_chapter_number is None
        assert CommentAnalyzer().pending_comments(session=session, project_id=project_id, limit=8) == [raw]
