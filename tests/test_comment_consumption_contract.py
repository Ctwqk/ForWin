from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.models.base import new_id
from forwin.models.project import Project
from forwin.models.publisher import PublisherRawComment
from forwin.simulation.world import CommentAnalyzer, build_reader_feedback_snapshot
from tests.test_publisher_runtime_comment_sync import _runtime


class EmptyAnalyzerModel:
    def __init__(self):
        self.bodies = []

    def chat(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"].split("评论列表：", 1)[1])
        self.bodies.extend(row["body"] for row in payload)
        return '{"signals":[]}'


@pytest.fixture
def comments_runtime():
    engine, runtime = _runtime("comment-consumption-contract")
    with runtime.session_factory() as session:
        project = Project(id=new_id(), title="同名书", premise="测试", genre="玄幻")
        session.add(project)
        session.commit()
        project_id = project.id
    yield runtime, project_id
    engine.dispose()


def _comment(session, project_id, number, body=None):
    row = PublisherRawComment(
        id=new_id(),
        project_id=project_id,
        platform_id="fanqie",
        remote_comment_id=f"comment-{number}",
        work_id="work-one",
        work_name="同名书",
        body_text=body or f"普通读后感 {number}",
    )
    session.add(row)
    session.flush()
    return row


def test_zero_signals_records_completion_instead_of_repeating_model(comments_runtime):
    runtime, project_id = comments_runtime
    model = EmptyAnalyzerModel()
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1)
        analyzer = CommentAnalyzer(llm_client=model)
        analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        session.commit()
        analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        assert model.bodies == [comment.body_text]


def test_pending_filter_precedes_limit_and_consumes_all_100(comments_runtime):
    runtime, project_id = comments_runtime
    model = EmptyAnalyzerModel()
    with runtime.session_factory() as session:
        for number in range(100):
            _comment(session, project_id, number)
        session.commit()
        for _ in range(15):
            build_reader_feedback_snapshot(
                session,
                "同名书",
                project_id=project_id,
                chapter_number=90,
                limit=8,
                llm_client=model,
                analyze_missing=True,
            )
            session.commit()
        assert len(set(model.bodies)) == 100
        assert len(model.bodies) == 100


def test_signal_source_is_not_analysis_time_generation_progress(comments_runtime):
    runtime, project_id = comments_runtime
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1, "这一段太拖了")
        rows = CommentAnalyzer().analyze_and_store(
            session=session,
            project_id=project_id,
            comments=[comment],
            chapter_number=90,
        )
        assert rows
        assert {row.chapter_number for row in rows} == {0}


def test_remote_identity_is_scoped_by_account_and_work(comments_runtime):
    runtime, project_id = comments_runtime
    for account, work in [("a", "work-1"), ("a", "work-2"), ("b", "work-1")]:
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            comments=[
                {
                    "project_id": project_id,
                    "account_id": account,
                    "work_id": work,
                    "remote_comment_id": "same",
                    "body": f"{account}/{work}",
                }
            ],
        )
    with runtime.session_factory() as session:
        rows = session.scalars(select(PublisherRawComment)).all()
        assert len(rows) == 3
        assert {row.body_text for row in rows} == {"a/work-1", "a/work-2", "b/work-1"}


def test_work_title_does_not_invent_project_identity(comments_runtime):
    runtime, project_id = comments_runtime
    runtime.comment_sync.ingest_comments_batch(
        client_id="client",
        platform="fanqie",
        comments=[
            {
                "work_name": "同名书",
                "remote_comment_id": "unknown",
                "body": "同名不等于同书",
            }
        ],
    )
    with runtime.session_factory() as session:
        comment = session.scalars(select(PublisherRawComment)).one()
        assert comment.project_id == ""
        result = build_reader_feedback_snapshot(
            session, "同名书", project_id=project_id
        )
        assert result["comment_count"] == 0


def test_http_input_retains_explicit_origin_fields():
    from forwin.api_schema.publisher import PublisherRawCommentInput

    payload = {
        "remote_comment_id": "id",
        "project_id": "p",
        "account_id": "a",
        "observed_at": "2026-09-01T10:00:00Z",
    }
    assert payload.items() <= PublisherRawCommentInput(**payload).model_dump().items()


