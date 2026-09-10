from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.audience.feedback import SignalAggregator
from forwin.candidate_drafts import candidate_body_hash
from forwin.models.publisher import (
    CommentSignalCandidate,
    PublisherRawComment,
    SignalWindowAggregate,
)
from forwin.simulation.world import CommentAnalyzer, aggregate_and_level_signals
from tests.test_comment_source_identity import _published_chapter

pytest_plugins = ["tests.test_comment_consumption_contract"]


class OpinionModel:
    def chat(self, messages, **kwargs):
        comments = json.loads(messages[-1]["content"].split("评论列表：", 1)[1])
        signals = []
        for item in comments:
            body = item["body"]
            if body == "普通读后感":
                continue
            for _ in range(3 if "重复" in body else 1):
                signals.append(
                    {
                        "comment_index": item["comment_index"],
                        "signal_type": "pacing",
                        "direction": "too_fast" if "太快" in body else "too_slow",
                        "target_type": "arc",
                        "target_name": "节奏",
                        "severity": 3,
                        "confidence": 0.95,
                        "evidence_span": body,
                    }
                )
        return json.dumps({"signals": signals}, ensure_ascii=False)


def seed(runtime, project_id, bodies, *, authors=True, analyze=True):
    _published_chapter(runtime, project_id)
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "project_id": project_id,
                "account_id": "account",
                "work_id": "remote-work",
                "chapter_id": "remote-chapter",
                "remote_comment_id": f"r-{i}",
                "body": body,
                "author_id": f"reader-{i}" if authors else "",
            }
            for i, body in enumerate(bodies)
        ],
    )
    with runtime.session_factory() as session:
        comments = session.scalars(select(PublisherRawComment)).all()
        if analyze:
            CommentAnalyzer(llm_client=OpinionModel()).analyze_and_store(
                session=session,
                project_id=project_id,
                comments=comments,
            )
        session.commit()


def short(session, project_id):
    return [
        row
        for row in SignalAggregator().aggregate(session, project_id, 2)
        if row.window_type == "short"
    ]


def test_denominator_includes_zero_signal_comments_and_hit_is_per_comment(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["重复：太拖了", "普通读后感", "普通读后感"])
    with runtime.session_factory() as session:
        row = short(session, project_id)[0]
        assert row.total_comment_count == 3
        assert row.hit_comment_count == 1
        assert row.analyzed_comment_count == 3


def test_opposite_pacing_directions_have_distinct_evidence_buckets(comments_runtime):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了", "太快了"])
    with runtime.session_factory() as session:
        rows = short(session, project_id)
        assert len(rows) == 2
        assert {r.direction for r in rows} == {"too_slow", "too_fast"}
        assert len({r.signal_key for r in rows}) == 2


def test_unknown_authors_do_not_become_confirmed_users(comments_runtime):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 3, authors=False)
    with runtime.session_factory() as session:
        row = short(session, project_id)[0]
        assert row.unique_user_count == 0
        assert row.unknown_author_comment_count == 3
        assert row.source_qualified is False
        assert (
            "unknown_authors"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )


def test_snapshot_is_hash_idempotent_and_preserves_previous_evidence(comments_runtime):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"])
    with runtime.session_factory() as session:
        first = short(session, project_id)[0]
        session.commit()
        again = short(session, project_id)[0]
        assert again.id == first.id
        saved = first.provenance_json
        comment = session.scalars(select(PublisherRawComment)).one()
        comment.body_text = "太快了"
        from forwin.publisher_runtime.comment_source import body_hash

        comment.content_sha256 = body_hash(comment.body_text)
        comment.active_analysis_id = ""
        CommentAnalyzer(llm_client=OpinionModel()).analyze_and_store(
            session=session,
            project_id=project_id,
            comments=[comment],
        )
        changed = short(session, project_id)[0]
        assert changed.id != first.id
        assert session.get(SignalWindowAggregate, first.id).provenance_json == saved


def test_snapshot_and_window_aggregator_consume_same_view(comments_runtime):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了", "普通读后感"])
    with runtime.session_factory() as session:
        row = short(session, project_id)[0]
        signals = session.scalars(select(CommentSignalCandidate)).all()
        views = aggregate_and_level_signals(session, signals)
        view = next(iter(views.values()))
        assert view["aggregate_id"] == row.id
        assert view["total_comment_count"] == 2
        assert view["direction"] == row.direction


