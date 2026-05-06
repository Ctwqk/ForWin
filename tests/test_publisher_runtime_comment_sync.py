from __future__ import annotations

import json

from sqlalchemy import select

from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import DecisionEvent
from forwin.models.project import Project
from forwin.models.publisher import PublisherRawComment
from forwin.publisher_runtime.service import PublisherRuntimeService


def _runtime(name: str) -> tuple[object, PublisherRuntimeService]:
    engine = get_engine(postgres_test_url(name))
    init_db(engine)
    return engine, PublisherRuntimeService(
        session_factory=get_session_factory(engine),
        extension_api_key="secret",
        heartbeat_stale_seconds=90,
        preferred_client_id="",
        publisher_session_secret="",
        publisher_session_encryption_required=False,
    )


def test_comment_sync_service_claim_ingest_and_result_keep_audit_redacted() -> None:
    engine, runtime = _runtime("publisher-runtime-comment")
    project_id = new_id()
    try:
        with runtime.session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    title="评论运行时",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
            )
            session.commit()

        job = runtime.comment_sync.create_comment_sync_job(
            project_id=project_id,
            platform="fanqie",
            work_id="work-1",
            work_name="评论运行时",
            chapter_id="chapter-1",
            chapter_title="第一章",
            limit=20,
        )
        claimed = runtime.comment_sync.claim_next_comment_sync_job(
            client_id="client-1",
            connected_platforms=["fanqie"],
        )
        batch = runtime.comment_sync.ingest_comments_batch(
            client_id="client-1",
            platform="fanqie",
            job_id=job["job_id"],
            comments=[
                {
                    "project_id": project_id,
                    "remote_comment_id": "comment-1",
                    "work_name": "评论运行时",
                    "chapter_title": "第一章",
                    "author_id": "private-author-id",
                    "author_name": "隐私作者",
                    "body": "好看",
                    "like_count": 3,
                    "reply_count": 1,
                }
            ],
        )
        finished = runtime.comment_sync.update_comment_sync_job_result(
            job_id=job["job_id"],
            client_id="client-1",
            status="succeeded",
            message="评论同步已完成。",
            error="",
            result_payload={"fetched_count": 1},
        )

        assert claimed is not None
        assert claimed["job_id"] == job["job_id"]
        assert batch["inserted"] == 1
        assert finished["status"] == "succeeded"
        assert runtime.comment_sync.get_comment_sync_job(job["job_id"])["job_id"] == job["job_id"]
        assert runtime.comment_sync.list_comment_sync_jobs()[0]["job_id"] == job["job_id"]

        with runtime.session_factory() as session:
            stored_comment = session.execute(select(PublisherRawComment)).scalar_one()
            events = session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.project_id == project_id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            ).scalars().all()

        assert stored_comment.like_count == 3
        assert stored_comment.reply_count == 1
        event_types = [event.event_type for event in events]
        assert DecisionEventType.COMMENT_SYNC_JOB_CREATED in event_types
        assert DecisionEventType.COMMENT_SYNC_JOB_CLAIMED in event_types
        assert DecisionEventType.RAW_COMMENTS_INGESTED in event_types
        for event in events:
            payload_text = json.dumps(json.loads(event.payload_json or "{}"), ensure_ascii=False)
            assert "private-author-id" not in payload_text
            assert "隐私作者" not in payload_text
    finally:
        engine.dispose()