def test_edit_and_new_analyzer_version_preserve_prior_evidence(comments_runtime):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime
    model = EmptyAnalyzerModel()
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1, "原评论")
        analyzer = CommentAnalyzer(llm_client=model)
        analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=[comment], chapter_number=9
        )
        session.commit()
        comment.body_text = "编辑后的评论"
        session.commit()
        analyzer.analyze_and_store(
            session=session,
            project_id=project_id,
            comments=[comment],
            chapter_number=19,
        )
        session.commit()
        CommentAnalyzer(
            llm_client=model, analyzer_version="comment-v3"
        ).analyze_and_store(
            session=session,
            project_id=project_id,
            comments=[comment],
            chapter_number=29,
        )
        session.commit()
        records = session.scalars(
            select(CommentAnalysisRecord).order_by(CommentAnalysisRecord.created_at)
        ).all()
        assert len(records) == 3
        assert [row.input_body for row in records] == [
            "原评论",
            "编辑后的评论",
            "编辑后的评论",
        ]
        assert {row.generation_chapter_number for row in records} == {9, 19, 29}
        assert all(row.analyzed_at and row.status == "completed" for row in records)


def test_failed_analysis_has_visible_bounded_retry_and_does_not_starve(
    comments_runtime,
):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime

    class Broken:
        def chat(self, messages, **kwargs):
            raise TimeoutError("bounded model failure")

    analyzer = CommentAnalyzer(llm_client=Broken(), retry_delay_seconds=0)
    with runtime.session_factory() as session:
        bad = _comment(session, project_id, 1)
        session.commit()
        for _ in range(5):
            rows = analyzer.pending_comments(
                session=session, project_id=project_id, limit=1
            )
            analyzer.analyze_and_store(
                session=session, project_id=project_id, comments=rows
            )
            session.commit()
        state = session.scalars(select(CommentAnalysisRecord)).one()
        assert (state.status, state.attempt_count, state.analyzed_at) == (
            "exhausted",
            3,
            None,
        )
        assert "bounded model failure" in state.last_error
        good = _comment(session, project_id, 2)
        assert analyzer.pending_comments(
            session=session, project_id=project_id, limit=1
        ) == [good]
        assert bad.id != good.id


def test_unknown_scope_does_not_merge_independent_observations(comments_runtime):
    runtime, project_id = comments_runtime
    for body in ["不知账号的来源甲", "不知账号的来源乙"]:
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            comments=[
                {
                    "project_id": project_id,
                    "work_id": "w",
                    "remote_comment_id": "same",
                    "body": body,
                }
            ],
        )
    with runtime.session_factory() as session:
        rows = session.scalars(select(PublisherRawComment)).all()
        assert len(rows) == 2
        assert all(row.source_status == "unknown" for row in rows)


def test_older_observation_cannot_overwrite_newer_comment_edit(comments_runtime):
    runtime, project_id = comments_runtime
    for body, observed in [
        ("新内容", "2026-09-03T00:00:00Z"),
        ("旧内容", "2026-09-01T00:00:00Z"),
    ]:
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            comments=[
                {
                    "project_id": project_id,
                    "account_id": "a",
                    "work_id": "w",
                    "remote_comment_id": "same",
                    "body": body,
                    "observed_at": observed,
                }
            ],
        )
    with runtime.session_factory() as session:
        assert session.scalars(select(PublisherRawComment)).one().body_text == "新内容"


def test_empty_body_keeps_other_comments_model_indices(comments_runtime):
    runtime, project_id = comments_runtime

    class SignalModel:
        def chat(self, messages, **kwargs):
            return json.dumps(
                {
                    "signals": [
                        {
                            "comment_index": 1,
                            "signal_type": "pacing",
                            "evidence_span": "太拖了",
                        }
                    ]
                }
            )

    with runtime.session_factory() as session:
        empty = _comment(session, project_id, 1)
        empty.body_text = ""
        comment = _comment(session, project_id, 2, "太拖了")
        rows = CommentAnalyzer(llm_client=SignalModel()).analyze_and_store(
            session=session, project_id=project_id, comments=[empty, comment]
        )
        assert [row.source_comment_id for row in rows] == [comment.id]


def test_malformed_signal_response_is_not_zero_signal_completion(comments_runtime):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime

    class BadResponse:
        def chat(self, messages, **kwargs):
            return '{"signals":[{"comment_index":99,"signal_type":"made_up"}]}'

    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1)
        CommentAnalyzer(llm_client=BadResponse()).analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        state = session.scalars(select(CommentAnalysisRecord)).one()
        assert state.status == "failed"
        assert state.analyzed_at is None