def test_pending_coverage_and_unpublished_sources_are_observation_only(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了", "普通读后感"])
    with runtime.session_factory() as session:
        comments = session.scalars(
            select(PublisherRawComment).order_by(PublisherRawComment.remote_comment_id)
        ).all()
        comments[1].active_analysis_id = ""
        row = short(session, project_id)[0]
        assert row.source_qualified is False
        assert (
            "incomplete_analysis"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )


def reanalyze(session, project_id, comments):
    CommentAnalyzer(llm_client=OpinionModel()).analyze_and_store(
        session=session,
        project_id=project_id,
        comments=comments,
    )
    session.flush()


def test_multiple_platform_accounts_do_not_add_up_to_consensus(comments_runtime):
    from forwin.models.base import new_id
    from forwin.models.canon import CanonPublicationProtection

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 2)
    with runtime.session_factory() as session:
        comments = session.scalars(
            select(PublisherRawComment).order_by(PublisherRawComment.id)
        ).all()
        first_publication = session.get(
            CanonPublicationProtection, comments[0].source_publication_id
        )
        second_publication = CanonPublicationProtection(
            id=new_id(),
            project_id=project_id,
            chapter_plan_id=first_publication.chapter_plan_id,
            chapter_number=2,
            canon_commit_id=first_publication.canon_commit_id,
            upload_job_id=new_id(),
            platform_id="qidian",
            content_sha256=first_publication.content_sha256,
            state="published",
            remote_book_id="remote-work",
            remote_chapter_id="remote-chapter",
        )
        session.add(second_publication)
        comments[1].platform_id = "qidian"
        comments[1].source_publication_id = second_publication.id
        reanalyze(session, project_id, comments)
        row = short(session, project_id)[0]
        assert row.known_author_count == 2
        assert row.unique_user_count == 1
        assert row.signal_level == "noise"


def test_same_stable_chapter_different_publication_versions_cannot_qualify(
    comments_runtime,
):
    from forwin.models.base import new_id
    from forwin.models.canon import CanonPublicationProtection
    from forwin.models.project import ChapterPlan

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 3)
    with runtime.session_factory() as session:
        comments = session.scalars(select(PublisherRawComment)).all()
        first = session.get(
            CanonPublicationProtection, comments[0].source_publication_id
        )
        plan = session.get(ChapterPlan, first.chapter_plan_id)
        second = CanonPublicationProtection(
            id=new_id(),
            project_id=project_id,
            chapter_plan_id=plan.id,
            chapter_number=2,
            canon_commit_id=plan.active_commit_id,
            upload_job_id=new_id(),
            platform_id="fanqie",
            content_sha256="second",
            state="published",
            remote_book_id="remote-work",
            remote_chapter_id="remote-chapter",
        )
        session.add(second)
        comments[-1].source_canon_commit_id = second.canon_commit_id
        comments[-1].source_publication_id = second.id
        reanalyze(session, project_id, comments)
        row = short(session, project_id)[0]
        assert not row.source_qualified
        assert (
            "mixed_publication_versions"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )
        assert row.signal_level == "noise"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("publication", "unproven_publication"),
        ("confidence", "low_confidence"),
        ("evidence", "ungrounded_evidence"),
    ],
)
def test_unproven_or_weak_evidence_cannot_enter_automatic_view(
    comments_runtime, change, reason
):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 3)
    with runtime.session_factory() as session:
        comments = session.scalars(select(PublisherRawComment)).all()
        if change == "publication":
            for comment in comments:
                comment.source_status = "chapter_known"
                comment.source_publication_id = ""
                comment.source_canon_commit_id = ""
            reanalyze(session, project_id, comments)
        else:
            signals = session.scalars(select(CommentSignalCandidate)).all()
            for signal in signals:
                if change == "confidence":
                    signal.confidence = 0.4
                else:
                    signal.evidence_span = "这不是评论原文"
        row = short(session, project_id)[0]
        assert row.source_qualified is False
        assert row.signal_level == "noise"
        assert reason in json.loads(row.provenance_json)["qualification_reasons"]


