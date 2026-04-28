from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from forwin.governance import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.governance import DecisionEvent
from forwin.models.project import Project
from forwin.publishers.manager import PublisherManager


def _event_payload(row: DecisionEvent) -> dict[str, object]:
    value = json.loads(row.payload_json or "{}")
    assert isinstance(value, dict)
    return value


def test_publisher_upload_job_lifecycle_records_project_events_without_body_text() -> None:
    with TemporaryDirectory() as tmp:
        engine = get_engine(postgres_test_url("publisher-events"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        project_id = new_id()
        with session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    title="发布审计",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
            )
            session.commit()

        manager = PublisherManager(session_factory, extension_api_key="secret")
        try:
            created = manager.create_upload_job(
                project_id=project_id,
                platform="qidian",
                book_name="发布审计",
                chapter_title="第一章",
                body="这是一段不应该进入 DecisionEvent 的正文",
                upload_url="https://example.test/book",
                publish=True,
                create_if_missing=False,
                book_meta={"audience": "male"},
            )
            claimed = manager.claim_next_upload_job(
                client_id="client-1",
                connected_platforms=["qidian"],
            )
            assert claimed is not None
            manager.update_upload_job_result(
                job_id=created["job_id"],
                client_id="client-1",
                status="succeeded",
                message="uploaded",
                current_url="https://example.test/chapter/1",
                error="",
                result_payload={"remote_chapter_id": "remote-1"},
            )

            with session_factory() as session:
                rows = session.execute(
                    select(DecisionEvent)
                    .where(DecisionEvent.project_id == project_id)
                    .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
                ).scalars().all()
        finally:
            engine.dispose()

    event_types = [row.event_type for row in rows]
    assert DecisionEventType.UPLOAD_JOB_CREATED in event_types
    assert DecisionEventType.UPLOAD_JOB_CLAIMED in event_types
    assert DecisionEventType.UPLOAD_JOB_SUCCEEDED in event_types
    for row in rows:
        if row.event_type.startswith("upload_job_"):
            payload = _event_payload(row)
            assert payload["platform_id"] == "qidian"
            assert payload["job_id"] == created["job_id"]
            assert "正文" not in json.dumps(payload, ensure_ascii=False)


def test_comment_sync_and_ingest_records_project_events_without_author_identity() -> None:
    with TemporaryDirectory() as tmp:
        engine = get_engine(postgres_test_url("comment-events"))
        init_db(engine)
        session_factory = get_session_factory(engine)
        project_id = new_id()
        with session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    title="评论审计",
                    premise="测试 premise",
                    genre="玄幻",
                    setting_summary="",
                )
            )
            session.commit()

        manager = PublisherManager(session_factory, extension_api_key="secret")
        try:
            job = manager.create_comment_sync_job(
                project_id=project_id,
                platform="qidian",
                work_id="work-1",
                work_name="评论审计",
                chapter_id="chapter-1",
                chapter_title="第一章",
                limit=20,
            )
            claimed = manager.claim_next_comment_sync_job(
                client_id="client-1",
                connected_platforms=["qidian"],
            )
            assert claimed is not None
            manager.ingest_comments_batch(
                client_id="client-1",
                platform="qidian",
                job_id=job["job_id"],
                comments=[
                    {
                        "project_id": project_id,
                        "remote_comment_id": "comment-1",
                        "work_name": "评论审计",
                        "chapter_title": "第一章",
                        "author_id": "private-author-id",
                        "author_name": "隐私作者",
                        "body": "好看",
                    }
                ],
            )

            with session_factory() as session:
                rows = session.execute(
                    select(DecisionEvent)
                    .where(DecisionEvent.project_id == project_id)
                    .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
                ).scalars().all()
        finally:
            engine.dispose()

    event_types = [row.event_type for row in rows]
    assert DecisionEventType.COMMENT_SYNC_JOB_CREATED in event_types
    assert DecisionEventType.COMMENT_SYNC_JOB_CLAIMED in event_types
    assert DecisionEventType.RAW_COMMENTS_INGESTED in event_types
    for row in rows:
        payload_text = json.dumps(_event_payload(row), ensure_ascii=False)
        assert "private-author-id" not in payload_text
        assert "隐私作者" not in payload_text