def test_edited_comment_old_signals_remain_evidence_but_leave_current_read(
    comments_runtime,
):
    from forwin.models.publisher import CommentSignalCandidate
    from forwin.simulation.world import load_recent_signals

    runtime, project_id = comments_runtime
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1, "太拖了")
        comment.source_status = "chapter_known"
        comment.source_chapter_number = 2
        analyzer = CommentAnalyzer()
        first = analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        assert len(first) == 1
        session.commit()
        comment.body_text = "没有意见"
        session.commit()
        analyzer.analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        assert load_recent_signals(session, project_id, current_chapter=3) == []
        assert session.scalars(select(CommentSignalCandidate)).all() == first


def test_unknown_historical_signal_chapter_cannot_supply_cross_chapter_evidence(
    comments_runtime,
):
    from forwin.models.publisher import CommentSignalCandidate
    from forwin.simulation.world import load_recent_signals

    runtime, project_id = comments_runtime
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1, "太拖了")
        session.add(
            CommentSignalCandidate(
                id=new_id(),
                project_id=project_id,
                source_comment_id=comment.id,
                signal_type="pacing",
                chapter_number=99,
            )
        )
        session.flush()
        assert load_recent_signals(session, project_id, current_chapter=100) == []


def test_analysis_pending_workers_skip_locked_rows_and_do_not_duplicate_completion(
    comments_runtime,
):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime
    with runtime.session_factory() as session:
        for n in range(16):
            _comment(session, project_id, n)
        session.commit()
    with runtime.session_factory() as first, runtime.session_factory() as second:
        analyzer = CommentAnalyzer()
        a = analyzer.pending_comments(session=first, project_id=project_id, limit=8)
        b = analyzer.pending_comments(session=second, project_id=project_id, limit=8)
        assert len(a) == len(b) == 8
        assert not {row.id for row in a} & {row.id for row in b}
        analyzer.analyze_and_store(session=first, project_id=project_id, comments=a)
        analyzer.analyze_and_store(session=second, project_id=project_id, comments=b)
        first.commit()
        second.commit()
    with runtime.session_factory() as session:
        assert (
            analyzer.pending_comments(session=session, project_id=project_id, limit=8)
            == []
        )
        assert len(session.scalars(select(CommentAnalysisRecord)).all()) == 16


def test_comment_job_does_not_override_mismatched_platform_or_work(comments_runtime):
    runtime, project_id = comments_runtime
    job = runtime.comment_sync.create_comment_sync_job(
        project_id=project_id,
        platform="fanqie",
        work_id="right-work",
        work_name="同名书",
        chapter_id="",
        chapter_title="",
        limit=8,
    )
    with pytest.raises(ValueError, match="work"):
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            job_id=job["job_id"],
            comments=[
                {
                    "remote_comment_id": "bad",
                    "work_id": "other-work",
                    "body": "不能串书",
                }
            ],
        )


def test_truncated_zero_signal_response_is_not_complete(comments_runtime):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime

    class Truncated(EmptyAnalyzerModel):
        def __init__(self):
            super().__init__()
            self.llm_attempt_events = []

        def chat(self, messages, **kwargs):
            self.llm_attempt_events.append({"finish_reason": "length"})
            return super().chat(messages, **kwargs)

    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1)
        CommentAnalyzer(llm_client=Truncated()).analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        assert session.scalars(select(CommentAnalysisRecord)).one().status == "failed"


def test_unversioned_signal_never_rejoins_after_source_is_known_and_body_changes(
    comments_runtime,
):
    from forwin.models.publisher import CommentSignalCandidate
    from forwin.simulation.world import load_recent_signals

    runtime, project_id = comments_runtime
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1, "旧正文太拖了")
        old = CommentSignalCandidate(
            id=new_id(),
            project_id=project_id,
            source_comment_id=comment.id,
            signal_type="pacing",
            chapter_number=2,
        )
        session.add(old)
        session.commit()
        comment.source_status = "chapter_known"
        comment.source_chapter_number = 2
        comment.body_text = "新正文没有意见"
        session.commit()
        CommentAnalyzer().analyze_and_store(
            session=session, project_id=project_id, comments=[comment]
        )
        assert load_recent_signals(session, project_id, current_chapter=2) == []
        assert session.get(CommentSignalCandidate, old.id) is old