@pytest.mark.parametrize(
    "signal_type,direction,body",
    [
        ("confusion", "unclear", "我看不懂这条规则"),
        ("confusion", "clear", "这条规则讲清楚了"),
        ("pacing", "too_slow", "故事太慢了"),
        ("pacing", "too_fast", "故事太快了"),
        ("character_heat", "positive", "喜欢这个人物"),
        ("character_heat", "negative", "讨厌这个人物"),
        ("risk", "concern", "规则前后矛盾"),
        ("relationship_interest", "want_more", "想看更多互动"),
        ("relationship_interest", "want_less", "不想看这些互动"),
        ("prediction", "predicts", "我猜门后有人"),
    ],
)
def test_six_signal_types_preserve_grounded_direction(
    comments_runtime, signal_type, direction, body
):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, [body], analyze=False)

    class FrozenModel:
        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "signals": [
                        {
                            "comment_index": 0,
                            "signal_type": signal_type,
                            "direction": direction,
                            "target_type": "general",
                            "target_name": "",
                            "severity": 2,
                            "confidence": 0.9,
                            "evidence_span": body,
                        }
                    ]
                }
            )

    with runtime.session_factory() as session:
        comments = session.scalars(select(PublisherRawComment)).all()
        CommentAnalyzer(llm_client=FrozenModel()).analyze_and_store(
            session=session, project_id=project_id, comments=comments
        )
        row = short(session, project_id)[0]
        assert row.signal_type == signal_type
        assert row.direction == direction
        assert row.source_qualified
        assert json.loads(row.provenance_json)["signal_evidence"][0]["quote"] == body


def test_world_snapshot_does_not_limit_aggregation_to_200_signals(comments_runtime):
    from forwin.simulation.world import build_reader_feedback_snapshot

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 205 + ["普通读后感"] * 5)
    with runtime.session_factory() as session:
        snapshot = build_reader_feedback_snapshot(
            session, "同名书", project_id=project_id, chapter_number=2, limit=3
        )
        view = next(iter(snapshot["signals"].values()))
        assert view["hit_count"] == 205
        assert view["total_comment_count"] == 210


def test_distinct_canon_ids_across_stable_chapters_are_valid_consensus(
    comments_runtime,
):
    from forwin.models.base import new_id
    from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
    from forwin.models.project import ChapterPlan
    from forwin.models.publisher import PublisherChapterBinding, PublisherWorkBinding

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 2)
    with runtime.session_factory() as session:
        old = session.scalars(select(ChapterPlan)).one()
        plan = ChapterPlan(
            id=new_id(),
            project_id=project_id,
            arc_plan_id=old.arc_plan_id,
            chapter_number=3,
        )
        session.add(plan)
        session.flush()
        draft = ChapterDraft(
            id=new_id(), chapter_plan_id=plan.id, version=1, body_text="第三章"
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
            chapter_number=3,
            body_hash=candidate_body_hash(draft.body_text),
            candidate_draft_id=draft.id,
            review_id=review.id,
            version=1,
        )
        session.add(candidate)
        session.flush()
        commit = CanonCommitRecord(
            id=new_id(),
            idempotency_key=new_id(),
            candidate_id=candidate.id,
            project_id=project_id,
            chapter_plan_id=plan.id,
            chapter_number=3,
        )
        session.add(commit)
        session.flush()
        plan.active_commit_id = commit.id
        binding = session.scalars(select(PublisherWorkBinding)).one()
        session.add(
            PublisherChapterBinding(
                id=new_id(),
                project_id=project_id,
                platform_id="fanqie",
                work_binding_id=binding.id,
                chapter_number=3,
                remote_chapter_id="chapter-three",
            )
        )
        session.add(
            CanonPublicationProtection(
                id=new_id(),
                project_id=project_id,
                chapter_plan_id=plan.id,
                chapter_number=3,
                canon_commit_id=commit.id,
                upload_job_id=new_id(),
                platform_id="fanqie",
                content_sha256=candidate_body_hash("第三章"),
                state="published",
                remote_book_id="remote-work",
                remote_chapter_id="chapter-three",
            )
        )
        session.commit()
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "project_id": project_id,
                "account_id": "account",
                "work_id": "remote-work",
                "chapter_id": "chapter-three",
                "remote_comment_id": "third",
                "body": "太拖了",
                "author_id": "reader-three",
            }
        ],
    )
    with runtime.session_factory() as session:
        reanalyze(
            session, project_id, session.scalars(select(PublisherRawComment)).all()
        )
        rows = SignalAggregator().aggregate(session, project_id, 3)
        row = next(r for r in rows if r.window_type == "short")
        assert row.source_qualified
        assert row.signal_level == "confirmed"
        assert row.unique_user_count == 3
        manifest = json.loads(row.provenance_json)["source_scope"]["chapters"]
        assert len(manifest) == 2
        assert len({entry["canon_commit_ids"][0] for entry in manifest.values()}) == 2
        assert SignalAggregator().aggregate(session, project_id, 99) == []