def test_same_body_source_enrichment_creates_new_analysis_identity(comments_runtime):
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime
    with runtime.session_factory() as session:
        comment = _comment(session, project_id, 1, "太拖了")
        analyzer = CommentAnalyzer()
        first = analyzer.analyze_and_store(
            session=session,
            project_id=project_id,
            comments=[comment],
            chapter_number=90,
        )
        session.commit()
        comment.source_status = "chapter_known"
        comment.source_chapter_number = 2
        session.commit()
        second = analyzer.analyze_and_store(
            session=session,
            project_id=project_id,
            comments=[comment],
            chapter_number=91,
        )
        assert len(second) == 1
        assert first[0].chapter_number == 0
        assert second[0].chapter_number == 2
        records = session.scalars(select(CommentAnalysisRecord)).all()
        assert len(records) == 2
        assert len({r.content_sha256 for r in records}) == 1
        assert {
            json.loads(r.source_identity_json)["source_status"] for r in records
        } == {"unknown", "chapter_known"}


def test_real_snapshot_consumer_commits_three_failures_then_exhausts(
    comments_runtime, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from forwin.audience import comment_analysis
    from forwin.models.publisher import CommentAnalysisRecord

    runtime, project_id = comments_runtime

    class Broken:
        def chat(self, messages, **kwargs):
            raise TimeoutError("model offline")

    clock = [datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)]
    monkeypatch.setattr(comment_analysis, "now_utc", lambda: clock[0])
    with runtime.session_factory() as session:
        _comment(session, project_id, 1)
        session.commit()
        for _ in range(5):
            snapshot = build_reader_feedback_snapshot(
                session,
                "同名书",
                project_id=project_id,
                llm_client=Broken(),
                analyze_missing=True,
                limit=8,
            )
            session.commit()
            clock[0] += timedelta(seconds=61)
        record = session.scalars(select(CommentAnalysisRecord)).one()
        assert record.attempt_count == 3
        assert record.status == "exhausted"
        assert snapshot["analysis_status"]["exhausted_count"] == 1
        assert snapshot["analysis_status"]["errors"][0]["last_error"].endswith(
            "model offline"
        )


def test_simultaneous_duplicate_ingest_is_idempotent(comments_runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import forwin.publisher_runtime.comment_sync as sync_module

    runtime, project_id = comments_runtime
    for client in ("first", "second"):
        runtime.comment_sync.ingest_comments_batch(
            client_id=client, platform="fanqie", comments=[]
        )
    original = sync_module.resolve_comment_source
    ready = Barrier(2)

    def concurrent_source(*args, **kwargs):
        result = original(*args, **kwargs)
        ready.wait(timeout=5)
        return result

    monkeypatch.setattr(sync_module, "resolve_comment_source", concurrent_source)
    item = {
        "project_id": project_id,
        "account_id": "a",
        "work_id": "w",
        "remote_comment_id": "same",
        "body": "幂等摄入",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                runtime.comment_sync.ingest_comments_batch,
                client_id=client,
                platform="fanqie",
                comments=[item],
            )
            for client in ("first", "second")
        ]
        results = [future.result(timeout=10) for future in futures]
    assert sum(row["inserted"] for row in results) == 1
    assert sum(row["updated"] for row in results) == 1
    with runtime.session_factory() as session:
        assert len(session.scalars(select(PublisherRawComment)).all()) == 1


def test_same_book_title_never_mixes_two_explicit_project_feeds(comments_runtime):
    runtime, first_id = comments_runtime
    with runtime.session_factory() as session:
        other = Project(id=new_id(), title="同名书", premise="另一部作品", genre="玄幻")
        session.add(other)
        session.commit()
        second_id = other.id
    for number, project_id in enumerate((first_id, second_id)):
        runtime.comment_sync.ingest_comments_batch(
            client_id="client",
            platform="fanqie",
            comments=[
                {
                    "project_id": project_id,
                    "account_id": "a",
                    "work_id": f"work-{number}",
                    "work_name": "同名书",
                    "remote_comment_id": "same",
                    "body": f"作品{number}的评论",
                }
            ],
        )
    with runtime.session_factory() as session:
        first = build_reader_feedback_snapshot(session, "同名书", project_id=first_id)
        second = build_reader_feedback_snapshot(session, "同名书", project_id=second_id)
        assert [row.body_text for row in first["recent_comments"]] == ["作品0的评论"]
        assert [row.body_text for row in second["recent_comments"]] == ["作品1的评论"]