def test_direction_without_grounded_body_or_missing_direction_remains_unknown(
    comments_runtime,
):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"], analyze=False)

    class UngroundedModel:
        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "signals": [
                        {
                            "comment_index": 0,
                            "signal_type": "pacing",
                            "direction": "too_fast",
                            "evidence_span": "速度飞快",
                            "confidence": 0.9,
                        }
                    ]
                }
            )

    with runtime.session_factory() as session:
        CommentAnalyzer(llm_client=UngroundedModel()).analyze_and_store(
            session=session,
            project_id=project_id,
            comments=session.scalars(select(PublisherRawComment)).all(),
        )
        row = short(session, project_id)[0]
        assert row.direction == "unknown"
        assert not row.source_qualified
        assert (
            "unknown_direction"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )


def test_aggregate_view_preserves_occurrence_observation_and_receipt_times(
    comments_runtime,
):
    from forwin.audience.aggregation import aggregate_view

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"])
    with runtime.session_factory() as session:
        row = short(session, project_id)[0]
        view = aggregate_view(row)
        timing = view["source_scope"]["timing"]
        assert timing["received_at"]["start"]
        assert timing["observed_at"]["end"]
        assert timing["remote_created_at"]["unknown_count"] == 1
        evidence = view["input_comments"][0]
        assert evidence["ingested_at"]
        assert evidence["analyzed_at"]
        assert evidence["remote_created_at"] == ""


def test_nonfinite_confidence_cannot_qualify_or_poison_snapshot(comments_runtime):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"])
    with runtime.session_factory() as session:
        signal = session.scalars(select(CommentSignalCandidate)).one()
        signal.confidence = float("nan")
        row = short(session, project_id)[0]
        assert not row.source_qualified
        assert row.avg_confidence == 0
        assert (
            "invalid_confidence"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )


@pytest.mark.parametrize("confidence", ["Infinity", 8, -1])
def test_analyzer_rejects_invalid_model_confidence(comments_runtime, confidence):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"], analyze=False)

    class InvalidConfidenceModel:
        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "signals": [
                        {
                            "comment_index": 0,
                            "signal_type": "pacing",
                            "direction": "too_slow",
                            "confidence": confidence,
                            "evidence_span": "太拖了",
                        }
                    ]
                }
            )

    with runtime.session_factory() as session:
        result = CommentAnalyzer(llm_client=InvalidConfidenceModel()).analyze_and_store(
            session=session,
            project_id=project_id,
            comments=session.scalars(select(PublisherRawComment)).all(),
        )
        assert result == []
        assert session.scalars(select(CommentAnalysisRecord)).one().status == "failed"


def test_concurrent_aggregate_runs_share_one_immutable_snapshot(comments_runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 3)
    barrier = Barrier(2)

    def aggregate():
        with runtime.session_factory() as session:
            barrier.wait(timeout=5)
            ids = {r.id for r in SignalAggregator().aggregate(session, project_id, 2)}
            session.commit()
            return ids

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(aggregate) for _ in range(2)]
        identities = [future.result(timeout=10) for future in futures]
    assert identities[0] == identities[1]
    with runtime.session_factory() as session:
        assert len(session.scalars(select(SignalWindowAggregate)).all()) == 3


def test_partial_analyzer_upgrade_cannot_claim_complete_coverage(comments_runtime):
    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了", "普通读后感"])
    with runtime.session_factory() as session:
        comments = session.scalars(
            select(PublisherRawComment).order_by(PublisherRawComment.remote_comment_id)
        ).all()
        CommentAnalyzer(
            llm_client=OpinionModel(), analyzer_version="new-policy"
        ).analyze_and_store(
            session=session, project_id=project_id, comments=[comments[0]]
        )
        row = short(session, project_id)[0]
        assert not row.source_qualified
        assert (
            "mixed_analyzer_versions"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )


@pytest.mark.parametrize("tamper", ["publication_hash", "candidate_hash", "body"])
def test_published_identity_requires_immutable_accepted_body_match(
    comments_runtime, tamper
):
    from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"])
    with runtime.session_factory() as session:
        publication = session.scalars(select(CanonPublicationProtection)).one()
        canon = session.get(CanonCommitRecord, publication.canon_commit_id)
        candidate = session.get(CandidateDraftRecord, canon.candidate_id)
        if tamper == "publication_hash":
            publication.content_sha256 = "not-the-body"
        elif tamper == "candidate_hash":
            candidate.body_hash = "not-the-body"
        else:
            session.get(
                ChapterDraft, candidate.candidate_draft_id
            ).body_text = "changed accepted content"
        row = short(session, project_id)[0]
        assert not row.source_qualified
        assert (
            "unproven_publication"
            in json.loads(row.provenance_json)["qualification_reasons"]
        )


def test_stale_session_cannot_combine_new_signal_with_previous_analysis(
    comments_runtime,
):
    from forwin.audience.aggregation import aggregate_window
    from forwin.models import new_id

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["太拖了"] * 3)
    with runtime.session_factory() as stale:
        old_rows = stale.scalars(select(PublisherRawComment)).all()
        old_id = old_rows[0].active_analysis_id
        remote_id = old_rows[0].remote_comment_id
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            comments=[
                {
                    "project_id": project_id,
                    "account_id": "account",
                    "work_id": "remote-work",
                    "chapter_id": "remote-chapter",
                    "remote_comment_id": remote_id,
                    "body": "太拖了，不过仍会追读。",
                    "author_id": old_rows[0].author_id,
                }
            ],
        )

        class CommonQuoteModel(OpinionModel):
            def chat(self, messages, **kwargs):
                payload = json.loads(super().chat(messages, **kwargs))
                for signal in payload["signals"]:
                    signal["evidence_span"] = "太拖了"
                return json.dumps(payload, ensure_ascii=False)

        with runtime.session_factory() as newer:
            current = newer.scalar(
                select(PublisherRawComment).where(
                    PublisherRawComment.remote_comment_id == remote_id
                )
            )
            CommentAnalyzer(llm_client=CommonQuoteModel()).analyze_and_store(
                session=newer, project_id=project_id, comments=[current]
            )
            newer.commit()
            new_id = current.active_analysis_id
        assert new_id != old_id
        row = aggregate_window(
            stale, project_id=project_id, chapter_start=1, chapter_end=2
        )[0]
        proof = json.loads(row.provenance_json)
        saved = next(
            item
            for item in proof["input_comments"]
            if item["comment_id"] == old_rows[0].id
        )
        assert saved["analysis_id"] == new_id
        assert new_id in proof["evidence_analysis_ids"]
        assert old_id not in proof["evidence_analysis_ids"]


def test_high_risk_severity_cannot_be_borrowed_by_another_platform_authors(
    comments_runtime,
):
    from forwin.models import new_id
    from forwin.models.canon import CanonPublicationProtection

    runtime, project_id = comments_runtime
    seed(runtime, project_id, ["规则矛盾"] * 3)
    with runtime.session_factory() as session:
        comments = session.scalars(
            select(PublisherRawComment).order_by(PublisherRawComment.id)
        ).all()
        original = session.get(
            CanonPublicationProtection, comments[-1].source_publication_id
        )
        publication = CanonPublicationProtection(
            id=new_id(),
            project_id=project_id,
            chapter_plan_id=original.chapter_plan_id,
            chapter_number=2,
            canon_commit_id=original.canon_commit_id,
            upload_job_id=new_id(),
            platform_id="qidian",
            content_sha256=original.content_sha256,
            state="published",
            remote_book_id="remote-work",
            remote_chapter_id="remote-chapter",
        )
        session.add(publication)
        comments[-1].platform_id = "qidian"
        comments[-1].source_publication_id = publication.id
        reanalyze(session, project_id, comments)
        by_id = {c.id: c for c in comments}
        for signal in session.scalars(select(CommentSignalCandidate)):
            signal.signal_type = "risk"
            signal.direction = "concern"
            signal.severity = (
                3 if by_id[signal.source_comment_id].platform_id == "qidian" else 1
            )
        row = short(session, project_id)[0]
        assert row.source_qualified
        assert row.signal_level == "watchlist"
